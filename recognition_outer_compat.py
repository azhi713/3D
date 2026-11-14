"""Opt-in outer syntax candidate. Existing parser and monetary rules stay authoritative."""
import re
from decimal import Decimal

_INLINE = re.compile(
    r'(?P<lot>福彩|体彩|福|体|排)?(?P<numbers>[0-9]{3}(?:[ 、,，]+[0-9]{3})+)'
    r'(?:直选|直)(?P<multiple>[1-9][0-9]{0,3})倍'
)

_PATTERN = re.compile(
    r'[ \t]*(?P<numbers>[0-9]{3}(?:[ \t]*[、,，;； ][ \t]*[0-9]{3})+)[ \t]*\r?\n'
    r'[ \t]*共(?P<count>[1-9][0-9]{0,3})注直选[ \t]*(?P<multiple>[1-9][0-9]{0,3})[ \t]*倍[ \t]*'
)

def recover(parser, lottery, text):
    """Return a fully checked candidate or None; never mutate the original parser."""
    if lottery not in ('福彩', '体彩') or not isinstance(text, str) or len(text) > 20000:
        return None
    match = _PATTERN.fullmatch(text)
    if not match:
        match = _INLINE.fullmatch(text)
        if not match:
            return recover_explicit_receipt(parser, lottery, text)
        lot = match['lot']
        if lot and ((lot in ('福','福彩')) != (lottery == '福彩')):
            return None
    tokens = list(re.finditer(r'[0-9]{3}', match['numbers']))
    if 'count' in match.re.groupindex and len(tokens) != int(match['count']):
        return None
    numbers = [t.group() for t in tokens]
    multiple = int(match['multiple'])
    candidate = ' '.join(numbers) + f'\n{len(numbers)}注{multiple}倍'
    probe = type(parser)(parser.rules, parser.policy)
    bets, normalized, reason = probe.parse(lottery, candidate)
    if reason or len(bets) != len(numbers) or probe.last_claimed is not None:
        return None
    for bet, number in zip(bets, numbers):
        if bet.play != '直选' or bet.selection != number or Decimal(str(bet.multiplier)) != multiple:
            return None
    # Preserve each original occurrence, including legal repeated numbers.
    for bet, token in zip(bets, tokens):
        start = match.start('numbers') + token.start()
        bet.raw = text
        bet.source_span = {
            'reference': 'current_text', 'start': start, 'end': start+3,
            'text': token.group(), 'rule': 'outer_explicit_direct_count_v1',
            'context_start': 0, 'context_end': len(text),
            'candidate_text': candidate,
            'explanation': '原文明确直选、注数及倍数；逐项返回原解析器校验',
        }
    return bets, normalized, '外层兼容：原文明示直选注数与倍数，候选经原解析器核验', probe

_RECEIPT = re.compile(
    r'(?P<lot>福彩|体彩|福|体|排)?(?P<number>[0-9]{3})直选'
    r'(?P<multiple>[1-9][0-9]{0,3})倍[ \t]*(?:[,，]|\r?\n)[ \t]*'
    r'合计(?P<amount>[0-9]+(?:\.[0-9]{1,2})?)元'
)
def recover_explicit_receipt(parser, lottery, text):
    match = _RECEIPT.fullmatch(text)
    if not match:
        return None
    lot = match['lot']
    if lot and ((lot in ('福','福彩')) != (lottery == '福彩')):
        return None
    candidate = match['number']+'直选'+match['multiple']+'倍'
    probe = type(parser)(parser.rules, parser.policy)
    bets, normalized, reason = probe.parse(lottery, candidate)
    if reason or len(bets)!=1 or bets[0].play!='直选' or bets[0].selection!=match['number']:
        return None
    if Decimal(str(bets[0].multiplier))!=Decimal(match['multiple']):
        return None
    from context_receipts_v271 import strong_receipt_evidence
    if not strong_receipt_evidence(text,bets):
        return None
    probe.last_claimed=float(Decimal(match['amount']))
    bets[0].raw=text
    bets[0].source_span=dict(reference='current_text',start=0,end=len(text),text=text,
                            rule='outer_explicit_direct_receipt_v1',candidate_text=candidate,
                            declared_amount_yuan=probe.last_claimed)
    return bets, normalized, '外层兼容：明示倍数经原解析器核验，自报合计独立留存', probe
