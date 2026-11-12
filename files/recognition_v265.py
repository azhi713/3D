# -*- coding: utf-8 -*-
"""V2.6.5: conservative, fully source-backed local wager families.

Parse complete clauses only.  Original numbers (including repeated codes) are
preserved.  Amounts are calculated by the configured parser, not by AI.
"""
import re
import unicodedata
from decimal import Decimal

_CN = r'(?:半|\d+(?:\.\d{1,2})?|[一二两三四五六七八九十]+)'
_LOT = r'(?:福|福彩|3[Dd]|体彩|体|排列三|排)'
_NUMBER_LIST = re.compile(r'^\d{3}(?:[\s,，、。;；/\-]+\d{3})*$', re.S)
_CODE = re.compile(r'(?<!\d)\d{3}(?!\d)')
_BATCH_HEADER = re.compile(r'(?m)^\s*(?:共|计)\s*(\d+)\s*注\s*('+_CN+r')\s*倍\s*$')
_LAST_TOTAL = re.compile(r'(?:合计|总计|合)\s*[:：]?\s*(\d+(?:\.\d{1,2})?)\s*[元米块]?\s*$')
_EXTRA = re.compile(r'\s*(\d{3}(?:[\s,、，/\-]+\d{3})*)\s*(直选|直|单|组选|组|组三|组六)\s*(?:各)?\s*('+_CN+r')\s*倍\s*')
_POS_BLOCK = re.compile(r'\s*(?:(?:福彩|福|体彩|体|3[Dd])\s*)?(?:定位\s*)?百\s*[:：]?\s*(\d{1,10})\s+十\s*[:：]?\s*(\d{1,10})\s+个\s*[:：]?\s*(\d{1,10})\s+(?:打|买|投)?\s*('+_CN+r')\s*倍\s*(?:共)?\s*(\d+(?:\.\d{1,2})?)\s*[元米块]?\s*')
_POS_TRAILING = re.compile(
    rf'^\s*(?:(?P<head>{_LOT})\s*)?百(?:位)?\s*[:：]?\s*(?P<hundred>[0-9](?:[ \t,，、]*[0-9]){{0,9}})\s+'
    rf'十(?:位)?\s*[:：]?\s*(?P<ten>[0-9](?:[ \t,，、]*[0-9]){{0,9}})\s+'
    rf'个(?:位)?\s*[:：]?\s*(?P<one>[0-9](?:[ \t,，、]*[0-9]){{0,9}})\s+'
    rf'(?:(?P<tail>{_LOT})\s*)?(?:直选)?(?:三码)?定位\s*(?P<multiple>{_CN})\s*倍'
    rf'\s*(?:(?:🈴|合计|总计|合|共)\s*[:：]?\s*(?P<total>\d+(?:\.\d{{1,2}})?)\s*[元米块]?)?\s*$',
    re.I)
_SHORT = re.compile(r'^\s*(?:福|福彩)?\s*(\d{3})\s*('+_CN+r')\s*(?:单|直)\s*('+_CN+r')\s*组\s*(?:福)?\s*(\d+(?:\.\d{1,2})?)\s*[元米块]?\s*$')
_TWO_SHORT = re.compile(r'^\s*(?:福|福彩)?\s*(\d{3})\s*('+_CN+r')\s*(?:单|直)\s*('+_CN+r')\s*组\s*[,，。;；\s]+\s*(\d{3})\s*('+_CN+r')\s*组\s*[,，。;；\s]+\s*(\d+(?:\.\d{1,2})?)\s*[元米块]?\s*$')


def _num(s):
    if s == '半':return Decimal('0.5')
    if re.fullmatch(r'\d+(?:\.\d{1,2})?',s):return Decimal(s)
    digits={'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}
    if s in digits:return Decimal(digits[s])
    if s.startswith('十') and len(s)==2 and s[1] in digits:return Decimal(10+digits[s[1]])
    if s.endswith('十') and len(s)==2 and s[0] in digits:return Decimal(digits[s[0]]*10)
    if len(s)==3 and s[1]=='十' and s[0] in digits and s[2] in digits:return Decimal(digits[s[0]]*10+digits[s[2]])
    raise ValueError('不支持的中文倍数')


def _due(bets):
    return sum((Decimal(str(x.principal)) for x in bets), Decimal('0'))


def _finish(p,lot,original,bets,declared=None,receipts=None):
    if not bets:return None
    p.last_claimed = float(declared) if declared is not None else None
    p.last_receipts = receipts or []
    p.last_warnings = []
    return bets,p.normalize_text(original),''


def _make(p,lot,original,code,kind,mult):
    return p._from_kind(lot,original,code,kind,p._multiplier(str(_num(mult))))


def _big_list(p,lot,original,text):
    m=_BATCH_HEADER.search(text)
    if not m:return None
    prefix=text[:m.start()].strip()
    # Explicit lottery prefix may appear at the very beginning of the list.
    prefix=re.sub(r'^\s*'+_LOT+r'\s*[:：,，。]?\s*(?=\d{3})','',prefix,count=1,flags=re.I)
    if not _NUMBER_LIST.fullmatch(prefix):return None
    codes=_CODE.findall(prefix)
    if len(codes)!=int(m[1]):
        p.last_claimed=None;p.last_receipts=[];p.last_warnings=[]
        return [],p.normalize_text(original),'声明注数与实际三位选号数量不一致；禁止自动计入'
    tail=text[m.end():].strip()
    total=None
    mt=_LAST_TOTAL.search(tail)
    if mt:
        total=Decimal(mt[1]);tail=tail[:mt.start()].strip(' \n,，。;；')
    # Every additional clause must be consumed; never silently ignore text.
    extras=[]
    if tail:
        for line in re.split(r'[\n。；;]+',tail):
            line=line.strip()
            if not line:continue
            x=_EXTRA.fullmatch(line)
            if not x:return None
            extras.append(x)
    try:
        bets=[]
        for code in codes:bets.extend(_make(p,lot,original,code,'直选',m[2]))
        for x in extras:
            for code in _CODE.findall(x[1]):
                bets.extend(_make(p,lot,original,code,x[2],x[3]))
    except (KeyError,ValueError,ArithmeticError):return None
    return _finish(p,lot,original,bets,total)


def _positions(p,lot,original,text):
    # Three separately labelled positions followed by one shared multiplier:
    # 百位1358\n十位2468\n个位1357\n福定位半倍🈴64米.
    # This is one 三码定位 purchase, not three unrelated digit pools.
    trail=_POS_TRAILING.fullmatch(text)
    if trail:
        from source_context_v281 import lottery_of
        # Spaces/commas separate digits within one explicitly labelled
        # position. Preserve duplicates: they are repeated purchases.
        position_digits=[re.sub(r'[^0-9]','',trail[name])
                         for name in ('hundred','ten','one')]
        selection='|'.join(label+':'+digits for label,digits in zip('百十个',position_digits))
        claim=Decimal(trail['total']) if trail['total'] is not None else None
        p.last_claimed=float(claim) if claim is not None else None
        p.last_receipts=[]
        p.last_warnings=[]
        try:
            for name in ('head','tail'):
                if trail[name] is not None and lottery_of(trail[name])!=lot:
                    raise ValueError('定位彩种与接收彩种不一致')
            bet=p._make(lot,original,'三码定位',selection,p._multiplier(str(_num(trail['multiple']))))
            due=Decimal(str(bet.principal))
            if claim is not None and due!=claim:
                raise ValueError(f'定位合计金额不一致：应收{due:g}元，自报{claim:g}元')
        except (ValueError,KeyError,ArithmeticError) as exc:
            return [],p.normalize_text(original),str(exc)
        bet.source_span={'start':0,'end':len(original),'text':original,
                         'granularity':'message','reference':'current_text',
                         'compatibility':'v265_trailing_position'}
        if claim is not None:
            p.last_receipts=[{'start':0,'end':len(original),
                              'declared':float(claim),'computed':float(due)}]
        return [bet],p.normalize_text(original),''
    # A line with '合计' names the grand total; '共' after a block names a subtotal.
    mt=_LAST_TOTAL.search(text)
    grand=Decimal(mt[1]) if mt else None
    body=text[:mt.start()].strip() if mt else text.strip()
    if not re.match(r'^\s*(?:(?:福|福彩|体|体彩|3[Dd])\s*)?(?:定位\s*)?百\d',body):
        return None
    if not ('十' in body and '个' in body):return None
    offset=0;bets=[];receipts=[]
    try:
        while offset<len(body):
            m=_POS_BLOCK.match(body,offset)
            if not m:return None
            selection='百:'+m[1]+'|十:'+m[2]+'|个:'+m[3]
            unit=p._make(lot,original,'三码定位',selection,p._multiplier(str(_num(m[4]))))
            if Decimal(str(unit.principal))!=Decimal(m[5]):
                return None # original subsection is inconsistent, don't force it
            bets.append(unit)
            receipts.append({'start':m.start(),'end':m.end(), 'declared':float(m[5]),'computed':float(unit.principal)})
            offset=m.end()
    except (KeyError,ValueError,ArithmeticError):return None
    if len(bets)<1:return None
    return _finish(p,lot,original,bets,grand,receipts)


def _short(p,lot,original,text):
    x=_TWO_SHORT.fullmatch(text)
    if x:
        try:
            bets=(_make(p,lot,original,x[1],'直选',x[2])+_make(p,lot,original,x[1],'组选',x[3])+
                  _make(p,lot,original,x[4],'组选',x[5]))
        except (KeyError,ValueError,ArithmeticError):return None
        if _due(bets)!=Decimal(x[6]):return None
        return _finish(p,lot,original,bets,Decimal(x[6]))
    x=_SHORT.fullmatch(text)
    if x:
        try:bets=_make(p,lot,original,x[1],'直选',x[2])+_make(p,lot,original,x[1],'组选',x[3])
        except (KeyError,ValueError,ArithmeticError):return None
        if _due(bets)!=Decimal(x[4]):return None
        return _finish(p,lot,original,bets,Decimal(x[4]))
    return None


def parse_v265(p,lot,original):
    if not isinstance(original,str) or not original.strip():return None
    text=unicodedata.normalize('NFKC',original).replace('\r\n','\n').replace('\r','\n')
    if any(word in text for word in ('取消','撤单','更正','不买','改单')):return None
    for parser in (_big_list,_positions,_short):
        result=parser(p,lot,original,text)
        if result is not None:return result
    return None
