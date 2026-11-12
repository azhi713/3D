"""Message-level shared total ownership; no money is attached to the last color.

Returns None for ordinary messages so existing routing remains authoritative.
"""
import re
import unicodedata
from decimal import Decimal

_LOT=re.compile(r'(?:(?<![\w])|(?<=倍)|(?<=元)|(?<=米)|(?<=块))(?:福|体|福彩|体彩|3[Dd]|排)(?=\s*(?:[:：,，、.]?\s*)?(?:\d|百|十|个|直|单|组|定|独|跨|和))',re.I)
_TOTAL=re.compile(r'(?:(?:合计|总计|合|总)\s*[:：]?\s*)(\d+(?:\.\d{1,2})?)\s*[元米块]?\s*$',re.I)
_DIRECT_POOL=re.compile(
    r'(?P<lot>福彩|体彩|福|体|3[Dd]|排)\s*[:：]?\s*'
    r'(?P<codes>\d{3}(?:[ \t,，、.。/\-]+\d{3})*)\s*$',re.I)
_DIRECT_SHARED=re.compile(
    r'(?:直选|单选|直|单)\s*各\s*'
    r'(?:半|\d+(?:\.\d{1,2})?|[一二两三四五六七八九十]+)\s*倍\s*$',re.I)


def _shared_directive_for_two_colors(body):
    """One standalone direct-multiple line explicitly applies to both pools.

    Scope this narrowly: two complete colour-labelled 3-digit lists, no other
    play/receipt instructions, followed by one shared '直选各N倍' directive.
    The terminal receipt is checked separately across both parsed records.
    """
    lines=[line.strip() for line in body.splitlines() if line.strip()]
    if len(lines)!=3 or not _DIRECT_SHARED.fullmatch(lines[2]):
        return None
    first=_DIRECT_POOL.fullmatch(lines[0])
    second=_DIRECT_POOL.fullmatch(lines[1])
    if first is None or second is None:
        return None
    def color(token):
        return '福彩' if token.lower() in ('福','福彩','3d') else '体彩'
    a,b=color(first['lot']),color(second['lot'])
    if a==b:
        return None
    return {a:lines[0]+'\n'+lines[2],b:lines[1]+'\n'+lines[2]}

def split_shared_total(text,default_lottery):
    if not isinstance(text,str):return None
    s=unicodedata.normalize('NFKC',text).strip()
    if s.startswith(('福体','体福')):return None
    tail=_TOTAL.search(s)
    if not tail:return None
    # If a preceding color or subtotal explicitly owns the receipt, never
    # reinterpret it as a shared amount. Keep existing per-color routing.
    before=s[:tail.start()]
    if re.search(r'(?:合计|总计|福家|体家|小计)\s*[:：]?\s*\d+(?:\.\d+)?',before):return None
    if re.search(r'(?:福彩|体彩|福|体)\s*$',before) and not re.search(r'(?:福体|体福)\s*$',before):return None
    markers=list(_LOT.finditer(before))
    colors=['福彩' if m.group().lower() in ('福','福彩','3d') else '体彩' for m in markers]
    if len(markers)<2 or len(set(colors))!=2:return None
    segments={'福彩':[],'体彩':[]}
    text_body=s[:tail.start()].rstrip(' \n，,。;；')
    paired=_shared_directive_for_two_colors(text_body)
    if paired is not None:
        return paired,Decimal(tail[1])
    prefix=text_body[:markers[0].start()].strip(' \n，,。;；')
    if prefix:segments[default_lottery].append(prefix)
    for n,m in enumerate(markers):
        stop=markers[n+1].start() if n+1<len(markers) else len(text_body)
        value=text_body[m.start():stop].strip(' \n，,。;；')
        if value:segments[colors[n]].append(value)
    return ({k:'\n'.join(v) for k,v in segments.items() if v},Decimal(tail[1]))

def shared_total(text,default_lottery):
    result=split_shared_total(text,default_lottery)
    return result[1] if result is not None else None
