"""V2.5.8: deterministic recognition before AI, limited to evidence-complete tickets.

No imported project modules, no I/O, no mutation of rules or user data.  The
existing BetParser still prices tickets via _make/_from_kind.  Returning None
means this recognizer makes no claim, so the old pipeline remains available.
"""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
import re
import unicodedata

_NUM = r'\d{3}'
_MONEY = r'\d+(?:\.\d{1,2})?'
_CNUM = r'(?:半|\d+(?:\.\d{1,2})?|[一二两三四五六七八九十百]+)'
_LOT = r'(?:福彩3[Dd]?|福彩|3[Dd]|体彩排列三|体彩|排列三|福|体|排)'
_QTY = re.compile(r'(?<!\d)(\d+)\s*注')
_TOTAL = re.compile(r'(?:(?:福|体)?合计|总计|总共|总)\s*[:：]?\s*('+_MONEY+r')\s*[元米块]?\s*[。！!\s]*$', re.I)
_SUMMARY = re.compile(r'^(?:(?:福|体|排|福彩|体彩|3[Dd])\s*[,，、\s]*)?(?P<count>\d+)\s*注\s*(?P<tail>.*)$',re.I)
_BOTH = re.compile(r'(?<!\d)(\d{3})\s*一\s*单\s*一\s*组')

class RecognitionError(ValueError):
    pass


def dec(v):
    try:
        x=Decimal(str(v))
    except (InvalidOperation,TypeError):
        raise RecognitionError('非法金额或倍数字段') from None
    if not x.is_finite() or x<0 or x.as_tuple().exponent < -2:
        raise RecognitionError('金额必须为非负、最多两位小数')
    return x


def norm(text):
    return (unicodedata.normalize('NFKC',text or '')
            .replace('🈴','合').replace('㊗','注').replace('💰','元')
            .replace('\r\n','\n').replace('\r','\n'))


def fmt(x):
    return format(x,'f')


def _mult(p,val):
    if str(val).strip() in ('半','半倍'):return p._multiplier('0.5')
    if str(val).strip() in ('一','二','两','三','四','五','六','七','八','九','十'):
        from_numbers={'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}
        return p._multiplier(str(from_numbers[str(val).strip()]))
    return p._multiplier(fmt(dec(val)))


def _strip_lot(text,lottery):
    s=text.lstrip(' ，,\t')
    m=re.match(r'^'+_LOT+r'(?=\s*[,，、:：\d百十个直选单组双独和跨定]|$)',s,re.I)
    if m:
        token=m.group().replace(' ','').lower()
        source='福彩' if token in ('福','福彩','3d','福彩3d') else '体彩'
        if source!=lottery:raise RecognitionError('彩种标记与目标彩种不同')
        return s[m.end():].lstrip(' ，,\t')
    return s


def _make(p,lot,raw,code,play,mult,amount=None):
    if play in ('直选','独胆','双飞','和值','跨度','组三','组六','组选'):
        if play in ('和值','跨度'):
            b=p._make(lot,raw,play+str(int(code)),str(int(code)),mult)
            result=[b]
        else:
            result=p._from_kind(lot,raw,code,play,mult)
    else:
        result=[p._make(lot,raw,play,code,mult)]
    if amount is not None and len(result)==1:
        result[0].entered_amount_yuan=float(dec(amount))
    return result


def _amount_to_mult(p,lot,raw,code,play,amount):
    base=sum(dec(b.principal) for b in _make(p,lot,raw,code,play,1))
    if base<=0:raise RecognitionError('玩法单倍金额无效')
    result=dec(amount)/base
    if result.as_tuple().exponent<-2:raise RecognitionError('金额无法按业务倍数精度整除')
    return _mult(p,fmt(result))


def _finish(p,lot,raw,bets,total=None,*,require_total=False,warnings=()):
    if not bets:return None
    due=sum((dec(b.principal) for b in bets),Decimal('0'))
    claimed=dec(total) if total is not None else None
    if require_total and (claimed is None or due!=claimed):
        raise RecognitionError('无单位金额只能凭整条准确合计确认')
    p.last_claimed=float(claimed) if claimed is not None else None
    p.last_receipts=[]
    p.last_warnings=list(warnings)
    if claimed is not None and due!=claimed:
        p.last_warnings.append(f'自报合计{claimed}元与明确投注应收{due}元不符；未改写原始倍数')
    # Keep exact originating message; no fabricated offsets or new tickets.
    for b in bets:
        b.source_span={'start':0,'end':len(raw),'text':raw,'granularity':'message','reference':'current_text'}
    return bets,p.normalize_text(raw),''


def _extract_total(s):
    m=_TOTAL.search(s)
    if m:return s[:m.start()].rstrip(' ，,。;；\n'),dec(m[1])
    return s,None


def _summary_line(text):
    """Return a terminal number-block receipt, or None for nonreceipts."""
    s=re.sub(r'[，,、\s]+','',text).replace('🈴','合')
    if not s:return None
    # Permits both 福189注直选0.5合94.5 and 3D,2倍,515注.
    count=_QTY.search(s)
    if not count or len(list(_QTY.finditer(s)))!=1:return None
    rest=s[:count.start()]+s[count.end():]
    lot_match=re.match(r'^(体彩排列三|福彩3[Dd]?|福彩|3[Dd]|体彩|排列三|福|体|排)',rest,re.I)
    marker=lot_match.group(1) if lot_match else None
    if marker:rest=rest[lot_match.end():]
    # Some inputs append a lottery token after count; this is not a bet.
    rest=re.sub(r'^(?:福|体|排|福彩|体彩|3[Dd])','',rest,flags=re.I)
    a=re.search(r'(?:合计|总计|合|共)('+_MONEY+r')(?:元|米|块)?$',rest)
    amount=dec(a[1]) if a else None
    if a:rest=rest[:a.start()]
    playm=re.search(r'直选|单选|组选|组六|组三|直|单|组',rest)
    kind='组选' if playm and playm[0] in ('组选','组','组三','组六') else '直选'
    if playm:rest=rest[:playm.start()]+rest[playm.end():]
    multiple=re.search(r'(半倍|'+_CNUM+r'倍)$',rest)
    mult=_mult_text=multiple[1][:-1] if multiple else None
    if multiple:rest=rest[:multiple.start()]
    # Bare decimal directly after play is per-ticket amount ONLY when a subtotal confirms.
    bare=re.fullmatch(r'('+_MONEY+r')',rest)
    if bare and mult is None:
        per_ticket=dec(bare[1])
    elif not rest:
        per_ticket=None
    else:return None
    return {'count':int(count[1]),'play':kind,'multiple':mult,'per_ticket':per_ticket,
            'amount':amount,'marker':marker}


def _check_marker(marker,lottery):
    if not marker:return
    source='福彩' if marker.lower() in ('福','福彩','3d','福彩3d') else '体彩'
    if source!=lottery:raise RecognitionError('大底汇总的彩种标记与目标彩种冲突')


def _big_bottom(p,lot,text):
    if '注' not in text or not re.search(r'(?<!\d)\d{3}(?!\d)',text):return None
    lines=[v.strip() for v in text.split('\n') if v.strip()]
    # Only enter this parser for number blocks + explicit count summaries.
    if not any(_summary_line(line) for line in lines):return None
    groups=[];pending=[];global_count=None;global_multiple=None;final_check=None
    for line in lines:
        if re.fullmatch(r'\s*\d+(?:\.\d{1,2})?(?:\s*[+]\s*\d+(?:\.\d{1,2})?)+\s*=\s*\d+(?:\.\d{1,2})?\s*',line):
            if final_check is not None:raise RecognitionError('重复的人工算式不能自动入账')
            lhs,rhs=line.split('=',1)
            final_check=(sum((dec(n) for n in lhs.split('+')),Decimal('0')),dec(rhs))
            continue
        summary=_summary_line(line)
        if summary:
            _check_marker(summary['marker'],lot)
            if pending:
                codes=[]
                for num_line in pending:
                    clean=' '.join(num_line.replace('，',' ').replace(',',' ').replace('、',' ').split())
                    if not re.fullmatch(r'\d{3}(?:\s+\d{3})*',clean):
                        return None
                    codes.extend(clean.split())
                if summary['count']!=len(codes):
                    raise RecognitionError(f'大底注数不一致：原文声明{summary["count"]}注，实际{len(codes)}注')
                groups.append((codes,summary));pending=[]
            else:
                # Terminal count/multiple line has no attached number block,
                # but may refer to all preceding *unpriced* groups.
                if not groups or global_count is not None or summary['amount'] is not None or summary['per_ticket'] is not None:
                    raise RecognitionError('独立注数行没有对应号码块')
                global_count=summary['count'];global_multiple=summary['multiple']
                if global_multiple is None:
                    raise RecognitionError('全局汇总未明确倍数')
        else:
            pending.append(line)
    if pending or not groups:
        raise RecognitionError('大底尾部存在未归属的号码或说明行')
    if global_count is not None and global_count!=sum(len(codes) for codes,_ in groups):
        raise RecognitionError('末尾总注数与各组实际号码数量不一致')
    warnings=[];bets=[];subtotals=[]
    if global_multiple is not None and any(s['multiple'] is not None or s['per_ticket'] is not None for _,s in groups):
        raise RecognitionError('尾部统一倍数与分组倍数存在冲突')
    for codes,s in groups:
        multiplier=s['multiple'] or global_multiple
        if s['per_ticket'] is not None:
            if s['amount'] is None or dec(s['amount'])!=s['per_ticket']*len(codes):
                raise RecognitionError('无倍字单注金额与该组小计不匹配')
            unit=dec(_make(p,lot,text,codes[0],s['play'],1)[0].principal)
            multiplier=fmt(s['per_ticket']/unit)
        elif multiplier is None:
            if s['amount'] is not None:
                unit=dec(_make(p,lot,text,codes[0],s['play'],1)[0].principal)
                multiplier=fmt(s['amount']/Decimal(len(codes))/unit)
            else:
                return None  # Missing price and no global quantity instruction.
        mult=_mult(p,multiplier)
        block=[]
        for code in codes:block.extend(_make(p,lot,text,code,s['play'],mult))
        subtotal=sum((dec(b.principal) for b in block),Decimal(0))
        if s['amount'] is not None and subtotal!=s['amount']:
            if s['multiple'] is not None:
                warnings.append(f'大底小计声明{s["amount"]}元与明确倍数应收{subtotal}元不同')
            else:raise RecognitionError('大底小计与金额反推倍数不吻合')
        if len(set(codes))!=len(codes):warnings.append('大底号码块内出现重复购买，已按次数保留')
        bets.extend(block);subtotals.append(subtotal)
    if final_check is not None:
        lhs,rhs=final_check
        if lhs!=rhs or rhs!=sum(subtotals):
            raise RecognitionError('人工金额算式与大底合计不一致')
    return _finish(p,lot,text,bets,total=sum((s['amount'] for _,s in groups),Decimal(0))
                   if all(s['amount'] is not None for _,s in groups) else None,warnings=warnings)




def _position_plus_pool(p,lot,raw,text,total):
    """One verified three-position bet followed by two independently priced pools."""
    if total is None:return None
    lines=[x.strip() for x in text.split('\n') if x.strip()]
    if len(lines)!=5 or not re.fullmatch(r'(?:福|体|排|福彩|体彩)?\s*定位',lines[0]):
        return None
    position=[]
    for i,label in enumerate('百十个'):
        m=re.fullmatch(label+r'\s*[:：]?\s*(\d{1,10})(?:\s*('+_CNUM+r')\s*倍\s*('+_MONEY+r'))?' ,lines[1+i])
        if not m:return None
        if i<2 and (m[2] or m[3]):return None
        position.append(m)
    if not position[-1][2] or not position[-1][3]:return None
    selection='|'.join(f'{l}:{m[1]}' for l,m in zip('百十个',position))
    fixed=_make(p,lot,raw,selection,'三码定位',_mult(p,position[-1][2]))
    if sum(dec(b.principal) for b in fixed)!=dec(position[-1][3]):return None
    last=_strip_lot(lines[4],lot)
    group=re.fullmatch(r'(\d{4,9})\s*(组六|组三)\s*('+_MONEY+r')\s*(组三|组六)\s*('+_MONEY+r')',last)
    if not group or group[2]==group[4]:return None
    pool=group[1]; solutions=[]
    for amount_mode in (True,False):
        try:
            part=[]
            for play,value in ((group[2],group[3]),(group[4],group[5])):
                multiple=_amount_to_mult(p,lot,raw,pool,play,value) if amount_mode else _mult(p,value)
                part+=_make(p,lot,raw,pool,play,multiple,value if amount_mode else None)
            if sum(dec(b.principal) for b in fixed+part)==total:
                solutions.append(part)
        except (ValueError,RecognitionError):pass
    if len(solutions)!=1:return None
    return _finish(p,lot,raw,fixed+solutions[0],total,require_total=True)


def _two_group_pools(p,lot,raw,text,total):
    """Two distinct named group plays using one pool and distinct money scopes."""
    compact=re.sub(r'\s+', '',_strip_lot(text,lot))
    m=re.fullmatch(r'(\d{3,9})(组三|组六)('+_MONEY+r')([倍元米块]?)(组三|组六)('+_MONEY+r')([倍元米块]?)',compact)
    if not m or m[2]==m[5]:return None
    pool=m[1]
    solutions=[]
    # An explicit unit is authoritative. A bare number needs an exact receipt
    # to disambiguate it from the alternate interpretation.
    modes=[]
    for play,value,unit in ((m[2],m[3],m[4]),(m[5],m[6],m[7])):
        if unit=='倍': modes.append([(play,value,'multiple')])
        elif unit in ('元','米','块'):modes.append([(play,value,'amount')])
        else:modes.append([(play,value,'amount'),(play,value,'multiple')])
    for a in modes[0]:
        for b in modes[1]:
            try:
                bets=[]
                for play,value,mode in (a,b):
                    multiple=_amount_to_mult(p,lot,raw,pool,play,value) if mode=='amount' else _mult(p,value)
                    bets+=_make(p,lot,raw,pool,play,multiple,value if mode=='amount' else None)
                due=sum((dec(x.principal) for x in bets),Decimal(0))
                if total is None and all(unit in ('元','米','块','倍') for unit in (m[4],m[7])):
                    solutions.append((bets,False))
                elif total is not None and due==total:
                    solutions.append((bets,any(unit=='' for unit in (m[4],m[7]))))
            except (ValueError,RecognitionError):pass
    if len(solutions)!=1:return None
    bets,requires_total=solutions[0]
    return _finish(p,lot,raw,bets,total,require_total=requires_total)


def _both_one(p,lot,raw,text,total):
    found=list(_BOTH.finditer(text))
    if not found:return None
    leftover=_BOTH.sub('',text).strip(' ，,、。;；')
    if leftover:return None
    bets=[]
    for m in found:
        bets+=_make(p,lot,raw,m[1],'直选',1)
        bets+=_make(p,lot,raw,m[1],'组选',1)
    if total is None:return None
    return _finish(p,lot,raw,bets,total,require_total=True)


def _shared_direct_and_sum(p,lot,raw,text,total):
    """福841 / 341两倍直选8 / 3和直20 / 福013,093...直2倍合计44."""
    lines=[v.strip() for v in text.split('\n') if v.strip()]
    if len(lines)!=4:return None
    first=_strip_lot(lines[0],lot)
    if not re.fullmatch(_NUM,first):return None
    second=re.fullmatch(r'('+_NUM+r')\s*('+_CNUM+r')\s*倍\s*(?:直选|直|单选|单)\s*('+_MONEY+r')',lines[1])
    third=re.fullmatch(r'(\d{1,2})\s*(?:和直|和值|合值)\s*('+_MONEY+r')\s*[元米块]?',lines[2])
    fourth=_strip_lot(lines[3],lot)
    last=re.fullmatch(r'(?P<codes>'+_NUM+r'(?:\s*[,，、.。\s]+\s*'+_NUM+r')+)\s*(?:直|单|直选)\s*('+_CNUM+r')\s*倍',fourth)
    if not (second and third and last and total is not None):return None
    first_bets=[*_make(p,lot,raw,first,'直选',_mult(p,second[2])),
                *_make(p,lot,raw,second[1],'直选',_mult(p,second[2]))]
    if sum(dec(b.principal) for b in first_bets)!=dec(second[3]):return None
    sum_bet=_make(p,lot,raw,third[1],'和值',_amount_to_mult(p,lot,raw,third[1],'和值',third[2]),third[2])
    codes=re.findall(r'(?<!\d)\d{3}(?!\d)',last['codes'])
    remaining=[b for code in codes for b in _make(p,lot,raw,code,'直选',_mult(p,last[2]))]
    return _finish(p,lot,raw,first_bets+sum_bet+remaining,total,require_total=True)


def _groups_explicit(p,lot,raw,text,total):
    """福独胆7 300米。1378组六100米组三100米福合计500."""
    t=re.sub(r'。\s*(?=米|元|块)', '', text)
    t=re.sub(r'(?<=\d)\s*[。]\s*(?=米|元|块)', '', t)
    t=t.replace('。',' ').replace('，',' ').replace(',',' ')
    t=_strip_lot(t,lot)
    start=re.match(r'^独胆\s*(\d)\s*[，,\s]+('+_MONEY+r')\s*[米元块]\s*',t)
    if not start:return None
    rest=t[start.end():].strip()
    rest=_strip_lot(rest,lot)
    pattern=(r'^(?P<pool>\d{4,9})\s*组六\s*(?P<a>'+_MONEY+r')\s*[米元块]'
             r'\s*组三\s*(?P<b>'+_MONEY+r')\s*[米元块]\s*$')
    m=re.fullmatch(pattern,rest)
    if not m:return None
    bets=[]
    for code,play,amount in ((start[1],'独胆',start[2]),(m['pool'],'组六',m['a']), (m['pool'],'组三',m['b'])):
        b=_make(p,lot,raw,code,play,_amount_to_mult(p,lot,raw,code,play,amount),amount)
        bets+=b
    return _finish(p,lot,raw,bets,total,require_total=True)


def _multi_clause(p,lot,raw,text,total):
    """Sequential independent sets separated by their own play/multiple/receipts."""
    t=_strip_lot(text.strip(),lot)
    t=t.replace('，',',').replace('。',';').replace('；',';')
    pattern=re.compile(r'\s*(?P<codes>\d{2,3}(?:\s*[-、,./\s]\s*\d{2,3})*)\s*[-、,./\s]*'
                       r'(?P<play>直选|组选|双飞|直|组|单)\s*(?:各\s*)?(?P<mult>'+_CNUM+r')\s*倍'
                       r'(?:\s*(?:共|合计|合)?\s*(?P<amount>'+_MONEY+r')\s*[米元块])?')
    out=[];pos=0;matched=0
    while pos<len(t):
        rem=t[pos:].lstrip(' ,;。\t\n')
        if not rem:break
        consumed=len(t[pos:])-len(rem);pos+=consumed
        m=pattern.match(t,pos)
        if not m:return None
        codes=re.findall(r'(?<!\d)\d{2,3}(?!\d)',m['codes'])
        play={'直':'直选','单':'直选','组':'组选'}.get(m['play'],m['play'])
        for code in codes:
            out+=_make(p,lot,raw,code,play,_mult(p,m['mult']))
        if m['amount'] is not None:
            expected=sum((dec(b.principal) for b in out[-len(codes):]),Decimal(0))
            if expected!=dec(m['amount']):
                raise RecognitionError('分组小计与明确倍数计算不一致')
        pos=m.end();matched+=1
    if matched<2:return None
    return _finish(p,lot,raw,out,total)


def _scope_doublefly(p,lot,raw,text,total):
    """An explicit direct batch and a separate two-number double-fly clause."""
    t=_strip_lot(text,lot)
    m=re.fullmatch(r'(?P<codes>\d{3}(?:\s*[-、,.\s]\s*\d{3})+)\s*直选\s*各\s*('+_CNUM+r')\s*倍\s*[。;；]\s*'
                   r'(?P<flies>\d{2}(?:\s*[-、,.\s]\s*\d{2})+)\s*[-、,.\s]*双飞\s*各\s*('+_MONEY+r')\s*',t)
    if not m or total is None:return None
    direct=[b for code in re.findall(r'\d{3}',m['codes']) for b in _make(p,lot,raw,code,'直选',_mult(p,m[2]))]
    # A bare '各20' can be money *or* multiple. Require unique total fit.
    flies=re.findall(r'\d{2}',m['flies']);rawvalue=dec(m[4])
    solutions=[]
    for mode in ('amount','multiple'):
        try:
            b=[]
            for code in flies:
                mult=_amount_to_mult(p,lot,raw,code,'双飞',rawvalue) if mode=='amount' else _mult(p,rawvalue)
                b+=_make(p,lot,raw,code,'双飞',mult,rawvalue if mode=='amount' else None)
            if sum(dec(x.principal) for x in direct+b)==total:
                solutions.append((mode,b))
        except (ValueError,RecognitionError):pass
    if len(solutions)!=1:return None
    return _finish(p,lot,raw,direct+solutions[0][1],total,require_total=True)


def _typo_main(p,lot,raw,text,total):
    """One bounded typo 主->组, only if fully corroborated by subtotal+global total."""
    if total is None or '主' not in text:return None
    lines=[s.strip() for s in text.split('\n') if s.strip()]
    if len(lines)!=3:return None
    first=_strip_lot(lines[0],lot)
    m1=re.fullmatch(r'(\d{4,9})\s*(?:组6|组六)\s*[,，]?\s*('+_CNUM+r')\s*倍\s*('+_MONEY+r')',first)
    m2=re.fullmatch(r'(\d{3})\s*(?:直选|直|单)\s*('+_CNUM+r')\s*主\s*('+_CNUM+r')\s*倍\s*('+_MONEY+r')',lines[1])
    m3=re.fullmatch(r'(\d{3})\s*(?:直选|直|单)\s*('+_CNUM+r')\s*倍\s*('+_MONEY+r')',lines[2])
    if not (m1 and m2 and m3):return None
    a=_make(p,lot,raw,m1[1],'组六',_mult(p,m1[2]))
    b=_make(p,lot,raw,m2[1],'直选',_mult(p,m2[2]))+_make(p,lot,raw,m2[1],'组选',_mult(p,m2[3]))
    c=_make(p,lot,raw,m3[1],'直选',_mult(p,m3[2]))
    if [sum(dec(z.principal) for z in bs) for bs in (a,b,c)]!=[dec(m1[3]),dec(m2[4]),dec(m3[3])]:
        return None
    return _finish(p,lot,raw,a+b+c,total,require_total=True,
                   warnings=['按明确玩法及金额核对，原文“主”作为“组”的有证据纠正；原文已保留'])


def _dual_lottery(p,lot,raw):
    """Each of the two controller-routed records receives only its own share."""
    m=re.match(r'^\s*(?:福体|体福)\s*',raw)
    if not m:return None
    body=raw[m.end():].strip()
    # Existing special shared-total contract remains owned by old parser.
    if re.fullmatch(r'\d{3}\s*一单一组\s*\d+(?:\.\d{1,2})?\s*[米元块]?',body):
        return None
    body,total=_extract_total(body)
    direct=re.fullmatch(r'(\d{3})\s*(直选|直|单选|单|组选|组)\s*(?:各)?('+_CNUM+r')\s*倍',body)
    both=re.fullmatch(r'(\d{3})\s*一单一组',body)
    if direct:
        bets=_make(p,lot,raw,direct[1],'直选' if direct[2] in ('直选','直','单选','单') else '组选',_mult(p,direct[3]))
    elif both:
        bets=_make(p,lot,raw,both[1],'直选',1)+_make(p,lot,raw,both[1],'组选',1)
    else:return None
    value=sum((dec(b.principal) for b in bets),Decimal(0))
    if total is not None and total!=2*value:
        raise RecognitionError('福体彩种共享合计与两彩金额不一致')
    return _finish(p,lot,raw,bets,value if total is not None else None)


def parse_unambiguous(p,lottery,text):
    if not isinstance(text,str) or not text.strip():return None
    raw=norm(text)
    if re.search(r'撤单|取消|更正|改成|不买|修改',raw):return None
    # The old parser handles 福体537一单一组8 with a shared dual-lottery amount.
    if raw.lstrip().startswith(('福体','体福')):
        try:return _dual_lottery(p,lottery,raw)
        except (RecognitionError,ValueError,KeyError) as exc:
            return [],p.normalize_text(raw),str(exc)
    try:
        big=_big_bottom(p,lottery,raw)
        if big is not None:return big
        remainder,total=_extract_total(raw)
        # Don't allow accounting text to stand in for a real ticket.
        body=_strip_lot(remainder,lottery)
        for rule in (_both_one,_two_group_pools,_position_plus_pool,_shared_direct_and_sum,_groups_explicit,
                     _multi_clause,_scope_doublefly,_typo_main):
            result=rule(p,lottery,raw,body,total)
            if result is not None:return result
    except RecognitionError as exc:
        return [],p.normalize_text(raw),str(exc)
    except (ValueError, KeyError, ArithmeticError) as exc:
        # A recognized shape with invalid digits/price may be rejected by
        # native BetParser rules; keep it pending rather than invent a bet.
        return [],p.normalize_text(raw),str(exc)
    return None
