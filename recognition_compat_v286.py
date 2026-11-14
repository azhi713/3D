# -*- coding: utf-8 -*-
"""V286 严格兼容若干常见下单省略写法，不猜测未声明的额外投注。

范围：数字组＋“共5组直选3倍／福294注”、明确金额的多码组6，
以及“福344 3单3组🈴12”的直选＋组选共享号码。
"""
from decimal import Decimal
import re
import unicodedata

_LOT = r'(?:福彩\s*3[Dd]?|福彩|3[Dd]|体彩\s*排列[三3]|体彩|排列[三3]|福|体|排)'
_NUM = r'\d+(?:\.\d{1,2})?'
_AMOUNT = r'(?:合计|总计|合|共)\s*[:：]?\s*(?P<amount>'+_NUM+r')\s*(?:元|米|块)?'
_GROUP_CASH = re.compile(
    r'^\s*(?P<lot>'+_LOT+r')\s*(?P<digits>\d{4,9})\s*'
    r'(?P<play>组6|组六|组六|组3|组三)\s*\n\s*'+_AMOUNT+r'\s*$', re.I)
_COUNT_SUMMARY = re.compile(
    r'^\s*(?P<lot>'+_LOT+r')?\s*'
    r'(?P<prefix>共|计)?\s*(?P<count>\d+)\s*(?P<unit>组|注)\s*'
    r'(?P<play>直选|单选|直|单)?\s*'
    r'(?P<mult>半倍|0\.5倍|'+_NUM+r'\s*倍)?\s*$', re.I)
_CODE_GROUP = r'(?P<codes>\d{3}(?:[\s,，、]+\d{3})*)'
_BOTH_TOKEN = (
    r'(?:(?:各\s*)?(?P<dm1>'+_NUM+r')\s*(?:单|直)\s*'
    r'(?P<gm1>'+_NUM+r')\s*(?:组|组选)'
    r'|(?:单|直)\s*(?P<dm2>'+_NUM+r')\s*(?:组|组选)\s*(?P<gm2>'+_NUM+r')'
    r'|(?:直选|单选|直)\s*(?P<dm3>'+_NUM+r')\s*倍\s*'
    r'(?:组选|组)\s*(?P<gm3>'+_NUM+r')\s*倍)'
)
_BOTH_CASH = re.compile(r'^\s*(?P<lot>'+_LOT+r')\s*'+_CODE_GROUP+
                        r'\s*'+_BOTH_TOKEN+r'\s*'+_AMOUNT+r'\s*$', re.I)
_CODE_LINE = re.compile(r'^\d{3}(?:\s+\d{3})*$')


def _due(bets):
    return sum((Decimal(str(b.principal)) for b in bets), Decimal(0))


def _fail(parser, original, reason):
    parser.last_claimed = None
    parser.last_receipts = []
    parser.last_warnings = []
    return [], parser.normalize_text(original), reason


def _finish(parser, original, bets, claim=None, warnings=()):
    parser.last_claimed = float(claim) if claim is not None else None
    parser.last_receipts = []
    parser.last_warnings = list(warnings)
    for b in bets:
        b.raw = original
        b.source_span = {'start': 0, 'end': len(original), 'text': original,
                         'granularity': 'message', 'reference': 'current_text',
                         'evidence': 'full_literal'}
    return bets, parser.normalize_text(original), ''


def _lottery_ok(token, lottery):
    from parser import lottery_from_token
    return not token or lottery_from_token(token) == lottery


def _cash_multiplier(parser, lottery, original, digits, play, amount):
    cash = Decimal(amount)
    if cash <= 0:
        raise ValueError('投注金额必须大于零')
    unit = parser._from_kind(lottery, original, digits, play, 1)
    if len(unit) != 1 or _due(unit) <= 0:
        raise ValueError('玩法单倍本金不能唯一计算')
    mult = cash / _due(unit)
    if mult.as_tuple().exponent < -2:
        raise ValueError('金额不能精确换算为最多两位小数的倍数')
    bets = parser._from_kind(lottery, original, digits, play,
                             parser._multiplier(format(mult, 'f')))
    if _due(bets) != cash:
        raise ValueError('金额与实际玩法本金不一致')
    bets[0].entered_amount_yuan = float(cash)
    return bets


def _single_group_cash(parser, lottery, original, text):
    match = _GROUP_CASH.fullmatch(text)
    if match is None:
        return None
    if not _lottery_ok(match['lot'], lottery):
        return None
    play = '组六' if match['play'] in ('组6', '组六', '组六') else '组三'
    try:
        bets = _cash_multiplier(parser, lottery, original, match['digits'], play, match['amount'])
    except (ValueError, ArithmeticError, KeyError) as exc:
        return _fail(parser, original, str(exc))
    return _finish(parser, original, bets, Decimal(match['amount']))


def _both_cash(parser, lottery, original, text):
    match = _BOTH_CASH.fullmatch(text)
    if match is None:
        return None
    if not _lottery_ok(match['lot'], lottery):
        return None
    dm = next(match[key] for key in ('dm1', 'dm2', 'dm3') if match[key] is not None)
    gm = next(match[key] for key in ('gm1', 'gm2', 'gm3') if match[key] is not None)
    try:
        direct = parser._multiplier(dm)
        group = parser._multiplier(gm)
        bets = []
        for code in re.findall(r'(?<!\d)\d{3}(?!\d)', match['codes']):
            bets.extend(parser._from_kind(lottery, original, code, '直选', direct))
            bets.extend(parser._from_kind(lottery, original, code, '组选', group))
        claim = Decimal(match['amount'])
        if _due(bets) != claim:
            raise ValueError(f'直选和组选合计金额不一致：应收{_due(bets):g}元，自报{claim:g}元')
    except (ValueError, ArithmeticError, KeyError) as exc:
        return _fail(parser, original, str(exc))
    return _finish(parser, original, bets, claim)


def _counted_blocks(parser, lottery, original, text):
    lines = text.split('\n')
    saw_new_form = any(
        (m is not None and
         ((m['prefix'] is not None and m['unit'] == '组' and m['play'] is not None)
          or (m['lot'] is not None and m['unit'] == '注' and
              m['play'] is None and m['mult'] is None)))
        for m in (_COUNT_SUMMARY.fullmatch(line.strip()) for line in lines)
    )
    if not saw_new_form:
        return None
    blocks = []
    buffer = []
    saw_summary = False
    for line in lines:
        part = line.strip()
        if not part:
            continue
        if not blocks and not buffer and _lottery_ok(part, lottery) and part in ('福', '福彩', '体', '体彩'):
            continue
        match = _COUNT_SUMMARY.fullmatch(part)
        if not match:
            buffer.append(part)
            continue
        saw_summary = True
        if not _lottery_ok(match['lot'], lottery):
            return _fail(parser, original, '号码块汇总彩种与当前彩种不一致')
        number_text = ' '.join(buffer)
        if not _CODE_LINE.fullmatch(number_text):
            return _fail(parser, original, '号码块存在非三位号码或未解释内容')
        numbers = number_text.split()
        if len(numbers) != int(match['count']):
            return _fail(parser, original,
                         f'号码块注数不一致：实际{len(numbers)}注，声明{match["count"]}注')
        mult = (match['mult'] or '1倍').replace(' ', '')
        if mult.endswith('倍'):
            mult = mult[:-1]
        mult = '0.5' if mult == '半' else mult
        blocks.append((numbers, match['play'] or '直选', mult))
        buffer = []
    if not saw_summary:
        return None
    if any(buffer):
        # R1: only a trailing shared instruction left after validated counted
        # blocks may defer to V282's complete-source evidence. Count failures
        # above remain failures; never silently repair historical records.
        import os
        if os.environ.get('FC3D_SHARED_ORDER_FIX', '1') == '1':
            from shared_orders_v282 import parse_shared_order
            probe = type(parser)(parser.rules, parser.policy)
            shared = parse_shared_order(probe, lottery, original)
            if shared is not None and shared[0] and not shared[2]:
                parser.last_claimed = probe.last_claimed
                parser.last_receipts = probe.last_receipts
                parser.last_warnings = probe.last_warnings
                return shared
        return _fail(parser, original, '大底末尾有未关联注数的号码块')
    try:
        bets = []
        for numbers, play, mult in blocks:
            if play not in ('直选', '单选', '直', '单'):
                raise ValueError('当前简写只支持直选大底，不自动推测组选')
            multiplier = parser._multiplier(mult)
            for code in numbers:
                bets.extend(parser._from_kind(lottery, original, code, '直选', multiplier))
    except (ValueError, ArithmeticError, KeyError) as exc:
        return _fail(parser, original, str(exc))
    return _finish(parser, original, bets)


def parse_compat_v286(parser, lottery, original):
    if not isinstance(original, str) or lottery not in ('福彩', '体彩'):
        return None
    text = unicodedata.normalize('NFKC', original).replace('🈴', '合')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    if re.search(r'取消|撤单|更正|修改|改成|作废|不买', text):
        return None
    for handler in (_single_group_cash, _both_cash, _counted_blocks):
        result = handler(parser, lottery, original, text)
        if result is not None:
            return result
    return None
