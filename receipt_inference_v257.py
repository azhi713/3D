"""V2.5.7 follow-up: conservative program-owned receipt interpretation.

This runs before a paid AI call for one *unresolved* independent message line.
It never calculates a bet multiplier, creates a lottery rule, or posts a bet.
The existing candidate decoder and full-message evidence validator make the
final decision using period-specific rules. Anything not uniquely supported
by the raw text and confirmed total stays in the AI/review path.
"""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
import re
import unicodedata

_AMOUNT = r'\d+(?:\.\d{1,2})?'
_LINE = re.compile(
    r'^(?P<prefix>福|福彩(?:3[Dd])?|3[Dd]|体|体彩|排|排列三)?\s*'
    r'(?P<pool>\d{4,9})\s*(?P<orders>.*?)\s*'
    r'(?:(?:福|体|福彩|体彩)\s*)?(?:合计|总计)\s*(?P<total>' + _AMOUNT + r')\s*[元米块]?\s*$',
    re.I,
)
_ORDER = re.compile(r'\s*(?P<play>组六|组三)\s*(?P<amount>' + _AMOUNT + r')\s*(?P<unit>[元米块]?)', re.I)
_TOTAL = re.compile(r'(?:合计|总计)\s*[:：]?\s*' + _AMOUNT, re.I)
_FORBIDDEN = re.compile(r'撤单|取消|更正|改成|不买|不下|退款')
_SIZE = {4: '四', 5: '五', 6: '六', 7: '七', 8: '八', 9: '九'}


def _decimal(value):
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not result.is_finite() or result < 0 or result.as_tuple().exponent < -2:
        return None
    return result


def infer_receipt_candidates(context):
    """Return structured *candidates* only for a uniquely receipt-backed clause.

    Requires program-provided source segments, precisely one unresolved clause,
    local-confirmed principal and a message-final receipt. This is intentionally
    narrower than a general-purpose free-text parser: no receipt -> no inference.
    """
    if not isinstance(context, dict):
        return None
    raw = context.get('raw_text')
    if not isinstance(raw, str) or not raw.strip() or _FORBIDDEN.search(raw):
        return None
    if unicodedata.normalize('NFKC', raw) != raw:
        return None  # keep source offsets and evidence identical
    segments = context.get('source_segments')
    unresolved = context.get('unresolved_segments')
    confirmed = context.get('local_confirmed')
    if not isinstance(segments, list) or not segments or not isinstance(unresolved, list) or len(unresolved) != 1:
        return None
    if not isinstance(confirmed, list) or not confirmed:
        return None
    if context.get('local_confirmed_count') != len(confirmed):
        return None
    known_total = _decimal(context.get('local_confirmed_amount_yuan'))
    if known_total is None:
        return None
    # Do not silently turn an earlier line's subtotal into a global receipt.
    if len(_TOTAL.findall(raw)) != 1:
        return None
    last = segments[-1]
    if not isinstance(last, dict) or not isinstance(last.get('segment_id'), str):
        return None
    start, end = last.get('start'), last.get('end')
    if type(start) is not int or type(end) is not int or not (0 <= start < end <= len(raw)):
        return None
    if raw[start:end] != last.get('text') or raw[end:].strip():
        return None
    section = unresolved[0]
    if not isinstance(section, dict) or section.get('segment_ids') != [last['segment_id']]:
        return None
    # The line being inferred must be the unresolved line, not an already
    # confirmed local order or a vaguely intersecting fragment.
    unresolved_text = section.get('text')
    if not isinstance(unresolved_text, str) or not unresolved_text.strip():
        return None
    if unresolved_text.strip() not in last['text'] and last['text'] not in unresolved_text:
        return None
    line = last['text']
    match = _LINE.fullmatch(line)
    if not match:
        return None
    marker = (match['prefix'] or '').lower()
    lottery = context.get('lottery')
    if lottery not in ('福彩', '体彩'):
        return None
    if marker and ((marker in ('福', '福彩', '福彩3d', '3d') and lottery != '福彩')
                   or (marker in ('体', '体彩', '排', '排列三') and lottery != '体彩')):
        return None
    pool = match['pool']
    # A 4-9 digit original combined group pool has a published per-pool rule.
    # Special two/three-digit variants and explicit permutation plays remain
    # for the existing interpreter, not this fast path.
    if len(pool) not in _SIZE or len(set(pool)) != len(pool):
        return None
    orders_text = match['orders']
    if not orders_text or '倍' in orders_text:
        return None
    orders = []
    offset = 0
    while offset < len(orders_text):
        found = _ORDER.match(orders_text, offset)
        if not found or found.end() == offset:
            return None
        amount = _decimal(found['amount'])
        if amount is None or amount <= 0:
            return None
        orders.append((found['play'], amount))
        offset = found.end()
        if len(orders) > 3:
            return None
    if len(orders) != 2 or {x[0] for x in orders} != {'组三', '组六'}:
        return None
    declared = _decimal(match['total'])
    if declared is None or known_total + sum((entry[1] for entry in orders), Decimal(0)) != declared:
        return None
    # This *only* produces a candidate. Amount-vs-multiple and the precise
    # per-period rule will be rechecked by decode_candidate_v257 and validate_full.
    items = [{
        'segment_id': last['segment_id'],
        'bet_type': _SIZE[len(pool)] + '码' + play,
        'digit_set': pool,
        'declared_amount_yuan': float(amount),
        'bet_multiples': None,
        'amount_kind': 'amount',
        'amount_type': 'inferred_from_total',
        'multiple_type': 'missing',
    } for play, amount in orders]
    return {
        'candidate_bets': items,
        'needs_review': False,
        'uncertainties': [],
        'brief_reason': '已有投注金额加各玩法金额与原文合计严格相符，交由本地规则和完整证据二次核验',
    }
