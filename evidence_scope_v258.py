"""V2.5.8 separate-play scope guard for AI candidates.

Use the preexisting evidence_v257 checks for source spans and amount evidence;
this helper only replaces the unsafe rule 'every number has every play anywhere
in the same line'.  Unknown grammar remains with the legacy strict validator.
"""
import re

_PLAY = r'(?:直选|组选|双飞|组三|组六|单选|直|组|单)'
_CODES = r'(?P<codes>\d{2,9}(?:\s*[-,，、/.\s]\s*\d{2,9})*)'
_ORDER = re.compile(_CODES + r'\s*[-,，、/.\s]*' + r'(?P<play>'+_PLAY+r')')


def _type(play):
    if play in ('直选','直','单','单选'):return 'direct'
    if play in ('组选','组'):return 'group'
    if play=='双飞':return 'fly'
    if play=='组三':return 'group3'
    if play=='组六':return 'group6'
    return None


def _matches(bet,kind):
    play=bet.play
    if kind=='direct':return play=='直选'
    if kind=='fly':return play=='双飞'
    if kind=='group':return play in ('组三（组选）','组六（组选）')
    if kind=='group3':return play=='组三（组选）' or play.endswith('码组三')
    if kind=='group6':return play=='组六（组选）' or play.endswith('码组六')
    return False


def verify_segment_plays(text, included):
    """Returns (handled, issue). Only handle fully segmented explicit clauses.

    For every code+play expectation, a corresponding actual bet must exist.
    Also forbid an AI bet whose code+play pairing never occurred in a clause.
    This intentionally leaves unsupported syntaxes to the existing verifier.
    """
    # Decimal point is not a clause separator; using only unambiguous punctuation.
    clauses=[s.strip() for s in re.split(r'[。；;]+',text) if s.strip()]
    if len(clauses)<2:return False,''
    demands=[]
    for clause in clauses:
        # Remove global receipts to avoid interpreting an amount as a ticket.
        clause=re.sub(r'(?:福|体)?(?:合计|总计|总)\s*\d+(?:\.\d+)?\s*[米元块]?\s*$', '', clause)
        matches=list(_ORDER.finditer(clause))
        if len(matches)!=1:return False,''
        m=matches[0]
        # Only numeric prefix, and purchase text afterward. Prevent a prefix
        # amount or loose stray number from being silently dropped.
        prefix=clause[:m.start()].strip(' ,，福体彩排\t')
        if prefix:return False,''
        codes=re.findall(r'(?<!\d)\d{2,9}(?!\d)',m['codes'])
        kind=_type(m['play'])
        if not codes or kind is None:return False,''
        demands.extend((code,kind) for code in codes)
    if len(demands)<2:return False,''
    for code,kind in demands:
        if not any(str(b.selection)==code and _matches(b,kind) for b in included):
            return True,f'原文子句{code}明确购买{kind}，但候选缺少对应投注'
    expected=set(demands)
    for b in included:
        if b.parse_source!='ai_evidence_verified':continue
        if not any(str(b.selection)==code and _matches(b,kind) for code,kind in expected):
            return True,'AI候选把一个投注子句的号码错误关联到其他玩法'
    return True,''
