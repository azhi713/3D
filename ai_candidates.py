"""AI只提交带原文证据的候选；金额和号码由本地校验、定价。"""
import json,re
from decimal import Decimal

def decode_candidate(candidate,parser,lottery,source):
    if not isinstance(candidate,str) or not candidate.lstrip().startswith('{'):
        bets,normalized,error=parser.parse(lottery,candidate)
        return bets,normalized,error
    try:
        obj=json.loads(candidate)
        if not isinstance(obj.get('candidate_bets'),list) or not obj['candidate_bets']:
            raise ValueError('AI没有提供投注候选')
        from bet_evidence import independent_source_bets,ledger
        full_proved,full_complete=independent_source_bets(parser,lottery,source)
        bets=[]
        used={}
        for item in obj['candidate_bets']:
            start,end=item.get('source_start'),item.get('source_end')
            if type(start) is not int or type(end) is not int or not 0<=start<end<=len(source):
                raise ValueError('AI原文证据位置无效')
            evidence=source[start:end]
            if item.get('evidence_text')!=evidence:
                raise ValueError('AI原文证据与字符位置不一致')
            target=item.get('lottery',lottery)
            if target not in (lottery,'FC3D' if lottery=='福彩' else 'PL3'):
                raise ValueError('AI候选彩种冲突')
            kind=item.get('bet_type') or item.get('play')
            aliases={'DIRECT':'直选','GROUP3':'组三（组选）','GROUP6':'组六（组选）',
                     'GROUP3_PERM':'组三复式','GROUP6_PERM':'组六复式','SUM':'和值','SPAN':'跨度','DOUBLE_FLY':'双飞'}
            kind=aliases.get(kind,kind)
            codes=item.get('ticket_codes')
            if codes is None:
                selection=item.get('digit_set') or item.get('selection')
                if item.get('dan') is not None:
                    selection=str(item['dan'])+':'+str(item.get('tuo',''))
                    if kind in ('胆拖','胆拖组三','胆拖组六'):
                        kind=f'1胆{len(str(item.get("tuo","")))}拖组'+('3' if kind=='胆拖组三' else '6')
                if item.get('position_sets') is not None:
                    positions=item['position_sets']
                    if not isinstance(positions,list) or len(positions)!=3:raise ValueError('定位候选结构无效')
                    selection='|'.join(label+':'+str(x) for label,x in zip('百十个',positions) if x not in (None,''))
                codes=[selection]
            if not isinstance(codes,list) or not codes or any(not isinstance(x,str) for x in codes):
                raise ValueError('AI号码必须使用字符串保留前导零')
            if kind in ('和值','跨度'):
                codes=[str(int(x)) for x in codes]
            amount_kind=item.get('amount_kind')
            value=item.get('bet_multiples') if amount_kind=='multiplier' else item.get('declared_amount_yuan')
            if amount_kind not in ('amount','multiplier') or value is None or isinstance(value,bool):
                raise ValueError('AI未说明金额/倍数依据')
            value=Decimal(str(value))
            if not value.is_finite() or value<=0:raise ValueError('AI金额或倍数无效')
            tentative=[]
            for code in codes:
                play=kind+code if kind in ('和值','跨度') else kind
                selection=code
                tentative.append(parser._make(lottery,evidence,play,selection,1))
            base=sum(Decimal(str(b.principal)) for b in tentative)
            multiple=value/base if amount_kind=='amount' else value
            chunk=[parser._make(lottery,evidence,b.play,b.selection,parser._multiplier(format(multiple,'f'))) for b in tentative]
            proved,complete=independent_source_bets(parser,lottery,evidence)
            if not complete and full_complete:
                proved=[b for b in full_proved if type(b.source_span.get('start')) is int
                        and start<=b.source_span['start'] and b.source_span.get('end',len(source)+1)<=end]
                complete=bool(proved)
            expected=ledger(proved)
            evidence_key=(start,end)
            current=used.setdefault(evidence_key,ledger([]))
            proposed=current+ledger(chunk)
            if not complete or proposed-expected:
                raise ValueError('AI号码、玩法或数量缺少对应原文依据')
            used[evidence_key]=proposed
            for b in chunk:
                b.source_span={'start':start,'end':end,'text':evidence,'position_verified':True}
                b.parse_source='ai_evidence_verified'
            bets.extend(chunk)
        return bets,candidate,''
    except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
        return [],candidate,str(exc)
