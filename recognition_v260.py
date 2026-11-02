"""V2.6.0 deterministic, scope-aware local recognizer.

This layer is deliberately *conservative*: complete known formats return priced
bets; anything with unconsumed words/figures falls through to previous parsers.
Uses BetParser pricing (_make/_from_kind), never invents odds or drops repeats.
"""
from __future__ import annotations
from decimal import Decimal
import re
import unicodedata
from recognition_v258 import (parse_unambiguous as previous,
    _make, _mult, _finish, _strip_lot, _extract_total, dec, _CNUM,
    _MONEY, RecognitionError)

_CODES3=r'\d{3}(?:[\s,，、.。\-]+\d{3})*'
_NUMBER_RE=re.compile(r'(?<!\d)\d{3}(?!\d)')
_CN={'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}

def _value(text):
    s=text.strip()
    if s in _CN:return Decimal(_CN[s])
    if s=='半':return Decimal('0.5')
    return dec(s)

def _codes(text,*,width=3):
    if not re.fullmatch(r'\d{'+str(width)+r'}(?:[\s,，、.。\-]+\d{'+str(width)+r'})*',text.strip()):
        return None
    nums=re.findall(r'\d{'+str(width)+r'}',text)
    return nums or None

def _price(bets):return sum((dec(x.principal) for x in bets),Decimal('0'))

def _checked(p,lot,raw,bets,total,*,must=False):
    if total is not None and _price(bets)!=dec(total):
        # These formats explicitly rely on a receipt for disambiguation: never
        # silently post a subset or adjust an unambiguous multiple.
        raise RecognitionError('分组金额或整条合计与原文明示下注不一致，须人工核对')
    if must and total is None:return None
    return _finish(p,lot,raw,bets,total,require_total=must)

def _two_lot_header(raw):
    m=re.match(r'^\s*(福体|体福)\s*',raw)
    return m.end() if m else None

def _shared_one_each(p,lot,raw):
    cut=_two_lot_header(raw)
    dual=cut is not None
    body=raw[cut:] if dual else re.sub(r'^\s*(?:福彩|体彩|福|体|排|3[Dd])\s*(?=一单一组|一直一组)', '', _strip_lot(raw,lot))
    body,total=_extract_total(body)
    # Both directions: 一单一组325 324..., or 325 324...一单一组.
    body=body.strip(' ，、。;；\n')
    front=re.fullmatch(r'(?:一单一组|一直一组)\s*(?P<codes>'+_CODES3+r')',body)
    back=re.fullmatch(r'(?P<codes>'+_CODES3+r')\s*(?:一单一组|一直一组)',body)
    m=front or back
    if not m:return None
    codes=_codes(m['codes'])
    if not codes:return None
    bets=[]
    for code in codes:
        bets.extend(_make(p,lot,raw,code,'直选',1))
        bets.extend(_make(p,lot,raw,code,'组选',1))
    expected=_price(bets)*(2 if dual else 1)
    if total is not None and expected!=total:
        raise RecognitionError('福体共享总金额与双彩种实际投注金额不一致' if dual else '合计与每号一单一组金额不一致')
    return _finish(p,lot,raw,bets, _price(bets) if dual and total is not None else total)

def _plural_each_direct_group(p,lot,raw,body,total):
    # Examples: 037 055各十单十组80 ; 037、055各10直10组80米.
    m=re.fullmatch(r'(?P<codes>'+_CODES3+r')\s*各\s*(?P<direct>'+_CNUM+r')\s*(?:单|直)\s*(?P<group>'+_CNUM+r')\s*(?:组|组选)\s*(?P<receipt>'+_MONEY+r')?\s*(?:元|米|块)?',body)
    if not m:return None
    codes=_codes(m['codes'])
    if not codes:return None
    bets=[]
    for code in codes:
        bets+=_make(p,lot,raw,code,'直选',_mult(p,str(_value(m['direct']))))
        bets+=_make(p,lot,raw,code,'组选',_mult(p,str(_value(m['group']))))
    receipt=dec(m['receipt']) if m['receipt'] else total
    if m['receipt'] and total is not None and dec(m['receipt'])!=total:
        raise RecognitionError('行小计与整条合计不一致')
    # A bare number requires exactly corroborating arithmetic.
    return _checked(p,lot,raw,bets,receipt,must=bool(m['receipt']))

def _bracket_receipts(p,lot,raw,body,total):
    lines=[x.strip() for x in body.splitlines() if x.strip()]
    if len(lines)!=2:return None
    direct=re.fullmatch(r'(?P<code>\d{3})\s*(?:直选|直|单)\s*(?P<mul>'+_CNUM+r')\s*(?:倍)?\s*\(\s*(?P<receipt>'+_MONEY+r')\s*\)',lines[0])
    pool=re.fullmatch(r'(?P<digits>\d{4,9})\s*(?:组六|组6|组六|组3|组三)\s*(?:打)?\s*(?P<mul>'+_CNUM+r')\s*(?:倍)?\s*\(\s*(?P<receipt>'+_MONEY+r')\s*\)',lines[1])
    if not direct or not pool:return None
    play='组三' if re.search(r'组(?:3|三)',lines[1]) else '组六'
    first=_make(p,lot,raw,direct['code'],'直选',_mult(p,str(_value(direct['mul']))))
    second=_make(p,lot,raw,pool['digits'],play,_mult(p,str(_value(pool['mul']))))
    if _price(first)!=dec(direct['receipt']) or _price(second)!=dec(pool['receipt']):
        raise RecognitionError('括号标注金额与前面的投注倍数不符')
    return _checked(p,lot,raw,first+second,total,must=True)

def _simple_lines_with_total(p,lot,raw,body,total):
    # Independent one-direct-one-group lines, plus play-first '组388'.
    lines=[x.strip() for x in body.splitlines() if x.strip()]
    if len(lines)<2:return None
    bets=[]
    for line in lines:
        m=re.fullmatch(r'(\d{3})\s*(?:一单一组|一直一组|1单一组|1单1组)',line)
        if m:
            code=m[1]
            bets+=_make(p,lot,raw,code,'直选',1)
            bets+=_make(p,lot,raw,code,'组选',1)
            continue
        m=re.fullmatch(r'(?:组|组选)\s*(\d{3})',line)
        if m:
            bets+=_make(p,lot,raw,m[1],'组选',1)
            continue
        return None
    return _checked(p,lot,raw,bets,total)

def _position_then_lines(p,lot,raw,body,total):
    lines=[x.strip() for x in body.splitlines() if x.strip()]
    if len(lines)<5:return None
    first=re.fullmatch(r'(?:2|二|两)\s*码\s*定位',lines[0])
    if not first:return None
    matches=[]
    for line in lines[1:3]:
        m=re.fullmatch(r'(百|十|个)(?:位)?\s*[:：]?\s*(\d{1,10})',line)
        if not m:return None
        if len(set(m[2]))!=len(m[2]):return None
        matches.append((m[1],m[2]))
    if matches[0][0]==matches[1][0]:return None
    qty=re.fullmatch(r'(?P<mul>'+_CNUM+r')\s*倍\s*(?P<receipt>'+_MONEY+r')\s*(?:元|米|块)',lines[3])
    if not qty:return None
    selection='|'.join(k+':'+v for k,v in matches)
    fixed=_make(p,lot,raw,selection,'两码定位',_mult(p,str(_value(qty['mul']))))
    if _price(fixed)!=dec(qty['receipt']):raise RecognitionError('两码定位金额与组合及倍数不符')
    rest=[]
    for line in lines[4:]:
        m=re.fullmatch(r'(\d{3})\s*(?:一单一组|一直一组|1单一组|1单1组)',line)
        if not m:return None
        rest+=_make(p,lot,raw,m[1],'直选',1)
        rest+=_make(p,lot,raw,m[1],'组选',1)
    return _checked(p,lot,raw,fixed+rest,total,must=True)

def _batch_arithmetic(p,lot,raw,body,total):
    # Groups can carry a check expression '12*6=72' or a bare subtotal '32'.
    chunks=[x.strip() for x in re.split(r'\n\s*\n+',body) if x.strip()]
    if len(chunks)<2:return None
    result=[]
    for chunk in chunks:
        m=re.fullmatch(r'(?P<codes>'+_CODES3+r')\s*(?:组选|组)\s*(?:各\s*)?(?P<mul>'+_CNUM+r')\s*倍\s*[。,.，\s]*(?P<check>(?:\d+\s*[*×x]\s*\d+\s*=\s*'+_MONEY+r')|(?:'+_MONEY+r'))\s*',chunk)
        if not m:return None
        codes=_codes(m['codes'])
        if not codes:return None
        bets=[]
        for code in codes:bets+=_make(p,lot,raw,code,'组选',_mult(p,str(_value(m['mul']))))
        receipt=m['check']
        if '=' in receipt:
            mm=re.fullmatch(r'(\d+)\s*[*×x]\s*(\d+)\s*=\s*('+_MONEY+r')',receipt)
            if not mm or int(mm[1])!=len(codes) or dec(mm[2])!=2*_value(m['mul']):
                raise RecognitionError('人工算式数量与组选笔数或单笔金额不符')
            amount=dec(mm[3])
        else:amount=dec(receipt)
        if _price(bets)!=amount:raise RecognitionError('组选小计与批量号码金额不一致')
        result+=bets
    return _checked(p,lot,raw,result,total,must=True)

def _punctuation_batches(p,lot,raw,body,total):
    # Header-before-numbers; delimiters are not independent bet boundaries.
    pieces=[x.strip() for x in re.split(r'[。；;\n]+',body) if x.strip()]
    if len(pieces)<6:return None
    bets=[];pos=0;sections=0
    while pos<len(pieces):
        head=pieces[pos]
        direct=re.fullmatch(r'(?:直选|直|单)\s*('+_CNUM+r')\s*倍',head)
        pool=re.fullmatch(r'(?:组六|组6|组六)\s*(?:([四五六七八九]|[4-9])\s*码)?\s*(\d{4,9})?',head)
        if direct:
            pos+=1;codes=[]
            while pos<len(pieces) and re.fullmatch(r'\d{3}',pieces[pos]):
                codes.append(pieces[pos]);pos+=1
            if not codes:return None
            group=[b for c in codes for b in _make(p,lot,raw,c,'直选',_mult(p,str(_value(direct[1]))))]
            receipt=re.fullmatch(r'(?:总|合计|小计)\s*('+_MONEY+r')\s*(?:元|米|块)?',pieces[pos]) if pos<len(pieces) else None
            if receipt:
                if _price(group)!=dec(receipt[1]):raise RecognitionError('直选批量小计错误')
                pos+=1
            bets+=group;sections+=1;continue
        if pool and pool[2]:
            required=pool[1]
            named_width={'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
            width=named_width[required] if required in named_width else int(required) if required else len(pool[2])
            if len(pool[2])!=width:return None
            codes=[pool[2]];pos+=1
            while pos<len(pieces) and re.fullmatch(r'\d{'+str(width)+r'}',pieces[pos]):
                codes.append(pieces[pos]);pos+=1
            # Monetary amounts apply to each N码组, not a shared multiplier.
            if pos>=len(pieces):return None
            amountm=re.fullmatch(r'(?:各\s*)?('+_MONEY+r')\s*(?:元|米|块)?',pieces[pos])
            if not amountm:return None
            amount=dec(amountm[1]);pos+=1
            group=[]
            for code in codes:
                base=_price(_make(p,lot,raw,code,'组六',1))
                if base<=0:return None
                multiplier=amount/base
                group+=_make(p,lot,raw,code,'组六',_mult(p,str(multiplier)))
            receipt=re.fullmatch(r'(?:总|合计|小计)\s*('+_MONEY+r')\s*(?:元|米|块)?',pieces[pos]) if pos<len(pieces) else None
            if receipt:
                if _price(group)!=dec(receipt[1]):raise RecognitionError('组六批量小计错误')
                pos+=1
            bets+=group;sections+=1;continue
        return None
    if sections<2:return None
    return _checked(p,lot,raw,bets,total,must=True)

def parse_v260(p,lottery,text):
    if not isinstance(text,str) or not text.strip():return None
    raw=unicodedata.normalize('NFKC',text).replace('\r\n','\n').replace('\r','\n')
    if re.search(r'撤单|取消|更正|改成|不买|修改',raw):return None
    # The standalone recognizer does not silently reinterpret cross-lottery
    # messages. The routing layer owns that scope.
    try:
        shared=_shared_one_each(p,lottery,raw)
        if shared is not None:return shared
        if _two_lot_header(raw) is not None:return previous(p,lottery,raw)
        body,total=_extract_total(raw)
        body=_strip_lot(body,lottery).strip(' ：:，,。\t')
        for rule in (_plural_each_direct_group,_bracket_receipts,_simple_lines_with_total,
                     _position_then_lines,_batch_arithmetic,_punctuation_batches):
            result=rule(p,lottery,raw,body,total)
            if result is not None:return result
    except (RecognitionError,ValueError,KeyError,ArithmeticError) as exc:
        return [],p.normalize_text(raw),str(exc)
    return previous(p,lottery,raw)
