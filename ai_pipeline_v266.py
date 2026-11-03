# -*- coding: utf-8 -*-
"""V2.6.6: independently validate AI structure against *source*, not grammar parse.

This module never calls BetParser.parse/parse_partial, and never trusts model
prices.  Parser._make is used only to enforce configured play/selection and
pricing invariants.  A rejected candidate cannot become a bet in the ledger.
"""
import json
import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from evidence_v257 import (EvidenceError, source_segments, locate_evidence,
                           _selection_ok, _play_ok, _multiplier_mentions,
                           _amount_mentions, _play_amount_token, declared_total,
                           validate_full)
from candidate_v261 import normalize_candidate
from models import cents


def message_total(source):
    """Explicit whole-message cash totals only; also support colloquial 合1040米."""
    found=declared_total(source)
    if found is not None:return found
    tail=re.search(r'(?:^|[\s，,;；。])(?:合计|总计|合|共)\s*[:：]?\s*(\d+(?:\.\d{1,2})?)\s*[元米块]?\s*$',source)
    return Decimal(tail[1]) if tail else None


@dataclass
class AIResult:
    bets: list
    pending: list
    accepted: int
    rejected: tuple
    complete: bool
    detail: str = ''


def _number(value, name):
    if type(value) is bool or value is None:
        raise EvidenceError(name + '必须为明确正数')
    try:
        result = Decimal(str(value))
    except (ValueError, ArithmeticError, TypeError):
        raise EvidenceError(name + '格式无效') from None
    if not result.is_finite() or result <= 0 or result.as_tuple().exponent < -2:
        raise EvidenceError(name + '必须为有效正数，最多两位小数')
    return result


def _kind(kind, codes, fragment):
    names = {'DIRECT':'直选', '直':'直选', '单':'直选', '单选':'直选',
             'GROUP3':'组三（组选）', 'GROUP6':'组六（组选）',
             'GROUP3_PERM':'组三复式', 'GROUP6_PERM':'组六复式',
             'DOUBLE_FLY':'双飞', '组3':'组三（组选）',
             '组6':'组六（组选）', '组三组选':'组三（组选）',
             '组六组选':'组六（组选）'}
    kind = names.get(kind, kind)
    if not isinstance(kind,str) or not kind.strip():
        raise EvidenceError('缺少投注玩法')
    if kind in ('具体组选','组选','组'):
        if len(codes)!=1 or len(codes[0])!=3:
            raise EvidenceError('组选候选必须逐组三/组六区分')
        kind=('组三（组选）' if len(set(codes[0]))==2 else
              '组六（组选）' if len(set(codes[0]))==3 else None)
        if not kind:raise EvidenceError('豹子不能自动转换为组选')
    if kind in ('组三复式','组六复式','组三（组选）','组六（组选）') and len(codes)==1:
        digits=codes[0]
        if digits.isascii() and digits.isdigit() and 4<=len(digits)<=9:
            if not re.search(r'(?:组[三六36]\s*(?:复式|复试)|(?:复式|复试)\s*组[三六36])',fragment):
                size='二三四五六七八九'[len(digits)-2]
                kind=size+'码'+('组三' if '组三' in kind else '组六')
    return kind


def _codes(item):
    codes=item.get('ticket_codes')
    if codes is None:
        positions=item.get('position_sets')
        if isinstance(positions,dict):
            normalized={}
            for k,v in positions.items():
                label={'百':'百','百位':'百','十':'十','十位':'十','个':'个','个位':'个'}.get(k)
                if not label or label in normalized:raise EvidenceError('定位位置重复或无效')
                normalized[label]=v
            positions=[normalized.get(x) for x in '百十个']
        if positions is not None:
            if not isinstance(positions,list) or len(positions)!=3:
                raise EvidenceError('定位字段应为百十个三位置')
            selection='|'.join(label+':'+str(v) for label,v in zip('百十个',positions) if v is not None and v!='')
            codes=[selection]
        elif item.get('dan') is not None:
            codes=[str(item['dan'])+':'+str(item.get('tuo',''))]
        else:
            codes=[item.get('digit_set',item.get('selection'))]
    if not isinstance(codes,list) or not codes or not all(isinstance(c,str) and c for c in codes):
        raise EvidenceError('选号必须是保留前导零的字符串')
    return codes


def _has_local_multiplier(fragment,play,multiple):
    """Accept explicit 中文 N单/N组 without relying on the old parser grammar."""
    if _multiplier_mentions(fragment,multiple,play):return True
    if multiple != multiple.to_integral_value():return False
    words={'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,
           '七':7,'八':8,'九':9,'十':10}
    for suffix in ('单','直') if play=='直选' else ('组','组选') if '组选' in play else ():
        for m in re.finditer(r'([一二两三四五六七八九十]+)\s*'+suffix, fragment):
            if words.get(m[1])==int(multiple):return True
        if suffix in ('组','组选'):
            for m in re.finditer(suffix+r'\s*([一二两三四五六七八九十]+)', fragment):
                if words.get(m[1])==int(multiple):return True
    return False


def _money_evidence(fragment, amount, play):
    if _amount_mentions(fragment,amount,play):return True
    # An explicit Chinese money unit immediately qualifying a play is still
    # cash evidence: “一米直一米组”, never an inferred multiple.
    chinese={'半':Decimal('0.5'),'一':Decimal('1'),'二':Decimal('2'),
             '两':Decimal('2'),'三':Decimal('3'),'四':Decimal('4'),
             '五':Decimal('5'),'十':Decimal('10')}
    label=(r'(?:直选|直|单)' if play=='直选' else
           r'(?:组选|组[三六36]?)' if ('组选' in play or '码组' in play) else None)
    if label:
        for m in re.finditer(r'([半一二两三四五十])\s*[元米块]\s*'+label,fragment):
            if chinese[m[1]]==amount:return True
    # Double-fly and numeric group suffixes may write e.g. “双飞20合计80”.
    # The untyped value must also match a whole-message receipt before use.
    value=format(amount,'f').rstrip('0').rstrip('.') if '.' in format(amount,'f') else str(amount)
    words = r'双飞|飞' if play=='双飞' else r'组六|组三|组选|组|直|单'
    if re.search(r'(?:'+words+r')\s*'+re.escape(value)+r'(?![\d.]|\s*倍)',fragment):
        return 'needs_total'
    return False



# V267: business-confirmed per-ticket cash evidence, candidate-local normalization.
def _v267_per_ticket_amount(fragment, play):
    """Return explicitly priced Yuan per wager (never a multiplier).

    '一米直一米组' is two independent unit-price declarations.
    Does not borrow one play's amount to price another play.
    """
    text = fragment
    words={'半':Decimal('0.5'),'一':Decimal('1'),'二':Decimal('2'),
           '两':Decimal('2'),'三':Decimal('3'),'四':Decimal('4'),
           '五':Decimal('5'),'六':Decimal('6'),'七':Decimal('7'),
           '八':Decimal('8'),'九':Decimal('9'),'十':Decimal('10')}
    family = (r'(?:直选|直|单)' if play=='直选' else
              r'(?:组选|组[三六36]?)' if (
                  play in ('组三（组选）','组六（组选）')) else None)
    if family is None:return None
    hits=list(re.finditer(r'(?P<n>半|一|二|两|三|四|五|六|七|八|九|十|\d+(?:\.\d{1,2})?)'
                         r'\s*[米元块]\s*'+family, text))
    if not hits:return None
    values=[]
    for match in hits:
        n=match.group('n')
        values.append(words[n] if n in words else Decimal(n))
    return values[0] if values and len(set(values))==1 else None


def _v267_cash_bound_to_other_code(fragment, play, codes, amount):
    """Do not borrow '1369组六30' to price preceding '369四单一组'."""
    family = ('直选','直','单','组六','组三','组选','组')
    expression=(r'(?<!\d)(?P<selection>\d{3,9})\s*'
                r'(?P<play>直选|直|单|组六|组三|组选|组)\s*'
                r'(?P<price>\d+(?:\.\d{1,2})?)\s*(?:元|米|块)?(?!\d|\s*倍)')
    matched=[]
    for m in re.finditer(expression,fragment):
        try:
            if Decimal(m.group('price'))==amount:matched.append(m)
        except ArithmeticError:
            continue
    # Only act when the amount is explicitly bound to a code-play pair.
    # A group 1369组六30 cannot be assigned to 369直选 or 369组选.
    if matched and not any(m.group('selection') in codes and
        (('直' in m.group('play') or m.group('play')=='单') == (play=='直选'))
        for m in matched):
        return True
    return False


def _v267_normalize_input(source, raw):
    """Normalize candidates independently. One malformed row cannot reject all."""
    payload=json.loads(raw)
    if not isinstance(payload,dict) or not isinstance(payload.get('candidate_bets'),list):
        raise EvidenceError('没有 candidate_bets 数组')
    good=[]; errors=[]
    pos={'百':'百','百位':'百','hundred':'百','hundreds':'百',
         '十':'十','十位':'十','ten':'十','tens':'十',
         '个':'个','个位':'个','unit':'个','units':'个','ones':'个'}
    for index,rawitem in enumerate(payload['candidate_bets'],1):
        try:
            if not isinstance(rawitem,dict):
                raise EvidenceError('候选不是对象')
            item=dict(rawitem)
            positions=item.get('position_sets')
            if isinstance(positions,dict):
                mapped={}
                for label,value in positions.items():
                    k=pos.get(label)
                    if not k or k in mapped:raise EvidenceError('定位位置重复或未知')
                    if value is None or value=='':continue
                    if isinstance(value,list):
                        if not value or not all(isinstance(v,(str,int)) and
                             len(str(v))==1 and str(v).isascii() and str(v).isdigit()
                             for v in value):
                            raise EvidenceError('定位列表必须是一位数字数组')
                        value=''.join(str(v) for v in value)
                    elif type(value) is int:
                        value=str(value)
                    elif not isinstance(value,str):
                        raise EvidenceError('定位号码格式无效')
                    if not value or not value.isascii() or not value.isdigit():
                        raise EvidenceError('定位号码格式无效')
                    mapped[k]=value
                item['position_sets']=mapped
            normalized=normalize_candidate(source,json.dumps(
                {'candidate_bets':[item]},ensure_ascii=False))
            group=json.loads(normalized)['candidate_bets']
            good.extend(group)
        except (ValueError,TypeError,KeyError,ArithmeticError,EvidenceError) as exc:
            errors.append(f'第{index}项字段规范化失败：{exc}')
    return payload,good,errors


def _v267_mentions_count(source,code):
    return len(re.findall(r'(?<!\d)'+re.escape(str(code))+r'(?!\d)',source))


def _one_candidate(item, parser, lottery, source):
    if not isinstance(item,dict):raise EvidenceError('候选必须是JSON对象')
    target=item.get('lottery',lottery)
    target={'福':'福彩','3D':'福彩','3d':'福彩','福彩3D':'福彩','FC3D':'福彩',
            '体':'体彩','排':'体彩','排列三':'体彩','PL3':'体彩'}.get(target,target)
    if target!=lottery:
        raise EvidenceError('候选彩种与记录不符')
    codes=_codes(item)
    first=codes[0] if len(codes[0])==3 and codes[0].isdigit() else ''
    start,end,region=locate_evidence(source,item,first)
    play=_kind(item.get('bet_type') or item.get('play'),codes,region)
    # Source-wide count evidence can legitimately cover preceding lines.
    # Do not permit this override when the previous ticket is absent.
    from context_receipts_v271 import (parse_context_order,
                                        contextual_bet_evidence,
                                        play_bound_cash_evidence)
    contextual=parse_context_order(parser,lottery,source)
    matched_count=re.search(r'(?:直选|直|单)\s*(\d+)\s*注',region)
    if (matched_count and all(len(code)==3 and code.isdigit() for code in codes)
            and len(codes)<int(matched_count.group(1))):
        supported=(contextual is not None and
                   len(contextual[0])==int(matched_count.group(1)) and
                   all(any(b.play==play and b.selection==code for b in contextual[0])
                       for code in codes))
        if not supported:
            raise EvidenceError('声明'+matched_count.group(1)+'注，原文全单未证明足够号码；不编造新注')
    # The colloquial '飞' is a valid double-fly play when the source gives
    # an exact two-digit selection and cash can be priced by runtime rules.
    if play=='双飞' and (not re.search(r'双飞|飞',region)
                        or not all(len(c)==2 and c.isdigit() for c in codes)):
        raise EvidenceError('双飞选号或玩法不完整，不能凭空补全')
    if play in ('和值','跨度'):
        codes=[str(int(code)) for code in codes]
    amount=item.get('declared_amount_yuan',item.get('amount'))
    multiple=item.get('bet_multiples',item.get('multiple'))
    mode=item.get('amount_kind')
    if mode not in (None,'multiplier','amount'):raise EvidenceError('金额类型无效')
    if multiple is not None and item.get('multiple_type')!='inferred':
        mode='multiplier'
    elif mode is None:mode='amount' if amount is not None else 'multiplier'
    if multiple is None and amount is None:
        raise EvidenceError('缺少金额和倍数')
    base=[]
    for code in codes:
        this_play=play+code if play in ('和值','跨度') else play
        base.append(parser._make(lottery,region,this_play,code,1))
    cost=sum((Decimal(str(b.principal)) for b in base),Decimal(0))
    if cost<=0:raise EvidenceError('玩法单倍本金无效')
    if mode=='multiplier':
        if multiple is None:raise EvidenceError('倍数不能为空')
        mult=_number(multiple,'倍数')
    else:
        if amount is None:raise EvidenceError('金额不能为空')
        stated=_number(amount,'金额')
        per_ticket=_v267_per_ticket_amount(region,play)
        if per_ticket is not None:
            computed=per_ticket*len(codes)
            if stated!=computed:
                raise EvidenceError('每笔金额与本组号码数量不符：应为'+str(computed)+'元')
        elif _v267_cash_bound_to_other_code(region,play,codes,stated):
            raise EvidenceError('该金额属于原文另一组选号，不能跨组分配')
        mult=stated/cost
    mult=parser._multiplier(format(mult,'f'))
    bets=[parser._make(lottery,region,b.play,b.selection,mult) for b in base]
    for b in bets:
        if not _selection_ok(region,b):
            raise EvidenceError('号码或定位未在对应原文段落出现：'+b.selection)
        if not _play_ok(region,b):
            raise EvidenceError('玩法在对应原文段落没有依据：'+b.play)
        if mode=='multiplier':
            if (not re.search(r'(?:\d+(?:\.\d+)?|[一二两三四五六七八九十])\s*倍',region)
                and re.search(r'(?:\d+(?:\.\d+)?|[一二两三四五六七八九十半])\s*[元米块]',region)
                and not re.search(r'(?:[一二两三四五六七八九十\d]+)\s*(?:单|直|组)(?!\s*[元米块])',region)):
                raise EvidenceError('原文是金额表达，不能直接当作倍数')
            if (not re.search(r'(?:\d+(?:\.\d+)?|[一二两三四五六七八九十])\s*倍',region)
                and re.search(r'(?:直选|直|组[三六36]?|组选|双飞|飞)\s*各\s*\d+(?!\s*[倍元米块])',region)
                and message_total(source) is None):
                raise EvidenceError('无单位“各N”必须结合独立金额证据核实')
            if (not _has_local_multiplier(region,b.play,Decimal(str(mult)))
                and not contextual_bet_evidence(parser,lottery,source,b)):
                raise EvidenceError('倍数及整条上下文均没有原文证据：'+str(mult))
        else:
            unit=_v267_per_ticket_amount(region,b.play)
            mention=(unit is not None and unit*len(codes)==_number(amount,'金额'))
            if not mention:
                if _v267_cash_bound_to_other_code(region,b.play,codes,_number(amount,'金额')):
                    raise EvidenceError('金额属于其他原文选号，不能跨组取用：'+str(amount))
                mention=_money_evidence(region,_number(amount,'金额'),b.play)
            if not mention:raise EvidenceError('金额没有原文证据：'+str(amount))
            # Converting a bare value into a multiple requires a complete total.
            # A numerical "1直1组" does NOT establish a monetary per-bet price.
            if (mention=='needs_total' and message_total(source) is None
                and not contextual_bet_evidence(parser,lottery,source,b)
                and not play_bound_cash_evidence(region,b,amount)):
                raise EvidenceError('无单位金额需要完整原文或玩法单倍本金佐证')
        b.source_span={'start':start,'end':end,'text':region,
                       'position_verified':True, 'segment_id':item.get('segment_id')}
        if mode=='amount':
            unit=_v267_per_ticket_amount(region,b.play)
            if unit is not None and unit*len(codes)==_number(amount,'金额'):
                b.source_span['amount_evidence']='per_ticket_unit_price'
                b.source_span['unit_amount_yuan']=float(unit)
                b.source_span['derived_group_amount_yuan']=float(_number(amount,'金额'))
        b.parse_source='ai_evidence_verified'
    if mode=='multiplier' and amount is not None:
        stated=_number(amount,'金额')
        if len(bets)==1:
            bets[0].entered_amount_yuan=float(stated)
            if cents(stated)!=cents(bets[0].principal):
                bets[0].source_span['customer_amount_discrepancy']={
                    'declared':float(stated),'computed':bets[0].principal}
                # The original multiplier is authoritative; a customer's
                # wrong arithmetic is not a pending settlement rule.
    return bets


def _observed(source, bets):
    """Conservative independent completeness checks; never reparses source."""
    if not bets:return False,'未获取可信投注'
    tokens=[]
    for m in re.finditer(r'(?<!\d)\d{3}(?!\d)',source):
        if re.match(r'\s*注',source[m.end():]):continue
        if re.search(r'(?:合计|总计|共)\s*[:：]?\s*$',source[max(0,m.start()-8):m.start()]):continue
        tokens.append(m.group())
    present=Counter(b.selection for b in bets if len(b.selection)==3 and b.selection.isascii() and b.selection.isdigit())
    expected=Counter(tokens)
    for code,count in expected.items():
        if present[code]<count:
            return False,'仍有未解释的三位号码：'+code
    # Position declarations require their source-position span to be covered.
    for m in re.finditer(r'([百十个])(?:位)?\s*[:：]?\s*(\d+)',source):
        if not any(b.play.endswith('码定位') and
                   b.source_span.get('start',len(source))<=m.start()<b.source_span.get('end',0) and
                   (m[1]+':'+m[2]) in b.selection.split('|') for b in bets):
            return False,'定位数字仍有未解释部分：'+m.group()
    if '双飞' in source and not any(b.play=='双飞' for b in bets):
        return False,'双飞玩法仍未解释'
    # Requiring at least the explicitly named play family prevents a direct-only
    # AI answer from silently swallowing a source saying '直1组1'.
    if re.search(r'直选|直|单',source) and not any(b.play=='直选' for b in bets):
        return False,'原文的直选玩法未解释'
    if re.search(r'组选|组[三六36]?|一组',source) and not any(
        b.play in ('组三（组选）','组六（组选）') or b.play.endswith(('码组三','码组六'))
        for b in bets):
        return False,'原文的组选玩法未解释'
    normal=re.sub(r'[。.,，、;/\-]+',' ',source)
    mixed=re.search(r'(?<!\d)(?P<codes>\d{3}(?:\s+\d{3})+)\s*'
                    r'(?:[一二两三四五六七八九十\d]+\s*[元米块])?\s*'
                    r'(?:直选|直|单)\s*(?:[一二两三四五六七八九十\d]+\s*[倍元米块]?)?'
                    r'\s*(?:和|与|及)?\s*(?:组选|组)',normal)
    if mixed:
        for code in mixed.group('codes').split():
            if not any(b.selection==code and b.play=='直选' for b in bets):
                return False,'混合玩法中直选遗漏号码：'+code
            if not any(b.selection==code and b.play in ('组三（组选）','组六（组选）') for b in bets):
                return False,'混合玩法中组选遗漏号码：'+code
    if not expected and not re.search(r'[百十个](?:位)?\s*[:：]?\s*\d',source) and not re.search(r'双飞|飞',source):
        return False,'无法证明整单投注覆盖完整'
    return True,''


def decode_structured(source, candidate, parser, lottery):
    try:
        payload,items,errors=_v267_normalize_input(source,candidate)
    except (ValueError,TypeError,KeyError,EvidenceError) as exc:
        return [], (), ('AI结构化JSON无效：'+str(exc),), True
    good=[]
    # Preserve atomic candidate boundaries for conservative recovery.
    groups=[]
    # Check the source's number of identical purchases; do not double-book
    # AI-proposed rows merely because they share a legal ticket and play.
    used=Counter()
    for i,item in enumerate(items):
        try:
            bets=_one_candidate(item,parser,lottery,source)
            accepted=[]
            for b in bets:
                key=(b.play,b.selection,str(Decimal(str(b.multiplier)).normalize()))
                if (b.selection.isascii() and b.selection.isdigit()
                        and _v267_mentions_count(source,b.selection)<used[key]+1):
                    raise EvidenceError('同一玩法号码出现次数不足，疑似AI重复生成')
                accepted.append(b)
            good.extend(accepted)
            groups.append((i+1, accepted))
            for b in accepted:
                used[(b.play,b.selection,str(Decimal(str(b.multiplier)).normalize()))]+=1
        except (EvidenceError,ValueError,KeyError,TypeError,ArithmeticError) as exc:
            errors.append(f'第{i+1}项：{exc}')
    if good:
        ok,why=validate_full(source,good,allow_unresolved=True)
        if not ok:
            # Do not let a corrupt group erase independently verified
            # groups from other source spans. A group that depends on missing
            # context or conflicting totals stays pending, never auto-booked.
            errors.append('证据总复核：'+why)
            recovered=[]
            for original_index, group in groups:
                independent_ok, independent_reason=validate_full(
                    source, recovered+group, allow_unresolved=True)
                if independent_ok:
                    recovered.extend(group)
                else:
                    errors.append(
                        f'第{original_index}项隔离待核对：{independent_reason}')
            good=recovered
    needs_review=payload.get('needs_review') is True or bool(payload.get('uncertainties'))
    if isinstance(payload.get('uncertainties'),list):
        errors.extend(str(x)[:120] for x in payload['uncertainties'][:5])
    return good,tuple(items),tuple(errors),needs_review


def _key(b):
    return b.play,b.selection,str(Decimal(str(b.multiplier)).normalize())


def _combine(local, ai, source):
    """Use source overlapping + ledger identity; do not discard repeat purchases."""
    result=list(local)
    matched=set()
    added=[]
    for item in ai:
        span=item.source_span or {}
        skip=False
        for i,old in enumerate(local):
            if i in matched or _key(item)!=_key(old):continue
            # When a ticket appears only once in the source, local and AI
            # cannot both book it as two purchases irrespective of spans.
            if _v267_mentions_count(source,item.selection)==1:
                matched.add(i);skip=True;break
            other=old.source_span or {}
            a,b=span.get('start'),span.get('end')
            c,d=other.get('start'),other.get('end')
            if all(type(v) is int for v in (a,b,c,d)) and a<d and c<b:
                matched.add(i);skip=True;break
        if not skip:
            result.append(item);added.append(item)
    return result,added


def reconcile(source, raw_candidate, parser, lottery, local, pending, claimed=None):
    """Result for controller: accepted bets + independent unresolved fragments.

    Whole-ledger replacement is allowed only with full original coverage and
    declared total agreeing, otherwise strong local records are preserved.
    """
    if not isinstance(raw_candidate,str) or not raw_candidate.lstrip().startswith('{'):
        return AIResult(list(local),list(pending),0,('AI必须返回结构化JSON，不能用改写文本直接入账',),False)
    # V271: AI candidates must still be independently checked against source.
    # Whole-message deterministic patterns are handled before AI in parser.py.
    ai,items,rejected,model_uncertain=decode_structured(source,raw_candidate,parser,lottery)
    if not ai:
        return AIResult(list(local),list(pending),0,rejected or ('AI未返回可验证投注',),False,'；'.join(rejected) if rejected else 'AI未返回可验证投注')
    complete,missing=_observed(source,ai)
    if rejected or model_uncertain:
        complete=False
        if not missing:missing='AI仍有未通过验证的候选或已声明存在歧义'
    if complete:
        full_due=sum(cents(b.principal) for b in ai)
        grand=message_total(source)
        from context_receipts_v271 import strong_receipt_evidence
        receipt_typo=strong_receipt_evidence(source,ai)
        if grand is not None and full_due!=cents(grand) and not receipt_typo:
            complete=False;missing='程序计算金额与自报合计不一致，且投注尚未独立证实'
        if claimed is not None and full_due!=cents(claimed) and not receipt_typo:
            complete=False;missing='原始自报金额与AI计算金额不符，尚缺独立投注证据'
    # The AI must not rewrite an independently verified local purchase. A
    # complete source-backed AI ledger may replace only a tentative parse with
    # an exact matching whole-message total. Otherwise preserve local bets.
    local_keys=Counter(_key(b) for b in local)
    ai_keys=Counter(_key(b) for b in ai)
    if complete and (not local or all(ai_keys[k]>=n for k,n in local_keys.items())):
        return AIResult(ai,[],len(ai),(),True,'完整原文证据已验证')
    if complete and local and message_total(source) is not None and (
        sum(cents(b.principal) for b in local) != cents(message_total(source))
        or any(x.get('code') in ('amount_mismatch','line_amount_mismatch') for x in pending)
    ):
        return AIResult(ai,[],len(ai),(),True,'完整证据及整单金额覆盖暂算结果')
    combined,additions=_combine(local,ai,source)
    unresolved=list(pending)
    # When all source bets are covered by combined ledger, clear original parser
    # errors even if original syntax could not be parsed. Never clear due to the
    # mere presence of an AI response.
    filled,why=_observed(source,combined)
    grand=message_total(source)
    from context_receipts_v271 import strong_receipt_evidence
    if filled and not rejected and not model_uncertain and (
        grand is None or sum(cents(b.principal) for b in combined)==cents(grand)
        or strong_receipt_evidence(source,combined)
    ):
        unresolved=[]
    elif filled and grand is not None and sum(cents(b.principal) for b in combined)!=cents(grand):
        missing='自报合计与结构化投注复算不一致'
    if not unresolved and not filled:
        unresolved=list(pending)
    detail='；'.join(list(rejected)[:6]+([missing] if missing else []))
    return AIResult(combined,unresolved,len(additions),rejected,bool(not unresolved),detail)

# V267-AI-LEDGER-20261103
