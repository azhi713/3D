"""Normalize safe AI aliases while retaining strict evidence validation.

Never make up an arbitrary multiplier: infer 1 only from explicit one-unit
phrases or the confirmed default for isolated '组XYZ' items.
"""
import json
import re
from candidate_v257 import decode_candidate_v257


def _normalize_position(value):
    if not isinstance(value,dict):return value
    keys={'百':'百','百位':'百','十':'十','十位':'十','个':'个','个位':'个'}
    normalized={}
    for k,v in value.items():
        if k not in keys:raise ValueError('AI定位字段出现未知位置')
        name=keys[k]
        if name in normalized or not isinstance(v,(str,int)):
            raise ValueError('AI定位位置重复或格式不对')
        normalized[name]=str(v)
    return [normalized.get(k) for k in '百十个']


def normalize_candidate(source, candidate):
    if not isinstance(candidate,str) or not candidate.lstrip().startswith('{'):return candidate
    data=json.loads(candidate)
    if not isinstance(data,dict) or not isinstance(data.get('candidate_bets'),list):return candidate
    from evidence_v257 import source_segments
    spans={part['segment_id']:part for part in source_segments(source)}
    result=[]
    for original in data['candidate_bets']:
        if not isinstance(original,dict):result.append(original);continue
        item=dict(original)
        kind=item.get('bet_type') or item.get('play')
        item['position_sets']=_normalize_position(item['position_sets']) if 'position_sets' in item else item.get('position_sets')
        sid=item.get('segment_id')
        if isinstance(sid,list):
            selected=[spans[s] for s in sid if s in spans and isinstance(s,str)]
            if len(selected)!=len(sid) or not selected:
                raise ValueError('AI定位引用的片段不存在')
            left=min(s['start'] for s in selected);right=max(s['end'] for s in selected)
            # Cross-line evidence is only legal when AI names every segment
            # in that contiguous region. Anything skipped may change meaning.
            between=[s for s in spans.values() if left<=s['start'] and s['end']<=right]
            if len(between)!=len(selected):raise ValueError('AI定位引用的多行证据不连续')
            item.pop('segment_id',None)
            item['source_start'],item['source_end']=left,right
        if kind in ('单组','一单一组','一直一组'):
            codes=item.get('ticket_codes')
            if not isinstance(codes,list) or not all(isinstance(c,str) and len(c)==3 and c.isdigit() for c in codes):
                raise ValueError('AI单组需要有效三位号码')
            for target in ('直选','具体组选'):
                v=dict(item);v['bet_type']=target
                result.append(v)
            continue
        if kind in ('具体组选','组选'):
            codes=item.get('ticket_codes')
            if isinstance(codes,list) and len(codes)>0:
                groups={'组三（组选）':[],'组六（组选）':[]}
                for code in codes:
                    if not isinstance(code,str) or len(code)!=3 or not code.isascii() or not code.isdigit() or len(set(code))==1:
                        raise ValueError('具体组选号码非法或为豹子')
                    groups['组三（组选）' if len(set(code))==2 else '组六（组选）'].append(code)
                for target,values in groups.items():
                    if values:
                        v=dict(item);v['bet_type']=target;v['ticket_codes']=values
                        result.append(v)
                continue
        result.append(item)
    for item in result:
        # Explicit defaults only; every inferred unit remains checked by
        # the evidence validator against the untouched source region.
        if item.get('bet_multiples') is None and item.get('declared_amount_yuan') is None:
            evidence=''
            sid=item.get('segment_id')
            if isinstance(sid,str) and sid in spans:evidence=spans[sid]['text']
            elif type(item.get('source_start')) is int and type(item.get('source_end')) is int:
                evidence=source[item['source_start']:item['source_end']]
            elif len(spans)==1:evidence=source
            if re.search(r'(?:一单一组|1单一组|1单1组|一直一组)',evidence) or re.search(r'(?:^|\s)组\s*\d{3}(?:\s|$)',evidence):
                item['bet_multiples']=1;item['amount_kind']='multiplier'
        if item.get('bet_multiples') is not None and not item.get('amount_kind'):
            item['amount_kind']='multiplier'
        elif item.get('declared_amount_yuan') is not None and not item.get('amount_kind'):
            item['amount_kind']='amount'
    data['candidate_bets']=result
    return json.dumps(data,ensure_ascii=False)


def decode_candidate_v260(candidate,parser,lottery,source):
    try:
        candidate=normalize_candidate(source,candidate)
    except (ValueError,TypeError,KeyError) as exc:
        return [],candidate,'AI数据规范化失败：'+str(exc)
    return decode_candidate_v257(candidate,parser,lottery,source)
