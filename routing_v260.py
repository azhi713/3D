"""Message-level shared total ownership; no money is attached to the last color.

Returns None for ordinary messages so existing routing remains authoritative.
"""
import re
import unicodedata
from decimal import Decimal

_LOT=re.compile(r'(?:(?<![\w])|(?<=倍)|(?<=元)|(?<=米)|(?<=块))(?:福|体|福彩|体彩|3[Dd]|排)(?=\s*(?:[:：,，、.]?\s*)?(?:\d|百|十|个|直|单|组|定|独|跨|和))',re.I)
_TOTAL=re.compile(r'(?:(?:合计|总计|合|总)\s*[:：]?\s*)(\d+(?:\.\d{1,2})?)\s*[元米块]?\s*$',re.I)

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
