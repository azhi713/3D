"""V2.6.1 bridge for common AI aliases; final ledger is still validated locally."""
import json
from candidate_v260 import normalize_candidate as normalize_v260
from candidate_v260 import decode_candidate_v260

ALIASES={'单':'直选','直':'直选','单选':'直选','组':'具体组选','组选':'具体组选',
         '组3':'组三（组选）','组6':'组六（组选）'}

def normalize_candidate(source,candidate):
    if not isinstance(candidate,str) or not candidate.lstrip().startswith('{'):
        return candidate
    data=json.loads(candidate)
    if not isinstance(data,dict) or not isinstance(data.get('candidate_bets'),list):
        return candidate
    for item in data['candidate_bets']:
        if isinstance(item,dict):
            kind=item.get('bet_type') or item.get('play')
            if kind in ALIASES:item['bet_type']=ALIASES[kind]
    return normalize_v260(source,json.dumps(data,ensure_ascii=False))

def decode_candidate_v261(candidate,parser,lottery,source):
    try:candidate=normalize_candidate(source,candidate)
    except (ValueError,TypeError,KeyError) as exc:
        return [],candidate,'AI候选字段无效：'+str(exc)
    # candidate_v260 already validates structured evidence and amounts;
    # do not weaken that verification for malformed aliases.
    return decode_candidate_v260(candidate,parser,lottery,source)
