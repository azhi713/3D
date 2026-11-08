"""V2.5.7: verification of model explanations against the source text.

This module intentionally never calls BetParser.parse or a second semantic parser.
The model may explain *how* a sentence expresses a purchase, but cannot supply
numbers, play types, multiples or amounts without a traceable source.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
import json
import re
import unicodedata

from models import cents


class EvidenceError(ValueError):
    pass


PLAY_TAGS = {
    '直选': r'直|单|直选|单选|一单|几单|注',
    '组三（组选）': r'组|组选|组三|组3',
    '组六（组选）': r'组|组选|组六|组6',
    '三码组三': r'组三|组3',
    '四码组三': r'组三|组3',
    '五码组三': r'组三|组3',
    '六码组三': r'组三|组3',
    '七码组三': r'组三|组3',
    '八码组三': r'组三|组3',
    '九码组三': r'组三|组3',
    '双飞': r'双飞|(?:各)?飞|[xX](?=\d{2})',
    '独胆': r'独|毒',
    '对子': r'对子',
    '豹子': r'豹子',
    '转圈打': r'转圈打',
    '一码定位': r'定位|定|[百十个](?:位)?\s*\d',
    '两码定位': r'定位|定|[百十个](?:位)?\s*\d',
    '三码定位': r'定位|定|[百十个](?:位)?\s*\d',
    '组六复式': r'组六\s*复式',
    '组三复式': r'组三\s*复式',
}

_AMOUNT_WORDS = r'(?:元|米|块)'
_TOTAL_PATTERN = re.compile(r'(?:合计|总计|福家|体家|金额合计|总共)\s*[:：]?\s*(\d+(?:\.\d{1,2})?)\s*(?:元|米|块)?')
_ITEM_AMOUNT = re.compile(r'(?:[各打买投]|(?:组[三六36]?|直选|直|单|双飞))\s*(\d+(?:\.\d{1,2})?)\s*(?:元|米|块)')


def _normalize(value):
    return unicodedata.normalize('NFKC', str(value or ''))


def _decimal(value):
    if isinstance(value, bool):
        raise EvidenceError('金额/倍数不能是布尔值')
    try:
        amount=Decimal(str(value))
        if not amount.is_finite() or amount<=0 or amount.as_tuple().exponent < -2:
            raise ValueError('invalid amount')
    except (ArithmeticError, ValueError, TypeError) as exc:
        raise EvidenceError('金额或倍数格式无效') from exc
    return amount


def source_segments(source):
    """Only the program creates IDs/character offsets, never the model."""
    out=[]
    for index,m in enumerate(re.finditer(r'[^\r\n]+',source)):
        if not m.group().strip():
            continue
        start=m.start()+len(m.group())-len(m.group().lstrip())
        end=m.end()-len(m.group())+len(m.group().rstrip())
        out.append({'segment_id':f'S{len(out)+1}', 'start':start,'end':end,
                    'text':source[start:end]})
    return out if out else [{'segment_id':'S1','start':0,'end':len(source),'text':source}]


def locate_evidence(source,item,code_hint=''):
    """Read exact program-side ranges, or map an AI-selected segment ID.

    Old candidates with a correct source_start/source_end remain supported.
    An exact evidence_text can be searched when unique. No inferred offset is
    created when the evidence is ambiguous.
    """
    if not isinstance(item,dict):
        raise EvidenceError('AI候选必须是对象')
    segments=source_segments(source)
    ref=item.get('segment_id',item.get('source_ref'))
    if ref is not None:
        matched=next((x for x in segments if x['segment_id']==str(ref)),None)
        if matched is None:
            raise EvidenceError('AI引用了不存在的原文段落')
        start,end=matched['start'],matched['end']
    else:
        start,end=item.get('source_start'),item.get('source_end')
        if type(start) is not int or type(end) is not int:
            evidence=item.get('evidence_text','')
            if isinstance(evidence,str) and evidence and source.count(evidence)==1:
                start=source.index(evidence);end=start+len(evidence)
            elif len(segments)==1:
                start,end=segments[0]['start'],segments[0]['end']
            elif code_hint:
                hits=[s for s in segments if _code_occurs(s['text'],code_hint)]
                if len(hits)==1:
                    start,end=hits[0]['start'],hits[0]['end']
                else:
                    raise EvidenceError('无法确定候选对应的原文行，请返回segment_id')
            else:
                raise EvidenceError('缺少原文段落ID，无法核对购买依据')
        if not 0<=start<end<=len(source):
            raise EvidenceError('原文证据位置不在消息范围内')
        if (type(item.get('source_start')) is int and type(item.get('source_end')) is int
            and (start>0 and source[start-1] not in '\n\r\t ，,。.!！:：;；、/'
                 or end<len(source) and source[end] not in '\n\r\t ，,。.!！:：;；、/')):
            raise EvidenceError('AI原文证据截断了一个完整投注块，请使用segment_id')
    region=source[start:end]
    if item.get('evidence_text') is not None and item['evidence_text'] != region:
        raise EvidenceError('模型返回的原文证据与实际片段不符')
    return start,end,region


def _without_totals(text):
    text=_TOTAL_PATTERN.sub(' ',_normalize(text))
    # A trailing isolated receipt after a delimiter is not another ticket.
    return re.sub(r'(?:(?<=。)|(?<=，)|(?<=,)|(?<=!)|(?<=！))\s*\d+(?:\.\d{1,2})?\s*[元米块]?\s*$', ' ',text)


def _code_occurs(text,code):
    if not code:return False
    clean=_without_totals(text)
    if re.search(r'(?<!\d)'+re.escape(code)+r'(?!\d)',clean):return True
    # A positional item has per-position strings rather than a contiguous code.
    return False


def _selection_ok(fragment,bet):
    text=_without_totals(fragment)
    play=bet.play
    selection=str(bet.selection)
    if play.endswith('码定位'):
        for part in selection.split('|'):
            if ':' not in part:return False
            label,digits=part.split(':',1)
            if not re.search(re.escape(label)+r'(?:位)?\s*[:：]?\s*'+re.escape(digits)+r'(?!\d)',text):
                return False
        return True
    if play.startswith('和值'):
        number=play[2:]
        return bool(re.search(r'(?:和值|合值|和直)\s*'+re.escape(number)+r'(?!\d)|(?<!\d)'+re.escape(number)+r'\s*(?:和值|合值|和直)',text))
    if play.startswith('跨度'):
        number=play[-1]
        return bool(re.search(r'(?:跨度|跨)\s*'+re.escape(number)+r'(?!\d)|(?<!\d)'+re.escape(number)+r'\s*(?:跨度|跨)',text))
    if play.startswith('1胆'):
        if ':' not in selection:return False
        dan,tuo=selection.split(':',1)
        return bool(re.search(re.escape(dan)+r'\s*(?:胆|拖)\s*'+re.escape(tuo)+r'(?!\d)',text))
    if play in ('全包组六','全包组三','豹子'):
        return bool(re.search('全包|包全|豹子',text))
    return _code_occurs(text,selection)


def _play_ok(fragment,bet):
    play=bet.play
    text=_normalize(fragment)
    if play.startswith('和值'):
        return bool(re.search(r'和值|合值|和直',text))
    if play.startswith('跨度'):
        return bool(re.search(r'跨度|跨',text))
    if play.startswith('1胆'):
        return bool(re.search(r'胆|拖',text))
    if play.startswith('全包'):
        return bool(re.search('全包|包全',text))
    if play.endswith('码复试'):
        return bool(re.search('复试|复式|(?<!六)(?<!三)复',text))
    if play.endswith('码组六'):
        return bool(re.search('组六|组6',text))
    if play.endswith('码组三'):
        return bool(re.search('组三|组3',text))
    match=PLAY_TAGS.get(play)
    if match and re.search(match,text):return True
    # No play label on a bare three-digit number means direct, not group.
    if play=='直选' and not re.search(r'组|双飞|对子|定位|和值|合值|复试|复式',text):return True
    return False


def _play_amount_token(fragment, play, amount):
    """Locate the amount immediately following this play, not another play.

    Only distinct 组三/组六 or 直选 suffixes are accepted.  A plain number
    remains ambiguous until the message-level receipt matches exactly.
    """
    text=_without_totals(fragment)
    value=format(_decimal(amount),'f')
    if '.' in value:value=value.rstrip('0').rstrip('.')
    if '组三' in play: label=r'(?:组三|组3)'
    elif '组六' in play: label=r'(?:组六|组6)'
    elif '直选' in play: label=r'(?:直选|直|单选|单)'
    else:return None
    found=re.search(label+r'\s*'+re.escape(value)+r'\s*(元|米|块)?(?![\d.]|\s*倍)',text)
    if not found:return None
    # Do not allow a match that cuts a longer price or another expression.
    tail=text[found.end():]
    if found.group(1):return 'explicit'
    if not tail or re.match(r'\s*(?:组[三六36]|直|单|合计|福合计|体合计|福\s*$|体\s*$|$)',tail):
        return 'bare'
    return None


def _amount_mentions(fragment,amount,play=None):
    amount=_decimal(amount)
    if play and _play_amount_token(fragment,play,amount):return True
    text=_without_totals(fragment)
    value=format(amount,'f')
    if '.' in value:value=value.rstrip('0').rstrip('.')
    return bool(re.search(r'(?<!\d)'+re.escape(value)+r'\s*(?:元|米|块)(?![0-9])',text)
                or re.search(r'(?:各|打|买|投)\s*'+re.escape(value)+r'(?!\d|\s*倍)',text))


def _v263_chinese_number(word):
    """Convert common written multipliers only; never infer a missing multiple.

    This helper is used exclusively on phrases ending in 倍.  Positional
    digits, ticket codes, receipts and amounts are never changed.
    """
    aliases={'零':0,'〇':0,'一':1,'壹':1,'二':2,'两':2,'贰':2,
             '三':3,'叁':3,'四':4,'肆':4,'五':5,'伍':5,'六':6,'陆':6,
             '七':7,'柒':7,'八':8,'捌':8,'九':9,'玖':9}
    units={'十':10,'拾':10,'百':100,'佰':100}
    if word=='半':return '0.5'
    if not word or any(ch not in aliases and ch not in units for ch in word):return None
    if all(ch in aliases for ch in word):
        # "一一倍" is not a normal Chinese numeral; do not misread it as 11.
        return str(aliases[word]) if len(word)==1 else None
    total=0;prefix=None
    for ch in word:
        if ch in aliases:
            if prefix is not None:return None
            prefix=aliases[ch]
        else:
            size=units[ch]
            total+=(1 if prefix is None else prefix)*size
            prefix=None
    if prefix is not None:total+=prefix
    return str(total) if total>0 else None


def _v263_normalized_multiplier_text(fragment):
    text=_normalize(fragment).replace(' ','')
    numerals='零〇一壹二两贰三叁四肆五伍六陆七柒八捌九玖十拾百佰'
    # "一倍半" is not two unrelated wagers.
    def half_after(match):
        converted=_v263_chinese_number(match.group(1))
        if converted is None:return match.group(0)
        return format(Decimal(converted)+Decimal('0.5'),'f')+'倍'
    text=re.sub('(?<!组)(['+numerals+']+)倍半',half_after,text)
    def replace_one(match):
        converted=_v263_chinese_number(match.group(1))
        return converted+'倍' if converted is not None else match.group(0)
    text=re.sub('(?<!组)(['+numerals+']+|半)倍',replace_one,text)
    return text


def _v263_play_category(label):
    if label in ('直选','直','单选','单'):return '直'
    if label in ('组三','组3'):return '三'
    if label in ('组六','组6'):return '六'
    if label in ('组选','组'):return '组'
    if label in ('定位','定'):return '定'
    return None


def _multiplier_mentions(fragment,multiplier,play):
    """Source-backed multiples; Chinese numbers and play-local scopes.

    A multi-play fragment must not borrow the '2倍' of 组三 to prove a
    组六2倍.  A shared '直和组选都各一倍' may lawfully apply to both.
    """
    expected=_decimal(multiplier)
    text=_v263_normalized_multiplier_text(fragment)
    target=('三' if '组三' in play else '六' if '组六' in play else
            '直' if play=='直选' else '定' if play.endswith('码定位') else None)
    mention=list(re.finditer(r'(?<![\d.])(\d+(?:\.\d{1,2})?)倍',text))
    labels=r'组六|组三|组6|组3|直选|单选|组选|定位|直|单|组|定'
    for m in mention:
        if _decimal(m.group(1))!=expected:continue
        # Commas/line breaks end a natural-language purchase clause;
        # '、' does not (it also separates ticket codes).
        before=text[:m.start()]
        last=max((before.rfind(char) for char in '，,。；;\n'),default=-1)
        clause=before[last+1:]
        hits=[_v263_play_category(x.group()) for x in re.finditer(labels,clause)]
        hits=[x for x in hits if x]
        if not hits:
            # A single explicit number may follow a prior positional clause:
            # "十位9、个位0，3倍".  Do not borrow from a different
            # explicitly priced play in the same fragment.
            if len(mention)==1 and not re.search(r'组三|组3|组六|组6|直|单|组选',clause):
                return True
            continue
        specific=[x for x in hits if x!='组']
        if not specific:
            if target in ('三','六') and len(mention)==1:return True
            continue
        # '直和组选都各一倍': a generic 组选 shares a single explicit
        # unit with 直选, even though it does not name 组三/组六.
        if (len(mention)==1 and '组' in hits and '直' in specific
                and target in ('直','三','六') and re.search(r'和|与|及|都|各',clause)):
            return True
        # A shared multiplier with an explicit conjunction applies to each
        # named play.  Otherwise the last explicit play owns the amount.
        if len(mention)==1 and len(set(specific))>1 and re.search(r'和|与|及|都|各',clause):
            if target in specific or (target in ('三','六') and '组' in hits):return True
        if specific[-1]==target:return True
        # A generic 组选 explicitly covers a valid 组三/组六 candidate.
        if specific[-1]=='组' and target in ('三','六'):return True
    if mention:return False
    # Preserve legacy numeric shorthand (1单 / 组1), but never use it to
    # override an explicit different multiple.
    value=format(expected,'f')
    if '.' in value:value=value.rstrip('0').rstrip('.')
    if play=='直选' and re.search(r'(?:各)?'+re.escape(value)+r'(?:单|直)',text):return True
    if play in ('组六（组选）','组三（组选）') and re.search(r'(?:各)?'+re.escape(value)+r'(?:组|组选)',text):return True
    if play in ('组六（组选）','组三（组选）') and re.search(r'(?:组|组选)'+re.escape(value)+r'(?!\d)',text):return True
    if play=='直选' and re.search(r'(?:单|直)'+re.escape(value)+r'(?!\d)',text):return True
    # Existing business rule: an otherwise unqualified order defaults to 1倍
    # only when no explicit conflicting multiplier exists anywhere.
    if expected==1 and not re.search(r'\d+(?:\.\d+)?\s*倍|[零〇一二两三四五六七八九十百壹贰叁肆伍陆柒捌玖拾佰半]+倍',text):
        return True
    return False


def verify_candidate_bet(fragment,bet,amount_kind,declared_amount=None):
    """Return whether global total is needed to resolve bare money vs multiple."""
    if not _selection_ok(fragment,bet):
        raise EvidenceError(f'AI号码{bet.selection}在对应投注原文中找不到依据')
    if not _play_ok(fragment,bet):
        raise EvidenceError(f'AI玩法{bet.play}在对应原文中没有依据')
    if amount_kind=='amount':
        scoped=_play_amount_token(fragment,bet.play,declared_amount) if declared_amount is not None else None
        # Multiple distinct plays in one sentence must not borrow a monetary
        # number from the neighbouring play, even when its unit is explicit.
        simultaneous=bool(re.search(r'组三|组3',fragment) and re.search(r'组六|组6',fragment))
        if declared_amount is None or (simultaneous and not scoped) or not _amount_mentions(fragment,declared_amount,bet.play):
            raise EvidenceError('AI声明金额未在该玩法的原文位置找到依据')
        if scoped=='explicit':return False
        if scoped=='bare':return True
        # Other unmarked phrases also need a matching whole-message receipt.
        return not bool(re.search(r'(?<!\d)'+re.escape(str(_decimal(declared_amount).normalize()))+r'\s*[元米块]',_without_totals(fragment)))
    if amount_kind!='multiplier':
        raise EvidenceError('AI没有声明金额或倍数类型')
    if _multiplier_mentions(fragment,bet.multiplier,bet.play):
        return False
    # An explicit (numeric or written Chinese) multiple belongs to
    # the relevant play. A different play or subtotal cannot justify
    # an AI-invented value, even when that subtotal is present.
    if re.search(r"\d+(?:\.\d+)?倍", _v263_normalized_multiplier_text(fragment)):
        raise EvidenceError('AI提供的倍数没有对应的原文依据')
    # AI may describe the converted multiple when the source says "各20"
    # as an amount, but only a complete receipt can disambiguate the two.
    if _amount_mentions(fragment,bet.principal):
        return True
    raise EvidenceError('AI提供的倍数没有对应的原文依据')


def declared_total(source):
    hits=list(_TOTAL_PATTERN.finditer(_normalize(source)))
    if hits:return _decimal(hits[-1][1])
    compact=re.search(r"合\s*[:：]?\s*(\d+(?:\.\d{1,2})?)\s*[元米块]?\s*$",_normalize(source))
    if compact:return _decimal(compact[1])
    # A trailing independent number after a sentence boundary can be a total.
    m=re.search(r'[。！!，,]\s*(\d+(?:\.\d{1,2})?)\s*[元米块]?\s*[。！!]*$',_normalize(source))
    return _decimal(m[1]) if m else None


def _fragment_bet_codes(fragment):
    """Only ticket-like numeric tokens, never amounts or `合计108`."""
    text=_without_totals(fragment)
    # Position numbers and胆拖digit pools are *not* independent straight tickets;
    # they are checked by _selection_ok against the structured selection field.
    if re.search(r'[百十个](?:位)?\s*[:：]?\s*\d|\d\s*(?:胆|拖)',text):
        return []
    # Extract prefix number list of an order; cover direct/group/round/胆拖,
    # but never label price or multiple tokens as ticket codes.
    m=re.search(r'(?<!\d)(\d{2,9}(?:\s*[-.。,，、/:：\s]\s*\d{2,9})*)(?=\s*[-.。,，、/:：\s]*(?:各|直|单|组|直选|组选|双飞|复式|复试|胆拖|拖))',text)
    if m:
        return [x for x in re.findall(r'\d{2,9}',m[1]) if len(x) in (2,3,4,5,6,7,8,9)]
    return []


def _explicit_play_demands(fragment):
    text=_normalize(fragment)
    plays=set()
    if re.search(r'直|单',text):plays.add('直选')
    if re.search(r'组三|组3',text):plays.add('组三')
    if re.search(r'组六|组6',text):plays.add('组六')
    if re.search(r'组|组选',text) and not re.search(r'组三|组六|组3|组6',text):plays.add('组选')
    if re.search(r'双飞|飞',text):plays.add('双飞')
    if re.search(r'组三复式|组六复式',text):
        plays.discard('组三');plays.discard('组六')
    return plays


def validate_full(source,combined,allow_unresolved=False):
    """Validate all distinct ticket evidence, without asking a parser to parse source.

    The source is the legal authority; amounts only corroborate explicit plays.
    Returns (ok, reason). Local verified bets retain their own status.
    """
    if not combined:return False,'AI没有提供任何有效投注'
    require_total=False
    seen=Counter()
    per_span=defaultdict(list)
    try:
        for bet in combined:
            span=bet.source_span or {}
            start,end=span.get('start'),span.get('end')
            if type(start) is int and type(end) is int and 0<=start<end<=len(source):
                fragment=source[start:end]
            else:
                fragment=bet.raw or source
                start=source.find(fragment) if source.count(fragment)==1 else 0
                end=start+len(fragment) if start else len(source)
            if bet.parse_source=='ai_evidence_verified':
                if not _selection_ok(fragment,bet) or not _play_ok(fragment,bet):
                    raise EvidenceError('AI结构化候选缺少对应原文依据')
                if span.get('needs_total_for_amount'):
                    require_total=True
            per_span[(start,end)].append(bet)
            # Do not silently convert two independent duplicate purchases into one;
            # but also do not count one single ticket in a source fragment twice.
            key=(start,end,bet.play,bet.selection,str(Decimal(str(bet.multiplier)).normalize()))
            seen[key]+=1
            if bet.parse_source=='ai_evidence_verified' and seen[key]>max(1,len(re.findall(r'(?<!\d)'+re.escape(str(bet.selection))+r'(?!\d)',_without_totals(fragment)))):
                raise EvidenceError('AI在同一原文中重复增加了未购买的投注')
        if require_total:
            total=declared_total(source)
            if total is None:
                return False,'无单位金额存在两种解释且没有整条合计证据'
            subtotal=sum(cents(b.principal) for b in combined)
            if subtotal!=cents(total):
                return False,f'原文合计{total}元与AI解释{Decimal(subtotal)/100}元不符，无法确定无单位金额含义'
        if not allow_unresolved:
            for segment in source_segments(source):
                if re.fullmatch(r'(?:福|体|排|福彩|体彩|定位|合计\s*\d+|福家\s*\d+)\s*',segment['text']):
                    continue
                codes=_fragment_bet_codes(segment['text'])
                if not codes:continue
                included=[b for (start,end),bets in per_span.items() if start<segment['end'] and end>segment['start'] for b in bets]
                for code in codes:
                    if code not in [b.selection for b in included]:
                        raise EvidenceError('AI没有解释原文中的部分明确选号：'+code)
                # When all ticket codes precede a shared tail clause, each
                # explicitly purchased play applies to each code.
                demands=_explicit_play_demands(segment['text'])
                if codes and demands and all(segment['text'].find(c)<max(
                        [segment['text'].find(t) for t in ('直','单','组','双飞') if t in segment['text']] or [len(segment['text'])])
                        for c in codes):
                    for code in codes:
                        matches=[b.play for b in included if b.selection==code]
                        if '直选' in demands and '直选' not in matches:
                            raise EvidenceError(f'原文{code}明确购买直选但AI漏单')
                        if '组选' in demands and not any(p in matches for p in ('组三（组选）','组六（组选）')):
                            raise EvidenceError(f'原文{code}明确购买组选但AI漏单')
                        if '双飞' in demands and '双飞' not in matches:
                            raise EvidenceError(f'原文{code}明确购买双飞但AI漏单')
                        if '组三' in demands and not any('组三' in p for p in matches):
                            raise EvidenceError(f'原文{code}明确购买组三但AI漏单')
                        if '组六' in demands and not any('组六' in p for p in matches):
                            raise EvidenceError(f'原文{code}明确购买组六但AI漏单')
        return True,''
    except (EvidenceError,ValueError) as exc:
        return False,str(exc)


def fragment_resolved(source,fragment,ai_bets,all_bets):
    """A fragment is resolved only if AI cited it and its numeric tickets are covered."""
    left,right=fragment.get('start',0),fragment.get('end',len(source))
    if type(left) is not int or type(right) is not int or not 0<=left<right<=len(source):
        return False
    matched=[b for b in ai_bets if type(b.source_span.get('start')) is int and
             b.source_span['start']<right and b.source_span.get('end',0)>left]
    if not matched:return False
    frag_text=source[left:right]
    codes=_fragment_bet_codes(frag_text)
    covered={b.selection for b in all_bets}
    if any(code not in covered for code in codes):return False
    demands=_explicit_play_demands(frag_text)
    if not codes and not demands:return False
    matched_plays={b.play for b in all_bets if type(b.source_span.get('start')) is int and
                   b.source_span['start']<right and b.source_span.get('end',0)>left}
    def has_play(key):
        if key=='直选':return '直选' in matched_plays
        if key=='组选':return any(x in matched_plays for x in ('组三（组选）','组六（组选）'))
        if key=='组三':return any('组三' in x for x in matched_plays)
        if key=='组六':return any('组六' in x for x in matched_plays)
        return key in matched_plays
    return all(has_play(d) for d in demands)

# V263-20261102-CN-MULT-V3
