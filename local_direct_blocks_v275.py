# -*- coding: utf-8 -*-
"""V275: 本地优先识别明确的多行直选号码块。

支持「福直选20倍\n569」、 「直选3D四倍\n...」以及
「三位号码清单\n福384直选1米合384」等模板。
不调用模型、不修改正式数据，不猜测没有明确价格的号码。
"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation

# Longest alternatives first: 福彩3D cannot be broken into 福彩 + 3D.
_LOT = r'(?:体彩排列三|福彩3D|排列三|福彩|体彩|3D|福|体|排)'
_PLAY = r'(?:直选|单选|直|单)'
_TOKEN = re.compile(rf'{_LOT}|{_PLAY}', re.I)
_MULT = r'(?:半|\d+(?:\.\d{1,2})?|[零〇一二两三四五六七八九十百千万]+)'
_NUM_LINE = re.compile(r'[0-9]{3}(?:[\s,，、]+[0-9]{3})*', re.ASCII)
_RECEIPT = re.compile(r'(?:(?:合计|总计|合|共)\s*[:：]?\s*(\d+(?:\.\d{1,2})?)\s*(?:元|米|块)?)$', re.I)


def _lottery(token):
    return '体彩' if token in ('体彩排列三', '排列三', '体彩', '体', '排') else '福彩'


def _known_tokens(prefix, lottery):
    """严格扫描彩种与直选词。None 表示不是本模板；冲突抛出 ValueError。"""
    where = 0
    plays = 0
    found_lottery = False
    for m in _TOKEN.finditer(prefix):
        if m.start() != where:
            return None
        tok = m.group()
        if re.fullmatch(_PLAY, tok, re.I):
            plays += 1
        else:
            found_lottery = True
            if _lottery(tok) != lottery:
                raise ValueError('直选号码块的彩种与当前彩种不一致')
        where = m.end()
    if where != len(prefix) or plays > 1:
        return None
    return plays, found_lottery


def _header(line, lottery):
    """玩法/倍数在前，号码在后，例如「直选3D四倍」或「福直选20倍」。"""
    compact = re.sub(r'\s+', '', unicodedata.normalize('NFKC', line))
    m = re.fullmatch(rf'(.*?)(?P<mult>{_MULT})倍', compact, re.I)
    if not m:
        return None
    pieces = _known_tokens(m.group(1), lottery)
    if pieces is None or pieces[0] != 1:
        return None
    return m.group('mult')


def _summary(line, lottery):
    """号码在前，分组汇总在后；无「倍」且有可靠合计时数字是单注金额。"""
    compact = re.sub(r'\s+', '', unicodedata.normalize('NFKC', line))
    compact = compact.replace('🈴', '合')
    total_match = _RECEIPT.search(compact)
    claimed = Decimal(total_match.group(1)) if total_match else None
    core = compact[:total_match.start()] if total_match else compact
    m = re.fullmatch(rf'(?P<lot>{_LOT})?(?P<count>\d+)(?P<marker>注)?(?P<tail>.*)', core, re.I)
    if not m:
        return None
    marker = m.group('lot')
    if marker is not None and _lottery(marker) != lottery:
        raise ValueError('大底汇总行的彩种与当前彩种不一致')
    tail = m.group('tail')
    play_m = re.match(_PLAY, tail, re.I)
    if play_m:
        tail = tail[play_m.end():]
    # 不能将普通「123直选2倍」错判为 123 注的大底。
    if not m.group('marker') and claimed is None:
        return None
    explicit = re.fullmatch(rf'(?P<value>{_MULT})倍', tail, re.I)
    if explicit:
        mode, value = 'multiple', explicit.group('value')
    else:
        cash = re.fullmatch(r'(?P<value>\d+(?:\.\d{1,2})?)(?:元|米|块)?', tail)
        if not cash or claimed is None:
            # 没有倍数字样，且没有合计作印证时，不猜测单价。
            return None
        mode, value = 'per_ticket_cash', cash.group('value')
    if not m.group('marker') and not play_m:
        return None
    return {'count':int(m.group('count')),'mode':mode,'value':value,'claimed':claimed}


def _numbers(line, start):
    """只接受完整的三位直选号码；保留每次实际出现的购买和字符位置。"""
    stripped = line.strip()
    if not _NUM_LINE.fullmatch(stripped):
        return None
    return [(m.group(), start+m.start(), start+m.end())
            for m in re.finditer(r'(?<![0-9])[0-9]{3}(?![0-9])', line)]


def _format_mult(value):
    try:
        number = Decimal(str(value))
    except (ValueError, InvalidOperation, TypeError):
        raise ValueError('大底单注金额不是合法数字') from None
    if not number.is_finite() or number <= 0 or number.as_tuple().exponent < -2:
        raise ValueError('大底单注金额必须大于0且最多两位小数')
    return format(number, 'f')


def parse_local_blocks(parser, lottery, original):
    """返回 None 表示不适用；否则返回 BetParser 标准三元组。

    一旦明确属于本模板，任何错误都整单失败，绝不把半笔号码入账。
    """
    if lottery not in ('福彩','体彩') or not isinstance(original, str) or not original.strip():
        return None
    lines=[]
    for match in re.finditer(r'[^\r\n]+', original):
        raw = match.group()
        if raw.strip():
            lines.append((raw, match.start()))
    if len(lines) < 2:
        return None

    # 仅忽略独立标题。不能吞掉包含金额或额外指令的行。
    start_index=0
    first = re.sub(r'\s+', '', unicodedata.normalize('NFKC', lines[0][0]))
    if re.fullmatch(_LOT, first, re.I):
        if _lottery(first) != lottery:
            return [], parser.normalize_text(original), '独立彩种标题与当前彩种冲突'
        start_index=1
    if start_index>=len(lines):
        return None

    first_line, first_start = lines[start_index]
    try:
        header = _header(first_line, lottery)
        numbers = _numbers(first_line, first_start)
        if header is None and numbers is None:
            return None  # 常见普通投注仍交回现有引擎

        mode = 'header_first' if header is not None else 'summary_last'
        groups=[]
        current=[]
        current_mult=None
        saw_marker=header is not None
        for index, (line, absolute_start) in enumerate(lines[start_index:]):
            nums = _numbers(line, absolute_start)
            if mode == 'header_first':
                hdr = _header(line, lottery)
                if hdr is not None:
                    if current_mult is not None:
                        if not current:
                            raise ValueError(f'第{len(groups)+1}组直选倍数后没有三位号码')
                        groups.append((current, 'multiple', current_mult, None))
                    current_mult, current = hdr, []
                    continue
                if nums is None:
                    raise ValueError(f'第{len(groups)+1}组只能包含三位号码和空格，不能夹杂其他内容')
                current.extend(nums)
            else:
                if nums is not None:
                    current.extend(nums)
                    continue
                receipt=_summary(line, lottery)
                if receipt is None:
                    raise ValueError(f'第{len(groups)+1}组号码后没有可验证的「注数/倍数/合计」汇总行')
                if not current:
                    raise ValueError(f'第{len(groups)+1}组汇总行之前缺少三位号码')
                if len(current)!=receipt['count']:
                    raise ValueError(f'第{len(groups)+1}组注数不一致：声明{receipt["count"]}注，实际{len(current)}注')
                groups.append((current,receipt['mode'],receipt['value'],receipt['claimed']))
                current=[]
                saw_marker=True

        if mode=='header_first':
            if not current:
                raise ValueError('最后一组直选倍数后没有号码')
            groups.append((current, 'multiple', current_mult, None))
        elif current:
            # 完全没有汇总/倍数时，交给程序原有「纯三位数字默认直选1倍」规则。
            # 只有前面已经存在明确定价分组时，尾部残缺才属于本模板错误。
            if not groups:
                return None
            raise ValueError('大底末尾存在没有注数和金额汇总的号码块')
        if not saw_marker or not groups:
            return None

        bets=[]
        warnings=[]
        claimed_total=Decimal('0')
        has_all_claimed=True
        for group_number,(codes,value_mode,value,declared) in enumerate(groups,1):
            if not codes:
                raise ValueError(f'第{group_number}组没有三位号码')
            sample = parser._from_kind(lottery, original, codes[0][0], '直选', 1)
            cost = sum(Decimal(str(b.principal)) for b in sample)
            if cost <= 0:
                raise ValueError('当前直选规则的单倍成本无效')
            if value_mode=='multiple':
                multiple=parser._multiplier(value)
            else:
                # 「1米」是单注1元（等于0.5倍）；「0.5合72」是单注0.5元（0.25倍）。
                unit_price=Decimal(_format_mult(value))
                result=unit_price/cost
                if result.as_tuple().exponent < -2:
                    raise ValueError(f'第{group_number}组单注金额无法准确换算到两位小数倍数')
                multiple=parser._multiplier(format(result,'f'))
            computed=Decimal('0')
            for code,begin,end in codes:
                entries=parser._from_kind(lottery, original, code, '直选', multiple)
                for entry in entries:
                    entry.source_span={'start':begin,'end':end,'text':original[begin:end],
                                       'granularity':'ticket','reference':'current_text',
                                       'local_group':group_number}
                    if value_mode=='per_ticket_cash':
                        entry.entered_amount_yuan=float(Decimal(_format_mult(value)))
                    computed+=Decimal(str(entry.principal))
                bets.extend(entries)
            if declared is not None:
                if computed!=declared:
                    if value_mode=='multiple':
                        # 明确倍数优先。客户汇总写错只提示，不把已验证投注退回待确认。
                        warnings.append(f'第{group_number}组金额填写错误：原文{declared:g}元，按明确倍数应收{computed:g}元')
                        has_all_claimed=False
                    else:
                        # 金额是推算倍数的唯一依据时，冲突不能自动入账。
                        raise ValueError(f'第{group_number}组金额不一致：声明{declared:g}元，按单注金额计算{computed:g}元')
                else:
                    claimed_total+=declared
            else:
                has_all_claimed=False
            if len({code for code,_,_ in codes})<len(codes):
                warnings.append(f'第{group_number}组存在重复号码，已按重复购买保留')

        parser.last_claimed=float(claimed_total) if has_all_claimed else None
        parser.last_receipts=[]
        parser.last_warnings=warnings
        return bets, parser.normalize_text(original), ''
    except (ValueError,ArithmeticError,KeyError,InvalidOperation) as exc:
        parser.last_claimed=None
        parser.last_receipts=[]
        parser.last_warnings=[]
        return [], parser.normalize_text(original), str(exc)
