"""V2.6.1: grammar-like recognition for local amount / multiplier clauses.

The recognizer is conservative: every token must be accounted for, and a bare
number may be interpreted as monetary amount only if a declared grand total
makes that interpretation unique. Otherwise the existing pipeline or AI handles
it. No wager is ever inserted merely to make a receipt total match.
"""
from __future__ import annotations
import itertools
import re
import unicodedata
from decimal import Decimal

from recognition_v260 import parse_v260
from recognition_v258 import _make, _mult, _strip_lot, _extract_total, dec, RecognitionError

MONEY = r'\d+(?:\.\d{1,2})?'
GROUP = r'(?:组六|组三|组6|组3)'
DIRECT = r'(?:直选|直|单)'
LOT = r'(?:福彩3[Dd]?|福彩|3[Dd]|体彩排列三|体彩|排列三|福|体|排)'
DELIM = r'[\s,，、。;；:：]+'


def _clean(s):
    s = unicodedata.normalize('NFKC', s).replace('\r\n', '\n').replace('\r', '\n')
    # Remove only clearly standalone colour prefixes, never edit number tokens.
    s = re.sub(r'(?m)^\s*' + LOT + r'\s*[:：。,.，、]?\s*(?=\d|组|直|单)', '', s)
    return s.strip(' \t\n,，。:：;；')


def _price(bets):
    return sum((dec(b.principal) for b in bets), Decimal('0'))


def _make_amount(p,lot,raw,code,kind,value,mode):
    # This also correctly computes 2-position/3-position and multi-code stake
    # bases from the actual configured odds instead of hardcoding 10 yuan.
    if not re.fullmatch(r'\d{3,9}', code):
        raise RecognitionError('选号格式无效')
    if kind in ('组三','组六') and len(code)==3:
        # An explicitly named group3/group6 on one concrete code must agree
        # with the number's actual repeat structure; no silent play switch.
        repeats=len(set(code))
        if (kind=='组三' and repeats!=2) or (kind=='组六' and repeats!=3):
            raise RecognitionError('具体组选号码与原文组别不符')
    if kind=='直选' and len(code)!=3:
        raise RecognitionError('直选必须提供三位选号')
    if kind in ('组三','组六') and len(code)>3 and len(set(code))!=len(code):
        raise RecognitionError('多码选号存在重复数字')
    base=_price(_make(p,lot,raw,code,kind,1))
    if not base:
        raise RecognitionError('缺少合法玩法单倍本金')
    value=dec(value)
    mul=value / base if mode=='amount' else value
    if mul<=0 or mul.as_tuple().exponent<-2:
        raise RecognitionError('金额无法换算为有效投注倍数')
    return _make(p,lot,raw,code,kind,_mult(p,format(mul,'f')))


def _split_and_parse(p,lot,raw,body,claimed):
    """Parse one or more independently priced clauses and solve ambiguous bare
    numeric markers by checking the *whole* receipt. All options must preserve
    the same literal selections and plays.
    """
    # '。' is either a separator or punctuation; decimal points are untouched.
    body=re.sub(r'(?<!\d)\.(?!\d)', '。', body)
    body=re.sub(r'(?<=\d)\.(?=\D)', '。', body)
    # Repeated same-lottery markers delimit independent monetary clauses.
    body=re.sub(r'[,，、]\s*(?='+LOT+r'\s*[:：]?\s*\d{3,9})','\\n',body)
    lines=[s.strip(' \t\n,，。:：;；') for s in re.split(r'[\n。；;]+',body) if s.strip(' \t\n,，。:：;；')]
    if not lines or len(lines)>100:return None
    # One three-digit item, one direct + one group, each explicitly one.
    # This is a sentence pattern, not an exception keyed to a particular number.
    one_both=(r'(?P<code>\d{3})\s*[,，、。;；:：]*\s*(?:一单一组|一直一组|1单一组|1单1组)'
              + r'(?:\s*[,，、。;；:：]*\s*(?P<receipt>' + MONEY + r')\s*[元米块])?')
    one_both=re.compile(one_both)
    # Interpreting the optional preceding lottery marker is handled by _clean.
    if len(lines)==1 or (len(lines)<=5 and not re.search(r'\d{4,9}|(?:直选|直|组)(?:\d+倍)', body)):
        whole=_clean(re.sub(r'[。；;]+',' ',body))
        m=one_both.fullmatch(whole)
        if m:
            bets=_make(p,lot,raw,m['code'],'直选',1)+_make(p,lot,raw,m['code'],'组选',1)
            if m['receipt'] is not None and _price(bets)!=dec(m['receipt']):
                return ([],p.normalize_text(raw),'一单一组的明确金额与单倍本金不符')
            if claimed is not None and _price(bets)!=claimed:
                return ([],p.normalize_text(raw),'原文合计与投注金额不一致，需复核')
            return _finish_all(p,lot,raw,bets,claimed)
    clauses=[]
    for line in lines:
        line=_clean(line)
        if not line:return None
        # 3-digit direct/group + paired explicit multipliers, with trailing
        # claimed subtotal (e.g. 186直1组1倍4). Bare subtotal must corroborate.
        paired=re.fullmatch(r'(?P<code>\d{3})\s*' + DIRECT +
            r'(?P<d>\d+(?:\.\d{1,2})?)\s*(?:倍)?\s*(?:组选|组)' +
            r'(?P<g>\d+(?:\.\d{1,2})?)\s*(?:倍)?\s*(?P<receipt>' + MONEY + r')?',line)
        if paired:
            bs=_make(p,lot,raw,paired['code'],'直选',_mult(p,paired['d']))
            bs+=_make(p,lot,raw,paired['code'],'组选',_mult(p,paired['g']))
            if paired['receipt'] is not None and dec(paired['receipt'])!=_price(bs):
                return ([],p.normalize_text(raw),'本行小计与一单一组倍数冲突')
            clauses.append([bs]);continue
        m=one_both.fullmatch(line)
        if m:
            bs=_make(p,lot,raw,m['code'],'直选',1)+_make(p,lot,raw,m['code'],'组选',1)
            if m['receipt'] and dec(m['receipt'])!=_price(bs):
                return ([],p.normalize_text(raw),'一单一组小计不符')
            clauses.append([bs]);continue
        # Either (1) code then a sequence of distinct play/amount clauses,
        # or (2) play then code then amount, separated by ordinary punctuation.
        # Prefixes like '五码' are descriptive and must match digit width.
        first=re.match(r'^(?P<code>\d{3,9})\s*(?P<tail>.*)$',line)
        if first:
            code=first['code'];tail=first['tail'].strip(' ,，:：')
        else:
            before=re.fullmatch(r'(?P<play>' + GROUP + r')\s*[,，:： ]*'
                                  r'(?P<code>\d{3,9})\s*[,，:： ]*'
                                  r'(?P<amount>' + MONEY + r')\s*(?P<unit>倍|元|米|块)?',line)
            if not before:return None
            code=before['code']
            tail=before['play']+before['amount']+(before['unit'] or '')
        # Optional size indicator is *not* a play. Match against the actual
        # code length rather than trusting an unverified declaration.
        size=re.match(r'^([二三四五六七八九2-9])\s*码\s*',tail)
        if size:
            sizes=dict(zip('二三四五六七八九',range(2,10)))
            if (sizes.get(size[1]) or int(size[1]))!=len(code):
                return None
            tail=tail[size.end():]
        # 组3/组6 aliases: regex alternatives are parsed as complete tokens.
        # Disallow arbitrary interspersed text, even if the final total matches.
        ops=[];pos=0
        op_re=re.compile(r'(?P<kind>' + GROUP + r'|'+DIRECT+r')\s*[,，:：、 ]*\s*'
                          r'(?P<num>' + MONEY + r')\s*(?P<unit>倍|元|米|块)?')
        while pos<len(tail):
            skip=re.match(r'\s*[,，:：、 ]*\s*',tail[pos:])
            pos+=skip.end()
            if pos>=len(tail):break
            match=op_re.match(tail,pos)
            if not match:return None
            kind=match['kind']
            kind='组三' if kind=='组3' else '组六' if kind=='组6' else '直选' if kind in ('单','直','直选') else kind
            ops.append((kind,match['num'],match['unit']))
            pos=match.end()
        if not ops:return None
        per_clause=[[]]
        for kind,num,unit in ops:
            modes=('amount',) if unit in ('元','米','块') else ('multiplier',) if unit=='倍' else ('amount','multiplier')
            choices=[]
            for mode in modes:
                try:choices.append(_make_amount(p,lot,raw,code,kind,num,mode))
                except (ValueError,KeyError,ArithmeticError,RecognitionError):continue
            if not choices:return None
            per_clause=[left+choice for left in per_clause for choice in choices]
        if len(per_clause)>16:return None
        clauses.append(per_clause)
    if not clauses:return None
    if len(clauses)>40:return None
    # Cartesian expansion is bounded; too many ambiguous clauses fall through
    # to the AI path rather than an exponential local search.
    possible_count=1
    for group in clauses:
        possible_count*=len(group)
        if possible_count>256:return None
    candidates=[]
    for parts in itertools.product(*clauses):
        bets=[bet for part in parts for bet in part]
        if claimed is None or _price(bets)==claimed:
            signature=tuple((b.play,b.selection,str(b.multiplier)) for b in bets)
            if signature not in {x[0] for x in candidates}:
                candidates.append((signature,bets))
    if len(candidates)!=1:
        # This grammar matched every token, but the meaning is not unique.
        # Returning a pending state prevents older templates from guessing.
        why=('金额或倍数单位缺失，存在多种合理解释，交给AI复核' if len(candidates)>1 else
             '按原文玩法和收费规则计算，与声明合计不一致，交给AI复核')
        return [],p.normalize_text(raw),why
    return _finish_all(p,lot,raw,candidates[0][1],claimed)


def _finish_all(p,lot,raw,bets,total):
    from recognition_v258 import _finish
    result=_finish(p,lot,raw,bets,total)
    return result


def parse_v261(p, lottery, text):
    if not isinstance(text,str) or not text.strip():return None
    raw=unicodedata.normalize('NFKC',text).replace('\r\n','\n').replace('\r','\n')
    if re.search(r'撤单|取消|更正|改成|不买',raw):return None
    try:
        base,claimed=_extract_total(raw.strip())
        cleaned=_clean(base)
        parsed=_split_and_parse(p,lottery,raw,cleaned,claimed)
        if parsed is not None:return parsed
    except (RecognitionError,ValueError,KeyError,ArithmeticError):
        # Let legacy parser/AI provide detailed error; do not silently
        # manufacture a full confirmed order from an ambiguous clause.
        pass
    return parse_v260(p,lottery,text)
