# -*- coding: utf-8 -*-
"""V2.7.8 narrow, source-backed numeric aliases across existing lottery plays.

No odds are defined here. All priced bets are made by BetParser._parse_part,
which delegates validation to the existing parser and configured rule table.
Returns None for unfamiliar syntax so pre-existing recognizers remain in charge.
No source text is mutated or used to invent numbers/plays.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
import unicodedata

_NUM = r'(?:\d+(?:\.\d{1,2})?)'
_LOT = r'(?:福彩\s*3[Dd]?|福彩|3[Dd]|体彩\s*排列[三3]|排列[三3]|体彩|福|体|排)'
# These are *guards*, not a new play-to-price table. Actual play resolution
# is performed exclusively by the existing BetParser._parse_part.
_PLAY = re.compile(r'(?:直选|单选|直|单|组选|组三|组六|组[36]|双飞|飞|独胆|毒胆|对子|豹子|'
                   r'复试|复式|复|定位|定|和值|合值|和直|跨度|跨|全包|包全|转圈打|胆.*拖)')
_CASH = re.compile(r'(?P<value>\d+(?:\.\d{1,2})?|半|[一二两三四五六七八九十])\s*'
                   r'(?P<unit>元|米|块|角|毛|分)\s*$')
_BARE = re.compile(r'(?P<value>\d+(?:\.\d{1,2})?)\s*$')
_TOTAL = re.compile(r'^(?P<body>[\s\S]*?)(?:合计|总计|总共|合|共)\s*[:：]?\s*'
                    r'(?P<amount>\d+(?:\.\d{1,2})?)\s*(?:元|米|块)?\s*$', re.I)
_SIMPLE_FRAC = re.compile(r'(?<!\d)(?P<a>\d{1,2})\s*/\s*(?P<b>\d{1,2})\s*倍')
_CH_FRAC = re.compile(r'(?P<den>[一二两三四五六七八九十百])分之(?P<num>[一二两三四五六七八九十百])\s*倍|(?P<half>一半)\s*倍')
_BONUS_HALF = re.compile(r'(?<![0-9])(?P<base>\d{1,5}|[一二两三四五六七八九十])\s*倍\s*半')
_CASH_ALIAS=re.compile(r'(?P<val>半|[一二两三四五六七八九十]|\d+(?:\.\d{1,2})?)\s*(?P<unit>米|元|块|毛|角|分)')
_BARE_DIRECT_PREFIX=re.compile(rf'^(?:{_LOT})?\s*[:：,，]?\s*\d{{3}}\s*$',re.I)
# One expressly named position, one digit, and one whole-ticket cash amount.
# This is an alias of the installed 一码定位 play, never a new odds rule.
_POSITION_FIXED_DIGIT_CASH = re.compile(
    rf'^\s*(?:{_LOT}\s*[:：]?\s*)?(?P<position>[百十个])(?:位)?\s*'
    r'定码\s*(?P<digit>[0-9])\s*(?:合计|总计|合|共)\s*[:：]?\s*'
    r'(?P<amount>[0-9]+(?:\.[0-9]{1,2})?)\s*(?:元|米|块)?\s*$', re.I)
_CN_CASH = {'半': Decimal('0.5'), '一':Decimal(1), '二':Decimal(2),
            '两':Decimal(2), '三':Decimal(3), '四':Decimal(4),
            '五':Decimal(5), '六':Decimal(6), '七':Decimal(7),
            '八':Decimal(8), '九':Decimal(9), '十':Decimal(10)}
_CN_FRAC_DIGITS={**_CN_CASH, '百':Decimal(100)}


def _text(value):
    return unicodedata.normalize('NFKC', str(value or '')).replace('⁄','/').replace('\r\n','\n').replace('\r','\n')


def _fmt(value):
    value=format(Decimal(value), 'f')
    return value.rstrip('0').rstrip('.') if '.' in value else value


def _fail(parser, raw, reason):
    parser.last_claimed = None
    parser.last_receipts = []
    parser.last_warnings = []
    return [], parser.normalize_text(raw), reason


def _finish(parser, raw, bets, claimed=None, warnings=(), unit_cash=None):
    amount=sum((Decimal(str(b.principal)) for b in bets), Decimal(0))
    notices=list(warnings)
    if claimed is not None and amount != claimed:
        notices.append(f'自报合计{_fmt(claimed)}元与计算应收{_fmt(amount)}元不一致；保留明确金额或倍数')
    parser.last_claimed=float(claimed) if claimed is not None else None
    parser.last_receipts=[]
    parser.last_warnings=notices
    for b in bets:
        b.raw=raw
        b.source_span={'start':0,'end':len(raw),'text':raw,
                       'granularity':'message','reference':'current_text',
                       'evidence':'full_literal'}
        if unit_cash is not None:
            b.entered_amount_yuan=float(unit_cash)
    return bets,parser.normalize_text(raw),''


def _decompose(raw):
    """An explicit terminal total is a receipt, never another purchase."""
    m=_TOTAL.fullmatch(raw.strip())
    if m:
        return m.group('body').strip(), Decimal(m.group('amount'))
    return raw.strip(), None


def _check_lottery(body, lottery):
    # If an explicit color appears at the start, it must match the target pool.
    m=re.match(r'^\s*('+_LOT+r')(?=\s*[:：,，]?\s*(?:\d|[百十个]|直选|组三|组六|组选|组|全包|包全|双飞|飞|'
               r'独胆|毒胆|对子|豹子|跨度|跨|和值|合值|定位|定|转圈打))', body, re.I)
    if m:
        token=re.sub(r'\s+','',m.group(1)).lower()
        actual=('福彩' if token in ('福','福彩','3d','福彩3d') else '体彩')
        if actual != lottery:
            return False
    return True


def _variants(raw):
    """Normalize only numeric expressions immediately bound to 倍, not code slashes."""
    ambiguities=[]
    def replace_fraction(m):
        a,b=int(m.group('a')),int(m.group('b'))
        if b==0:
            ambiguities.append('分母不能为零');return m.group(0)
        if (a,b)==(4,1):
            ambiguities.append('4/1倍可能是4倍，也可能写反了1/4倍；需结合合计确认')
            return m.group(0)
        val=Decimal(a)/Decimal(b)
        if val<=0 or val.as_tuple().exponent < -2:
            ambiguities.append('分数倍无法精确表示为最多两位小数的合法倍数');return m.group(0)
        return _fmt(val)+'倍'
    normalized=_SIMPLE_FRAC.sub(replace_fraction, raw)
    def chinese_frac(m):
        if m.group('half'):
            return '0.5倍'
        num=_CN_FRAC_DIGITS[m.group('num')]
        den=_CN_FRAC_DIGITS[m.group('den')]
        val=num/den
        if val<=0 or val.as_tuple().exponent < -2:
            ambiguities.append('中文分数倍无法精确换算为最多两位小数的倍数')
            return m.group(0)
        return _fmt(val)+'倍'
    normalized=_CH_FRAC.sub(chinese_frac, normalized)
    def plus_half(m):
        base=m.group('base')
        number=_CN_CASH[base] if base in _CN_CASH else Decimal(base)
        return _fmt(number+Decimal('0.5'))+'倍'
    normalized=_BONUS_HALF.sub(plus_half,normalized)
    def cash_word(m):
        value=m.group('val')
        unit=m.group('unit')
        if value not in _CN_CASH and unit in ('元','米','块'):
            return m.group(0)  # numeric Yuan already canonical; no recursion
        try:
            amount=_value_amount(value,unit)
        except (ValueError,ArithmeticError,InvalidOperation):
            ambiguities.append('金额别名精度非法或金额为零')
            return m.group(0)
        return _fmt(amount)+'米'
    normalized=_CASH_ALIAS.sub(cash_word,normalized)
    return normalized, ambiguities


def _parse_explicit_frac(parser, lottery, original, normalized, claimed):
    """Re-enter current parser with canonical 倍, preserving original evidence."""
    if normalized==original:
        return None
    # No possible infinite recursion: this pass has removed recognized aliases.
    bets, _unused, error = parser.parse(lottery, normalized)
    if error or not bets:
        return _fail(parser, original, '金额/倍数别名已规范化，但原玩法未能完整核实：'+str(error))
    warnings=list(parser.last_warnings)
    if claimed is not None and sum((Decimal(str(b.principal)) for b in bets),Decimal(0))!=claimed:
        # An explicitly stated multiple is authoritative. Never invent a new one
        # because a receipt disagrees; let caller see the discrepancy warning.
        pass
    return _finish(parser,original,bets,claimed,warnings)


def _literal_price(prefix, suffix):
    """Bare cost must have an explicit play immediately before the value."""
    if _BARE_DIRECT_PREFIX.fullmatch(prefix):
        return True  # One literal 3-digit code defaults to direct.
    if not _PLAY.search(prefix):
        return False
    # Do not capture an unknown instruction as a code-price pair.
    if re.search(r'(?:撤单|取消|更正|改成|不买|不要|退单)', prefix):
        return False
    # These word endings are only permitted immediately after an explicit play.
    end=re.search(r'(?:直选|单选|直|单|组选|组三|组六|组[36]|组|双飞|飞|'
                  r'独胆|毒胆|对子|豹子|复试|复式|复|定位|定|和值|合值|和直|'
                  r'跨度|跨|全包|包全|转圈打)(?:各|打|买|投)?\s*$',prefix)
    if end:
        return True
    # Hezhi and span include their target number after the play marker.
    if re.search(r'(?:和值|合值|和直)\s*\d{1,2}(?:打|买|投)?\s*$',prefix):
        return True
    if re.search(r'(?:跨度|跨)\s*\d(?:打|买|投)?\s*$',prefix):
        return True
    if re.search(r'(?:直选|单选|直|单|组选|组三|组六|双飞|独胆|毒胆|对子|复试|复式|复|组[36])\s*\d{1,9}(?:各)?\s*$',prefix):
        return True
    return False


def _source_bets(parser, lottery, original, prefix, mul):
    """Use the established play grammar + price, never make a play by analogy."""
    cleaned=prefix.rstrip()
    attempts=[cleaned + _fmt(mul)+'倍']
    if _BARE_DIRECT_PREFIX.fullmatch(cleaned):
        attempts.append(cleaned+' '+_fmt(mul)+'倍')
    if cleaned.endswith(('打','买','投')):
        attempts.append(cleaned[:-1].rstrip()+' '+_fmt(mul)+'倍')
    for attempt in attempts:
        try:
            bets=parser._parse_part(lottery,attempt)
            if bets:
                return bets
        except (ValueError,KeyError,ArithmeticError):
            pass
    return None


def _fixed_digit_position_cash(parser, lottery, raw, text):
    """Prove `十位定码5 合200米` from the existing 一码定位 unit cost.

    No multiplier is guessed from a grand total spanning other purchases:
    the entire message must be this single explicit position-and-digit order.
    """
    m = _POSITION_FIXED_DIGIT_CASH.fullmatch(text)
    if m is None:
        return None
    if not _check_lottery(text, lottery):
        return _fail(parser, raw, '彩种标记与当前记录不一致')
    amount = Decimal(m['amount'])
    if amount <= 0:
        return _fail(parser, raw, '定码投注金额必须大于零')
    selection = m['position'] + ':' + m['digit']
    baseline = parser._make(lottery, raw, '一码定位', selection, 1)
    unit = Decimal(str(baseline.principal))
    if unit <= 0:
        return _fail(parser, raw, '一码定位单倍本金配置无效')
    multiple = amount / unit
    if multiple.as_tuple().exponent < -2:
        return _fail(parser, raw, '金额无法精确换算为合法倍数（最多两位小数）')
    bet = parser._make(lottery, raw, '一码定位', selection,
                       parser._multiplier(format(multiple, 'f')))
    if Decimal(str(bet.principal)) != amount:
        return _fail(parser, raw, '一码定位金额与核算本金不一致')
    return _finish(parser, raw, [bet], claimed=amount, unit_cash=amount)


def _value_amount(value, unit):
    amount=_CN_CASH[value] if value in _CN_CASH else Decimal(value)
    if unit in ('角','毛'):amount /=10
    elif unit=='分':amount /=100
    if amount<=0 or amount.as_tuple().exponent < -2:
        raise ValueError('金额必须大于零且精确到分')
    return amount


def _price_alias(parser, lottery, original, body, claimed):
    if re.search(r'(撤单|取消|更正|改成|不买|不要|退单)',body):
        return None
    cash=_CASH.search(body)
    if cash:
        amount=_value_amount(cash.group('value'),cash.group('unit'))
        prefix=body[:cash.start()]
        mode='cash'
    else:
        bare=_BARE.search(body)
        if not bare or claimed is None:
            return None
        amount=Decimal(bare.group('value'))
        prefix=body[:bare.start()]
        mode='bare'
    if not _literal_price(prefix,str(amount)):
        return None
    if not _check_lottery(prefix,lottery):
        return _fail(parser,original,'彩种标记与当前记录不一致')
    # A one-multiple probe proves exactly which bets this clause would buy.
    baseline=_source_bets(parser,lottery,original,prefix,Decimal(1))
    if not baseline:
        return None  # Unfamiliar plays handled by existing recognizers and AI.
    costs=[Decimal(str(b.principal)) for b in baseline]
    if any(x<=0 for x in costs):
        return _fail(parser,original,'玩法单倍本金无效')
    if mode=='cash':
        # Explicit cash for one complete selection is unambiguous. Multiple
        # selections require an '各' marker or a matching whole-message receipt.
        unit=costs[0]
        if len(set(costs))!=1:
            return None
        if len(costs)>1 and '各' not in prefix:
            if claimed is None or amount*len(costs)!=claimed:
                return None
        multiplier=amount/unit
        expected=amount*len(costs)
        if multiplier<=0 or multiplier.as_tuple().exponent < -2:
            return _fail(parser,original,'金额无法精确换算为合法倍数（最多两位小数）')
        new=_source_bets(parser,lottery,original,prefix,multiplier)
        if not new or len(new)!=len(baseline):
            return None
        if [(b.play,b.selection) for b in new] != [(b.play,b.selection) for b in baseline]:
            return _fail(parser,original,'原文与重算选号不一致')
        due=sum((Decimal(str(b.principal)) for b in new),Decimal(0))
        if due!=expected:
            return _fail(parser,original,'配置本金与金额回推结果不一致')
        return _finish(parser,original,new,claimed,unit_cash=amount)
    # Bare value without 元/米/块/倍 is not intrinsically money or multiples.
    # The separate whole-message receipt can prove exactly one interpretation.
    # This also handles `1479四码组六10合100` => ten multiples; and
    # `1479四码组六100合100` => amount 100 yuan, ten multiples.
    candidates=[]
    for multiplier in (amount/sum(costs),amount):
        if multiplier<=0 or multiplier.as_tuple().exponent < -2:
            continue
        new=_source_bets(parser,lottery,original,prefix,multiplier)
        if not new or len(new)!=len(baseline):
            continue
        if [(b.play,b.selection) for b in new] != [(b.play,b.selection) for b in baseline]:
            continue
        due=sum((Decimal(str(b.principal)) for b in new),Decimal(0))
        if due != claimed:
            continue
        signature=tuple((b.play,b.selection,str(b.multiplier)) for b in new)
        if signature not in {x[0] for x in candidates}:
            candidates.append((signature,new))
    if len(candidates)!=1:
        return None if not candidates else _fail(parser,original,'合计金额不能唯一判断裸数字代表金额还是倍数')
    return _finish(parser,original,candidates[0][1],claimed)


def parse_alias_v278(parser, lottery, raw):
    """Return a fully verified parser triple, or None to retain previous path."""
    if not isinstance(raw,str) or not raw.strip() or lottery not in ('福彩','体彩'):
        return None
    text=_text(raw)
    if re.search(r'(?:撤单|取消|更正|改成|不买|不要|退单)',text):
        return None
    body, claimed=_decompose(text)
    normalized, issues=_variants(text)
    if issues:
        # '4/1倍' deliberately has no implicit direction. If receipt uniquely
        # validates one of two meanings, recover; otherwise keep for review.
        if len(issues)==1 and '4/1倍' in issues[0] and '4/1倍' in text and claimed is not None:
            good=[]
            for replacement in ('0.25倍','4倍'):
                alternative=text.replace('4/1倍',replacement)
                bets,_,error=parser.parse(lottery,alternative)
                due=sum((Decimal(str(b.principal)) for b in bets),Decimal(0))
                if bets and not error and due==claimed:
                    good.append((replacement,bets,list(parser.last_warnings)))
            if len(good)==1:
                replacement,bets,warnings=good[0]
                return _finish(parser,raw,bets,claimed,warnings+['4/1倍写法有歧义，按整条准确合计核定为'+replacement])
        return _fail(parser,raw,'；'.join(issues))
    if normalized!=text:
        return _parse_explicit_frac(parser,lottery,raw,normalized,claimed)
    try:
        fixed_position = _fixed_digit_position_cash(parser, lottery, raw, text)
        if fixed_position is not None:
            return fixed_position
        result=_price_alias(parser,lottery,raw,body,claimed)
    except (ValueError,InvalidOperation,KeyError,ArithmeticError) as exc:
        return _fail(parser,raw,str(exc))
    return result
