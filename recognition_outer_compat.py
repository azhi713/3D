"""Opt-in outer syntax candidate. Existing parser and monetary rules stay authoritative."""
import re
from decimal import Decimal

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
        return None
    tokens = list(re.finditer(r'[0-9]{3}', match['numbers']))
    if len(tokens) != int(match['count']):
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
