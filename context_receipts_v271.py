# -*- coding: utf-8 -*-
"""Whole-message purchase-count and cash evidence; no AI, no guessing new codes."""
from decimal import Decimal
import re
import unicodedata
from models import cents

LOT = r'(?:福彩3[Dd]?|福彩|3[Dd]|体彩排列三|体彩|排列三|福|体|排)'
CODES = r'[0-9]{3}(?:[\s,，、/\-]+[0-9]{3})*'
DIGITS = r'[0-9]{4,9}'
PREFIX = re.compile(r'^\s*(?P<lot>'+LOT+r')?\s*[:：]?\s*(?P<codes>'+CODES+
                    r')\s*(?:直选|直|单)\s*(?P<mult>\d+(?:\.\d{1,2})?)\s*倍\s*$',re.I)
LAST = re.compile(r'^\s*(?P<lot>'+LOT+r')?\s*[:：]?\s*(?P<codes>'+CODES+
                  r')\s*(?:直选|直|单)\s*(?P<count>\d+)\s*注\s*'
                  r'(?P<amount>\d+(?:\.\d{1,2})?)\s*(?:元|米|块)?\s*$',re.I)
FLY_CASH = re.compile(r'^\s*(?P<lot>'+LOT+r')?\s*[:：]?\s*(?P<code>[0-9]{2})\s*'
                      r'(?:双飞|飞)\s*(?P<amount>\d+(?:\.\d{1,2})?)\s*'
                      r'(?:元|米|块)?\s*$',re.I)
GROUP_CASH = re.compile(r'^\s*(?P<lot>'+LOT+r')?\s*[:：]?\s*(?P<code>'+DIGITS+
                        r')\s*(?P<play>组三|组六)\s*(?:各\s*)?'
                        r'(?P<amount>\d+(?:\.\d{1,2})?)\s*(?:元|米|块)?\s*$',re.I)
SHARED_LAST = re.compile(r'^\s*(?P<lot>'+LOT+r')\s*[:：]?\s*(?P<code>'+DIGITS+
                         r')\s*(?P<play>组三|组六)\s*各\s*(?P<amount>\d+(?:\.\d{1,2})?)'
                         r'\s*(?:元|米|块)?\s*(?:合计|总计|共)\s*'
                         r'(?P<total>\d+(?:\.\d{1,2})?)\s*(?:元|米|块)?\s*$',re.I)
BARE_POOL = re.compile(r'^\s*(?P<lot>'+LOT+r')\s*[:：]?\s*(?P<code>'+DIGITS+r')\s*$',re.I)
DIRECT_CASH = re.compile(r'^\s*(?P<lot>'+LOT+r')?\s*[:：]?\s*'
                         r'(?P<code>[0-9]{3})\s*(?:直选|直|单)\s*'
                         r'(?P<amount>\d+(?:\.\d{1,2})?)\s*(?:元|米|块)?\s*$',re.I)


def _canonical(text):
    return (unicodedata.normalize('NFKC', text or '')
            .replace('㊗️','注').replace('㊗','注').replace('祝️','注')
            .replace('祝\ufe0f','注'))


def _belongs(label, lottery):
    if not label:
        return True
    from parser import lottery_from_token
    return lottery_from_token(label) == lottery


def _cash_bet(parser, lottery, original, selection, play, amount):
    """Convert amount to multiplier using ONLY the configured unit price."""
    price = Decimal(amount)
    if price <= 0:
        return None
    try:
        baseline = parser._from_kind(lottery, original, selection, play, 1)
        unit = sum((Decimal(str(b.principal)) for b in baseline),Decimal(0))
        if unit <= 0:
            return None
        multiple = parser._multiplier(format(price / unit, 'f'))
        result = parser._from_kind(lottery, original, selection, play, multiple)
        if sum(cents(b.principal) for b in result) != cents(price):
            return None
        for item in result:
            item.entered_amount_yuan = float(price)
        return result
    except (ValueError, ArithmeticError, KeyError):
        return None


def _set_parser(parser, amount):
    parser.last_claimed = float(amount)
    parser.last_warnings = []
    parser.last_receipts = []


def parse_context_order(parser, lottery, original):
    """Only infer receipt ownership when every line, code and cost is proved.

    4注8元 following 689直选1倍 + 101/102/103直4注8元 covers all four
    distinct purchases once. The '4' is a count, never an invented multiplier.
    """
    if not isinstance(original,str):
        return None
    text = _canonical(original)
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    if lines:
        last = LAST.fullmatch(lines[-1])
        if last and _belongs(last['lot'], lottery):
            parts=[]
            for line in lines[:-1]:
                m=PREFIX.fullmatch(line)
                if not m or not _belongs(m['lot'], lottery):
                    parts=[];break
                parts.append(m)
            if len(parts)==len(lines)-1:
                try:
                    codes=[]
                    for m in parts:
                        mult=parser._multiplier(m['mult'])
                        codes.extend((c,mult) for c in re.findall(r'(?<!\d)\d{3}(?!\d)',m['codes']))
                    codes.extend((c,1) for c in re.findall(r'(?<!\d)\d{3}(?!\d)',last['codes']))
                    if len(codes) == int(last['count']):
                        bets=[parser._from_kind(lottery,original,c,'直选',mult)[0]
                              for c,mult in codes]
                        declared=Decimal(last['amount'])
                        # Count is proof of purchases. Receipt may contain a typo;
                        # it does not create another ticket or revise the count.
                        _set_parser(parser,declared)
                        return bets,parser.normalize_text(original),''
                except (ValueError,ArithmeticError,KeyError):
                    pass
    direct=DIRECT_CASH.fullmatch(text)
    if direct and _belongs(direct['lot'],lottery):
        bets=_cash_bet(parser,lottery,original,direct['code'],'直选',direct['amount'])
        if bets:
            _set_parser(parser,direct['amount'])
            return bets,parser.normalize_text(original),''
    fly=FLY_CASH.fullmatch(text)
    if fly and _belongs(fly['lot'],lottery):
        bets=_cash_bet(parser,lottery,original,fly['code'],'双飞',fly['amount'])
        if bets:
            _set_parser(parser,fly['amount'])
            return bets,parser.normalize_text(original),''
    group=GROUP_CASH.fullmatch(text)
    if group and _belongs(group['lot'],lottery):
        bets=_cash_bet(parser,lottery,original,group['code'],group['play'],group['amount'])
        if bets:
            _set_parser(parser,group['amount'])
            return bets,parser.normalize_text(original),''
    return None


def parse_shared_context(parser, lottery, current_text, original_text):
    """Use whole order only if the current per-color text is still unchanged.

    Group 'each amount' applies across two explicit lottery pools only if the
    complete original message and the complete total agree exactly.
    """
    if not original_text or original_text == current_text:
        return None
    # Earlier versions routed the trailing '直选各2倍' only to the second
    # colour. Reparse those saved per-colour fragments using the immutable
    # whole-message source, but only for the exact two-pool shared directive.
    from routing_v260 import split_shared_total
    paired = split_shared_total(original_text, lottery)
    if paired is not None:
        sections, grand = paired
        if set(sections) == {'福彩', '体彩'}:
            lines = {lot: [line.strip() for line in part.splitlines() if line.strip()]
                     for lot, part in sections.items()}
            if (all(len(item) == 2 for item in lines.values()) and
                lines['福彩'][1] == lines['体彩'][1] and
                re.fullmatch(r'(?:直选|单选|直|单)\s*各\s*'
                             r'(?:半|\d+(?:\.\d{1,2})?|[一二两三四五六七八九十]+)\s*倍',
                             lines['福彩'][1], re.I)):
                selected = sections[lottery]
                previous = lines[lottery][0]
                if _canonical(current_text).strip() in (
                        _canonical(selected).strip(), _canonical(previous).strip()):
                    from parser import BetParser
                    other_lot = '体彩' if lottery == '福彩' else '福彩'
                    other_parser = BetParser(parser.rules)
                    other_bets, _, other_error = other_parser.parse(
                        other_lot, sections[other_lot])
                    bets, normalized, error = parser.parse(lottery, selected)
                    total = (sum(cents(b.principal) for b in bets) +
                             sum(cents(b.principal) for b in other_bets))
                    if not error and not other_error and bets and other_bets and total == cents(grand):
                        for bet in bets:
                            # The shared multiplier is not in the saved
                            # per-colour fragment; its true source is the
                            # original complete incoming message.
                            bet.source_span = {
                                'start': 0, 'end': len(original_text),
                                'text': original_text,
                                'reference': 'source_original_text',
                                'granularity': 'shared_directive',
                            }
                        return bets, normalized, '据双彩完整接收原文复核直选各倍数'
    from parser import split_lottery_message, lottery_from_token
    original = _canonical(original_text)
    pieces = [x.strip() for x in original.splitlines() if x.strip()]
    if len(pieces)<2:
        return None
    tail=SHARED_LAST.fullmatch(pieces[-1])
    if not tail:
        return None
    parsed=[]
    for line in pieces[:-1]:
        m=BARE_POOL.fullmatch(line)
        if not m:
            return None
        parsed.append((lottery_from_token(m['lot']),m['code']))
    parsed.append((lottery_from_token(tail['lot']),tail['code']))
    if len(parsed)<2 or len({c for c,_ in parsed})<2:
        return None
    if any(len(set(code))!=len(code) for _,code in parsed):
        return None
    try:
        sections=split_lottery_message(original_text,lottery)
        selected=sections.get(lottery,'')
        if _canonical(selected).strip()!=_canonical(current_text).strip():
            return None   # A manual edit must not reapply obsolete original context.
        prices=[]
        for lot,code in parsed:
            single=_cash_bet(parser,lot,original_text,code,tail['play'],tail['amount'])
            if not single:
                return None
            prices.append((lot,single))
        total=sum((cents(b.principal) for _,bets in prices for b in bets),0)
        bets=[b for lot,rows in prices if lot==lottery for b in rows]
        if not bets:
            return None
        # Explicit 'each' money binds every pool; a wrong terminal grand total
        # is a receipt error, not evidence to invent/erase a ticket.
        _set_parser(parser,sum(cents(b.principal) for b in bets)/100)
        note=('原文跨彩种各笔金额核对通过' if total==cents(Decimal(tail['total']))
              else f'客户双彩合计{tail["total"]}元，程序复算{total/100:g}元，保留各笔明确投注')
        return bets,parser.normalize_text(current_text),note
    except (ValueError,KeyError,ArithmeticError):
        return None



def play_bound_cash_evidence(region,bet,amount):
    """Cash scoped to the actual source selection and play; never whole-total."""
    value=format(Decimal(str(amount)),'f')
    if '.' in value:value=value.rstrip('0').rstrip('.')
    play=bet.play
    if play=='双飞': label=r'(?:双飞|飞)'
    elif play=='直选': label=r'(?:直选|直|单)'
    elif play.endswith('码组六'): label=r'(?:组六|组6)'
    elif play.endswith('码组三'): label=r'(?:组三|组3)'
    elif play=='组六（组选）': label=r'(?:组六|组选|组)'
    elif play=='组三（组选）': label=r'(?:组三|组选|组)'
    else:return False
    pattern=(r'(?<![0-9])'+re.escape(str(bet.selection))+
             r'\s*'+label+r'\s*(?:各\s*)?'+re.escape(value)+
             r'\s*(?:元|米|块)?(?=$|[\s，,。;；])')
    return bool(re.search(pattern,_canonical(region)))


def contextual_bet_evidence(parser,lottery,source,bet):
    """Use strict full-order source proof, never a model's numerical guess."""
    parsed=parse_context_order(parser,lottery,source)
    if parsed is None:return False
    return any(x.play==bet.play and x.selection==bet.selection and
               Decimal(str(x.multiplier))==Decimal(str(bet.multiplier)) for x in parsed[0])


def shared_cash_evidence(parser,lottery,current_text,original_text):
    return parse_shared_context(parser,lottery,current_text,original_text) is not None


def strong_receipt_evidence(raw, bets):
    """Wrong customer receipt is advisory only when purchase quantity is known.

    A standalone '123直选8元' has no independent multiplier/count proof and
    must NOT be accepted at the parser's default one-times amount.
    """
    if not bets:
        return False
    text=_canonical(raw)
    # A separate amount-coded purchase must not inherit the explicit
    # multiplier from a different purchase elsewhere in the message.
    unproved_cash = re.search(
        r'(?:直选|直|单|组三|组六|组选|双飞|飞)\s*(?:各\s*)?'
        r'\d+(?:\.\d+)?(?!\s*(?:倍|注))'
        r'(?=\s*(?:[,，;；元米块]|$))',text)
    if unproved_cash:
        return False
    if re.search(r'\d+(?:\.\d{1,2})?\s*倍',text):
        # A complete local ledger must have actual source-number evidence.
        # These rules never use a receipt difference to alter the bet set.
        explicit_numbers=re.findall(r'(?<!\d)\d{3}(?!\d)', text)
        return bool(explicit_numbers or re.search(r'定位|对子|双飞|双飞|独胆|复试|组[三六]',text))
    counts=[int(m.group(1)) for m in re.finditer(r'(?:直选|直|单)\s*(\d+)\s*注',text)]
    if counts and sum(counts)==len(bets) and all(b.play=='直选' for b in bets):
        return True
    return False
