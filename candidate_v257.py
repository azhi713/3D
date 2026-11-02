"""V2.5.7: decode structured AI bet proposals using program-owned source spans.

Does not ask the old parser to parse the model's rewritten prose. BetParser._make
is used only for invariant price/selection validation.
"""
from __future__ import annotations
import json
from decimal import Decimal
from collections import Counter

from evidence_v257 import (EvidenceError, locate_evidence, verify_candidate_bet,
                           validate_full, _decimal)

_ALIASES={
 'DIRECT':'直选','直':'直选','单':'直选','单选':'直选',
 'GROUP3':'组三（组选）','GROUP6':'组六（组选）',
 'GROUP3_PERM':'组三复式','GROUP6_PERM':'组六复式',
 'SUM':'和值','SPAN':'跨度','DOUBLE_FLY':'双飞',
 '组六组选':'组六（组选）','组三组选':'组三（组选）',
 '组3':'组三（组选）','组6':'组六（组选）',
}


def decode_candidate_v257(candidate,parser,lottery,source):
    if not isinstance(candidate,str) or not candidate.lstrip().startswith('{'):
        return parser.parse(lottery,candidate)
    try:
        obj=json.loads(candidate)
        if not isinstance(obj,dict):raise EvidenceError('AI结果不是JSON对象')
        if obj.get('needs_review') is True or obj.get('uncertain') is True:
            raise EvidenceError('AI明确标注仍无法确认该投注')
        if isinstance(obj.get('uncertainties'),list) and obj['uncertainties']:
            raise EvidenceError('AI报告仍有未解释内容，不能直接确认')
        items=obj.get('candidate_bets')
        if not isinstance(items,list) or not items:
            raise EvidenceError('AI没有返回结构化投注')
        out=[]
        seen=Counter()
        for item in items:
            if not isinstance(item,dict):raise EvidenceError('AI候选必须是对象')
            target=item.get('lottery',lottery)
            if target not in (lottery,'FC3D' if lottery=='福彩' else 'PL3'):
                raise EvidenceError('AI候选彩种与原文冲突')
            kind=_ALIASES.get(item.get('bet_type') or item.get('play'),item.get('bet_type') or item.get('play'))
            if not isinstance(kind,str) or not kind:
                raise EvidenceError('AI没有明确投注玩法')
            codes=item.get('ticket_codes')
            if codes is None:
                selection=item.get('digit_set')
                if selection is None:selection=item.get('selection')
                if item.get('dan') is not None:
                    selection=str(item['dan'])+':'+str(item.get('tuo',''))
                    if kind in ('胆拖','胆拖组三','胆拖组六'):
                        kind='1胆'+str(len(str(item.get('tuo',''))))+'拖组'+('3' if kind=='胆拖组三' else '6')
                if item.get('position_sets') is not None:
                    positions=item['position_sets']
                    if not isinstance(positions,list) or len(positions)!=3:
                        raise EvidenceError('AI定位候选结构不完整')
                    selection='|'.join(label+':'+str(x) for label,x in zip('百十个',positions) if x not in (None,''))
                codes=[selection]
            if not isinstance(codes,list) or not codes or any(not isinstance(c,str) or not c for c in codes):
                raise EvidenceError('AI选号必须是保留前导零的字符串数组')
            if kind in ('和值','跨度'):
                codes=[str(int(c)) for c in codes]
            first=str(codes[0])
            start,end,region=locate_evidence(source,item,first)
            # A multi-digit group pool without explicit 复式/复试 is the
            # historical n码组 play, priced as one pool (not permutations).
            if (kind in ('组三复式','组六复式','组三（组选）','组六（组选）')
                and len(codes)==1 and codes[0].isdigit() and 4<=len(codes[0])<=9):
                explicit_permutation=bool(__import__('re').search(
                    r'(?:组[三六36]\s*(?:复式|复试)|(?:复式|复试)\s*组[三六36])',region))
                if not explicit_permutation:
                    zh={4:'四',5:'五',6:'六',7:'七',8:'八',9:'九'}[len(codes[0])]
                    kind=zh+'码'+('组三' if '组三' in kind else '组六')
            if kind in ('组三复式','组六复式') and len(codes)>1:
                raise EvidenceError('复式数字池必须作为单个digit_set，不可输出展开票数组')
            amount_value=item.get('declared_amount_yuan',item.get('amount'))
            multiplier_value=item.get('bet_multiples',item.get('multiple'))
            amount_kind=item.get('amount_kind')
            # An explicit multiplier wins only if the source really says that
            # multiple. The other amount is a cross-check, not a replacement.
            if multiplier_value is not None and item.get('multiple_type') != 'inferred':
                amount_kind='multiplier'
            elif amount_kind is None:
                amount_kind='amount' if amount_value is not None else 'multiplier'
            if amount_kind not in ('amount','multiplier'):
                raise EvidenceError('AI必须区分金额与倍数')
            value=amount_value if amount_kind=='amount' else multiplier_value
            if value is None:
                raise EvidenceError('AI没有提供对应金额或倍数')
            value=_decimal(value)
            tentative=[]
            for code in codes:
                play=kind+code if kind in ('和值','跨度') else kind
                tentative.append(parser._make(lottery,region,play,code,1))
            base=sum(Decimal(str(b.principal)) for b in tentative)
            if base<=0:raise EvidenceError('候选本金单价无效')
            mult=value/base if amount_kind=='amount' else value
            mult=parser._multiplier(format(mult,'f'))
            chunk=[parser._make(lottery,region,b.play,b.selection,mult) for b in tentative]
            needs_total=False
            for bet in chunk:
                needs_total=verify_candidate_bet(region,bet,amount_kind,value if amount_kind=='amount' else None) or needs_total
                bet.source_span={'start':start,'end':end,'text':region,
                                 'position_verified':True,'needs_total_for_amount':needs_total,
                                 'segment_id':item.get('segment_id')}
                bet.parse_source='ai_evidence_verified'
                if amount_value is not None and len(chunk)==1:
                    declared=_decimal(amount_value)
                    if amount_kind=='multiplier' and Decimal(str(bet.principal)) != declared:
                        # Explicit source multiples win; report the differing
                        # receipt but never rewrite that purchase silently.
                        bet.settlement_rule_pending.append(
                            '金额与明确倍数不一致：按明确倍数计算，应收'+str(bet.principal)+'元，原文金额'+str(declared)+'元')
                    bet.entered_amount_yuan=float(declared)
                # repeated same play/selection/multiple within the exact same source
                # span is allowed only if the source lists its number more than once.
                key=(start,end,bet.play,bet.selection,str(Decimal(str(bet.multiplier)).normalize()))
                seen[key]+=1
            out+=chunk
        ok,why=validate_full(source,out,allow_unresolved=True)
        # A candidate may address only one unresolved portion: the controller
        # completes the check against already locally confirmed bets.
        if not ok and not (why.startswith('原文合计') and 'AI解释' in why and '无法确定无单位金额含义' in why):
            raise EvidenceError(why)
        return out,candidate,''
    except (EvidenceError,ValueError,TypeError,KeyError,ArithmeticError) as exc:
        return [],candidate,str(exc)
