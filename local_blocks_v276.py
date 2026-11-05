# -*- coding: utf-8 -*-
"""V2.7.6: conservative complete-message local number-block recognizer.

Source-backed parsing of explicit 3-digit number groups, amounts, counts,
trailing shared directives, parenthesized plan labels and arithmetic receipts.
Unsupported formats fall through to the already installed V275 recognizer.
"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from local_direct_blocks_v275 import parse_local_blocks as _legacy_local_blocks

_LOT = r'(?:体彩排列三|福彩3[Dd]|排列三|福彩|体彩|3[Dd]|福|体|排)'
_PLAY = r'(?:直选|单选|直|单)'
_MULT = r'(?:半|\d+(?:\.\d{1,2})?|[零〇一二两三四五六七八九十百千万]+)'
_CODE_RE = re.compile(r'(?<![0-9])[0-9]{3}(?![0-9])')
_LIST_RE = re.compile(r'[0-9]{3}(?:[\s,，、]+[0-9]{3})*\Z')
_HEAD_RE = re.compile(rf'(?P<head>.*?)(?P<multiple>{_MULT})倍\Z',re.I)
_COUNT_RE = re.compile(rf'(?P<lot>{_LOT})?(?P<count>\d{{1,4}})注(?P<tail>.*)\Z',re.I)
_TOKENS = re.compile(rf'{_LOT}|{_PLAY}',re.I)
_TOTAL_RE = re.compile(r'(?:(?:合计|总计|合|共)[:：]?)(\d+(?:\.\d{1,2})?)(?:元|米|块)?\Z')
_ONLY_TOTAL = re.compile(r'(?:(?:合计|总计|合|共)[:：]?)(\d+(?:\.\d{1,2})?)(?:元|米|块)?\Z')
_ARITH = re.compile(r'(\d+(?:\.\d{1,2})?(?:\+\d+(?:\.\d{1,2})?)+)=(\d+(?:\.\d{1,2})?)\Z')
_NOTE = re.compile(r'[（(]方案(?:[一二三四五六七八九十\d]+)[)）]\Z')


def _compact(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value or ''))).replace('🈴','合').replace('㊗','注')


def _lottery(token):
    return '体彩' if token.lower() in ('体彩排列三','排列三','体彩','体','排') else '福彩'


def _parse_tokens(head, lottery, required_play=True):
    at = 0; plays = 0
    for m in _TOKENS.finditer(head):
        if m.start()!=at:
            return False
        token=m.group()
        if re.fullmatch(_PLAY,token,re.I):
            plays+=1
        elif _lottery(token)!=lottery:
            raise ValueError('彩种与当前选择的彩种冲突')
        at=m.end()
    return at==len(head) and plays==(1 if required_play else 0)


def _header(line, lottery):
    m=_HEAD_RE.fullmatch(_compact(line))
    if not m or not m.group('head'):
        return None
    if _parse_tokens(m.group('head'),lottery):
        return m.group('multiple')
    return None


def _codes(line, start):
    if not _LIST_RE.fullmatch(line.strip()):
        return None
    return [(m.group(),start+m.start(),start+m.end()) for m in _CODE_RE.finditer(line)]


def _parse_pricing(tail, lottery, allow_no_play=False):
    """Return (mode, value, declared), or None if not a strict pricing line."""
    m=_TOTAL_RE.search(tail)
    declared=Decimal(m.group(1)) if m else None
    base=tail[:m.start()] if m else tail
    # Optional lottery + direct prefix, followed by explicit multiple or per-ticket cash.
    pm=re.fullmatch(rf'(?P<head>(?:(?:{_LOT})?{_PLAY}|{_PLAY}(?:{_LOT})?))(?P<value>.*)',base,re.I)
    if pm:
        if not _parse_tokens(pm.group('head'),lottery):
            return None
        value=pm.group('value')
    elif allow_no_play:
        value=base
    else:
        return None
    mm=re.fullmatch(rf'(?P<value>{_MULT})倍',value,re.I)
    if mm:
        return ('multiple',mm.group('value'),declared)
    cash=re.fullmatch(r'(\d+(?:\.\d{1,2})?)(?:元|米|块)?',value)
    if cash and (declared is not None or re.search(r'[元米块]\Z',value)):
        return ('unit_cash',cash.group(1),declared)
    return None


def _summary(line,lottery):
    s=_compact(line)
    m=_COUNT_RE.fullmatch(s)
    if not m:
        return None
    if m.group('lot') and _lottery(m.group('lot'))!=lottery:
        raise ValueError('分组彩种与当前彩种冲突')
    tail=m.group('tail')
    if _NOTE.fullmatch(tail):
        return {'count':int(m.group('count')),'mode':None,'value':None,'declared':None}
    if not tail:
        return {'count':int(m.group('count')),'mode':None,'value':None,'declared':None}
    priced=_parse_pricing(tail,lottery)
    if priced is None:
        # Count-backed shorthand like '515注3倍' uses default 直选.
        priced=_parse_pricing(tail,lottery,allow_no_play=True)
    if priced is None:
        return None
    mode,value,claimed=priced
    return {'count':int(m.group('count')),'mode':mode,'value':value,'declared':claimed}


def _line_items(raw):
    return [(m.group(),m.start()) for m in re.finditer(r'[^\r\n]+',raw) if m.group().strip()]


def _num_mult(parser,raw,value,mode,unit):
    if mode=='multiple':
        return parser._multiplier(value)
    try:
        number=Decimal(str(value))
        if not number.is_finite() or number<=0 or number.as_tuple().exponent < -2:
            raise ValueError()
        multiple=number/unit
        if multiple.as_tuple().exponent < -2:
            raise ValueError()
        return parser._multiplier(format(multiple,'f'))
    except (ValueError,InvalidOperation,ArithmeticError) as exc:
        raise ValueError('单注金额无法准确换算为合法倍数') from exc


def _set_result(parser,bets,raw,warnings,claimed):
    parser.last_claimed=claimed
    parser.last_receipts=[]
    parser.last_warnings=warnings
    return bets,parser.normalize_text(raw),''


def _fail(parser,raw,error):
    parser.last_claimed=None
    parser.last_receipts=[]
    parser.last_warnings=[]
    return [],parser.normalize_text(raw),str(error)


def parse_local_blocks(parser,lottery,raw):
    if not isinstance(raw,str) or not raw.strip() or lottery not in ('福彩','体彩'):
        return None
    entries=_line_items(raw)
    if len(entries)<2:
        return _legacy_local_blocks(parser,lottery,raw)
    try:
        idx=0
        if re.fullmatch(_LOT,_compact(entries[0][0]),re.I):
            if _lottery(_compact(entries[0][0]))!=lottery:
                return _fail(parser,raw,'独立彩种标题与当前彩种冲突')
            idx=1
        if idx==len(entries):
            return _legacy_local_blocks(parser,lottery,raw)
        first,start=entries[idx]
        if _codes(first,start) is None and _header(first,lottery) is None:
            return _legacy_local_blocks(parser,lottery,raw)

        groups=[]
        pending=[]
        active_header=None
        final_total=None
        formula=None
        marked=False
        saw_pricing=False
        finished=False
        def add_group(codes,mode=None,value=None,claimed=None,count=None):
            if not codes:
                raise ValueError(f'第{len(groups)+1}组缺少三位号码')
            if count is not None and count!=len(codes):
                raise ValueError(f'第{len(groups)+1}组注数不一致：声明{count}注，实际{len(codes)}注')
            groups.append({'codes':list(codes),'mode':mode,'value':value,'claimed':claimed})

        for rawline,pos in entries[idx:]:
            s=_compact(rawline)
            # Full-order verification lines never create bets; only after at least one group.
            fm=_ARITH.fullmatch(s)
            tm=_ONLY_TOTAL.fullmatch(s)
            if fm or tm:
                if pending and active_header is not None:
                    add_group(pending,'multiple',active_header)
                    pending=[];active_header=None
                if pending or not groups or finished:
                    raise ValueError('整单合计之前仍有未定价的号码或重复合计')
                if fm:
                    formula=([Decimal(x) for x in fm.group(1).split('+')],Decimal(fm.group(2)))
                else:
                    final_total=Decimal(tm.group(1))
                finished=True
                marked=True
                continue
            if finished:
                raise ValueError('整单合计后不允许追加未核验的投注内容')
            nums=_codes(rawline,pos)
            if nums is not None:
                pending.extend(nums)
                continue
            hdr=_header(rawline,lottery)
            if hdr is not None:
                marked=True
                if pending:
                    if active_header is not None:
                        # A header-first group must end before a new header.
                        add_group(pending,'multiple',active_header)
                        pending=[]
                        active_header=hdr
                    else:
                        add_group(pending,'multiple',hdr)
                        pending=[]
                    saw_pricing=True
                elif groups and all(g['mode'] is None for g in groups):
                    for g in groups:
                        g['mode']='multiple';g['value']=hdr
                    saw_pricing=True
                elif groups and groups[-1]['mode'] is None and all(g['mode'] is not None for g in groups[:-1]):
                    # Only one unpriced group; end-of-message pricing belongs to that group.
                    groups[-1]['mode']='multiple';groups[-1]['value']=hdr
                    saw_pricing=True
                else:
                    if active_header is not None and not pending:
                        raise ValueError('前置玩法倍数后缺少三位号码')
                    active_header=hdr
                continue
            summary=_summary(rawline,lottery)
            if summary is not None:
                marked=True
                if active_header is not None:
                    raise ValueError('同组同时出现前置倍数和汇总指令，须核对')
                if not pending:
                    raise ValueError('汇总行前缺少号码')
                add_group(pending,summary['mode'],summary['value'],summary['declared'],summary['count'])
                pending=[]
                if summary['mode'] is not None:
                    saw_pricing=True
                continue
            # e.g. 福178注（方案二）\n直选1米合178: prior group needs the price.
            pricing=_parse_pricing(s,lottery)
            if pricing is not None and not pending and groups and groups[-1]['mode'] is None:
                mode,value,declared=pricing
                groups[-1].update(mode=mode,value=value,claimed=declared)
                marked=True;saw_pricing=True
                continue
            # Non-direct patterns are handled by the existing, separately
            # validated recognizers. Do not hijack their 组选/复式 syntax.
            if '注' in s and re.search(r'组选|组三|组六|复式|复试',s):
                return _legacy_local_blocks(parser,lottery,raw)
            raise ValueError(f'第{len(groups)+1}组存在无法核验的文字：{rawline.strip()[:70]}')
        if pending and active_header is not None:
            add_group(pending,'multiple',active_header)
            pending=[]
        if pending:
            if groups or marked:
                raise ValueError('末尾还有未关联玩法倍数的三位号码')
            return _legacy_local_blocks(parser,lottery,raw)
        if not marked or not groups:
            return _legacy_local_blocks(parser,lottery,raw)
        if any(g['mode'] is None for g in groups):
            raise ValueError('有号码组只有注数，没有对应玩法倍数或单注金额')
        if sum(len(g['codes']) for g in groups)>4000:
            raise ValueError('大底展开超过4000注，请核对是否重复粘贴')

        bets=[];warnings=[];total=Decimal('0')
        group_amounts=[];declared_all=True
        for n,g in enumerate(groups,1):
            code=g['codes'][0][0]
            sample=parser._from_kind(lottery,raw,code,'直选',1)
            unit=sum(Decimal(str(b.principal)) for b in sample)
            if unit<=0:
                raise ValueError('直选单注成本配置无效')
            mult=_num_mult(parser,raw,g['value'],g['mode'],unit)
            amount=Decimal('0')
            for code,lo,hi in g['codes']:
                new=parser._from_kind(lottery,raw,code,'直选',mult)
                for b in new:
                    b.source_span={'start':lo,'end':hi,'text':raw[lo:hi],
                                   'granularity':'ticket','reference':'current_text','local_group':n}
                    if g['mode']=='unit_cash':
                        b.entered_amount_yuan=float(Decimal(str(g['value'])))
                    amount+=Decimal(str(b.principal))
                bets.extend(new)
            expected=g['claimed']
            if expected is not None and amount!=expected:
                if g['mode']=='multiple':
                    warnings.append(f'第{n}组自报金额{expected:g}元有误，按明确倍数计算{amount:g}元')
                    declared_all=False
                else:
                    raise ValueError(f'第{n}组金额不符：声明{expected:g}元，单注金额计算{amount:g}元')
            if expected is None:
                declared_all=False
            if len({x[0] for x in g['codes']})!=len(g['codes']):
                warnings.append(f'第{n}组存在重复号码，已按重复购买保留')
            group_amounts.append(amount)
            total+=amount
        if formula is not None:
            operands,result=formula
            if sum(operands)!=result or result!=total or operands!=group_amounts:
                warnings.append(f'末尾人工算式核对不一致：计算应收{total:g}元；号码及明确倍数保持不变')
        if final_total is not None and final_total!=total:
            warnings.append(f'整单声明{final_total:g}元，实际应收{total:g}元；未改写倍数')
        claimed=float(total) if declared_all and not warnings else None
        return _set_result(parser,bets,raw,warnings,claimed)
    except (ValueError,KeyError,ArithmeticError,InvalidOperation) as exc:
        return _fail(parser,raw,exc)
