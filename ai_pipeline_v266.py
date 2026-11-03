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
             'DOUBLE_FLY':'双飞', '飞':'双飞', '组3':'组三（组选）',
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
    if '十' in fragment:
        for suffix in ('单','直') if play=='直选' else ('组','组选') if '组选' in play else ():
            for m in re.finditer(r'([一二两三四五六七八九十]+)'+suffix, fragment):
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
        mult=_number(amount,'金额')/cost
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
            if not _has_local_multiplier(region,b.play,Decimal(str(mult))):
                raise EvidenceError('倍数没有原文证据：'+str(mult))
        else:
            mention=_money_evidence(region,_number(amount,'金额'),b.play)
            if not mention:raise EvidenceError('金额没有原文证据：'+str(amount))
            # Converting a bare value into a multiple requires a complete total.
            # A numerical "1直1组" does NOT establish a monetary per-bet price.
            if mention=='needs_total' and message_total(source) is None:
                raise EvidenceError('无单位金额需要整单合计佐证')
        b.source_span={'start':start,'end':end,'text':region,
                       'position_verified':True, 'segment_id':item.get('segment_id')}
        b.parse_source='ai_evidence_verified'
    if mode=='multiplier' and amount is not None:
        stated=_number(amount,'金额')
        if len(bets)==1:
            bets[0].entered_amount_yuan=float(stated)
            if cents(stated)!=cents(bets[0].principal):
                bets[0].settlement_rule_pending.append('自报单笔金额与明确倍数计算不一致')
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
        normalized=normalize_candidate(source,candidate)
        payload=json.loads(normalized)
        items=payload.get('candidate_bets')
        if not isinstance(items,list):raise EvidenceError('没有 candidate_bets 数组')
    except (ValueError,TypeError,KeyError,EvidenceError) as exc:
        return [], (), ('AI结构化JSON无效：'+str(exc),), True
    good=[];errors=[]
    for i,item in enumerate(items):
        try:
            bets=_one_candidate(item,parser,lottery,source)
            good.extend(bets)
        except (EvidenceError,ValueError,KeyError,TypeError,ArithmeticError) as exc:
            errors.append(f'第{i+1}项：{exc}')
    if good:
        ok,why=validate_full(source,good,allow_unresolved=True)
        if not ok:
            # Partial candidate validation must not silently approve an unsafe
            # ledger; fail closed, retain errors for display.
            errors.append('证据总复核：'+why)
            good=[]
    needs_review=payload.get('needs_review') is True or bool(payload.get('uncertainties'))
    if isinstance(payload.get('uncertainties'),list):
        errors.extend(str(x)[:120] for x in payload['uncertainties'][:5])
    return good,tuple(items),tuple(errors),needs_review


def _key(b):
    return b.play,b.selection,str(Decimal(str(b.multiplier)).normalize())


def _combine(local, ai):
    """Use source overlapping + ledger identity; do not discard repeat purchases."""
    result=list(local)
    matched=set()
    added=[]
    for item in ai:
        span=item.source_span or {}
        skip=False
        for i,old in enumerate(local):
            if i in matched or _key(item)!=_key(old):continue
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
        if grand is not None and full_due!=cents(grand):
            complete=False;missing='程序计算金额与自报合计不一致，需确认投注或金额'
        if claimed is not None and full_due!=cents(claimed):
            complete=False;missing='原始自报金额与AI计算金额不符'
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
    combined,additions=_combine(local,ai)
    unresolved=list(pending)
    # When all source bets are covered by combined ledger, clear original parser
    # errors even if original syntax could not be parsed. Never clear due to the
    # mere presence of an AI response.
    filled,why=_observed(source,combined)
    grand=message_total(source)
    if filled and not rejected and not model_uncertain and (
        grand is None or sum(cents(b.principal) for b in combined)==cents(grand)
    ):
        unresolved=[]
    elif filled and grand is not None and sum(cents(b.principal) for b in combined)!=cents(grand):
        missing='自报合计与结构化投注复算不一致'
    if not unresolved and not filled:
        unresolved=list(pending)
    detail='；'.join(list(rejected)[:6]+([missing] if missing else []))
    return AIResult(combined,unresolved,len(additions),rejected,bool(not unresolved),detail)
