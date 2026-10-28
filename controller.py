from copy import deepcopy
from datetime import datetime, timedelta
from config import RULES_BY_LOTTERY, LOTTERIES, DUPLICATE_CHECK_MINUTES
from models import MessageRecord, Analysis, cents, now, money
from parser import BetParser, split_lottery_message
from risk_engine import RiskEngine


def _migrate_old_fushi_rules(rules):
    """仅修V2.3已知错误：复试奖金被统一写成1800。其它用户自定义值不擅自覆盖。"""
    changed = False
    for lottery in LOTTERIES:
        target = rules.get(lottery, {})
        for play, canonical in RULES_BY_LOTTERY[lottery].items():
            if not play.endswith('码复试') or play not in target:
                continue
            current = target[play]
            if current.get('principal') == 10 and current.get('prize') == 1800:
                target[play] = deepcopy(canonical)
                changed = True
    return changed


def _migrate_position_rules(rules):
    """定位收费已实盘确认：一码/两码10元/组合，三码2元/组合。"""
    changed = False
    expected = {'一码定位': 10, '两码定位': 10, '三码定位': 2}
    for lottery in LOTTERIES:
        target = rules.get(lottery, {})
        for play, principal in expected.items():
            if play in target and target[play].get('principal') != principal:
                target[play]['principal'] = principal
                changed = True
    return changed


class Controller:
    def __init__(self, store):
        self.store = store
        self.settings = store.load_settings()
        _migrate_old_fushi_rules(self.settings.get('rules', {}))
        _migrate_position_rules(self.settings.get('rules', {}))
        self.settings.setdefault('policy', {})['position_cost'] = 'combination'
        self.period = now().date().isoformat()
        self.rules = deepcopy(RULES_BY_LOTTERY)
        for lottery, overrides in self.settings.get('rules', {}).items():
            if lottery in self.rules:
                for play, rule in overrides.items():
                    if play in self.rules[lottery]:
                        self.rules[lottery][play].update(rule)
        _migrate_position_rules(self.rules)
        self.parser = BetParser(self.rules, self.settings['policy'])
        self.engine = RiskEngine(self.rules, self.settings['policy'])
        self.last_added_records = []
        store.import_legacy()
        self.activate_period(self.period)
        # 迁入后的所有历史数据均从原文重算，绝不沿用旧错误金额。
        for period in store.periods():
            for record in store.load_records(period):
                if record.note == '旧版数据迁入，等待新规则重新识别':
                    self.parse_record(record)
                    store.update(record, '旧数据重新识别')
                elif record.status == '已识别':
                    corrected = False
                    corrected_plays = set()
                    for bet in record.bets:
                        if bet.play.endswith('码复试') or bet.play.endswith('码定位'):
                            replacement = self.parser._make(record.lottery, bet.raw, bet.play, bet.selection, bet.multiplier)
                            if cents(bet.principal) != cents(replacement.principal):
                                bet.principal = replacement.principal
                                corrected = True
                                corrected_plays.add('定位' if bet.play.endswith('码定位') else '复试')
                    if corrected:
                        total = sum(cents(b.principal) for b in record.bets)
                        suffix = '、'.join(sorted(corrected_plays))
                        record.note = f'共{len(record.bets)}笔，应收{money(total)}元；{suffix}本金已按最新规则校正'
                        if record.claimed_amount is not None and cents(record.claimed_amount) != total:
                            record.note += f'；自报{record.claimed_amount:g}元，差额{(total-cents(record.claimed_amount))/100:g}元'
                        record.revision += 1
                        store.update(record, '修正复试/定位本金：按最新规则')
                elif record.status in ('待确认', '部分识别', 'AI待确认', 'AI待识别', 'AI识别中'):
                    before = record.to_dict()
                    self.parse_record(record)
                    if record.to_dict() != before:
                        record.revision += 1
                        store.update(record, '兼容规则重试／自动校验已有AI结果')
        self.activate_period(self.period)

    def activate_period(self, period):
        self.period = period
        snapshot = self.store.load_period_rules(period)
        if snapshot:
            self.rules = snapshot['rules']
            self.settings['policy'] = snapshot['policy']
            changed = _migrate_old_fushi_rules(self.rules)
            changed = _migrate_position_rules(self.rules) or changed
            self.settings['policy']['position_cost'] = 'combination'
            if changed:
                self.store.save_period_rules(period, self.rules, self.settings['policy'])
        else:
            self.rules = deepcopy(RULES_BY_LOTTERY)
            for lottery, overrides in self.settings.get('rules', {}).items():
                if lottery in self.rules:
                    for play, rule in overrides.items():
                        if play in self.rules[lottery]:
                            self.rules[lottery][play].update(rule)
            _migrate_position_rules(self.rules)
            self.settings['policy']['position_cost'] = 'combination'
            self.store.save_period_rules(period, self.rules, self.settings['policy'])
        if self.settings['policy'].get('span0_wins') is not True:
            self.settings['policy']['span0_wins'] = True
            self.store.save_period_rules(period, self.rules, self.settings['policy'])
        self.parser = BetParser(self.rules, self.settings['policy'])
        self.engine = RiskEngine(self.rules, self.settings['policy'])

    def records(self):
        return self.store.load_records(self.period)

    def parser_for_record(self, record):
        parser = self.parser
        if record.period != self.period:
            snapshot = self.store.load_period_rules(record.period)
            if snapshot is None:
                latest_rules = deepcopy(RULES_BY_LOTTERY)
                for lottery, overrides in self.settings.get('rules', {}).items():
                    if lottery in latest_rules:
                        for play, rule in overrides.items():
                            if play in latest_rules[lottery]:
                                latest_rules[lottery][play].update(rule)
                _migrate_position_rules(latest_rules)
                self.settings['policy']['position_cost'] = 'combination'
                self.store.save_period_rules(record.period, latest_rules, self.settings['policy'])
                snapshot = {'rules':latest_rules, 'policy':self.settings['policy']}
            parser = BetParser(snapshot['rules'], snapshot['policy'])
        return parser

    @staticmethod
    def ai_blocked(reason):
        return any(part in reason for part in ('冲突', '尚未确认', '倍数须', '倍数必须', '码数与', '同一位置写了多次', '算式', '自报金额与'))

    def parse_record(self, record, allow_conflict=False):
        parser = self.parser_for_record(record)
        text = self.settings.get('aliases', {}).get(record.raw_text.strip(), record.raw_text)
        bets, normalized, reason = parser.parse(record.lottery, text)
        claimed = parser.last_claimed
        pending = []
        if not bets:
            # 整条无法识别时，只抢救独立、明确且已通过本地校验的子项。
            partial, pending, normalized = parser.parse_partial(record.lottery, text)
            if partial:
                bets = partial
                claimed = None  # 未完成订单的收款合计绝不可分摊到已确认子项。
        used_ai = False
        review_note = ''
        if record.ai_candidate and (pending or not bets):
            # AI仅给候选；在本地校验完成前不生成正式投注。
            probe = BetParser(parser.rules, parser.policy)
            ai_bets, candidate_normalized, ai_error = probe.parse(record.lottery, record.ai_candidate)
            if ai_bets and (self.ai_blocked(reason) or (bets and pending)) and not allow_conflict:
                # 部分已识别的订单需要人工确认AI候选，防止重复加入或捏造号码。
                record.bets = bets
                record.pending_fragments = pending
                record.normalized_text = normalized
                record.claimed_amount = claimed
                record.status = 'AI待确认'
                record.note = ('本地已确认%d笔，待人工确认AI候选；' % len(bets)) + reason
                return
            if ai_bets:
                bets = bets + ai_bets
                pending = []
                normalized = candidate_normalized if not bets[:-len(ai_bets)] else normalized
                reason = ''
                used_ai = True
            else:
                review_note = '；AI转换未通过本地校验：' + ai_error
        record.bets = bets
        record.pending_fragments = pending
        record.normalized_text = normalized
        record.claimed_amount = claimed
        if bets and pending:
            record.status = '部分识别'
            due = sum(cents(b.principal) for b in bets)
            record.note = (f'本地已识别{len(bets)}笔、暂计{money(due)}元；'
                           f'另有{len(pending)}段待审核，金额未计入；' +
                           '；'.join(item['text'][:60] for item in pending))
        else:
            record.status = '已识别' if bets else '待确认'
            record.note = reason or f'共{len(bets)}笔，应收{money(sum(cents(b.principal) for b in bets))}元'
        if review_note:
            record.note += review_note
        if bets and getattr(parser, 'last_warnings', None):
            record.note += '；' + '；'.join(parser.last_warnings)
        if used_ai:
            record.note += '；AI转换已通过程序校验，自动计入' if not allow_conflict else '；AI候选经人工确认并通过本地校验'
        if record.claimed_amount is not None and bets and not pending:
            due = sum(cents(b.principal) for b in bets)
            if cents(record.claimed_amount) != due:
                record.note += f'；自报{record.claimed_amount:g}元，差额{(due-cents(record.claimed_amount))/100:g}元'

    def add(self, lottery, text, source='人工', message_id='', sender='', group_id='', created_at=None, period=None):
        if lottery not in LOTTERIES or not isinstance(text, str) or not text.strip():
            raise ValueError('请选择彩种并输入投注文字')
        # 先分流，不因解析失败拒收原始消息；分流异常也保留完整原文。
        try:
            sections = split_lottery_message(text, lottery)
        except (ValueError, KeyError, TypeError):
            sections = {lottery: text}
        if not sections:
            sections = {lottery: text}
        self.last_added_records = []
        for target_lottery, section_text in sections.items():
            if target_lottery not in LOTTERIES or not section_text.strip():
                continue
            record = MessageRecord(target_lottery, source, section_text, source_message_id=message_id,
                                   sender=sender, group_id=group_id, period=period or self.period)
            if created_at:
                record.created_at = created_at
            # 先事务提交原文，解析或AI失败仍可恢复。
            if not self.store.insert(record):
                continue
            try:
                self.parse_record(record)
            except Exception as exc:
                # 识别过程任何异常都不能回滚/抹掉已保存原文。
                record.bets = []
                record.status = '待确认'
                record.note = '本地识别异常，原始消息已保存：' + str(exc)[:200]
            self.store.update(record, '本地识别或待AI')
            self.last_added_records.append(record)
        return self.last_added_records[0] if self.last_added_records else None

    def possible_duplicate(self, lottery, text):
        normalized = self.parser.normalize_for_duplicate(text)
        cutoff = now()-timedelta(minutes=DUPLICATE_CHECK_MINUTES)
        for record in reversed(self.records()):
            if record.lottery != lottery or not record.source.startswith('微信'):
                continue
            timestamp = datetime.fromisoformat(record.created_at)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=now().tzinfo)
            if timestamp >= cutoff and self.parser.normalize_for_duplicate(record.raw_text) == normalized:
                return record
        return None

    def edit(self, record, text, received=None):
        if not text.strip():
            raise ValueError('投注文字不能为空')
        if received is not None:
            cents(received)
        record.raw_text = text
        record.received_amount = received
        record.ai_candidate = ''
        record.revision += 1
        self.parse_record(record)
        self.store.update(record, '人工修改')

    def reparse(self, record):
        record.revision += 1
        self.parse_record(record)
        self.store.update(record, '重新识别')

    def apply_ai_candidate(self, record_id, revision, candidate, reason=''):
        record = self.store.get(record_id)
        if not record or record.revision != revision or record.status == '已识别':
            return False
        record.ai_candidate = candidate
        self.parse_record(record)
        if reason:
            record.note += '；AI说明：'+reason[:200]
        self.store.update(record, 'AI转换程序校验并自动计入' if record.bets else 'AI转换未通过校验')
        return record.status == '已识别'

    def accept_ai(self, record):
        if not record.ai_candidate:
            raise ValueError('没有可确认的AI转换结果')
        record.revision += 1
        self.parse_record(record, allow_conflict=True)
        if record.status != '已识别':
            raise ValueError('AI转换仍未通过玩法和倍数校验，无法计入')
        self.store.update(record, '人工确认AI转换')

    def analysis(self, lottery):
        records = [r for r in self.records() if r.lottery == lottery]
        valid = [r for r in records if r.status == '已识别']
        # 只把逐笔本地验证通过的子项计入，未识别片段和整条实收都不混入。
        partial = [r for r in records if r.status in ('部分识别', 'AI识别中', 'AI待确认')
                   and r.pending_fragments and r.bets]
        bets = [b for r in valid + partial for b in r.bets]
        revenue = (sum(cents(r.received_amount) if r.received_amount is not None else
                       sum(cents(b.principal) for b in r.bets) for r in valid)
                   + sum(cents(b.principal) for r in partial for b in r.bets))
        principal, risks, payouts = self.engine.calculate(
            lottery, bets, self.settings['risk_threshold'], self.settings['external_direct_prize'][lottery],
            revenue/100, self.settings['threshold_metric'], self.settings['include_hedge_cost'],
            self.settings['direct_unit_cost'][lottery], self.settings['external_leopard_direct_prize'][lottery])
        return Analysis(cents(principal), revenue, risks, payouts,
                        len(records)-len(valid), sum(r.received_amount is None for r in valid))

    def save_settings(self, settings, rules):
        cents(settings['risk_threshold'])
        if settings['threshold_metric'] not in ('payout', 'net_loss'):
            raise ValueError('请选择总赔付或净亏损')
        for lottery in LOTTERIES:
            if min(cents(settings['external_direct_prize'][lottery]),cents(settings['external_leopard_direct_prize'][lottery]),cents(settings['direct_unit_cost'][lottery])) <= 0:
                raise ValueError('直选单倍奖金和成本须大于零')
            if settings['include_hedge_cost'] and min(cents(settings['external_direct_prize'][lottery]),cents(settings['external_leopard_direct_prize'][lottery])) <= cents(settings['direct_unit_cost'][lottery]):
                raise ValueError('计入成本时，奖金必须大于每倍成本')
            if set(rules[lottery]) != set(RULES_BY_LOTTERY[lottery]):
                raise ValueError('玩法表不完整')
            for rule in rules[lottery].values():
                if cents(rule['principal']) <= 0 or cents(rule['prize']) <= 0:
                    raise ValueError('本金和单倍奖金须为正数')
            if cents(rules[lottery]['直选'].get('leopard_prize',1600)) <= 0:
                raise ValueError('直选豹子单倍奖金须为正数')
        if settings['policy'].get('span0_wins') not in (None, True, False):
            raise ValueError('规则选项无效')
        settings['policy']['position_cost'] = 'combination'
        _migrate_position_rules(rules)
        if settings['wechat']['fc_group'] and settings['wechat']['fc_group'] == settings['wechat']['tc_group']:
            raise ValueError('福彩和体彩不能绑定同一个群')
        for key in ('bridge_port', 'wcf_port'):
            if not 1 <= int(settings['wechat'][key]) <= 65535:
                raise ValueError('连接端口须为1～65535')
        settings['rules'] = rules
        settings['policy']['span0_wins'] = True
        self.store.save_settings(settings)
        self.settings, self.rules = deepcopy(settings), deepcopy(rules)
        self.store.save_period_rules(self.period, self.rules, self.settings['policy'])
        self.parser = BetParser(self.rules, self.settings['policy'])
        self.engine = RiskEngine(self.rules, self.settings['policy'])
        # 已有投注也按当前期的规则重算；历史期保留当时快照。
        for record in self.records():
            self.parse_record(record)
            record.revision += 1
            self.store.update(record, '更新规则重算')

    def explain(self, lottery, number):
        result = []
        for record in self.records():
            if record.lottery != lottery or not (
                    record.status == '已识别' or
                    (record.status in ('部分识别', 'AI识别中', 'AI待确认')
                     and record.pending_fragments and record.bets)):
                continue
            for bet in record.bets:
                payout = self.engine.payout_for(lottery, bet, number)
                if payout:
                    result.append((record, bet, payout))
        return sorted(result, key=lambda row: -row[2])

    def drain_inbox(self):
        added = []
        errors = []
        groups = {self.settings['wechat'].get('fc_group'): '福彩', self.settings['wechat'].get('tc_group'): '体彩'}
        for inbox_id, payload in self.store.inbox(groups):
            lottery = groups.get(payload['group_id'])
            if not lottery:
                # 未绑定群保留，用户绑定后仍可接着处理。
                continue
            try:
                source = '微信历史' if payload.get('history') else '微信'
                record = self.add(lottery, payload['text'], source=source, message_id=payload['message_id'],
                                  sender=payload.get('sender', ''), group_id=payload['group_id'],
                                  created_at=payload.get('received_at'), period=payload['period'])
                self.store.mark_inbox_processed(inbox_id)
                if record:
                    added.extend(self.last_added_records)
            except (ValueError, OSError) as exc:
                errors.append(str(exc))
        return added, errors
