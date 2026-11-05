# -*- coding: utf-8 -*-
"""V2.7.6: parse editable hedge text into a strictly priced, duplicate-safe plan.

No database/network/Tk. Input mistakes raise ValueError instead of silently
partially applying an actual hedging purchase.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from collections import defaultdict
import re
import unicodedata
from models import RiskRow, cents

_LOT=r'(?:体彩排列三|福彩3[Dd]|排列三|福彩|体彩|3[Dd]|福|体|排)'
_CN=r'[零〇一二两三四五六七八九十百千万]+'
_MULT=r'(?:\d+(?:\.\d{1,2})?|半|'+_CN+r')'
_TOKEN_HEAD=re.compile(rf'^(?P<head>(?:(?:{_LOT})?(?:直选|单选|直|单)|(?:直选|单选|直|单)(?:{_LOT})?))(?P<mult>{_MULT})倍$',re.I)
_CODE_LINE=re.compile(r'[0-9]{3}(?:[\s,，、]+[0-9]{3})*\Z')
_CODE=re.compile(r'(?<![0-9])[0-9]{3}(?![0-9])')
_SINGLE=re.compile(rf'(?:(?P<lot>{_LOT}))?(?P<code>\d{{3}})(?:直选|单选|直|单)(?P<mult>{_MULT})倍\Z',re.I)
_TOTAL=re.compile(r'(?:(?:合计|总计|合|共)[:：]?)(\d+(?:\.\d{1,2})?)(?:元|米|块)?\Z')


def _norm(line):
    return re.sub(r'\s+','',unicodedata.normalize('NFKC',line or ''))


def _lot(value):
    if value and value.lower() in ('体彩排列三','排列三','体彩','体','排'):
        return '体彩'
    return '福彩'


def _mult(s):
    from parser import chinese_int
    if s=='半':return 0.5
    if s and all(not ch.isascii() or not ch.isdigit() for ch in s):
        value=Decimal(chinese_int(s))
    else:
        try:value=Decimal(s)
        except (InvalidOperation,TypeError):raise ValueError('倍数不是合法数字') from None
    if not value.is_finite() or value!=value.to_integral_value() and value.as_tuple().exponent<-2 or value<=0 or value>1_000_000:
        raise ValueError('倍数必须大于0、最多两位小数且不超过100万')
    return int(value) if value==int(value) else float(value)


def parse_hedge_text(text, lottery='福彩', unit_cost=2):
    if not isinstance(text,str) or len(text)>80_000:
        raise ValueError('方案不是有效文本或内容过长')
    if lottery not in ('福彩','体彩'):
        raise ValueError('彩种无效')
    # Validate non-negative unit cost, but zero is accepted for site-specified cost.
    unit_cents=cents(unit_cost)
    if unit_cents<=0:
        raise ValueError('外部补单每倍成本必须大于0')
    accum=defaultdict(lambda:Decimal(0))
    active=None
    saw_group=False
    ignored_total=False
    lines=[(i+1,s.strip()) for i,s in enumerate(text.replace('\r\n','\n').replace('\r','\n').split('\n')) if s.strip()]
    if not lines:
        raise ValueError('抛单方案不能为空；需要清空时请恢复系统方案')
    for n,rawline in lines:
        compact=_norm(rawline)
        if _TOTAL.fullmatch(compact):
            if ignored_total or not accum:
                raise ValueError(f'第{n}行合计说明没有对应的有效号码')
            ignored_total=True
            continue
        if ignored_total:
            raise ValueError(f'第{n}行出现在合计说明之后，请检查')
        if re.fullmatch(_LOT,compact,re.I):
            if _lot(compact)!=lottery:
                raise ValueError(f'第{n}行彩种冲突')
            continue
        single=_SINGLE.fullmatch(compact)
        if single:
            if single.group('lot') and _lot(single.group('lot'))!=lottery:
                raise ValueError(f'第{n}行彩种冲突')
            amount=_mult(single.group('mult'))
            accum[single.group('code')]+=Decimal(str(amount))
            saw_group=True
            continue
        header=_TOKEN_HEAD.fullmatch(compact)
        if header:
            prefix=header.group('head')
            lot_tokens=re.findall(_LOT,prefix,re.I)
            if any(_lot(s)!=lottery for s in lot_tokens):
                raise ValueError(f'第{n}行彩种冲突')
            if active is not None and not saw_group:
                raise ValueError(f'第{n}行之前的玩法倍数下没有号码')
            active=Decimal(str(_mult(header.group('mult'))))
            saw_group=False
            continue
        if _CODE_LINE.fullmatch(rawline):
            if active is None:
                raise ValueError(f'第{n}行三位号码没有对应的「直选N倍」')
            codes=_CODE.findall(rawline)
            for code in codes:
                accum[code]+=active
            saw_group=True
            continue
        raise ValueError(f'第{n}行无法识别：{rawline[:45]}')
    if active is not None and not saw_group:
        raise ValueError('最后一个直选倍数后缺少号码')
    if not accum:
        raise ValueError('方案没有明确的三位直选号码')
    if len(accum)>1000:
        raise ValueError('不同三位号码数量不能超过1000')
    rows=[]
    for code,value in sorted(accum.items()):
        if value<=0 or value>1_000_000 or value!=value.to_integral_value():
            raise ValueError(f'{code} 的累计倍数必须是1～100万的整数')
        rows.append(RiskRow(code,0.,0.,int(value)))
    cost_cents=sum(int(v)*unit_cents for v in accum.values())
    if cost_cents>10**13:
        raise ValueError('抛单成本过大，请核对倍数')
    return rows, cost_cents


def update_display_total(text, cost_cents):
    """Update an optional generated final total after manual editing, not bets."""
    lines=text.splitlines()
    if not lines:
        return text
    if _TOTAL.fullmatch(_norm(lines[-1])):
        amount=f'{Decimal(cost_cents)/100:.2f}'.rstrip('0').rstrip('.')
        return '\n'.join(lines[:-1]+['合计：'+(amount or '0')+'元'])
    return text
