# -*- coding: utf-8 -*-
"""V282: source-scoped number blocks and shared trailing instructions.

No prices/odds are declared here: every bet is built by the installed parser.
Original text, repeated purchases and the exact source line references survive.
None means outside this grammar; a recognized but incomplete order fails closed.
"""
from __future__ import annotations
from collections import Counter
from copy import copy
from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import itertools
import json
import re
import unicodedata

from source_context_v281 import scalar, lottery_of, _build

LOT = r'(?:体彩排列三|福彩3[Dd]|排列三|福彩|体彩|3[Dd]|福|体|排)'
PLAY = (r'(?:组六复式|组三复式|(?:[二三四五六七八九2-9]码)?(?:组六|组三|组6|组3|复试|复式)'
        r'|直选|单选|组选|双飞|独胆|毒胆|对子|直|单|组)')
VALUE = (r'(?:[0-9]+/[0-9]+|[零〇一二两三四五六七八九十百千万]+分之'
         r'[零〇一二两三四五六七八九十百千万]+|[0-9]+(?:\.[0-9]+)?|'
         r'[零〇一二两三四五六七八九十百千万]+|半)')
UNIT = r'(?:倍|元|米|块|毛|角|分)'
OP = re.compile(rf'(?P<play>{PLAY})?(?P<each>各)?(?:打|买|投)?(?P<value>{VALUE})(?P<unit>{UNIT})?', re.I)
COUNT = re.compile(r'(?P<count>[0-9]+)注(?P<tail>.*)\Z')
COUNT_GROUP_MULT = re.compile(rf'(?:共)?(?P<count>[0-9]+)组(?P<multiple>{VALUE})倍\Z')
RECEIPT = re.compile(r'(?:合计|总计|小计|合|共)[:：]?(?P<value>[0-9]+(?:\.[0-9]{1,2})?)(?:元|米|块)?[。!！]*\Z')
NUMBERS = re.compile(r'[0-9]{1,9}(?:[\s,，、/]+[0-9]{1,9})*\Z')
FORBIDDEN = re.compile(r'撤单|取消|更正|改成|不买|作废|不要|减掉|删除')
NAME = 'v282_shared_order'
MAX_ITEMS = 4000


@dataclass(frozen=True)
class Line:
    raw: str
    start: int
    end: int
    sid: str


@dataclass(frozen=True)
class Operation:
    play: str
    value: str
    unit: str | None
    each: bool


@dataclass
class Group:
    codes: list[str]
    lines: list[Line]
    count: int | None = None
    summary: Line | None = None
    operations: tuple[Operation, ...] | None = None
    instruction: Line | None = None
    batch: int | None = None
    strict_count: bool = False


@dataclass
class Batch:
    groups: list[Group]
    operations: tuple[Operation, ...]
    instruction: Line
    claimed: Decimal | None


def canonical(text: str) -> str:
    s = unicodedata.normalize('NFKC', text).replace('🈴', '合').replace('㊗', '注')
    s = s.replace('祝\ufe0f', '注').replace('\ufe0f', '').replace('⁄', '/').replace('／', '/')
    return re.sub(r'[ \t\u3000]+', '', s).strip('，,;；。')


def lines_of(text: str) -> list[Line]:
    out = []
    for m in re.finditer(r'[^\r\n]+', text):
        if not m.group().strip():
            continue
        left = m.start() + len(m.group()) - len(m.group().lstrip())
        right = m.end() - len(m.group()) + len(m.group().rstrip())
        out.append(Line(text[left:right], left, right, 'S'+str(len(out)+1)))
    return out


def strip_lottery(s: str, lottery: str) -> tuple[str, str | None]:
    m = re.match(LOT, s, re.I)
    if not m:
        return s, None
    if lottery_of(m.group()) != lottery:
        raise ValueError('同一号码块出现其他彩种；不得跨彩种套用共享玩法')
    return s[m.end():].lstrip(':：,，'), m.group()


def number_list(line: Line) -> list[str] | None:
    s = unicodedata.normalize('NFKC', line.raw).strip()
    s = re.sub(r'^'+LOT+r'\s*[:：]?\s*', '', s, count=1, flags=re.I)
    if not NUMBERS.fullmatch(s):
        return None
    return re.findall(r'[0-9]+', s)


def instructions(s: str, default_direct: bool = False):
    """Consume a whole instruction, including all play clauses and receipt."""
    claim = RECEIPT.search(s)
    claimed = Decimal(claim['value']) if claim else None
    body = s[:claim.start()] if claim else s
    body = body.strip('，,;；。')
    if not body:
        return (), claimed
    # Explicit one-and-a-half forms are lexical aliases, not two purchases.
    body = re.sub(r'('+VALUE+r')倍半', lambda m: format(scalar(m[1])+Decimal('.5'), 'f')+'倍', body)
    pos = 0
    ops = []
    while pos < len(body):
        sep = re.match(r'[，,;；。]+', body[pos:])
        if sep:
            pos += sep.end()
        if pos == len(body):
            break
        m = OP.match(body, pos)
        if not m or (m['play'] is None and not default_direct):
            return None
        if m['play'] is None and ops:
            return None
        play = m['play'] or '直选'
        if m['unit'] is None and m['play'] is None and claimed is None:
            return None
        ops.append(Operation(play, m['value'], m['unit'], bool(m['each'])))
        pos = m.end()
        if len(ops) > 4:
            raise ValueError('共享玩法超过4项，保留原文核对')
    if not ops:
        return None
    if len({op.play for op in ops}) != len(ops):
        raise ValueError('同一共享指令重复声明同一玩法，请分组列明')
    return tuple(ops), claimed


def parse_structure(lottery: str, original: str):
    lines = lines_of(original)
    if len(lines) < 2 or FORBIDDEN.search(original):
        return None
    # Do not intercept existing positional, sum/span, full-cover or mixed inline
    # grammars. Their original parser/evidence chain retains ownership.
    if re.search(r'定位|胆拖|[0-9]胆|和值|合值|跨度|全包|包全|转圈打|豹子', original):
        return None
    inline = re.compile(r'^(?:'+LOT+r')?\s*[0-9]{1,9}(?:[ \t,，、/]+[0-9]{1,9})*\s*(?:'+PLAY+r')', re.I)
    # A bare "2组30倍" is a strict count summary only when it is the entire
    # line and follows a pure-number pool. Do not mistake it for inline 组选.
    if any(inline.match(unicodedata.normalize('NFKC', line.raw)) and
           not COUNT_GROUP_MULT.fullmatch(canonical(line.raw)) for line in lines):
        return None
    # Only pure number blocks enter this additive grammar. Legacy inline forms
    # (e.g. 123直选1倍) remain on their original, independently tested path.
    numbered = [line for line in lines if number_list(line) is not None]
    if not numbered:
        return None
    marked = any(COUNT.fullmatch(re.sub(r'^'+LOT, '', canonical(l.raw), count=1, flags=re.I))
                 or re.search(r'倍|[元米块毛角]|直选|组[三六36]|复[试式]', l.raw) for l in lines)
    if not marked:
        return None
    groups: list[Group] = []
    batches: list[Batch] = []
    pending: list[str] = []
    pending_lines: list[Line] = []
    active = None
    grand = None
    grand_line = None
    finished = False

    def flush(count=None, summary=None, strict_count=False):
        nonlocal pending, pending_lines
        if not pending:
            raise ValueError('注数/玩法说明前没有对应的号码块')
        g = Group(pending, pending_lines, count, summary, strict_count=strict_count)
        groups.append(g)
        pending, pending_lines = [], []
        return g

    def bind(targets, ops, line, claim):
        if not targets:
            raise ValueError('共享玩法前没有未定价的号码组')
        for g in targets:
            if g.operations is not None:
                raise ValueError('不得覆盖前面已经明确的玩法或倍数')
            g.operations, g.instruction, g.batch = ops, line, len(batches)
        batches.append(Batch(targets, ops, line, claim))

    for line in lines:
        compact = canonical(line.raw)
        s, label = strip_lottery(compact, lottery)
        if not s:
            continue
        if finished:
            raise ValueError('整单合计之后还有未核验的投注内容')
        nums = number_list(line)
        if nums is not None:
            pending.extend(nums)
            pending_lines.append(line)
            if sum(len(g.codes) for g in groups) + len(pending) > MAX_ITEMS:
                raise ValueError('本单号码条目超过4000，请核对重复粘贴')
            continue
        count_group = COUNT_GROUP_MULT.fullmatch(s)
        if count_group:
            if active is not None:
                raise ValueError('同组前置倍数与后置组数倍数冲突，须人工核对')
            g = flush(int(count_group['count']), line, strict_count=True)
            # "组" is the number of listed three-digit entries, not 组选.
            # Following the existing pure-number-list convention, play=直选.
            spec = instructions(count_group['multiple']+'倍', default_direct=True)
            if spec is None or not spec[0] or len(spec[0]) != 1:
                raise ValueError('组数汇总缺少唯一、明确的倍数')
            bind([g], spec[0], line, None)
            continue
        cm = COUNT.fullmatch(s)
        if cm:
            if active is not None:
                raise ValueError('同组前置倍数与后置注数指令需要分别列明，未覆盖原倍数')
            g = flush(int(cm['count']), line)
            tail = cm['tail']
            if re.fullmatch(r'[（(]方案[一二三四五六七八九十0-9]+[)）]', tail):
                tail = ''
            if tail:
                spec = instructions(tail, default_direct=True)
                if spec is None or not spec[0]:
                    raise ValueError('分组说明包含未核验的玩法或金额：'+line.raw[:80])
                bind([g], spec[0], line, spec[1])
            continue
        spec = instructions(s, default_direct=True)
        if spec is None:
            # Leave an unrelated legacy syntax to its original parser when
            # it has not yet been claimed as a numbered/grouped order.
            if not groups and not pending and active is None:
                return None
            raise ValueError('号码块存在未解释内容：'+line.raw[:80])
        ops, claim = spec
        if not ops:
            if claim is None or grand is not None:
                raise ValueError('重复合计或无效的金额说明')
            if pending:
                if active is None:
                    raise ValueError('合计之前仍有未关联玩法/倍数的号码')
                g = flush()
                bind([g], *active)
                active = None
            if not groups or any(g.operations is None for g in groups):
                raise ValueError('仅凭整单合计不能给未定价分组猜测倍数')
            grand, grand_line, finished = claim, line, True
            continue
        if pending:
            g = flush()
            if active is not None:
                bind([g], *active)
                active = (ops, line, claim)
            else:
                targets = [g for g in groups if g.operations is None]
                bind(targets, ops, line, claim)
        elif any(g.operations is None for g in groups):
            bind([g for g in groups if g.operations is None], ops, line, claim)
        else:
            if active is not None:
                raise ValueError('前置玩法指令后缺少号码')
            active = (ops, line, claim)
    if pending:
        g = flush()
        if active is not None:
            bind([g], *active)
            active = None
    if active is not None or not groups or any(g.operations is None for g in groups):
        raise ValueError('有号码组尚未关联明确的玩法、倍数或单注金额')
    return groups, batches, grand, grand_line


def make(parser, lottery, source, code, play, multiplier):
    if play in ('组六复式', '组三复式'):
        return [parser._make(lottery, source, play, code, multiplier)]
    return _build(parser, lottery, source, code, play, multiplier)


def amount(bets) -> Decimal:
    return sum((Decimal(str(b.principal)) for b in bets), Decimal(0))


def signature(bets):
    return sorted((b.play, b.selection, str(Decimal(str(b.multiplier)).normalize()),
                   str(Decimal(str(b.principal)).normalize())) for b in bets)


def order_digest(source, bets):
    payload = json.dumps([source, signature(bets)], ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf8')).hexdigest()


def _build_batch(parser, lottery, source, batch, modes):
    result = []
    per_group = []
    for group in batch.groups:
        gbets = []
        for code in group.codes:
            for op, mode in zip(batch.operations, modes):
                base = make(parser, lottery, source, code, op.play, 1)
                unit = amount(base)
                if unit <= 0:
                    raise ValueError('玩法单倍本金无效')
                value = scalar(op.value)
                if not value.is_finite() or value <= 0:
                    raise ValueError('金额/倍数必须大于0')
                if mode == 'inverse':
                    value = Decimal(1) / value
                cash = value / (10 if op.unit in ('毛', '角') else 100 if op.unit == '分' else 1)
                multiple = value if mode in ('multiple', 'inverse') else cash / unit
                if multiple*100 != (multiple*100).to_integral_value():
                    raise ValueError('金额不能精确换算成两位小数倍数；未四舍五入')
                mult = parser._multiplier(format(multiple.normalize(), 'f'))
                built = make(parser, lottery, source, code, op.play, mult)
                if mode == 'cash' and amount(built) != cash:
                    raise ValueError('单注金额回推后复算不一致')
                refs = {line.sid: line for line in group.lines}
                if group.summary:
                    refs[group.summary.sid] = group.summary
                refs[batch.instruction.sid] = batch.instruction
                ordered = sorted(refs.values(), key=lambda line: line.start)
                start, end = min(l.start for l in ordered), max(l.end for l in ordered)
                for b in built:
                    if Decimal(str(b.principal))*100 != (Decimal(str(b.principal))*100).to_integral_value():
                        raise ValueError('投注本金不是精确到分')
                    b.raw = source
                    b.parse_source = 'local'
                    b.source_span = {
                        'start': start, 'end': end, 'text': source[start:end],
                        'position_verified': True, 'reference': 'current_text',
                        'granularity': 'shared_instruction', 'compatibility': NAME,
                        'segment_id': [l.sid for l in ordered],
                        'evidence_segments': [{'segment_id': l.sid, 'start': l.start,
                            'end': l.end, 'text': source[l.start:l.end]} for l in ordered],
                        'selection_segment_ids': [l.sid for l in group.lines if re.search(r'(?<![0-9])'+re.escape(code)+r'(?![0-9])', unicodedata.normalize('NFKC', l.raw))],
                        'instruction_segment_id': batch.instruction.sid,
                    }
                    if mode == 'cash':
                        b.entered_amount_yuan = float(cash)
                gbets.extend(built)
        per_group.append((group, gbets))
        result.extend(gbets)
    return result, per_group


def parse_shared_order(parser, lottery, original):
    """Return BetParser's tuple. Count mismatches are advisory, never new bets."""
    if not isinstance(original, str) or not original.strip() or lottery not in ('福彩', '体彩'):
        return None
    try:
        structure = parse_structure(lottery, original)
        if structure is None:
            return None
        groups, batches, grand, grand_line = structure
        bets = []
        warnings = []
        claims = []
        receipts = []
        group_results = []
        for batch in batches:
            choices = []
            bare = False
            for op in batch.operations:
                if op.unit == '倍':
                    if op.value == '4/1':
                        if batch.claimed is None:
                            raise ValueError('4/1倍需完整合计核验，不能自行倒置分数')
                        choices.append(('multiple', 'inverse'))
                        bare = True
                    else:
                        choices.append(('multiple',))
                elif op.unit is not None:
                    choices.append(('cash',))
                elif batch.claimed is not None:
                    choices.append(('cash', 'multiple'))
                    bare = True
                elif (all(4 <= len(c) <= 9 for g in batch.groups for c in g.codes)
                      and re.search(r'组[三六36]$', op.play)):
                    choices.append(('cash',))
                    warnings.append('多码组选未写单位，沿用金额口径；按本机单倍本金回推倍数')
                else:
                    raise ValueError('未写金额单位或倍数，且无可靠合计；不猜测定价')
            accepted = {}
            last_error = ''
            for modes in itertools.product(*choices):
                try:
                    built, rows = _build_batch(parser, lottery, original, batch, modes)
                    if bare and amount(built) != batch.claimed:
                        continue
                    accepted[tuple(signature(built))] = (built, rows)
                except (ValueError, KeyError, ArithmeticError) as exc:
                    last_error = str(exc)
            if len(accepted) != 1:
                raise ValueError(last_error or '共享金额/倍数无法根据完整原文唯一核验')
            built, rows = next(iter(accepted.values()))
            bets.extend(built)
            group_results.extend(rows)
            claims.append(batch.claimed)
            if batch.claimed is not None:
                due = amount(built)
                receipts.append({'start': min(l.start for g in batch.groups for l in g.lines),
                                 'end': batch.instruction.end, 'declared': float(batch.claimed),
                                 'computed': float(due)})
                if batch.claimed != due:
                    warnings.append(f'共享说明自报{batch.claimed:g}元，按明确投注应收{due:g}元；未改写号码倍数')
        for index, group in enumerate(groups, 1):
            rows = next(rows for g, rows in group_results if g is group)
            if group.count is not None and group.count != len(group.codes):
                if group.strict_count:
                    raise ValueError(f'第{index}组声明{group.count}组，实际{len(group.codes)}组；未将该组投注入账')
                warnings.append(f'核对提醒：第{index}组填写{group.count}注，实际{len(group.codes)}注；已按实际号码及明确定价核算{amount(rows):g}元，请核对是否漏列号码')
                for b in rows:
                    b.source_span['count_discrepancy'] = {'declared': group.count, 'actual': len(group.codes)}
            if len(set(group.codes)) != len(group.codes):
                warnings.append(f'第{index}组含重复号码，已按重复购买保留并累计')
        if grand is not None and grand != amount(bets):
            warnings.append(f'整单自报{grand:g}元，程序应收{amount(bets):g}元；号码倍数未改动')
        digest = order_digest(original, bets)
        for b in bets:
            b.source_span['v282_order_digest'] = digest
            b.source_span['v282_order_size'] = len(bets)
        parser.last_claimed = (float(grand) if grand is not None else
                               float(sum(claims)) if claims and all(x is not None for x in claims) else None)
        parser.last_receipts = receipts
        parser.last_warnings = list(dict.fromkeys(warnings))
        return bets, parser.normalize_text(original), ''
    except (ValueError, KeyError, ArithmeticError) as exc:
        parser.last_claimed = None
        parser.last_receipts = []
        parser.last_warnings = []
        return [], parser.normalize_text(original), str(exc)


def complete_shared_evidence(source, bets):
    """Only a complete program-built ledger can use shared-scope proof."""
    if not bets:
        return False
    fingerprint = order_digest(source, bets)
    return all(isinstance(getattr(b, 'source_span', None), dict)
               and b.source_span.get('compatibility') == NAME
               and b.source_span.get('v282_order_digest') == fingerprint
               and b.source_span.get('v282_order_size') == len(bets) for b in bets)


def decode_shared_candidates(source, raw_candidate, parser, lottery):
    """AI fallback: verify its ledger against complete deterministic source proof.

    Missing shared-instruction refs may be supplemented ONLY after the source
    grammar has uniquely established scope; invented references remain invalid.
    No arbitrary model price, changed number, missing bet or duplicate is accepted.
    """
    probe = copy(parser)
    parsed = parse_shared_order(probe, lottery, source)
    if parsed is None:
        return None
    expected, _, problem = parsed
    if problem or not expected:
        return [], (), ('共享原文未通过本地完整校验：'+problem,), True
    try:
        obj = json.loads(raw_candidate)
        items = obj['candidate_bets']
        if not isinstance(items, list) or not items:
            raise ValueError('candidate_bets为空或不是数组')
        available = list(expected)
        source_lines = {l.sid: l for l in lines_of(source)}
        accepted = []
        for index, item in enumerate(items, 1):
            if not isinstance(item, dict):
                raise ValueError('候选不是对象')
            target = item.get('lottery', lottery)
            if target not in (lottery, '福' if lottery == '福彩' else '体',
                               '3D' if lottery == '福彩' else '排列三',
                               '福彩3D' if lottery == '福彩' else '体彩排列三'):
                raise ValueError('候选彩种与当前订单不符')
            refs = item.get('segment_id', item.get('source_ref'))
            refs = [refs] if isinstance(refs, str) else refs
            if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or r not in source_lines for r in refs):
                raise ValueError('候选引用不存在或缺少的原文段落')
            lo, hi = min(source_lines[r].start for r in refs), max(source_lines[r].end for r in refs)
            if item.get('evidence_text') is not None and item['evidence_text'] != source[lo:hi]:
                raise ValueError('候选声明的原文与实际引用段落不一致')
            if item.get('source_start') is not None or item.get('source_end') is not None:
                if type(item.get('source_start')) is not int or type(item.get('source_end')) is not int or (item['source_start'], item['source_end']) != (lo, hi):
                    raise ValueError('候选原文坐标与引用段落不一致')
            codes = item.get('ticket_codes')
            if codes is None:
                codes = [item.get('digit_set', item.get('selection'))]
            if not isinstance(codes, list) or not codes or not all(isinstance(c, str) for c in codes):
                raise ValueError('选号必须为保留前导零的字符串')
            multiple = item.get('bet_multiples', item.get('multiple'))
            cash = item.get('declared_amount_yuan', item.get('amount'))
            selected = []
            for code in codes:
                named = item.get('bet_type', item.get('play'))
                if not isinstance(named, str):
                    raise ValueError('缺少玩法')
                canonical_play = make(probe, lottery, source, code, named, 1)[0].play
                matches = [b for b in available if b.selection == code and b.play == canonical_play
                           and any(r in b.source_span['selection_segment_ids'] for r in refs)
                           and (multiple is None or Decimal(str(multiple)) == Decimal(str(b.multiplier)))]
                if not matches:
                    raise ValueError('候选号码、玩法、倍数或引用范围与原文不符：'+code)
                if len({str(b.multiplier) for b in matches}) > 1:
                    raise ValueError('重复号码对应不同购买倍数，须明确对应原文行')
                bet = matches[0]
                available.remove(bet)
                selected.append(bet)
            if multiple is None and cash is None:
                raise ValueError('缺少金额和倍数')
            if type(multiple) is bool or type(cash) is bool:
                raise ValueError('金额或倍数不能为布尔值')
            if cash is not None and Decimal(str(cash)) != amount(selected):
                raise ValueError('候选金额与原文所属分组不符，不能借用整单合计')
            for bet in selected:
                bet.parse_source = 'ai_evidence_verified'
                bet.source_span['ai_original_segment_ids'] = refs
            accepted.extend(selected)
        if available or signature(accepted) != signature(expected):
            raise ValueError('AI遗漏或重复了原文的投注，不确认不完整账单')
        unsure = obj.get('needs_review') is True or bool(obj.get('uncertainties'))
        if unsure:
            return [], tuple(items), ('AI仍声明不确定，未自动覆盖本地结果',), True
        return accepted, tuple(items), (), False
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        return [], (), ('共享上下文校验失败：'+str(exc),), True
