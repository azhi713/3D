"""带单位、合计作用域和来源位置的消息语义层；不依赖AI决定定价。"""
from dataclasses import dataclass
from decimal import Decimal
from itertools import product
import re,unicodedata

class SemanticError(ValueError):
    def __init__(self,message,declared=None):
        super().__init__(message)
        self.declared=declared

@dataclass(frozen=True)
class Token:
    kind:str
    text:str
    start:int
    end:int
    line:int

def tokenize(raw):
    """按字符NFKC建立offset映射，数字是否为金额由后续语境标记。"""
    normalized=[];mapping=[]
    for i,char in enumerate(raw):
        chars=unicodedata.normalize('NFKC',char)
        normalized.extend(chars);mapping.extend([i]*len(chars))
    text=''.join(normalized)
    pattern=r'(?P<PLAY>组三复式|组六复式|直选|组选|组三|组六|和值|合值|和直|跨度|双飞|独胆|定位|转圈打|拖|胆)|(?P<NUMBER>\d+(?:\.\d+)?)|(?P<UNIT>倍|元|米|块|注)|(?P<TOTAL>合计|总计|福家|体家)|(?P<SEPARATOR>[\s,.，。;；:：/、-]+)|(?P<WORD>.)'
    tokens=[]
    for m in re.finditer(pattern,text):
        start=mapping[m.start()];end=mapping[m.end()-1]+1
        tokens.append(Token(m.lastgroup,m.group(),start,end,raw.count('\n',0,start)+1))
    return text,mapping,tokens

def number(value):
    from parser import chinese_int
    return Decimal(value) if re.fullmatch(r'\d+(?:\.\d+)?',value) else Decimal('.5') if value=='半' else Decimal(chinese_int(value))

def canonical_message(text,lot):
    """格式兼容仅限日期标题、分隔符和有上下文的别名；不添加选号/数量。"""
    from parser import MULT,LOTTERY_TOKEN,lottery_from_token
    text=unicodedata.normalize('NFKC',text).replace('㊗️','注').replace('祝️','注').replace('合直','合值')
    text=re.sub(r'(?m)^\s*20\d{2}[-/]\d{2}[-/]\d{2}(?:\s+\d{2}:\d{2}:\d{2})?\s*[/／]?\s*','',text)
    text=re.sub(r'(?m)^\s*[-—─]{3,}\s*$','',text)
    text=re.sub(r'(组)\s*([36])(?=\s*(?:打|[,，]))',lambda m:'组三' if m[2]=='3' else '组六',text)
    text=re.sub(r'([百十个](?:位)?)\s*[,，]\s*(?=\d)',r'\1',text)
    text=re.sub(r'(组六|组三|直选|组选|双飞|一直一组|一单一组|各)\s*[,，。:]\s*(?='+MULT+r')',r'\1',text)
    text=re.sub(r'(?:各)(?=一直一组|一单一组)','',text)
    # A repeated same-lottery marker inside a number list is a heading, not a new order.
    def internal(m):
        if lottery_from_token(m[2])!=lot:raise SemanticError('文字彩种与所选彩种冲突')
        return m[1]
    text=re.sub(r'(\d{2,3})[-\s.]*('+LOTTERY_TOKEN+r')(?=\s*(?:直|单|组|双|\d{3}))',internal,text)
    text=re.sub(r'(?<=\d)\s*双(?=\s*\d)', '双飞',text)
    text=re.sub(r'组\s*三','组三',text)
    text=re.sub(r'组\s*六','组六',text)
    # Prefix play labels with a colon have unambiguous scope over the following pool.
    text=re.sub(r'(组三|组六)\s*:\s*(\d{2,10})\s*(?:组|打)?',r'\2\1',text)
    text=re.sub(r'直选\s*(\d{3})\s*('+MULT+r')\s*([元米块])',r'\1直选\2\3',text)
    text=re.sub(r'(?<=\d)\s*组选\s*[.,，]\s*直选各\s*('+MULT+r')倍',r'直选\1倍组选\1倍',text)
    text=re.sub(r'(组三|组六)([2-9二三四五六七八九])码(?=\s*\d)',r'\1',text)
    # Slash between finished bets separates orders; slash in a list remains a list separator.
    lines=[]
    for line in text.splitlines():
        parts=re.split(r'\s*[/／]\s*',line);buffer=parts[0]
        for part in parts[1:]:
            if re.search(r'直|组|双飞|独胆|跨度|和值|合值|倍|元|米|块',buffer) and re.match(r'(?:'+LOTTERY_TOKEN+r')?\s*(?:\d{3,}|(?:合计|总计|福家|体家))',part):
                lines.append(buffer);buffer=part
            else:buffer+=' '+part
        lines.append(buffer)
    return '\n'.join(lines)

def _make(p,lot,raw,kind,selection,multiple):
    value=p._multiplier(format(Decimal(str(multiple)),'f'))
    if kind in ('直选','组选','组三','组六','复式','双飞','独胆','对子'):
        return p._from_kind(lot,raw,selection,kind,value)
    return [p._make(lot,raw,kind,selection,value)]

def requires_semantic(text):
    raw=unicodedata.normalize('NFKC',str(text or ''))
    compact=re.sub(r'\s+','',raw)
    if re.search(r'\d{2,9}(?:[二三四五六七八九]码)?(?:组三|组六)(?:各|打)?\d+(?![\d倍元米块])',compact):
        return True
    if re.search(r'(?:双飞各|各飞)\s*\d+(?!\d)\s*(?:合计|$)',raw) and '倍' not in raw.split('\n')[-1]:return True
    if re.fullmatch(r'\s*(?:福|福彩|体|体彩)?\s*\d{3}组\d+\s*',raw):return True
    numbers=re.findall(r'(?<!\d)\d{3}(?!\d)',raw)
    if '\n' not in raw and len(numbers)>1 and len(numbers)!=len(set(numbers)) and '一单一组' in raw:
        return True
    return False

def _strip_lottery(lot,line):
    from parser import LOTTERY_TOKEN,lottery_from_token
    line=line.strip().lstrip(':：#').strip()
    while True:
        m=re.match(r'^('+LOTTERY_TOKEN+r')\s*[:：,#]?\s*',line,re.I)
        if not m:break
        if lottery_from_token(m[1])!=lot:raise SemanticError('文字彩种与所选彩种冲突')
        line=line[m.end():].lstrip('#:：').strip()
    return re.sub(r'(福彩|体彩)(?=\s*\d+(?:\.[0-9]+)?\s*[元米块]?$)','',line)

def atom_options(p,lot,raw):
    from parser import MULT
    line=_strip_lottery(lot,unicodedata.normalize('NFKC',raw))
    line=re.sub(r'组三组选','组三',line)
    line=re.sub(r'两码组三','二码组三',line)
    line=re.sub(r'\(([^()]*(?:单|直|组)[^()]*)\)',r' \1 ',line)
    line=re.sub(r'(?<=\d)x(?=\d+(?:单|直))',' ',line)
    terminal=re.search(r'(?<=[元米块])(\d+(?:\.\d{1,2})?)$',line)
    if terminal:
        base=atom_options(p,lot,line[:terminal.start()])
        if base is not None:return base[0],float(terminal[1])
    sep=r'[\s.,。:：，、/+-]+'
    # 定位的明确金额下注，或历史“排1直”单位写法。
    pos=re.fullmatch(r'((?:[百十个](?:位)?\s*\d+\s*){1,3})(?:排)?(?:('+MULT+r')直\s*(\d+(?:\.\d{1,2})?)|(?:定位)?\s*(?:打)?\s*('+MULT+r')\s*[元米块])',line)
    if pos:
        pairs=re.findall(r'([百十个])(?:位)?\s*(\d+)',pos[1])
        selection='|'.join(a+':'+b for a,b in pairs)
        play={1:'一码定位',2:'两码定位',3:'三码定位'}[len(pairs)]
        unit=p._make(lot,raw,play,selection,1).principal
        mult=number(pos[2]) if pos[2] else number(pos[4])/Decimal(str(unit))
        return [[p._make(lot,raw,play,selection,p._multiplier(format(mult,'f')))]],float(pos[3]) if pos[3] else None
    pos=re.fullmatch(r'([百十个](?:位)?\s*\d+)\s*打\s*('+MULT+r')',line)
    if pos:return atom_options(p,lot,pos[1]+' '+pos[2]+'元')
    # 和值及目标清单，显式“打/无倍尾值”为金额，倍为数量。
    sum_line=re.sub(r'[,，]',' ',line) if re.match(r'^(?:和值|合值|和直)',line) else line
    summ=re.fullmatch(r'(?:和值|合值|和直)\s*[:：]?\s*(\d{1,2}(?:'+sep+r'\d{1,2})*)\s*(?:各)?\s*(?:打|买)?\s*('+MULT+r')\s*(倍|元|米|块)?(?:\s*(\d+))?',sum_line)
    reverse=re.fullmatch(r'(\d{1,2})\s*(?:和直|和值|合值)\s*('+MULT+r')\s*([元米块])?',line)
    if reverse:summ=reverse
    if summ:
        targets=re.split(sep,summ[1]);value=number(summ[2]);unit=summ[3]
        result=[]
        for target in targets:
            if not 0<=int(target)<=27:raise SemanticError('和值须为0～27')
            mult=value if unit=='倍' else value/10
            result+=_make(p,lot,raw,'和值'+str(int(target)),str(int(target)),mult)
        return [result],None
    dan=re.fullmatch(r'(\d)\s*(?:胆?拖|胆)\s*([0-9]{2,9})(?:拖)?(?:\s*[,， ]*\s*(组[三六36])\s*[,， ]*\s*|[\s,，]+)('+MULT+r')\s*(倍|元|米|块)?',line)
    if dan:
        d,t,group,value,unit=dan.groups()
        group=3 if group in ('组三','组3') else 6
        play=f'1胆{len(t)}拖组{group}'
        bets=_make(p,lot,raw,play,d+':'+t,number(value) if unit=='倍' else number(value)/10)
        if group==3:bets[0].settlement_rule_pending=['组三胆拖重号极端覆盖仍待确认']
        return [bets],None
    if re.search(r'\d\s*(?:胆|拖)',line):
        if re.search(r'\d\s*(?:胆?拖|胆)\s*\d',line):raise SemanticError('胆拖缺少有效金额/倍数或号码有误')
    # 明确的X双飞缩写，位置X不全局转换。
    x=re.fullmatch(r'((?:[xX]\d{2}\s*)+)各\s*('+MULT+r')\s*[元米块]?',line)
    if x:
        return [[b for code in re.findall(r'[xX](\d{2})',x[1]) for b in _make(p,lot,raw,'双飞',code,number(x[2])/10)]],None
    # 独/毒数字金额，不允许含糊的纯“毒7”自行补倍投。
    single=re.fullmatch(r'(?:独胆|毒胆|独|毒)\s*(\d)\s*[.,， ]*\s*('+MULT+r')\s*(倍|元|米|块)?',line)
    if single:
        return [_make(p,lot,raw,'独胆',single[1],number(single[2]) if single[3]=='倍' else number(single[2])/10)],None
    all_group=re.fullmatch(r'(?:全包(组三|组六)|(组三|组六)全包)\s*('+MULT+r')\s*[元米块]',line)
    if all_group:return [_make(p,lot,raw,'全包'+(all_group[1] or all_group[2]),'',number(all_group[3])/10)],None
    # 号码前的明确码数说明不改变号码；没有实际选号不能制造数字池。
    line=re.sub(r'[二两三四五六七八九]码(?=\d)','',line)
    if re.match(r'^[二两三四五六七八九]码',line):return None
    named=re.fullmatch(r'(\d{3}(?:'+sep+r'\d{3})*)\s*('+MULT+r')(?:单|直)(?:('+MULT+r')组)?\s*(?:('+MULT+r')[元米块])?',line)
    if named:
        codes=re.split(sep,named[1]);result=[]
        for code in codes:
            result+=_make(p,lot,raw,'直选',code,number(named[2]))
            if named[3]:
                if len(set(code))==1 and len(codes)==1:raise SemanticError('豹子不能购买组选')
                if len(set(code))>1:result+=_make(p,lot,raw,'组选',code,number(named[3]))
        return [result],float(number(named[4])) if named[4] else None
    # 选号列表与操作子句，允许前置组选。
    front=re.fullmatch(r'(组选|组|直选|直)\s*([0-9]{3}(?:'+sep+r'[0-9]{3})*)各?('+MULT+r')倍?',line)
    if front:
        kind='组选' if front[1] in ('组选','组') else '直选'
        return [[b for code in re.split(sep,front[2]) for b in _make(p,lot,raw,kind,code,number(front[3]))]],None
    m=re.fullmatch(r'([0-9]{1,9}(?:'+sep+r'[0-9]{1,9})*)\s*[-.,:，：]?\s*(.+)',line)
    if not m:return None
    codes=re.split(sep,m[1].strip());tail=re.sub(r'\s+','',m[2])
    if len(codes)>1 and all(len(c)==1 for c in codes):codes=[''.join(codes)]
    # 严格保留明确数字：不吞掉没有归属的选号。
    if any(len(s)>9 for s in codes):return None
    # 一米直一米组（包括已确认的尾部省“组”）。
    money_pair=re.fullmatch(r'('+MULT+r')[元米块](?:单|直)('+MULT+r')[元米块](?:组)?(?:('+MULT+r')[元米块])?',tail)
    if money_pair:
        result=[]
        for code in codes:
            result+=_make(p,lot,raw,'直选',code,number(money_pair[1])/2)
            result+=_make(p,lot,raw,'组选',code,number(money_pair[2])/2)
        return [result],float(number(money_pair[3])) if money_pair[3] else None
    # 数量写在玩法前；默认单个“直/组”1倍，复式和金额不混用。
    pair=re.fullmatch(r'('+MULT+r')(?:单|直)('+MULT+r')(?:组)(?:('+MULT+r')[元米块]?)?',tail)
    if pair:
        result=[]
        for code in codes:
            result+=_make(p,lot,raw,'直选',code,number(pair[1]))
            if len(set(code))==1 and len(codes)==1:raise SemanticError('豹子不能购买组选')
            if len(set(code))>1:result+=_make(p,lot,raw,'组选',code,number(pair[2]))
        # 保留旧“单一组金额”模板的金额换算，不覆盖明确包含豹子的最终确认例外。
        if pair[3] and tail.startswith('一单一组') and not any(len(set(c))==1 for c in codes):return None
        return [result],float(number(pair[3])) if pair[3] else None
    if tail in ('单','直','直选','组','组选','一直一组','一单一组'):
        kinds=['直选','组选'] if '一组' in tail else ['组选' if '组' in tail else '直选']
        return [[b for code in codes for kind in kinds if not(kind=='组选' and len(set(code))==1 and len(kinds)>1)
                 for b in _make(p,lot,raw,kind,code,1)]],None
    # 数字清单后的“一倍”是共同直选倍率。
    direct=re.fullmatch(r'('+MULT+r')倍(?:('+MULT+r'))?',tail)
    if direct and all(len(c)==3 for c in codes):
        return [[b for c in codes for b in _make(p,lot,raw,'直选',c,number(direct[1]))]],float(number(direct[2])) if direct[2] else None
    # 所有双飞组各金额/倍数。无单位同时保留两个解释，由整段金额唯一消歧。
    fly=re.fullmatch(r'(?:双飞各|各飞|双飞)(?:打)?('+MULT+r')(倍|元|米|块)?(?:('+MULT+r'))?',tail)
    if fly and all(len(c)==2 for c in codes):
        value=number(fly[1]);unit=fly[2]
        choices=[value] if unit=='倍' else [value/10] if unit else [value/10,value]
        return [[b for c in codes for b in _make(p,lot,raw,'双飞',c,mult)] for mult in choices],float(number(fly[3])) if fly[3] else None
    # 单组批量“组12”为整组金额；单号裸组100存在单位歧义，必须保留。
    grouped=re.fullmatch(r'(?:组|组选)('+MULT+r')',tail)
    if grouped and all(len(c)==3 for c in codes):
        if len(codes)==1:raise SemanticError('普通组选裸金额/倍率存在歧义，需单位')
        return [[b for c in codes for b in _make(p,lot,raw,'组选',c,number(grouped[1])/(2*len(codes)))]],float(number(grouped[1]))
    # 连续玩法子句共享选号，沿用其原本固定收费。
    words=r'二码组三|三码组三|组三|组六|复试|复式|组选|双飞|直选|直|组|单'
    shared=re.fullmatch(r'(组三组六|组六组三|组6组3)(?:各)('+MULT+r')(倍|元|米|块)?',tail)
    if shared:
        value=number(shared[2]);modes=[False] if shared[3]=='倍' else [True]
        ops=[('组三',value),('组六',value)]
    else:
        pieces=list(re.finditer(r'('+words+r')(?:各)?(?:打|买)?('+MULT+r')(倍|元|米|块)?',tail))
        if not pieces or ''.join(x.group() for x in pieces)!=tail:return None
        ops=[(('组三' if '码组三' in x[1] else '组选' if x[1]=='组' else '直选' if x[1] in ('单','直') else '复式' if x[1]=='复试' else x[1]),number(x[2])) for x in pieces]
        explicit=[x[3] for x in pieces]
        if all(x is None for x in explicit):
            if len(ops)==1 and not (ops[0][0] in ('组三','组六') and all(len(c)>=2 for c in codes)):return None
            modes=[False] if all(kind in ('直选','组选') for kind,val in ops) else [False,True]
        elif all(x in ('元','米','块') for x in explicit):modes=[True]
        elif all(x in (None,'倍') for x in explicit):modes=[False]
        else:return None
    results=[]
    for amount_mode in modes:
        batch=[]
        for code in codes:
            for kind,val in ops:
                base=_make(p,lot,raw,kind,code,1)[0].principal
                batch+=_make(p,lot,raw,kind,code,val/Decimal(str(base)) if amount_mode else val)
        results.append(batch)
    return results,None

def parse_message(p,lot,text):
    """有限候选求解只消除无单位歧义；明示倍率不受声明总额纠正。"""
    from bet_grammar import message_blocks,strip_receipt,decimal_number,normalize_list
    from parser import chinese_int
    if not isinstance(text,str) or not text.strip():return None
    if re.search(r'撤单|取消|更正|改成|不买',text):return None
    raw=canonical_message(text,lot)
    if raw!=text and not requires_semantic(text) and not requires_semantic(raw):
        known,_,error=p._parse_pipeline(lot,raw)
        if known and not error:
            for bet in known:
                offset=text.find(bet.raw) if bet.raw and text.count(bet.raw)==1 else -1
                bet.source_span=({'start':offset,'end':offset+len(bet.raw),'text':bet.raw}
                                 if offset>=0 else {'text':text,'start':0,'end':len(text),
                                                   'granularity':'message','normalized_fragment':bet.raw})
            return known,p.last_claimed,list(p.last_receipts),list(p.last_warnings)
    numbers=re.findall(r'(?<!\d)\d{3}(?!\d)',raw)
    if '\n' not in raw and len(numbers)>1 and len(numbers)!=len(set(numbers)) and '一单一组' in raw:
        raise SemanticError('同一列表重复号码的购买次数需明确，不自动去重或新增')
    if re.search(r'\d+\s*注|福体|体福',raw):return None
    declared=None
    total=re.search(r'(?:合计|总计|福家|体家|总)\s*[:：]?\s*(\d+(?:\.\d{1,2})?)\s*[元米块]?\s*$',raw)
    if total:declared=float(total[1]);raw=raw[:total.start()].rstrip()
    blocks=message_blocks(raw)
    options=[];receipts=[];used=False;errors=[];cursor=0
    for block in blocks:
        if not block.text:continue
        original=block.text
        # 货币标记后开始新选号，属于新段落；不拆“1米直1米组”。
        chunks=re.split(r'(?<=[元米块])\s+(?=(?:[二三四五六七八九]码)?\d{3,9})|(?<=[组直单倍])[/，,\s]+(?=\d{3}|(?:和值|合值|和直))',original)
        local=[]
        for chunk in chunks:
            try:
                extended=atom_options(p,lot,chunk)
            except ValueError as exc:
                raise SemanticError(str(exc),declared) from None
            if extended is not None:
                choices,receipt=extended;used=True
            else:
                cleaned,receipt=strip_receipt(chunk,lambda x:decimal_number(x,chinese_int))
                # 只有金额标签不能破坏单价已知的实际金额下注。
                normalized=normalize_list(cleaned)
                extended=atom_options(p,lot,normalized)
                if extended is not None:
                    choices,_=extended;used=True
                else:
                    bets,_,error=p._parse_pipeline(lot,chunk)
                    if error or not bets:
                        errors.append(error);break
                    choices=[bets]
                    if receipt is None:receipt=p.last_claimed
            if receipt is not None:
                if len(choices)>1:
                    matched=[choice for choice in choices if sum(Decimal(str(b.principal)) for b in choice)==Decimal(str(receipt))]
                    if len(matched)==1:choices=matched
                receipts.append({'declared':receipt,'block':len(options)+len(local),'text':chunk})
            offset=text.find(chunk,cursor)
            if offset>=0:cursor=offset+len(chunk)
            for choice in choices:
                for bet in choice:
                    # 使用实际原文片段，不虚构规范化后的offset。
                    bet.source_span=({'start':offset,'end':offset+len(chunk),'text':chunk} if offset>=0 else
                                     {'text':text,'start':0,'end':len(text),'granularity':'message','normalized_fragment':chunk})
            local.append(choices)
        if errors:break
        for choices in local:options.append(choices)
    if not used:return None
    if errors:raise SemanticError(errors[0],declared)
    if not options:return None
    variants=1
    for choices in options:variants*=len(choices)
    if variants>64:raise SemanticError('无单位的歧义组合过多，需确认金额/倍数',declared)
    interpretations=[]
    for choices in product(*options):
        bets=[b for part in choices for b in part]
        interpretations.append((bets,sum(Decimal(str(b.principal)) for b in bets)))
    if len(interpretations)>1:
        if declared is None:raise SemanticError('金额与倍数均可解释，缺少完整合计或单位证据')
        matches=[r for r in interpretations if r[1]==Decimal(str(declared))]
        if len(matches)!=1:raise SemanticError('金额/倍数解释不能由完整上下文唯一确认',declared)
        chosen=matches[0][0]
    else:chosen=interpretations[0][0]
    computed=sum(Decimal(str(b.principal)) for b in chosen)
    if declared is None and receipts:
        last=receipts[-1]['declared']
        if Decimal(str(last))==computed:declared=last
        elif len(blocks)==1:declared=last
    selected=next(choices for choices in product(*options) if [b for part in choices for b in part]==chosen)
    for receipt in receipts:
        receipt['computed']=float(sum(Decimal(str(b.principal)) for b in selected[receipt.pop('block')]))
    return chosen,declared,receipts,[]
