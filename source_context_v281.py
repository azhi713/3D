# -*- coding: utf-8 -*-
"""V281 source-bound layout compatibility. Prices always come from BetParser.

The original message is not overwritten by the layout normalizer. Routing may
move a trailing lottery label before its own contiguous number block; no code,
price or complete preceding purchase is borrowed from another block.
"""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import re
import unicodedata

LOT = r'(?:体彩排列三|福彩3[Dd]|排列三|福彩|体彩|3[Dd]|福|体|排)'
PLAY = r'(?:(?:[二三四五六七八九2-9]码)?(?:组六|组三|组6|组3|复试|复式)|直选|单选|组选|双飞|独胆|毒胆|对子|直|单|组)'
NUM = r'(?:[0-9]+(?:\.[0-9]{1,2})?|[零〇一二两三四五六七八九十百千万]+|半|[0-9]+/[0-9]+|[二三四五六七八九十]+分之[一二两三四五六七八九十]+)'
TOTAL = re.compile(r'(?:合计|总计|合|共)[:：]?(?P<cash>[0-9]+(?:\.[0-9]{1,2})?)(?:元|米|块)?[。！!]*$')
CLAUSE = re.compile(rf'^(?P<lot>{LOT})?(?P<code>[0-9]{{1,9}})(?P<play>{PLAY})(?:各|打|买|投)?(?P<value>{NUM})(?P<unit>倍|元|米|块|毛|角|分)?$', re.I)
DIRECTIVE = re.compile(rf'^(?P<lot>{LOT})?(?P<play>{PLAY})(?:各|打|买|投)?{NUM}(?:倍|元|米|块|毛|角|分)?(?:(?:合计|总计|合|共)[:：]?[0-9]+(?:\.[0-9]{{1,2}})?(?:元|米|块)?)?$', re.I)
PURE = re.compile(rf'^(?P<lot>{LOT})?(?P<code>[0-9]{{1,9}})$', re.I)
CANCEL = re.compile(r'取消|撤单|更正|改成|不买|作废')


def compact(text):
    return re.sub(r'[ \t\u3000]+', '', unicodedata.normalize('NFKC', str(text or ''))
                  .replace('🈴', '合').replace('㊗️', '注').replace('㊗', '注')
                  .replace('\ufe0f', '').replace('／', '/').replace('⁄', '/'))


def lottery_of(token):
    return '体彩' if token.lower() in ('体彩排列三','排列三','体彩','体','排') else '福彩'


def normalize_layout(text):
    """Only join an unfinished pure-number pool to its immediate directive.

    Unknown lines, comments, a different intervening lottery, or two pools in
    succession are left untouched; the normal parser must account for them.
    """
    if not isinstance(text,str) or CANCEL.search(text):
        return text
    lines=text.replace('\r\n','\n').replace('\r','\n').split('\n')
    out=[]; i=0
    while i<len(lines):
        # A trailing lottery/position instruction belongs to exactly the
        # preceding complete 百/十/个 block. Move only its lottery marker to
        # the front for routing; leave each position and its digits intact.
        if i+3<len(lines) and all(re.fullmatch(
                rf'\s*{position}(?:位)?\s*[:：]?\s*[0-9]{{1,10}}\s*', lines[i+index])
                for index,position in enumerate('百十个')):
            last=compact(lines[i+3])
            tail=re.match(rf'(?P<lot>{LOT})(?=定位|三码定位)',last,re.I)
            if tail:
                out.extend([tail['lot']+lines[i].strip(),lines[i+1],lines[i+2],
                            last[tail.end():]])
                i+=4
                continue
        m=PURE.fullmatch(compact(lines[i]))
        if not m:
            out.append(lines[i]);i+=1;continue
        j=i+1
        while j<len(lines) and not lines[j].strip():j+=1
        d=DIRECTIVE.fullmatch(compact(lines[j])) if j<len(lines) else None
        if d:
            first=m['lot']; second=d['lot']
            if first and second and lottery_of(first)!=lottery_of(second):
                # Keep the conflict visible. Never silently attach to either pool.
                out.append(lines[i]);i+=1;continue
            directive=compact(lines[j])
            if second:directive=directive[len(second):]
            # Distinct pools separated by whitespace are distinct purchases,
            # never one concatenated 8/9-digit pool (1358 0358 != 13580358).
            spaced=re.fullmatch(rf'(?:(?:{LOT})\s*)?([0-9]{{3,9}}(?:\s+[0-9]{{3,9}})+)\s*',
                                unicodedata.normalize('NFKC', lines[i]).strip(),re.I)
            if spaced:
                prefix=(second or first or '')
                out.append((prefix+'\n' if prefix else '')+' '.join(spaced[1].split())+'\n'+directive)
            else:
                out.append((second or first or '')+m['code']+directive)
            i=j+1
        else:
            out.append(lines[i]);i+=1
    return '\n'.join(out)


def scalar(raw):
    """Exact finite values only; the 4/1 shorthand is audited by parse_context."""
    if raw=='半':return Decimal('0.5')
    if re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?',raw):return Decimal(raw)
    if '/' in raw:
        a,b=map(int,raw.split('/'))
        if b==0:raise ValueError('分数分母不能为0')
        q=Fraction(a,b)
        if (q*100).denominator!=1:raise ValueError('倍数无法精确到两位小数，不四舍五入')
        return Decimal(q.numerator)/Decimal(q.denominator)
    if '分之' in raw:
        den,num=raw.split('分之')
        return scalar(str(cn_int(num))+'/'+str(cn_int(den)))
    return Decimal(cn_int(raw))


def cn_int(raw):
    digits={'零':0,'〇':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
    units={'十':10,'百':100,'千':1000,'万':10000}
    if raw in digits:return digits[raw]
    if raw and all(c in digits for c in raw):
        return int(''.join(str(digits[c]) for c in raw))
    total=section=number=0
    for c in raw:
        if c in digits:number=digits[c]
        elif c in units:
            n=units[c]
            if n==10000:total+=(section+number)*n;section=number=0
            else:section+=(number or 1)*n;number=0
        else:raise ValueError('数值格式不支持')
    return total+section+number


def _build(parser,lottery,raw,code,play,multiple):
    """Validate the declared size and use the existing configured play name."""
    aliases={'组6':'组六','组3':'组三','复试':'复式','单选':'直选','直':'直选',
             '单':'直选','组':'组选','毒胆':'独胆'}
    size=re.match(r'([二三四五六七八九2-9])码',play)
    if size:
        n=int(size[1]) if size[1].isdigit() else cn_int(size[1])
        if n!=len(code) or len(set(code))!=n:raise ValueError('声明码数与号码池不一致或数字重复')
        kind=aliases.get(play[size.end():],play[size.end():])
        name='二三四五六七八九'[n-2]+'码'+('复试' if kind=='复式' else kind)
        return [parser._make(lottery,raw,name,code,multiple)]
    kind=aliases.get(play,play)
    return parser._from_kind(lottery,raw,code,kind,multiple)


def _count_cash_alias(parser,lottery,original):
    """Canonicalize a complete N注 monetary summary; use existing block parser.

    This keeps V280 installations without the optional V278 patch compatible
    with the user's 微信 `福216注一块🈴216` order as well.
    """
    pattern=re.compile(rf'^(?P<lot>{LOT})?(?P<count>[0-9]+)注(?P<play>直选|单选|直|单)?(?P<value>{NUM})(?P<unit>元|米|块|毛|角|分)(?P<total>(?:合计|总计|合|共)[:：]?[0-9]+(?:\.[0-9]{{1,2}})?(?:元|米|块)?)?$',re.I)
    changed=False;lines=[]
    for line in original.replace('\r\n','\n').replace('\r','\n').split('\n'):
        m=pattern.fullmatch(compact(line))
        if m is None:
            lines.append(line);continue
        if re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?',m['value']) and m['unit'] in ('元','米','块'):
            lines.append(line);continue
        value=scalar(m['value'])/(10 if m['unit'] in ('毛','角') else 100 if m['unit']=='分' else 1)
        if value<=0 or value*100!=(value*100).to_integral_value():
            return [],parser.normalize_text(original),'单注金额须大于0并精确到分'
        lines.append((m['lot'] or '')+m['count']+'注'+(m['play'] or '')+
                     format(value.normalize(),'f')+'米'+(m['total'] or ''))
        changed=True
    if not changed:return None
    from local_blocks_v276 import parse_local_blocks
    result=parse_local_blocks(parser,lottery,'\n'.join(lines))
    if result is None:return None
    bets,_normalized,reason=result
    for b in bets:
        b.raw=original
        b.source_span={'start':0,'end':len(original),'text':original,
                      'granularity':'message','reference':'current_text',
                      'compatibility':'v281_count_cash_alias'}
    return bets,parser.normalize_text(original),reason


def parse_context(parser,lottery,original):
    """Additive recognizer; None = not our grammar (preserves legacy rules).

    Bare N after a single 4..9 digit 组三/组六 pool follows the installed
    GROUP_CASH business convention: cash, not N multiples. Explicit 倍 wins.
    Other untyped forms need a matching total; no order total is split into
    arbitrary per-number stakes.
    """
    if not isinstance(original,str) or not original.strip() or CANCEL.search(original):return None
    try:
        aliased=_count_cash_alias(parser,lottery,original)
    except (ValueError,ArithmeticError):
        aliased=None  # Unsupported alias remains visible to the original chain.
    if aliased is not None:return aliased
    normalized=normalize_layout(original)
    lines=[x for x in normalized.splitlines() if x.strip()]
    if not lines:return None
    parsed=[]
    for raw in lines:
        c=compact(raw).rstrip('。!！')
        total=TOTAL.search(c)
        claim=Decimal(total['cash']) if total else None
        body=c[:total.start()] if total else c
        m=CLAUSE.fullmatch(body)
        if m is None:return None
        # This layer handles explicit pricing or price-backed pools, not every
        # historical shorthand (leave e.g. 123直4 to its existing rule).
        is_pool=(4<=len(m['code'])<=9 and re.search(r'组[三六36]$',m['play']))
        if m['unit'] is None and claim is None and not is_pool:return None
        parsed.append((raw,m,claim,bool(is_pool)))
    bets=[];warnings=[];receipts=[];declared=[]
    try:
        for raw,m,claim,is_pool in parsed:
            if m['lot'] and lottery_of(m['lot'])!=lottery:raise ValueError('投注彩种与当前记录不一致')
            base=_build(parser,lottery,original,m['code'],m['play'],1)
            cost=sum((Decimal(str(b.principal)) for b in base),Decimal(0))
            if cost<=0:raise ValueError('玩法单倍本金无效')
            unit=m['unit']
            reversed_fraction=re.fullmatch(r'([0-9]+)/([0-9]+)',m['value'])
            if unit=='倍' and reversed_fraction and tuple(map(int,reversed_fraction.groups()))==(4,1):
                a,b=map(int,reversed_fraction.groups())
                if b==0 or claim is None:raise ValueError('疑似倒写分数倍，须有完整合计核验')
                choices={Decimal(a)/Decimal(b),Decimal(b)/Decimal(a)}
                choices=[v for v in choices if v*cost==claim and v*100==(v*100).to_integral_value()]
                if len(choices)!=1:raise ValueError('分数倍数与合计不能唯一核验')
                value=choices[0]
                warnings.append('分数倍按完整合计唯一核验；原文保留')
            else:value=scalar(m['value'])
            if not value.is_finite() or value<=0:raise ValueError('金额或倍数必须大于0')
            amount_mode=unit in ('元','米','块','毛','角','分')
            cash=value/(10 if unit in ('毛','角') else 100 if unit=='分' else 1)
            if unit=='倍':multiple=value
            elif amount_mode:multiple=cash/cost
            elif claim is not None:
                cash_ok=value==claim
                mult_ok=value*cost==claim
                if cash_ok and not mult_ok:multiple=value/cost;amount_mode=True;cash=value
                elif mult_ok and not cash_ok:multiple=value
                elif cash_ok and mult_ok:multiple=value
                else:raise ValueError('无单位金额与合计不能唯一核验；未改写投注')
            elif is_pool:
                multiple=value/cost;amount_mode=True;cash=value
                warnings.append('多码组选未写单位，沿用金额口径；按单倍本金回推倍数')
            else:return None
            if multiple*100!=(multiple*100).to_integral_value():raise ValueError('金额无法精确换算为两位小数倍数，不四舍五入')
            mult=parser._multiplier(format(multiple.normalize(),'f'))
            new=_build(parser,lottery,original,m['code'],m['play'],mult)
            due=sum((Decimal(str(b.principal)) for b in new),Decimal(0))
            if amount_mode and due!=cash:raise ValueError('金额回推复算不一致')
            if due*100!=(due*100).to_integral_value():raise ValueError('投注金额不是精确到分')
            if claim is not None and claim!=due:
                warnings.append(f'自报合计{claim:g}元，按明确投注应收{due:g}元；未修改倍数')
            for b in new:
                b.source_span={'start':0,'end':len(original),'text':original,
                    'granularity':'message','reference':'current_text','compatibility':'v281_context'}
                b.parse_source='local'
                if amount_mode:b.entered_amount_yuan=float(cash)
            bets.extend(new)
            declared.append(claim)
        parser.last_claimed=float(sum(declared)) if all(x is not None for x in declared) else None
        parser.last_warnings=warnings;parser.last_receipts=receipts
        return bets,parser.normalize_text(original),''
    except (ValueError,KeyError,ArithmeticError,InvalidOperation) as exc:
        parser.last_claimed=None;parser.last_warnings=[];parser.last_receipts=[]
        return [],parser.normalize_text(original),str(exc)
