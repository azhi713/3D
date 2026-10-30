from copy import deepcopy
from collections import Counter
import hashlib
import json
import re
from datetime import datetime, timedelta
from decimal import Decimal
from time import perf_counter
from config import (RULES_BY_LOTTERY, LOTTERIES, DUPLICATE_CHECK_MINUTES,
                    ENGINE_VERSION, RULES_VERSION)
from models import MessageRecord, Analysis, cents, now, money
from parser import BetParser, split_lottery_message
from risk_engine import RiskEngine
from batch_schedule import is_closed, finalized_on_time
from recognition_policy import classify_fragment, ai_fragments, ai_candidate_validation
from data_store import StaleRecordError


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
    def __init__(self, store, *, reconcile_history=False):
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
        if reconcile_history:
            store.import_legacy()
        self.activate_period(self.period)
        # 迁入后的所有历史数据均从原文重算，绝不沿用旧错误金额。
        # 常规启动禁止批量改写正式历史金额/状态；迁移只能由明确的维护操作开启。
        for period in (store.periods() if reconcile_history else ()):
            for record in store.load_records(period):
                if record.note == '旧版数据迁入，等待新规则重新识别':
                    self.parse_record(record)
                    store.update(record, '旧数据重新识别')
                elif record.status == '已识别':
                    corrected = False
                    corrected_plays = set()
                    legacy_metadata = not record.engine_version
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
                    elif legacy_metadata:
                        record.source_original_text = record.source_original_text or record.raw_text
                        record.rules_version = RULES_VERSION
                        record.engine_version = ENGINE_VERSION
                        record.parse_status = 'verified_legacy'
                        record.computed_amount_yuan = sum(cents(b.principal) for b in record.bets)/100
                        self._annotate_bets(record, self.parser_for_record(record))
                        record.revision += 1
                        store.update(record, '补录旧记录来源与版本元数据')
                elif record.status in ('待确认', '规则违规', '部分识别', 'AI待确认', 'AI待识别', 'AI识别中'):
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
            self._complete_rules(self.rules)
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

    @staticmethod
    def _complete_rules(rules):
        """旧期配置没有新玩法时补充默认项；不覆盖已有赔率，不改用户投注。"""
        for lottery in LOTTERIES:
            target=rules.setdefault(lottery,{})
            for play,rule in RULES_BY_LOTTERY[lottery].items():
                target.setdefault(play,deepcopy(rule))

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
            self._complete_rules(snapshot['rules'])
            parser = BetParser(snapshot['rules'], snapshot['policy'])
        return parser

    def deadline_enabled(self):
        """只有用户明确打开开关时，21:14封盘才影响正常操作。"""
        return self.settings.get('deadline', {}).get('enabled', False) is True

    def cutoff_applies(self, period, clock=None):
        return self.deadline_enabled() and is_closed(period, clock)

    @staticmethod
    def ai_blocked(reason):
        return not classify_fragment('', reason).get('ai_eligible', False)

    @staticmethod
    def _pending_with_policy(pending, full_text=''):
        """单独截出的'定位'/'4'不能直接定为无效：同条完整原文可能带有下注上下文。"""
        has_full_context = bool(re.search(r'(?<!\d)\d{3}(?!\d)', full_text)
                                or len(re.findall(r'[百十个](?:位)?\s*[:：]?\s*\d+', full_text)) >= 1)
        result = []
        for fragment in pending:
            checked = classify_fragment(fragment.get('text', ''), fragment.get('reason', ''))
            if checked['code'] == 'insufficient_context' and has_full_context:
                checked = {'code': 'needs_review', 'ai_eligible': True,
                           'reason': '短片段须结合整条原文和已验证明细识别'}
            result.append({**fragment, **checked})
        return result

    @staticmethod
    def _bet_key(bet):
        return (bet.play, bet.selection, str(Decimal(str(bet.multiplier)).normalize()))

    @classmethod
    def _merge_ai_bets(cls, original_bets, candidate_bets):
        """AI可返回完整标准化消息，也可只返回疑难子项；不允许重复计入已验证投注。"""
        original_count = Counter(cls._bet_key(b) for b in original_bets)
        proposed_count = Counter(cls._bet_key(b) for b in candidate_bets)
        if original_count and all(proposed_count[key] >= count for key, count in original_count.items()):
            # AI已包含全部本地明细：扣除对应数量，只接收新解释出的子项。
            used = Counter()
            missing = []
            for bet in candidate_bets:
                key = cls._bet_key(bet)
                if used[key] < original_count[key]:
                    used[key] += 1
                else:
                    missing.append(bet)
            return original_bets + missing, missing
        # AI只返回疑难小段：正常合并，然后用整条原文再做完整核验。
        return original_bets + candidate_bets, candidate_bets

    @staticmethod
    def _is_verified_total_note(full_text, pending, bets):
        """只有独立尾部金额恰好等于已验证本金时，才可认定不是新注。"""
        if len(pending) != 1 or not bets:
            return False
        text = str(pending[0].get('text', '')).strip()
        if not re.fullmatch(r'\d+(?:\.\d{1,2})?', text):
            return False
        ending = r'[，,;；\r\n]+\s*' + re.escape(text) + r'\s*$'
        if not re.search(ending, full_text):
            return False
        try:
            return cents(text) == sum(cents(b.principal) for b in bets)
        except ValueError:
            return False

    def ai_request_text(self, record, allow_existing_candidate=False):
        """向AI只提一个任务，但保留同条消息上下文；原文和本地已确认部分分开展示。"""
        unresolved = self.ai_text_for_record(record, allow_existing_candidate=allow_existing_candidate)
        if not unresolved:
            return ''
        if not record.bets and unresolved.strip() == record.raw_text.strip():
            return unresolved
        known = '\n'.join(f'{b.selection} {b.play} {b.multiplier:g}倍'
                          for b in record.bets)
        full = record.raw_text.strip()
        return ('【需要理解的疑难片段】\n' + unresolved +
                '\n【同一条消息完整原文（仅作语境依据）】\n' + full +
                ('\n【本地已确认明细（不可重复入账、不得修改）】\n' + known if known else '') +
                '\n【任务】请据原文还原所有明确的投注并输出完整规范化投注文本，'
                '绝不能补写原文没有的号码、玩法或倍数；尾部自报总额仅用于核对，不是一笔新注。')

    def ai_text_for_record(self, record, allow_existing_candidate=False):
        """外部AI仅获得符合发送条件的疑难片段；违规与金额冲突不发送。"""
        if self.deadline_enabled() and (is_closed(record.period, now()) or self.store.load_batch(record.period, record.lottery)):
            return ''
        if not record.bets and not re.search(r'[0-9]',record.raw_text):
            return ''  # 无选号数字的纯聊天，不花费模型请求去编造投注。
        if record.ai_candidate and not allow_existing_candidate:
            return ''  # 已有AI候选待审，禁止重启后反复发送并重复扣费。
        if any(not frag.get('ai_eligible', False) for frag in record.pending_fragments):
            return ''  # 同一消息含违规上下文时，整体人工核对，防止AI错配片段。
        return '\n'.join(item['text'] for item in ai_fragments(record))

    @staticmethod
    def _annotate_bets(record, parser):
        """保留源文本片段，不在无法确定字符坐标时编造offset。"""
        original = record.source_original_text or record.raw_text
        for bet in record.bets:
            bet.rules_version = record.rules_version
            bet.unit_price_yuan = parser.rules[record.lottery][bet.play]['principal']
            if not bet.source_span:
                needle = bet.raw or ''
                offset = original.find(needle) if needle and original.count(needle) == 1 else -1
                bet.source_span = ({'start':offset, 'end':offset+len(needle), 'text':needle}
                                   if offset >= 0 else {'text':needle, 'position_verified':False})

    def parse_record(self, record, allow_conflict=False):
        parser = self.parser_for_record(record)
        text = self.settings.get('aliases', {}).get(record.raw_text.strip(), record.raw_text)
        bets, normalized, reason = parser.parse(record.lottery, text)
        claimed = parser.last_claimed
        pending = []
        if not bets:
            # 先保留可靠的子项；无论是否部分成功，都不能丢失完整原文语境。
            partial, pending, normalized = parser.parse_partial(record.lottery, text)
            if partial:
                bets = partial
                claimed = None  # 未完成订单的收款合计绝不可分摊到已确认子项。
                if not pending:
                    reason = ''  # 原来整段解析报错，但分段已全部解释，不留错误提示。
        if self._is_verified_total_note(text, pending, bets):
            claimed = sum(cents(b.principal) for b in bets) / 100
            pending = []
            reason = '末尾自报合计与本地复算相符'
        pending = self._pending_with_policy(pending, text)
        record.source_original_text = record.source_original_text or record.raw_text
        record.rules_version = RULES_VERSION
        record.engine_version = ENGINE_VERSION
        used_ai = False
        review_note = ''
        if record.ai_candidate and (pending or not bets):
            eligible = [p for p in pending if p.get('ai_eligible')]
            forbidden = any(not frag['ai_eligible'] for frag in pending)
            if forbidden or not eligible or self.cutoff_applies(record.period):
                review_note = '；明确违规、冲突或已截止，AI候选不得改变原下注'
            else:
                probe = BetParser(parser.rules, parser.policy)
                ai_bets, candidate_normalized, ai_error = probe.parse(record.lottery, record.ai_candidate)
                combined, additional = self._merge_ai_bets(bets, ai_bets)
                # 不再拿“孤立的4/定位”与模型输出直接逐号比；必须对整条原文核验。
                matches, validation_reason = ai_candidate_validation(
                    text, record.ai_candidate, combined, parser, record.lottery)
                if not matches:
                    review_note = '；AI复核未通过：' + (validation_reason if ai_bets else ('AI转换未通过本地校验：' + ai_error))
                elif not additional and pending and not self._is_verified_total_note(text, pending, bets):
                    # AI只重复了已确定的订单，却没有处理剩余文字，不可假装已完成。
                    review_note = '；AI只返回已识别投注，未解释疑难片段，未重复入账'
                elif ai_bets:
                    for bet in additional:
                        bet.parse_source = 'ai_assisted'
                    bets = combined
                    pending = []
                    normalized = candidate_normalized
                    reason = ''
                    used_ai = True
                else:
                    review_note = '；AI转换未通过本地校验：' + ai_error
        record.bets = bets
        record.pending_fragments = pending
        record.issues = pending[:]
        record.normalized_text = normalized
        record.claimed_amount = claimed
        due = sum(cents(b.principal) for b in bets)
        record.computed_amount_yuan = due / 100
        record.declared_total_yuan = claimed
        if bets and pending:
            record.status = '部分识别'
            record.parse_status = ('rule_violation' if any(p['code']=='rule_violation' for p in pending)
                                   else 'needs_review')
            record.note = (f'本地已识别{len(bets)}笔、暂计{money(due)}元；'
                           f'另有{len(pending)}段待审核，金额未计入；' +
                           '；'.join(item['text'][:60] for item in pending))
        else:
            violation = any(p['code']=='rule_violation' for p in pending)
            record.status = '已识别' if bets else ('规则违规' if violation else '待确认')
            record.parse_status = ('verified_ai_assisted' if used_ai else 'verified_local') if bets else (
                'rule_violation' if violation else 'needs_review')
            record.note = reason or f'共{len(bets)}笔，应收{money(due)}元'
        if review_note:
            record.note += review_note
        if bets and getattr(parser, 'last_warnings', None):
            record.note += '；' + '；'.join(parser.last_warnings)
        if used_ai:
            record.note += '；AI转换已通过程序校验，自动计入' if not allow_conflict else '；AI候选经人工确认并通过本地校验'
        if record.claimed_amount is not None and bets and not pending:
            if cents(record.claimed_amount) != due:
                record.note += f'；自报{record.claimed_amount:g}元，差额{(due-cents(record.claimed_amount))/100:g}元'
                record.parse_status = 'amount_mismatch'
                record.issues.append({'code':'amount_mismatch', 'ai_eligible':False,
                                      'declared':record.claimed_amount, 'computed':due/100})
        for receipt in getattr(parser,'last_receipts',[]):
            if cents(receipt['declared'])!=cents(receipt['computed']):
                record.issues.append({'code':'line_amount_mismatch','ai_eligible':False,**receipt})
                if not pending:record.parse_status='amount_mismatch'
        self._annotate_bets(record, parser)

    def add(self, lottery, text, source='人工', message_id='', sender='', group_id='', created_at=None, period=None):
        if lottery not in LOTTERIES or not isinstance(text, str) or not text.strip():
            raise ValueError('请选择彩种并输入投注文字')
        record_period = period or self.period
        clock = now()
        if record_period == clock.date().isoformat() and self.cutoff_applies(record_period, clock):
            raise ValueError('今日21:14已截止接收，新投注未计入；原微信消息保留在收件箱')
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
                                   sender=sender, group_id=group_id, period=record_period,
                                   source_original_text=text, rules_version=RULES_VERSION,
                                   engine_version=ENGINE_VERSION)
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
                record.parse_status = 'needs_review'
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
        record.source_original_text = record.source_original_text or record.raw_text
        record.raw_text = text
        record.received_amount = received
        record.ai_candidate = ''
        record.revision += 1
        self.parse_record(record)
        record.review_status = 'manually_confirmed'
        self.store.update(record, '人工修改')

    def reparse(self, record):
        record.revision += 1
        self.parse_record(record)
        self.store.update(record, '重新识别')

    def apply_ai_candidate(self, record_id, revision, candidate, reason=''):
        started=perf_counter()
        self.last_ai_metrics={'verification_s':0.0,'posting_s':0.0}
        record = self.store.get(record_id)
        if not record or record.revision != revision or record.status == '已识别':
            return False
        source_text = self.ai_text_for_record(record)
        if not source_text:
            # 最关键的安全边界：无AI许可的违规订单不能靠模型改写后自动入账。
            return False
        record.ai_candidate = candidate
        record.ai_history.append({'at':now().isoformat(), 'source_fragments':source_text,
                                  'candidate':candidate, 'reason':reason[:200]})
        self.parse_record(record)
        self.last_ai_metrics['verification_s']=perf_counter()-started
        if reason:
            record.note += '；AI说明：'+reason[:200]
        posting=perf_counter()
        try:
            self.store.update(record, 'AI转换程序校验并自动计入' if record.status=='已识别' else 'AI转换未通过校验',expected_revision=revision)
        except StaleRecordError:
            return False
        self.last_ai_metrics['posting_s']=perf_counter()-posting
        return record.status == '已识别'

    def accept_ai(self, record):
        if not record.ai_candidate:
            raise ValueError('没有可确认的AI转换结果')
        if not self.ai_text_for_record(record, allow_existing_candidate=True):
            raise ValueError('明确违规或已截止的订单不能接纳AI建议，请人工修改原文')
        record.revision += 1
        self.parse_record(record, allow_conflict=True)
        if record.status != '已识别':
            raise ValueError('AI转换仍未通过玩法和倍数校验，无法计入')
        record.review_status = 'manually_confirmed'
        self.store.update(record, '人工确认AI转换')

    @staticmethod
    def _record_signature(records):
        """对参与当期的全部订单、状态和逐笔金额做稳定摘要；旧快照无摘要时不能正式结算。"""
        items = [{'id':r.record_id,'revision':r.revision,'status':r.status,
                  'parse_status':r.parse_status,'received_amount':r.received_amount,
                  'bets':[b.to_dict() for b in r.bets]} for r in records]
        payload = json.dumps(items,sort_keys=True,ensure_ascii=False,separators=(',',':'))
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    def _batch_snapshot(self, period, lottery):
        """使用截止时的已验证子项生成不可变快照；未处理片段单独列示。"""
        records = [r for r in self.store.load_records(period) if r.lottery == lottery]
        confirmed = [r for r in records if r.status == '已识别']
        partial = [r for r in records if r.bets and r.pending_fragments]
        confirmed_ids = {r.record_id for r in confirmed}
        partial = [r for r in partial if r.record_id not in confirmed_ids]
        bets = [b for r in confirmed + partial for b in r.bets]
        revenue = (sum(cents(r.received_amount) if r.received_amount is not None else
                       sum(cents(b.principal) for b in r.bets) for r in confirmed)
                   + sum(cents(b.principal) for r in partial for b in r.bets))
        period_rules = self.store.load_period_rules(period) or {'rules':self.rules,'policy':self.settings['policy']}
        engine = RiskEngine(period_rules['rules'], period_rules['policy'])
        principal, risks, _ = engine.calculate(
            lottery, bets, self.settings['risk_threshold'],
            self.settings['external_direct_prize'][lottery], revenue/100,
            self.settings['threshold_metric'], self.settings['include_hedge_cost'],
            self.settings['direct_unit_cost'][lottery], self.settings['external_leopard_direct_prize'][lottery])
        unresolved = [
            {'record_id':r.record_id, 'status':r.status, 'parse_status':r.parse_status,
             'fragments':[{'text':p.get('text',''), 'code':p.get('code','needs_review')}
                          for p in r.pending_fragments],
             'claimed_amount_yuan':r.claimed_amount}
            for r in records if r.status != '已识别'
        ]
        # 防止解封测试修改投注后，复用封盘前的快照做正式结算。
        record_signature = self._record_signature(records)
        return {'schema_version':'1.0', 'lottery':lottery,'period':period,
                'record_signature':record_signature,
                'engine_version':ENGINE_VERSION,'rules_version':RULES_VERSION,
                'message_count':len(records),'confirmed_messages':len(confirmed),
                'partial_messages':len(partial),'unresolved_messages':len(unresolved),
                'verified_bet_count':len(bets),
                'verified_principal_yuan':round(principal,2),
                'verified_revenue_yuan':revenue/100,
                'top_risk':[{'number':r.number,'payout_yuan':r.payout,
                             'net_loss_yuan':r.net_loss} for r in risks[:10]],
                'unresolved':unresolved}

    def tick_deadline(self, clock=None):
        """界面定时调用；只要过21:14立即冻结并完成当批汇总，不等到21:15才计算。"""
        simulated_clock = clock is not None
        clock = clock or now()
        period = clock.date().isoformat()
        if not self.cutoff_applies(period, clock):
            return []
        results = []
        for lottery in LOTTERIES:
            existing = self.store.load_batch(period, lottery)
            if existing and existing['state'] == 'finalized':
                continue
            started = perf_counter()
            if not existing:
                snapshot = self._batch_snapshot(period, lottery)
                self.store.freeze_batch(period, lottery, snapshot, clock.isoformat())
                existing = self.store.load_batch(period, lottery)
                # 截止时未解决项目保持原有可见状态，只追加独立的冻结状态。
                for record in self.store.load_records(period):
                    if record.lottery == lottery and record.status != '已识别':
                        record.deadline_state = 'unresolved_at_deadline'
                        self.store.update(record, '截止未解决标记')
            if existing and existing['state'] == 'frozen':
                finished = clock if simulated_clock else now()
                summary = dict(existing['frozen'])
                summary.update(finalized_at=finished.isoformat(),
                               processing_seconds=round(perf_counter()-started,3),
                               completed_before_21_15=finalized_on_time(period, finished))
                if self.store.finalize_batch(period, lottery, summary, finished.isoformat()):
                    results.append(summary)
        return results

    def preview_draw(self, lottery, draw, period=None):
        """不入库的实际开奖试算；只汇总已验证子项，不能把疑难投注当作零。"""
        period = period or self.period
        if lottery not in LOTTERIES or not isinstance(draw, str) or not re.fullmatch(r'[0-9]{3}', draw):
            raise ValueError('开奖号码必须是三位数字，例如012、293、000')
        records = [r for r in self.store.load_records(period) if r.lottery == lottery]
        valid = [r for r in records if r.status == '已识别']
        partial = [r for r in records if r.status in ('部分识别', 'AI识别中', 'AI待确认')
                   and r.pending_fragments and r.bets]
        counted = valid + partial
        bet_count = sum(len(r.bets) for r in counted)
        rules = self.store.load_period_rules(period) or {'rules':self.rules,'policy':self.settings['policy']}
        engine = RiskEngine(rules['rules'], rules['policy'])
        lines = []
        payout_cents = 0
        income_cents = 0
        concerns = []
        blockers = []
        for record in counted:
            bets_principal = sum(cents(b.principal) for b in record.bets)
            received = (cents(record.received_amount) if record.status == '已识别' and record.received_amount is not None
                        else bets_principal)
            income_cents += received
            for bet in record.bets:
                prize = engine.payout_for(lottery, bet, draw)
                if prize:
                    amount = cents(prize)
                    payout_cents += amount
                    lines.append({'record_id':record.record_id,'sender':record.sender or record.source,
                                  'raw_text':record.raw_text,'play':bet.play,'selection':bet.selection,
                                  'multiplier':bet.multiplier,'bet_principal_yuan':bet.principal,
                                  'payout_yuan':amount/100,'source_span':bet.source_span})
                if Decimal(str(bet.multiplier)) % 1 != 0:
                    blockers.append('涉及小数倍，兑奖比例尚未完成业务确认')
                if bet.play == '双飞' and len(set(bet.selection)) == 1:
                    blockers.append('涉及重复数字双飞，兑奖边界尚未完成确认')
                if bet.play == '三码组三':
                    blockers.append('涉及三码组三，兑奖覆盖边界尚未完成核验')
        missing = [r for r in records if r.status != '已识别']
        if missing:
            blockers.append(f'还有{len(missing)}条未完全识别/待审核记录；其未确认部分没有计入')
        if any(r.parse_status == 'amount_mismatch' for r in records):
            blockers.append('存在客户自报合计与程序核算不一致的记录')
        if any(r.status == '已识别' and r.received_amount is None for r in records):
            concerns.append('部分投注未填实际实收，目前按应收计入收入')
        if any(r.status == '已识别' and r.received_amount is not None for r in records):
            concerns.append('计入收入包含手动填写的实收，未额外核实款项是否到账')
        if not records:
            blockers.append('本期没有投注记录，不能把零赔付视作完整结算')
        batch = self.store.load_batch(period, lottery)
        if batch and not self.deadline_enabled():
            concerns.append('存在历史截止快照，但当前封盘开关已关闭；试算按当前有效记录')
        # 零总计也要返回，供人工查验；正式结算仍走 record_settlement 的独立安全门槛。
        return {'lottery':lottery, 'period':period, 'draw':draw,
                'message_count':len(records), 'verified_bets':bet_count,
                'income_yuan':income_cents/100, 'payout_yuan':payout_cents/100,
                'net_income_yuan':(income_cents-payout_cents)/100,
                'winning_count':len(lines), 'lines':sorted(lines, key=lambda row:-row['payout_yuan']),
                'warnings':list(dict.fromkeys(blockers + concerns)),
                'blockers':list(dict.fromkeys(blockers)), 'complete':not blockers}

    def record_settlement(self, lottery, draw, period=None, draw_info=None):
        """开奖与下注分账；未确认的派奖规则不作自动结算。"""
        period = period or self.period
        if lottery not in LOTTERIES or len(draw) != 3 or not draw.isascii() or not draw.isdigit():
            raise ValueError('请提供合法彩种与三位开奖号码')
        if not self.deadline_enabled():
            raise ValueError('21:14自动封盘未启用；不能把测试期间的旧快照当成正式结算依据')
        batch = self.store.load_batch(period, lottery)
        if not batch or batch['state'] != 'finalized':
            raise ValueError('请先完成当期截止汇总，再进行独立结算')
        records = [r for r in self.store.load_records(period) if r.lottery == lottery]
        saved_signature = batch.get('frozen', {}).get('record_signature')
        if not saved_signature or saved_signature != self._record_signature(records):
            raise ValueError('封盘后投注明细已变化或旧版快照缺少校验信息，禁止以旧快照正式结算')
        if any(r.status != '已识别' or r.parse_status == 'amount_mismatch' for r in records):
            raise ValueError('本期存在待审核或金额不符记录，不能自动结算')
        rules = self.store.load_period_rules(period) or {'rules':self.rules,'policy':self.settings['policy']}
        engine = RiskEngine(rules['rules'], rules['policy'])
        lines = []
        for record in records:
            for bet in record.bets:
                if Decimal(str(bet.multiplier)) % 1 != 0:
                    raise ValueError('小数倍兑奖尚未确认，不允许自动计算派奖')
                if bet.play == '双飞' and len(set(bet.selection)) == 1:
                    raise ValueError('重复数字双飞兑奖边界尚未确认，需人工核对')
                if bet.play == '三码组三':
                    raise ValueError('明确三码组三的兑奖边界尚未完成核验')
                payout = engine.payout_for(lottery, bet, draw)
                lines.append({'record_id':record.record_id,'play':bet.play,
                              'selection':bet.selection,'multiplier':bet.multiplier,
                              'payout_yuan':payout})
        payload = {'schema_version':'1.0','period':period,'lottery':lottery,
                   'draw':draw,'draw_info':draw_info or {'source':'手动输入'},'settled_at':now().isoformat(),
                   'rules_version':RULES_VERSION,'engine_version':ENGINE_VERSION,
                   'total_payout_yuan':sum(cents(line['payout_yuan']) for line in lines)/100,
                   'lines':lines}
        self.store.save_settlement(period, lottery, draw, payload)
        return payload

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
        reprice=(rules!=self.rules or settings.get('policy')!=self.settings.get('policy')
                 or settings.get('aliases')!=self.settings.get('aliases'))
        # 封盘已开启时仍允许保存AI/微信配置，尤其允许关掉封盘用于测试；
        # 但保持真实封盘快照与财务参数不可被无意改写。
        deadline_config = settings.get('deadline', {})
        if not isinstance(deadline_config, dict) or type(deadline_config.get('enabled', False)) is not bool:
            raise ValueError('21:14自动封盘开关设置无效')
        locked = (deadline_config.get('enabled', False) and
                  self.period == now().date().isoformat() and is_closed(self.period, now()))
        financial_keys = ('risk_threshold', 'threshold_metric', 'policy', 'direct_unit_cost',
                          'include_hedge_cost', 'external_direct_prize',
                          'external_leopard_direct_prize', 'plan_total_mode', 'aliases')
        if locked and (rules != self.rules or any(settings.get(k) != self.settings.get(k) for k in financial_keys)):
            raise ValueError('今日21:14已截止：如需测试修改金额或玩法规则，请先关闭自动封盘')
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
        if not locked:
            self.store.save_period_rules(self.period, self.rules, self.settings['policy'])
        self.parser = BetParser(self.rules, self.settings['policy'])
        self.engine = RiskEngine(self.rules, self.settings['policy'])
        # 已有投注也按当前期的规则重算；封盘期间保存非业务配置不改历史。
        if locked or not reprice:
            return
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
                if '21:14已截止' in str(exc):
                    self.store.mark_inbox_rejected(inbox_id, str(exc))
                errors.append(str(exc))
        return added, errors
