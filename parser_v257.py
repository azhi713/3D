"""Additive V2.5.7 recognizers for unambiguous real-world buy formats.

This deliberately does not replace the main parser or redefine the payout table.
It produces ordinary Bet objects via the existing _from_kind/_make rule paths.
"""
from __future__ import annotations
import re
import unicodedata
from decimal import Decimal

from models import cents

_AMOUNT=r'\d+(?:\.\d{1,2})?'
_NUM=r'\d{3}'
_SEPARATOR=r'[\s.。,，、:\-／/]+'
_MULT=r'(?:半|\d+(?:\.\d{1,2})?|[一二两三四五六七八九十]+)'
_LOT=r'(?:福彩3[Dd]?|福彩|3[Dd]|体|体彩|排列三|排|福)'


def _lottery(marker):
    return '福彩' if marker and marker.lower() in ('福','福彩','3d','福彩3d') else '体彩'


def _head(text,lottery):
    m=re.match(r'^\s*('+_LOT+r')\s*',text,re.I)
    if m:
        if _lottery(m[1])!=lottery:return None
        return text[m.end():]
    return text


def _close(p,lottery,text,bets,declared):
    if not bets:return None
    p.last_claimed=float(declared) if declared is not None else None
    p.last_warnings=[]
    p.last_receipts=[]
    total=sum(cents(b.principal) for b in bets)
    if declared is not None and cents(declared)!=total:
        p.last_warnings.append(f'自报金额{declared}元与应收{Decimal(total)/100}元不一致')
    for bet in bets:
        bet.source_span={'start':0,'end':len(text),'text':text,'granularity':'message'}
    return bets,p.normalize_text(text),''


def parse_common_v257(p,lottery,text):
    """Return a normal parser tuple or None, never guess an ambiguous amount."""
    if not isinstance(text,str) or not text.strip():return None
    raw=unicodedata.normalize('NFKC',text).strip()
    head=_head(raw,lottery)
    if head is None:return None
    # A. A shared number list purchases both direct and group, e.g.:
    #    福841.341，各1直1组。8米！
    #    420.146.114.712各直4组1(40米)
    clean=head.rstrip('!！。 .')
    pattern=(r'(?P<codes>'+_NUM+r'(?:'+_SEPARATOR+_NUM+r')+)\s*'
             r'[,，、.。\s]*各?\s*(?:(?P<d1>'+_MULT+r')\s*(?:单|直|直选)\s*(?P<g1>'+_MULT+r')\s*(?:组|组选)'
             r'|(?:直|单|直选)\s*(?P<d2>'+_MULT+r')\s*(?:组|组选)\s*(?P<g2>'+_MULT+r'))'
             r'\s*(?:[。！!,，\s]*\(?\s*(?P<amount>'+_AMOUNT+r')\s*[米元块]\s*\)?)?\s*$')
    m=re.fullmatch(pattern,clean,re.I)
    if m:
        codes=re.findall(r'(?<!\d)\d{3}(?!\d)',m['codes'])
        if len(codes)<2:return None
        dm=p._multiplier(m['d1'] or m['d2'])
        gm=p._multiplier(m['g1'] or m['g2'])
        bets=[]
        for code in codes:
            bets.extend(p._from_kind(lottery,text,code,'直选',dm))
            bets.extend(p._from_kind(lottery,text,code,'组选',gm))
        return _close(p,lottery,text,bets,m['amount'])
    # B. A single digit pool purchases two original (not permutation) plays.
    #    0357组六100米组三100米福合计200
    #    Do NOT reinterpret an explicit 100米 as 100 multiples.
    group=(r'(?P<pool>\d{3,9})\s*(?P<play1>组三|组六)\s*'
           r'(?P<value1>'+_AMOUNT+r')\s*(?P<unit1>[米元块倍])\s*'
           r'(?P<play2>组三|组六)\s*(?P<value2>'+_AMOUNT+r')\s*(?P<unit2>[米元块倍])'
           r'\s*(?:(?:福|体|福彩|体彩)?\s*(?:合计|总计)\s*(?P<total>'+_AMOUNT+r')\s*[米元块]?)?\s*$')
    m=re.fullmatch(group,clean,re.I)
    if m:
        if m['play1']==m['play2']:return None
        bets=[]
        for key,value,unit in ((m['play1'],m['value1'],m['unit1']),
                               (m['play2'],m['value2'],m['unit2'])):
            unitbet=p._from_kind(lottery,text,m['pool'],key,1)
            base=sum(Decimal(str(b.principal)) for b in unitbet)
            mult=Decimal(value) if unit=='倍' else Decimal(value)/base
            for bet in p._from_kind(lottery,text,m['pool'],key,p._multiplier(format(mult,'f'))):
                if unit!='倍':bet.entered_amount_yuan=float(Decimal(value))
                bets.append(bet)
        return _close(p,lottery,text,bets,m['total'])
    return None
