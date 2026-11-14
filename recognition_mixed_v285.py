# -*- coding: utf-8 -*-
"""严格识别一笔中“各9单1组＋多码组六/组三金额”的独立金额小计。

只接受完整的多行格式。所有金额必须能按当前规则的单倍本金精确回推倍数；
不能核对的原文返回待确认，不把金额当倍数，也不静默遗漏下注片段。
"""
from decimal import Decimal
import re
import unicodedata

_NUMBER = r'\d+(?:\.\d{1,2})?'
_CASH_UNIT = r'(?:元|米|块)?'
_HEADER = re.compile(r'^\s*(?P<lot>福彩\s*3[Dd]?|福彩|3[Dd]|体彩\s*排列[三3]|体彩|排列[三3]|福|体|排)\s*(?P<body>.*?)\s*$', re.I)
_DIRECT = re.compile(r'^\d{3}(?:[\s,，、]+\d{3})+$')
_BOTH = re.compile(
    r'^各\s*(?P<direct>'+_NUMBER+r')\s*倍?\s*(?:单|直)\s*'
    r'(?P<group>'+_NUMBER+r')\s*倍?\s*组\s*'
    r'(?:合计|总计|合|共)\s*[:：]?\s*(?P<subtotal>'+_NUMBER+r')\s*'+_CASH_UNIT+r'$', re.I)
_POOL = re.compile(
    r'(?P<digits>\d{4,9})?\s*(?P<play>组六|组三)\s*'
    r'(?P<cash>'+_NUMBER+r')\s*'+_CASH_UNIT, re.I)
_TOTAL = re.compile(r'^(?:合计|总计|合|共)\s*[:：]?\s*(?P<cash>'+_NUMBER+r')\s*'+_CASH_UNIT+r'$', re.I)
_SEP = re.compile(r'[\s,，、;；]+')


def _decimal_due(bets):
    return sum((Decimal(str(b.principal)) for b in bets), Decimal(0))


def _parse_pool_line(parser, lottery, original, line):
    """一行可含数个组选玩法；第二个玩法允许省略上一组选号码。"""
    index = 0
    previous_digits = None
    bets = []
    while index < len(line):
        match = _POOL.match(line, index)
        if match is None:
            raise ValueError('混合投注存在无法核验的组选片段：' + line[index:][:60])
        digits = match['digits'] or previous_digits
        if digits is None:
            raise ValueError('组三/组六前缺少有效号码')
        previous_digits = digits
        cash = Decimal(match['cash'])
        if cash <= 0:
            raise ValueError('组选金额必须大于零')
        unit_bets = parser._from_kind(lottery, original, digits, match['play'], 1)
        if len(unit_bets) != 1:
            raise ValueError('组选复式玩法必须是独立可计费方案')
        unit = _decimal_due(unit_bets)
        if unit <= 0:
            raise ValueError('组选单倍本金配置无效')
        multiple = cash / unit
        if multiple.as_tuple().exponent < -2:
            raise ValueError('组选金额无法精确换算为最多两位小数的倍数')
        bet = parser._from_kind(lottery, original, digits, match['play'],
                                parser._multiplier(format(multiple, 'f')))
        if _decimal_due(bet) != cash:
            raise ValueError('组选金额和计算本金不一致')
        bet[0].entered_amount_yuan = float(cash)
        bets.extend(bet)
        index = match.end()
        if index == len(line):
            break
        delimiter = _SEP.match(line, index)
        if delimiter is not None:
            index = delimiter.end()
        # 无分隔符时仅接受紧接的“组三/组六”，不能吞掉其它文本。
        if index == len(line):
            raise ValueError('组选金额后存在空白但没有下一注')
    if not bets:
        raise ValueError('没有完整的组三/组六金额投注')
    return bets


def parse_mixed_receipts_v285(parser, lottery, original):
    if not isinstance(original, str) or lottery not in ('福彩', '体彩'):
        return None
    if '\n' not in original and '\r' not in original:
        return None
    text = unicodedata.normalize('NFKC', original).replace('🈴', '合').replace('\r\n', '\n').replace('\r', '\n')
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    if len(lines) < 2 or _TOTAL.fullmatch(lines[-1]) is None:
        return None
    header = _HEADER.fullmatch(lines[0])
    if header is None:
        return None  # 本识别器只负责明确标注彩种的完整多行订单。
    from parser import lottery_from_token
    if lottery_from_token(header['lot']) != lottery:
        return None  # 双彩由上层路由拆分，不能把另一彩种计入当前记录。
    first = header['body']
    body = ([first] if first else []) + lines[1:-1]
    if not body:
        return None
    # 明写“倍”的复式由既有解析器处理；本识别器只负责裸数字金额。
    # 否则如“八码组六40倍”可能被错误截成“金额40＋多余的倍字”。
    if not _DIRECT.fullmatch(body[0]) and any('倍' in line for line in body):
        return None
    # 不吞掉同一条原文中尚未按彩种拆分的另一段。
    if any(_HEADER.fullmatch(line) for line in body):
        return None
    if not (_DIRECT.fullmatch(body[0]) or re.match(r'^\d{4,9}\s*(?:组六|组三)', body[0])):
        return None

    def fail(reason):
        parser.last_claimed = None
        parser.last_receipts = []
        parser.last_warnings = []
        return [], parser.normalize_text(original), reason

    try:
        bets = []
        start = 0
        if _DIRECT.fullmatch(body[0]):
            if len(body) < 3:
                raise ValueError('一单一组之后缺少独立的组选明细')
            modifier = _BOTH.fullmatch(body[1])
            if modifier is None:
                raise ValueError('直选号码块后缺少“各9单1组合计金额”等完整说明')
            codes = re.findall(r'(?<!\d)\d{3}(?!\d)', body[0])
            direct_multiple = parser._multiplier(modifier['direct'])
            group_multiple = parser._multiplier(modifier['group'])
            section = []
            for code in codes:
                section.extend(parser._from_kind(lottery, original, code, '直选', direct_multiple))
                section.extend(parser._from_kind(lottery, original, code, '组选', group_multiple))
            due = _decimal_due(section)
            claimed = Decimal(modifier['subtotal'])
            if due != claimed:
                raise ValueError(f'各单各组金额不一致：应收{due:g}元，自报{claimed:g}元')
            bets.extend(section)
            start = 2

        for line in body[start:]:
            bets.extend(_parse_pool_line(parser, lottery, original, line))
        if not bets:
            raise ValueError('混合投注没有任何有效投注')
        total = _decimal_due(bets)
        claimed_total = Decimal(_TOTAL.fullmatch(lines[-1])['cash'])
        if total != claimed_total:
            raise ValueError(f'混合投注总金额不一致：应收{total:g}元，自报{claimed_total:g}元')
    except (ValueError, KeyError, ArithmeticError) as exc:
        return fail(str(exc))

    parser.last_claimed = float(total)
    parser.last_receipts = []
    parser.last_warnings = []
    for bet in bets:
        bet.raw = original
        bet.source_span = {'start': 0, 'end': len(original), 'text': original,
                           'granularity': 'message', 'reference': 'current_text',
                           'evidence': 'full_literal'}
    return bets, parser.normalize_text(original), ''
