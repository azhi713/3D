# -*- coding: utf-8 -*-
"""Evidence-scoped checking of an *unlabelled* trailing number.

This module never creates wagers, changes odds or converts a bare number to
money.  It only prevents an uncertain receipt from overriding independently
calculated stake.  A genuine labelled receipt must still be checked.
"""
from __future__ import annotations
import re
import unicodedata
from decimal import Decimal, InvalidOperation

_TAIL = re.compile(r'(?:[，,。;；\n]|\s+)\s*(?P<number>\d{1,7}(?:\.\d{1,2})?)\s*[。；;]?\s*$')
_EXPLICIT = re.compile(r'(?:合计|总计|金额|应收|实收|收款|小计)\s*[:：]?\s*$')


def _dec(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _matches_final_receipt(receipt, value, raw):
    """Only remove an entry attributable to the exact untyped suffix.

    Never drop a separate labelled subtotal earlier in the same message.
    """
    if not isinstance(receipt, dict) or _dec(receipt.get('declared')) != value:
        return False
    end = receipt.get('end')
    if type(end) is int:
        return end >= len(raw.rstrip())
    source = receipt.get('text')
    if isinstance(source, str):
        return bool(_TAIL.search(unicodedata.normalize('NFKC', source).rstrip()))
    return False


def audit_untyped_tail(raw, bets, claimed, receipts):
    """Return None for no evidence, else outcome with money/row adjustments.

    count: last bare integer matches number of separately expanded bets, but
    differs from independently calculated stake. No currency is inferred.
    ambiguous: neither bet count nor stake matches; requires review.
    matches_due: matches independently computed stake; keep prior claim.

    Only considers fully specified bets. Parser/source verification must have
    happened before calling this function.
    """
    if not isinstance(raw, str) or not bets:
        return None
    text = unicodedata.normalize('NFKC', raw).rstrip()
    match = _TAIL.search(text)
    if not match:
        return None
    prefix = text[:match.start('number')]
    if _EXPLICIT.search(prefix):
        return None
    # These are not untyped tails: they explicitly specify currency or count.
    after = text[match.end('number'):].strip(' 。;；\t\n')
    if after:
        return None
    if not re.search(r'(?<!\d)\d{3}(?!\d)|定位|双飞|独胆|跨度|组三|组六|直选', prefix):
        return None
    if not re.search(r'直|单|组|选|定位|双飞|飞|独胆|跨|豹子|对子|复', prefix):
        return None
    number = _dec(match.group('number'))
    if number is None:
        return None
    # Do not alter a clearly annotated count/price attached to the final number.
    if re.search(r'(?:元|米|块|注|倍)\s*$', text[match.start('number'):]):
        return None
    # Numbers attached to a play, e.g. "组三2", are not a loose suffix.
    if re.search(r'(?:直选|组三|组六|组选|直|单|组|定|飞)\s*$', prefix):
        return None
    due = sum((_dec(getattr(b, 'principal', None)) or Decimal(0) for b in bets), Decimal(0))
    count = len(bets)
    if due <= 0:
        return None
    relevant = (_dec(claimed) == number or
                any(_matches_final_receipt(r, number, text) for r in receipts or ()))
    if not relevant:
        return None
    if number == due:
        return {'kind': 'matches_due', 'claimed': claimed, 'receipts': list(receipts or ()),
                'note': '', 'value': float(number), 'due': float(due)}
    if number == Decimal(count) and number == number.to_integral_value():
        kind = 'count'
        note = f'尾数{number:g}没有金额单位，恰与{count}笔投注数量相符；按注数提示保留，未当作客户自报金额；程序核算应收{due:g}元'
    else:
        kind = 'ambiguous'
        note = f'尾数{number:g}没有金额或注数单位，无法确认含义；程序暂算应收{due:g}元，待人工核对'
    kept = [r for r in receipts or () if not _matches_final_receipt(r, number, text)]
    return {'kind': kind, 'claimed': None if _dec(claimed) == number else claimed,
            'receipts': kept, 'note': note, 'value': float(number), 'due': float(due)}
