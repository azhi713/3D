"""先拆投注，后识别；整条必须完整识别，禁止默默漏掉未识别片段。"""
import re
import unicodedata
from decimal import Decimal
from itertools import permutations
from math import prod
from config import LOTTERIES, CN_NUM, RULES_BY_LOTTERY
from models import Bet, cents
from bet_grammar import SourceBlock, message_blocks, normalize_list, strip_receipt, decimal_number

MULT = r'(?:半|[0-9]+(?:\.[0-9]{1,2})?|[零〇一二两三四五六七八九十百千万]+)'
CN_MULT_START = r'[零〇一二两三四五六七八九十百千万]'
NAMED_SIZE = r'(?:(二|三|四|五|六|七|八|九|[2-9])码)?'
ALIASES = {
    '直选': ['直选', '单选', '直', '单'],
    '组三': ['组三', '组3'], '组六': ['组六', '组6'],
    '组选': ['组选', '组'], '复式': ['复式', '复试', '复'],
    '独胆': ['独胆', '毒胆'], '双飞': ['双飞'],
    '对子': ['对子'], '豹子': ['豹子'],
}
ALIAS_PATTERN = '|'.join(re.escape(x) for x in sorted({x for v in ALIASES.values() for x in v}, key=len, reverse=True))
ALIAS_LOOKUP = {alias: play for play, aliases in ALIASES.items() for alias in aliases}


LOTTERY_TOKEN = r'(?:福彩\s*3[Dd]|福彩|3[Dd]|体彩\s*排列[三3]|体彩|排列[三3]|排|体|福)'


def lottery_from_token(token):
    token = re.sub(r'\s+', '', unicodedata.normalize('NFKC', token or '')).lower()
    return '福彩' if token in ('福', '福彩', '3d', '福彩3d') else '体彩'


def _summary_lottery(line):
    """大底汇总行里的彩种；返回None表示不是汇总行。"""
    match = re.match(r'^\s*('+LOTTERY_TOKEN+r')\s*\d+\s*注', unicodedata.normalize('NFKC', line or ''), re.I)
    return lottery_from_token(match[1]) if match else None


def split_lottery_message(text, default_lottery):
    """按显式“福/3D/体/排”等标记拆分彩种；未标记内容沿用默认彩种。"""
    raw = unicodedata.normalize('NFKC', text or '')
    if default_lottery not in LOTTERIES:
        raise ValueError('请选择福彩或体彩')

    # “福体”是同一下注同时购买两种彩种，必须保留两份原文分别核算。
    if re.match(r'^\s*(?:福体|体福)', raw):
        return {'福彩': raw.strip(), '体彩': raw.strip()}

    # 大底格式的彩种写在号码块后面的汇总行，必须把“号码块+汇总行”作为整体路由。
    lines = raw.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    if any(_summary_lottery(line) for line in lines):
        grouped = {'福彩': [], '体彩': []}
        buffer = []
        last_target = default_lottery
        saw = False
        for line in lines:
            target = _summary_lottery(line)
            if target:
                saw = True
                block = '\n'.join(buffer + [line]).strip()
                if block:
                    grouped[target].append(block)
                buffer = []
                last_target = target
            else:
                buffer.append(line)
        tail = '\n'.join(buffer).strip()
        if tail:
            grouped[last_target].append(tail)
        if saw:
            return {lottery: '\n\n'.join(parts) for lottery, parts in grouped.items() if parts}

    # 普通消息：彩种标记从当前位置一直作用到下一个彩种标记。
    marker = re.compile(
        r'(?:(^|[\s，,;；。\n])|(?<=倍))(?P<token>'+LOTTERY_TOKEN+r')(?=\s*[:：,.，、。]?\s*(?:[0-9百十个全豹跨组直单复定和合]|$))',
        re.I,
    )
    matches = list(marker.finditer(raw))
    if not matches:
        return {default_lottery: raw.strip()}

    grouped = {'福彩': [], '体彩': []}
    prefix = raw[:matches[0].start()].strip(' \t\r\n，,;；。')
    if prefix:
        grouped[default_lottery].append(prefix)
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        target = lottery_from_token(match.group('token'))
        segment = raw[match.start('token'):end].strip(' \t\r\n，,;；。')
        if segment:
            grouped[target].append(segment)
    return {lottery: '\n'.join(parts) for lottery, parts in grouped.items() if parts}


def chinese_int(text):
    if text.isascii() and text.isdigit():
        return int(text)
    digits = dict(zip('零〇一二两三四五六七八九', [0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9]))
    units = {'十': 10, '百': 100, '千': 1000, '万': 10000}
    if not any(c in units for c in text):
        return int(''.join(str(digits[c]) for c in text))
    total, section, current = 0, 0, 0
    for char in text:
        if char in digits:
            current = digits[char]
        elif char == '万':
            total += (section + current) * 10000
            section, current = 0, 0
        else:
            section += (current or 1) * units[char]
            current = 0
    return total + section + current


class BetParser:
    def __init__(self, rules=None, policy=None):
        self.rules = rules or RULES_BY_LOTTERY
        self.policy = policy or {'position_cost': 'combination', 'span0_wins': True}
        self.last_claimed = None
        self.last_warnings = []
        self.last_receipts = []

    @staticmethod
    def normalize_text(text):
        t = unicodedata.normalize('NFKC', text or '').replace('×', 'x').replace('X', 'x')
        t = re.sub(r'[，,;；。\n]+', '，', t)
        return re.sub(r'[^\S\n]+', ' ', t).strip(' ，')

    def normalize_for_duplicate(self, text):
        return re.sub(r'[\s，,;；。/、:]+', '', self.normalize_text(text)).lower()

    def _parse_repeated_lottery_blocks(self, lottery, text):
        """同条多次出现同彩种标题时，先安全按完整段落本地解析。"""
        raw = unicodedata.normalize('NFKC', text or '').replace('\r\n', '\n')
        # 仅接受行首有明确彩种、后接完整号码或定位标题的段落；不改原文。
        marker = re.compile(
            r'(?m)^[ \t]*(?P<token>'+LOTTERY_TOKEN+r')(?=\s*(?:定位|[百十个]|\d{3,9}))',re.I)
        matches=list(marker.finditer(raw))
        if len(matches)<2 or raw[:matches[0].start()].strip():
            return None
        if any(lottery_from_token(m.group('token')) != lottery for m in matches):
            return None
        all_bets=[]
        for i,match in enumerate(matches):
            end=matches[i+1].start() if i+1<len(matches) else len(raw)
            block=raw[match.start():end].strip()
            sub=BetParser(self.rules,self.policy)
            bets,_,error=sub.parse(lottery,block)
            if error or not bets:
                return None
            all_bets.extend(bets)
        # 各段的自报金额只核对本段，不能自动当成整条消息总金额。
        self.last_claimed=None
        return all_bets

    def parse(self, lottery, text):
        from semantic import parse_message,SemanticError,requires_semantic
        base=self._parse_pipeline(lottery,text)
        metadata=(self.last_claimed,list(self.last_receipts),list(self.last_warnings))
        if base[0] and not requires_semantic(text):
            return base
        try:
            result=parse_message(self,lottery,text)
        except (SemanticError,ValueError,KeyError) as exc:
            self.last_claimed=getattr(exc,'declared',None)
            self.last_warnings=[]
            self.last_receipts=[]
            return [],self.normalize_text(text),str(exc)
        if result is not None:
            bets,declared,receipts,warnings=result
            self.last_claimed,self.last_receipts,self.last_warnings=declared,receipts,warnings
            return bets,self.normalize_text(text),''
        self.last_claimed,self.last_receipts,self.last_warnings=metadata
        return base

    def _parse_pipeline(self, lottery, text):
        """消息先分块/标注金额；玩法与收费仍由同一组严格校验负责。"""
        self.last_claimed=None
        self.last_warnings=[]
        self.last_receipts=[]
        if not isinstance(text,str) or not text.strip():
            return [],self.normalize_text(text),'内容为空'
        # 大底注数、双彩共享金额有自己的整体契约，不先切成普通投注。
        if re.search(r'\d+\s*注|^\s*(?:福体|体福)',text):
            return self._parse_legacy(lottery,text)
        blocks=message_blocks(text)
        all_bets=[]
        receipts=[]
        warnings=[]
        grand=None
        declared=None
        # 多行消息末尾合计是全局声明，其前的裸小计仍属于该行。
        if len(blocks)>1:
            tail=re.search(r'(?:福|体)?\s*(?:合计|总计)\s*[:：]?\s*(\d+(?:\.\d{1,2})?)\s*[元米块]?\s*$',blocks[-1].text)
            if tail:
                grand=float(tail[1])
                blocks[-1].text=blocks[-1].text[:tail.start()].rstrip(' ,，。;；')
        try:
            for block in blocks:
                if not block.text:
                    continue
                standard,declared=strip_receipt(block.text,lambda x:decimal_number(x,chinese_int))
                standard=normalize_list(standard)
                # 同一行分号/逗号仍可划分独立投注；号码列表已先规范化。
                bets,_,error=self._parse_legacy(lottery,standard)
                if not bets:
                    raise ValueError(error)
                if declared is None:
                    declared=self.last_claimed
                for bet in bets:
                    bet.source_span={'start':block.start,'end':block.end,'text':text[block.start:block.end]}
                all_bets.extend(bets)
                warnings.extend(self.last_warnings)
                if declared is not None:
                    subtotal=sum(cents(b.principal) for b in bets)/100
                    receipts.append({'start':block.start,'end':block.end,'declared':declared,'computed':subtotal})
                    if cents(declared)!=cents(subtotal):
                        warnings.append(f'行金额不一致：自报{declared:g}元，应收{subtotal:g}元')
            if not all_bets:
                raise ValueError('未找到完整投注')
            self.last_receipts=receipts
            self.last_claimed=(grand if grand is not None else
                               sum(row['declared'] for row in receipts) if receipts and len(receipts)==len([b for b in blocks if b.text]) else None)
            self.last_warnings=warnings
            return all_bets,self.normalize_text(text),''
        except (ValueError,KeyError) as exc:
            self.last_claimed=grand if grand is not None else (declared if len(blocks)==1 else None)
            self.last_receipts=receipts
            self.last_warnings=warnings
            return [],self.normalize_text(text),str(exc)

    def _parse_legacy(self, lottery, text):
        self.last_claimed = None
        self.last_warnings = []
        try:
            if lottery not in LOTTERIES:
                raise ValueError('请选择福彩或体彩')
            if not isinstance(text, str) or not text.strip():
                raise ValueError('内容为空')
            # 先尝试同彩种的多行订单块；失败时回退原来的整条识别。
            grouped = self._parse_repeated_lottery_blocks(lottery, text)
            if grouped is not None:
                return grouped, self.normalize_text(text), ''
            # 交接包已确认的“福体537一单一组8”——8元是双彩总额。
            dual = re.fullmatch(
                r'\s*(?:福体|体福)\s*([0-9]{3})\s*一单一组\s*([0-9]+(?:\.[0-9]{1,2})?)\s*(?:元|米|块)?\s*',
                unicodedata.normalize('NFKC', text),
            )
            if dual:
                total = Decimal(dual[2])
                multiplier = self._multiplier(str(total / 8))
                bets = (self._from_kind(lottery, text, dual[1], '直选', multiplier)
                        + self._from_kind(lottery, text, dual[1], '组选', multiplier))
                self.last_claimed = float(total / 2)
                if sum(cents(b.principal) for b in bets) != cents(total / 2):
                    raise ValueError('福体合计与两个彩种的下注金额不一致')
                return bets, self.normalize_text(text), ''

            # 支持玩法在首行、候选位置分行、最后附人工算式的下单。
            special = self._parse_position_with_receipt(lottery, text)
            if special is not None:
                return special, self.normalize_text(text), ''
            big = self._parse_big_bottom(lottery, text)
            if big is not None:
                bets, normalized = big
                if not bets:
                    raise ValueError('未找到完整投注')
                return bets, normalized, ''
            line = self.normalize_text(text)
            if not line:
                raise ValueError('内容为空')
            prefix = re.match(r'^('+LOTTERY_TOKEN+r')\s*(?:[，,]\s*)?', line, re.I)
            if prefix:
                requested = lottery_from_token(prefix[1])
                if requested != lottery:
                    raise ValueError('文字彩种与所选彩种冲突，请核对')
                line = line[prefix.end():]
            # 不带“倍”的“一单一组8”按已确认模板的金额处理，不当作8倍。
            shared = re.fullmatch(
                r'([0-9]{3}(?:\s+[0-9]{3})*)\s*一单一组\s*([0-9]+(?:\.[0-9]{1,2})?)\s*(?:元|米|块)?',
                line,
            )
            if shared:
                numbers = re.findall(r'[0-9]{3}', shared[1])
                declared = Decimal(shared[2])
                multiple = declared / (Decimal(4) * len(numbers))
                if multiple not in (Decimal('0.5'), Decimal('1'), Decimal('2')):
                    raise ValueError('一单一组未写倍数，金额可能有歧义，请人工确认')
                multiplier = self._multiplier(str(multiple))
                bets = []
                for number in numbers:
                    bets.extend(self._from_kind(lottery, text, number, '直选', multiplier))
                    bets.extend(self._from_kind(lottery, text, number, '组选', multiplier))
                self.last_claimed = float(declared)
                return bets, self.normalize_text(text), ''
            amount = None if re.search(r'(?:和值|合值)\s*\d{1,2}\s*(?:打|买|投)\s*'+MULT+r'\s*[元米块]$',line) else re.search(r'(?:[， ]*(?:合计|总计|金额|共)\s*[:：]?\s*([0-9]+(?:\.[0-9]{1,2})?)\s*元?|\s*([0-9]+(?:\.[0-9]{1,2})?)\s*元)\s*$', line)
            if amount:
                self.last_claimed = cents(amount[1] or amount[2]) / 100
                line = line[:amount.start()].strip(' ，')
            # 旧数据中“倍30”明确作为末尾自报金额，不把30吞进号码。
            trailing = re.search(r'(?<=倍)([0-9]+(?:\.[0-9]{1,2})?)$', line)
            if trailing:
                self.last_claimed = cents(trailing[1]) / 100
                line = line[:trailing.start()]
            bets = []
            # 多行定位：如“百789\n十039\n个0129\n定位各6倍”。
            # normalize_text 会把换行变成逗号，这里先把整块重新拼回一笔投注。
            position_line = re.sub(r'[，,]+', ' ', line).strip()
            if re.fullmatch(r'(?:定位\s*)?(?:(?:[百十个](?:位)?\s*[:：]?\s*[0-9]+)\s*){1,3}(?:定位|定)?\s*(?:各\s*)?'+MULT+r'\s*倍?', position_line):
                bets.extend(self._parse_part(lottery, position_line))
            else:
                for chunk in line.split('，'):
                    if not chunk.strip():
                        continue
                    for part in self._split(chunk.strip()):
                        bets.extend(self._parse_part(lottery, part))
            if not bets:
                raise ValueError('未找到完整投注')
            return bets, self.normalize_text(text), ''
        except (ValueError, KeyError) as exc:
            return [], self.normalize_text(text), str(exc)

    def parse_partial(self, lottery, text):
        """仅在整条解析失败后，保守保留独立、显式分隔且可完整验证的子项。

        不跨段猜测玩法/倍数；带合计、定位、跨彩种等上下文的复杂消息继续整体待审。
        返回(已验证投注, 未解决片段[原文字符位置], 规范化原文)。
        """
        bets, normalized, reason = self.parse(lottery, text)
        if bets:
            return bets, [], normalized
        pending_whole = [{'text': text, 'start': 0, 'end': len(text), 'reason': reason}]
        if not isinstance(text, str) or not text.strip() or lottery not in LOTTERIES:
            return [], pending_whole, normalized
        if re.search(r'福家|体家|福体|体福|撤单|取消|更正|修改|改成|不买|停', text):
            return [], pending_whole, normalized
        grouped=message_blocks(text)
        if len(grouped)>1:
            parts=grouped
        else:
            # 先规范选号列表，只有独立投注间的标点才参与分段。
            prepared=normalize_list(text)
            parts=[SourceBlock(m.group(),m.start(),m.end())
                   for m in re.finditer(r'[^，,;；。\r\n]+', prepared)]
        if len(parts) < 2:
            return [], pending_whole, normalized
        found, unresolved = [], []
        for part in parts:
            fragment = part.text.strip()
            if not fragment:
                continue
            # “福/体/3D”等单独彩种标题只负责后续段落归属，本身不是投注。
            # 整条含其他投注时跳过标题，不让AI为这个单字胡乱补号码。
            if re.fullmatch(LOTTERY_TOKEN, fragment, re.I) and lottery_from_token(fragment) == lottery:
                continue
            start = part.start + len(part.text) - len(part.text.lstrip())
            end = part.end - len(part.text) + len(part.text.rstrip())
            if re.fullmatch(r'(?:合计|总计|金额)\s*\d+(?:\.\d{1,2})?[元米块]?',fragment):
                continue
            # 只有明确写了玩法及倍率的独立投注，才能在整条失败时先入风险。
            has_play = re.search(r'直|单|组|复式|复试|复|独|双飞|对子|豹子|跨|全包|转圈打|定位|[百十个]|和值|合值', fragment)
            has_multiplier = re.search(r'倍|半|[0-9]+\s*[元米块]', fragment)
            if not has_play or not has_multiplier:
                unresolved.append({'text': fragment, 'start': start, 'end': end,
                                   'reason': '缺少独立明确的玩法或倍数'})
                continue
            probe = BetParser(self.rules, self.policy)
            item_bets, _, error = probe.parse(lottery, fragment)
            if item_bets:
                found.extend(item_bets)
            else:
                unresolved.append({'text': fragment, 'start': start, 'end': end,
                                   'reason': error})
        if not found:
            return [], pending_whole, normalized
        # 整条未完成时不能把子项小计当成整条实收金额。
        self.last_claimed = None
        return found, unresolved, normalized

    def _parse_position_with_receipt(self, lottery, text):
        """严格匹配“排定位 / 百069 / 十568 / 个14 / 8倍8*36=288”。

        仅定位原文完整符合此模板时处理。末尾算式只用于核验，
        算式与规则产生的应收不一致则放待确认，不能悄悄计入风险。
        """
        raw = unicodedata.normalize('NFKC', text).replace('×', '*').replace('X', '*')
        lines = [line.strip() for line in re.split(r'[\r\n]+', raw) if line.strip()]
        if len(lines) < 3:
            return None
        header = re.fullmatch(r'(?:(?P<lot>'+LOTTERY_TOKEN+r')\s*)?(?:定位|定)', lines[0], re.I)
        if not header:
            return None
        if header.group('lot') and lottery_from_token(header.group('lot')) != lottery:
            raise ValueError('文字彩种与所选彩种冲突')
        position_lines = lines[1:-1]
        if not 1 <= len(position_lines) <= 3 or not all(
            re.fullmatch(r'[百十个](?:位)?\s*[:：]?\s*[0-9]+', s) for s in position_lines
        ):
            return None
        tail = re.fullmatch(
            r'(?:定位\s*)?(?:各\s*)?(?P<mult>'+MULT+r')\s*倍'
            r'(?:\s*(?P<factor>\d+(?:\.\d+)?)\s*\*\s*(?P<unit>\d+(?:\.\d+)?)'
            r'\s*=\s*(?P<total>\d+(?:\.\d+)?))?',
            lines[-1], re.I,
        )
        if not tail:
            return None
        standard = ''.join(position_lines) + '定位' + tail.group('mult') + '倍'
        bets = self._parse_part(lottery, standard)
        if len(bets) != 1:
            raise ValueError('定位内容未能唯一识别')
        if tail.group('factor') is not None:
            if cents(tail.group('factor')) != cents(bets[0].multiplier):
                raise ValueError('定位人工算式的倍数与下单倍数不一致')
            if cents(tail.group('factor')) * cents(tail.group('unit')) // 100 != cents(tail.group('total')):
                raise ValueError('定位人工算式计算结果不一致')
            if cents(tail.group('total')) != cents(bets[0].principal):
                raise ValueError('定位人工算式金额与实际应收不一致')
            self.last_claimed = float(tail.group('total'))
        return bets

    def _parse_big_bottom(self, lottery, text):
        """大底：号码块 + “福/体N注[直选/组选][半倍/1倍]合金额”。"""
        raw = unicodedata.normalize('NFKC', text or '').replace('\r\n', '\n').replace('\r', '\n')
        lines = raw.split('\n')
        summary_re = re.compile(
            r'^\s*(?P<lot>'+LOTTERY_TOKEN+r')?\s*(?P<count>\d+)\s*注\s*'
            r'(?P<lot2>'+LOTTERY_TOKEN+r')?\s*'
            r'(?P<play>直选|单选|直|单|组选|组)?\s*'
            r'(?P<mult>半倍|0\.5倍|'+MULT+r'\s*倍)?\s*'
            r'(?:(?:🈴|合计|合|共)\s*[:：]?\s*(?P<amount>\d+(?:\.\d{1,2})?)\s*元?)?\s*$',
            re.I,
        )
        groups = []
        buffer = []
        saw_summary = False
        for line in lines:
            match = summary_re.fullmatch(line.strip())
            if not match:
                if line.strip():
                    buffer.append(line.strip())
                continue
            saw_summary = True
            number_text = ' '.join(buffer).strip()
            buffer = []
            if not number_text:
                raise ValueError('大底汇总行前没有三位号码')
            if re.sub(r'\s+', ' ', number_text) and not re.fullmatch(r'\d{3}(?:\s+\d{3})*', re.sub(r'\s+', ' ', number_text)):
                raise ValueError('大底号码块只能包含三位号码和空格/换行')
            numbers = re.findall(r'(?<!\d)\d{3}(?!\d)', number_text)
            target_tokens = [x for x in (match.group('lot'), match.group('lot2')) if x]
            targets = {lottery_from_token(x) for x in target_tokens}
            if len(targets) > 1:
                raise ValueError('大底汇总行彩种标记冲突')
            target = next(iter(targets), lottery)
            groups.append((target, numbers, match, number_text))
        if not saw_summary:
            return None
        if any(line.strip() for line in buffer):
            raise ValueError('大底末尾存在没有汇总行的号码块')

        bets = []
        claimed_total = 0.0
        all_claimed = True
        for target, numbers, match, number_text in groups:
            if target != lottery:
                continue
            declared_count = int(match.group('count'))
            if declared_count != len(numbers):
                raise ValueError(f'大底注数不一致：实际{len(numbers)}注，报单写{declared_count}注')
            play_token = (match.group('play') or '直选').strip()
            kind = '组选' if play_token in ('组选', '组') else '直选'
            mult_text = match.group('mult') or '1倍'
            mult_text = re.sub(r'\s+', '', mult_text)
            mult_value = mult_text[:-1] if mult_text.endswith('倍') else mult_text
            multiplier = self._multiplier(mult_value)
            for number in numbers:
                bets.extend(self._from_kind(lottery, number_text, number, kind, multiplier))
            duplicate_numbers = sorted({n for n in numbers if numbers.count(n) > 1})
            if duplicate_numbers:
                self.last_warnings.append('大底发现重复号码：' + ' '.join(duplicate_numbers) + '；已按重复投注保留')
            amount = match.group('amount')
            if amount is None:
                all_claimed = False
            else:
                declared = cents(amount) / 100
                due = sum(b.principal for b in bets[-len(numbers):])
                if cents(declared) != cents(due):
                    raise ValueError(f'大底合计金额不一致：实际应收{due:g}元，报单写{declared:g}元')
                claimed_total += declared
        if not bets:
            raise ValueError('这条大底数据没有属于当前彩种的投注')
        self.last_claimed = claimed_total if all_claimed else None
        return bets, self.normalize_text(text)

    def _split(self, chunk):
        # 把已经带倍数结束的投注与后面的投注分开。
        starts = r'(?:[0-9百十个全豹跨]|组[三六36]\s*(?:全包|包全|全)|定位\s*[百十个]|(?:'+ALIAS_PATTERN+r')\s*[0-9]+(?:\s+|x|(?='+CN_MULT_START+r'))'+MULT+r'\s*倍)'
        chunks = re.split(r'(?<=倍)\s*(?='+starts+r')', chunk)
        out = []
        for part in chunks:
            # 交接包确认的特殊习惯：095组505组三一倍，共享末尾倍数。
            match = re.fullmatch(r'([0-9]{3})组\s*([0-9]{3})组三\s*('+MULT+r')倍', part)
            if match and len(set(match[1])) == 3 and len(set(match[2])) == 2:
                out.extend([f'{match[1]}组三{match[3]}倍', f'{match[2]}组{match[3]}倍'])
            else:
                out.append(part)
        return out

    def _multiplier(self, value):
        value = str(value).strip()
        if value == '半':
            return 0.5
        if re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?', value):
            number = Decimal(value)
            if not 0 < number <= 1000000:
                raise ValueError('倍数须大于0且不超过1000000')
            return int(number) if number == int(number) else float(number)
        result = chinese_int(value)
        if not 1 <= result <= 1000000:
            raise ValueError('倍数须大于0且不超过1000000')
        return result

    def _make(self, lottery, raw, play, selection, mult):
        self.validate(play, selection, mult)
        rule = self.rules[lottery][play]
        units = 1
        if play in ('组六复式','组三复式'):
            n=len(selection)
            units=n*(n-1)*(n-2) if play=='组六复式' else 3*n*(n-1)
            cost=cents(rule['principal'])*units
        elif play.endswith('码定位'):
            positions = dict(item.split(':') for item in selection.split('|'))
            units = prod(len(v) for v in positions.values())
            # 定位收费已按实盘确认：一码/两码每个展开组合1倍10元；三码每个展开组合1倍2元。
            unit_principal = {'一码定位': 10, '两码定位': 10, '三码定位': 2}[play]
            cost = cents(unit_principal) * units
        else:
            # 复试与其它玩法一样，严格使用《明细规则2.xlsx》的固定单倍本金。
            cost = cents(rule['principal'])
        principal = Decimal(cost) * Decimal(str(mult)) / 100
        bet=Bet(play, selection, mult, float(principal), raw)
        if re.fullmatch(r'1胆[2-9]拖组3',play):
            bet.settlement_rule_pending=['组三胆拖重号极端覆盖仍待确认']
        return bet

    @staticmethod
    def validate(play, selection, mult):
        # 明确小数倍按金额精确核算；奖金小数倍的最终派奖边界另行确认。
        try:
            number = Decimal(str(mult))
        except Exception:
            raise ValueError('倍数必须为正数，最多两位小数') from None
        if (type(mult) is bool or not number.is_finite() or
                number <= 0 or number > 1000000 or number.as_tuple().exponent < -2):
            raise ValueError('倍数必须为正数，最多两位小数')
        if re.fullmatch(r'1胆[2-9]拖组[36]',play):
            if not re.fullmatch(r'[0-9]:[0-9]{2,9}',selection):
                raise ValueError('胆码/拖码格式不合法')
            dan,tuo=selection.split(':')
            if dan in tuo or len(set(tuo))!=len(tuo):
                raise ValueError('胆码拖码不能重叠，拖码不能重复')
            if len(tuo)!=int(play[2]):raise ValueError('拖码数量与赔率档位不符')
            return
        if play.endswith('码定位'):
            items = selection.split('|')
            parts = [item.split(':') for item in items]
            count = {'一码定位': 1, '两码定位': 2, '三码定位': 3}[play]
            if len(parts) != count or any(len(p) != 2 or p[0] not in '百十个' or not re.fullmatch(r'[0-9]+', p[1]) or len(set(p[1])) != len(p[1]) for p in parts) or len({p[0] for p in parts}) != count:
                raise ValueError('定位须明确指定位置，数字不能重复')
            return
        if play in ('全包组三', '全包组六', '豹子'):
            if selection:
                raise ValueError('全包或豹子不需要号码')
            return
        if play in ('组六复式','组三复式'):
            minimum=3 if play=='组六复式' else 2
            if (not re.fullmatch(r'[0-9]+',selection) or not minimum<=len(selection)<=10
                    or len(set(selection))!=len(selection)):
                raise ValueError(f'{play}须使用{minimum}～10个不重复数字')
            return
        if not re.fullmatch(r'[0-9]+', selection):
            raise ValueError('号码必须是0～9组成的数字')
        if play.startswith('和值'):
            if not selection.isdigit() or not 0<=int(selection)<=27 or play!='和值'+str(int(selection)):
                raise ValueError('和值须为0～27')
            return
        if play.startswith('跨度'):
            if selection != play[-1]:
                raise ValueError('跨度须为0～9')
            return
        if play == '直选':
            valid = len(selection) == 3
        elif play == '组三（组选）':
            valid = len(selection) == 3 and len(set(selection)) == 2
        elif play == '组六（组选）':
            valid = len(selection) == 3 and len(set(selection)) == 3
        elif play == '独胆':
            valid = len(selection) == 1
        elif play == '双飞':
            valid = len(selection) == 2  # 交接包确认：44、88双飞也是有效投注
        elif play == '对子':
            valid = len(selection) == 2 and len(set(selection)) == 1
        else:
            count = next((n for n, cn in CN_NUM.items() if play.startswith(cn+'码')), None)
            valid = count is not None and len(selection) == count and len(set(selection)) == count
        if not valid:
            raise ValueError(f'{play}的号码数量或重复情况不正确')

    def _parse_part(self, lottery, raw):
        line = raw.strip()
        line = re.sub(r'组\s+([三六])',r'组\1',line)
        # 复用既有玩法词表归一词内空格，包含新增组合词“组六复式”。
        for aliases in ALIASES.values():
            for alias in aliases:
                if len(alias)>1:
                    line=re.sub(r'\s+'.join(map(re.escape,alias)),alias,line)
        # 同条消息允许重复标记同一彩种，如“福841直2倍，福0127组六40倍”。
        # 只在片段起点且后面确实跟投注内容时移除标记，不改原始数据。
        prefix = re.match(r'^('+LOTTERY_TOKEN+r')\s*(?=[0-9百十个全豹跨组直单复定和合])', line, re.I)
        if prefix:
            if lottery_from_token(prefix.group(1)) != lottery:
                raise ValueError('同一投注片段出现不同彩种，需先拆分彩种')
            line = line[prefix.end():].strip()
        # 两类逐注复式明确独立于“复试”和普通包码；仅这两类按施工单默认1倍。
        new_group=re.fullmatch(r'([0-9]+)\s*(组六\s*复式|组三\s*复式)\s*(?:各\s*)?(?:x\s*)?('+MULT+r')?\s*倍?',line)
        if new_group:
            return [self._make(lottery,raw,re.sub(r'\s+','',new_group[2]),new_group[1],self._multiplier(new_group[3] or '1'))]
        new_group=re.fullmatch(r'(组六\s*复式|组三\s*复式)\s*([0-9]+)(?:(?:\s+|x\s*|(?='+CN_MULT_START+r'))('+MULT+r')\s*倍?)?',line)
        if new_group:
            return [self._make(lottery,raw,re.sub(r'\s+','',new_group[1]),new_group[2],self._multiplier(new_group[3] or '1'))]
        total=re.fullmatch(r'(?:和值|合值)\s*([0-9]{1,2})\s*(?:(?:打|买|投)\s*('+MULT+r')\s*[元米块]|(?:x\s*)?('+MULT+r')\s*倍)',line)
        if total:
            play='和值'+str(int(total[1]))
            if play not in self.rules[lottery]:
                raise ValueError(f'{play}本金和奖金尚未确认，不能自动入账')
            mult=self._multiplier(total[3]) if total[3] else self._multiplier(str(decimal_number(total[2],chinese_int)/Decimal(str(self.rules[lottery][play]['principal']))))
            bet=self._make(lottery,raw,play,str(int(total[1])),mult)
            if total[2]:
                bet.entered_amount_yuan=float(decimal_number(total[2],chinese_int))
            return [bet]
        pooled=self._parse_operations(lottery,raw,line)
        if pooled is not None:
            return pooled
        # 已知玩法词内部的空格只做词法兼容，号码仍保留边界。
        line = re.sub(r'组\s+([三六])', r'组\1', line)
        line = re.sub(r'全\s+包', '全包', line)
        line = re.sub(r'包\s+全', '包全', line)
        for aliases in ALIASES.values():
            for alias in aliases:
                if len(alias) > 1:
                    line = re.sub(r'\s+'.join(map(re.escape, alias)), alias, line)
        # 定位数字池有明确结束标记时，允许空格、斜线或顿号分隔数字。
        line = re.sub(r'([百十个](?:位)?\s*[:：]?\s*)([0-9](?:[ /、]+[0-9])+)(?=\s*(?:[百十个]|定位|定))',
                      lambda m: m[1]+re.sub(r'[ /、]+','',m[2]), line)
        line = re.sub(r'([零〇一二两三四五六七八九十百千万])\s+(?=[零〇一二两三四五六七八九十百千万])', r'\1', line)

        # 交接包确认的批量直组：841.851.857.861.直 2 倍组 1 倍。
        # “.”仅在两个完整三位号码之间充当分隔，不全局替换小数点。
        shared = re.fullmatch(
            r'([0-9]{3}(?:\s*[.．/、\s]\s*[0-9]{3})+)\s*[.．]?\s*'
            r'(?:直选|单选|直|单)\s*('+MULT+r')\s*倍\s*'
            r'(?:组选|组)\s*('+MULT+r')\s*倍', line,
        )
        if shared:
            numbers = re.findall(r'(?<![0-9])[0-9]{3}(?![0-9])', shared[1])
            dm, gm = self._multiplier(shared[2]), self._multiplier(shared[3])
            result = []
            for number in numbers:
                result.extend(self._from_kind(lottery, raw, number, '直选', dm))
                result.extend(self._from_kind(lottery, raw, number, '组选', gm))
            return result

        # 玩法前置 + 一批三位号码 + 统一倍数，例如：组选:003 588 669（一倍）。
        m = re.fullmatch(
            r'(直选|单选|直|单|组选|组)\s*[:：]\s*'
            r'([0-9]{3}(?:\s+[0-9]{3})+)\s*'
            r'(?:\(\s*('+MULT+r')\s*倍?\s*\)|('+MULT+r')\s*倍?)?',
            line,
        )
        if m:
            kind = '组选' if m[1] in ('组选', '组') else '直选'
            multiplier = self._multiplier(m[3] or m[4] or '1')
            out = []
            for number in re.findall(r'[0-9]{3}', m[2]):
                out.extend(self._from_kind(lottery, raw, number, kind, multiplier))
            return out

        # 只有三位号码时默认直选；未写倍数默认1倍。支持“293 154 826 5倍”。
        m = re.fullmatch(r'([0-9]{3}(?:\s+[0-9]{3})*)(?:\s+(?:各\s*)?('+MULT+r')\s*倍)?', line)
        if m:
            multiplier = self._multiplier(m[2] or '1')
            return [self._make(lottery, raw, '直选', number, multiplier)
                    for number in re.findall(r'[0-9]{3}', m[1])]


        # 转圈打按具体直选号码展开；单次投注内部排列去重，跨原始输入不去重。
        round_bet = re.fullmatch(
            r'([0-9]{3}(?:\s*[ /、]\s*[0-9]{3})*)\s*转圈打\s*(?:各\s*)?(?:x\s*)?('+MULT+r')\s*倍?',
            line,
        )
        if round_bet:
            multiplier = self._multiplier(round_bet[2])
            out = []
            for number in re.split(r'\s*[ /、]\s*', round_bet[1]):
                if len(set(number)) == 1:
                    raise ValueError('豹子不能转圈打，请人工核对')
                for direct in sorted({''.join(digits) for digits in permutations(number)}):
                    out.append(self._make(lottery, raw, '直选', direct, multiplier))
            return out

        # 全包关键词前后都合法，包全／全、组3／组6也映射同一玩法。
        groups = r'组[三六36](?:\s*组[三六36])?'
        full = re.fullmatch(r'(?:(?:全包|包全|全)\s*('+groups+r')|('+groups+r')\s*(?:全包|包全|全))\s*(?:各|x)?\s*('+MULT+r')\s*倍?', line)
        if full:
            selected = re.findall(r'组([三六36])', full[1] or full[2])
            plays = ['全包组三' if group in ('三','3') else '全包组六' for group in selected]
            if len(set(plays)) != len(plays):
                raise ValueError('同一全包玩法重复，请分成独立投注')
            multiplier = self._multiplier(full[3])
            return [self._make(lottery, raw, play, '', multiplier) for play in plays]
        # 位置信息先完整匹配，绝不从普通文本抓数字。
        m = re.fullmatch(r'(?:(一码|两码|三码)?定位\s*)?((?:[百十个](?:位)?\s*[:：]?\s*[0-9]+\s*)+)(?:定位|定)?\s*(?:各\s*)?(?:x\s*)?('+MULT+r')\s*倍?', line)
        if m:
            pairs = re.findall(r'([百十个])(?:位)?\s*[:：]?\s*([0-9]+)', m[2])
            if len({p[0] for p in pairs}) != len(pairs):
                raise ValueError('同一位置写了多次，请分成独立投注')
            play = {1: '一码定位', 2: '两码定位', 3: '三码定位'}.get(len(pairs))
            if not play or (m[1] and play != m[1]+'定位'):
                raise ValueError('定位位置数与玩法不符')
            selection = '|'.join(f'{p}:{nums}' for p, nums in sorted(pairs, key=lambda pair: '百十个'.index(pair[0])))
            return [self._make(lottery, raw, play, selection, self._multiplier(m[3]))]
        m = re.fullmatch(r'(豹子)\s*(?:x\s*)?('+MULT+r')\s*倍?', line)
        if m:
            return [self._make(lottery, raw, m[1], '', self._multiplier(m[2]))]
        m = re.fullmatch(r'(?:跨度|跨)\s*([0-9])(?:\s+|x\s*|(?='+CN_MULT_START+r'))('+MULT+r')\s*倍?', line)
        if m:
            return [self._make(lottery, raw, '跨度'+m[1], m[1], self._multiplier(m[2]))]
        m = re.fullmatch(r'([0-9])\s*(?:跨度|跨)\s*(?:x\s*)?('+MULT+r')\s*倍?', line)
        if m:
            return [self._make(lottery, raw, '跨度'+m[1], m[1], self._multiplier(m[2]))]
        # 明确倍字或乘号可支持“293x10倍直选”，不推断粘连数字的分界。
        m = re.fullmatch(r'([0-9]+(?:\s*[ /、]\s*[0-9]+)*)(?:\s+|x\s*)('+MULT+r')\s*倍\s*('+ALIAS_PATTERN+r')', line)
        if m:
            return self._selected_bets(lottery, raw, m[1], ALIAS_LOOKUP[m[3]], self._multiplier(m[2]))
        # 多号码共用明确玩法/倍数，如578 624 541直5组5。
        match = re.fullmatch(r'([0-9]+(?:\s*[ /、]\s*[0-9]+)*)\s*(.+)', line)
        if not match:
            # 兼容独胆7 10倍、双飞27 5倍等玩法在前的写法。
            front = re.fullmatch(NAMED_SIZE+r'('+ALIAS_PATTERN+r')\s*([0-9]+(?:\s*[ /、]\s*[0-9]+)*)(?:\s+|各\s*|x\s*|(?='+CN_MULT_START+r'))('+MULT+r')\s*倍?', line)
            if front:
                return self._selected_bets(lottery, raw, front[3], ALIAS_LOOKUP[front[2]], self._multiplier(front[4]), front[1])
            raise ValueError(f'无法完整识别：{raw}')
        selections = re.split(r'\s*[ /、]\s*', match[1].strip())
        suffix = re.sub(r'\s+', '', match[2])
        # 一单一组 / 单组各 / 三组六各；只共享这笔里的倍数。
        both = re.fullmatch(r'(一单一组|单组各?|直组各?|组三组六各?|组六组三各?)('+MULT+r')倍?', suffix)
        kinds = []
        if both:
            m = self._multiplier(both[2])
            kinds = [('组三', m), ('组六', m)] if '组三' in both[1] else [('直选', m), ('组选', m)]
        else:
            # “两单一组”“十直一组”等有明确数量的直组复合买入，直接本地识别。
            named_pair = re.fullmatch(r'('+MULT+r')(?:单|直)('+MULT+r')(?:组|组选)', suffix)
            if named_pair:
                kinds = [('直选', self._multiplier(named_pair[1])),
                         ('组选', self._multiplier(named_pair[2]))]
            separate = re.fullmatch(r'(?:直|单|直选)('+MULT+r')(?:倍)?(?:组|组选)('+MULT+r')倍?', suffix)
            if separate:
                kinds = [('直选', self._multiplier(separate[1])), ('组选', self._multiplier(separate[2]))]
            elif not kinds:
                single = re.fullmatch(NAMED_SIZE+r'('+ALIAS_PATTERN+r')(?:各|x)?('+MULT+r')倍?', suffix)
                if not single:
                    raise ValueError(f'玩法或倍数缺失／夹杂其他文字：{raw}')
                return self._selected_bets(lottery, raw, match[1], ALIAS_LOOKUP[single[2]], self._multiplier(single[3]), single[1])
        out = []
        if len(selections) > 1 and all(len(item) == 1 for item in selections):
            selections = [''.join(selections)]
        for selection in selections:
            for kind, mult in kinds:
                out.extend(self._from_kind(lottery, raw, selection, kind, mult))
        return out

    def _selected_bets(self, lottery, raw, selection_text, kind, multiplier, declared_size=None):
        selections = re.split(r'\s*[ /、]\s*', selection_text.strip())
        # 多码玩法里单个数字间的空格是同一数字池；三位号码列表仍逐笔保留。
        if len(selections) > 1 and all(len(item) == 1 for item in selections):
            if kind in ('组三','组六','复式','直选','组选','双飞','对子'):
                selections = [''.join(selections)]
        if declared_size and kind not in ('组三','组六','复式'):
            raise ValueError('码数说明只能用于多码组三、组六或复式')
        out = []
        for selection in selections:
            if declared_size and len(selection) != chinese_int(declared_size):
                raise ValueError('声明的码数与实际数字数量不符')
            out.extend(self._from_kind(lottery, raw, selection, kind, multiplier))
        return out

    def _parse_operations(self,lottery,raw,line):
        """号码列表×玩法子句。共享选号但各玩法独立倍数，金额单位独立换算。"""
        sep=r'[ /、.。,:：，\-]+'
        explicit_pair=re.fullmatch(r'([0-9]{3}(?:'+sep+r'[0-9]{3})*)\s*('+MULT+r')(?:单|直)('+MULT+r')(?:组|组选)倍?',line)
        if explicit_pair:
            return [bet for selection in re.split(sep,explicit_pair[1])
                    for kind,mult in (('直选',self._multiplier(explicit_pair[2])),('组选',self._multiplier(explicit_pair[3])))
                    for bet in self._from_kind(lottery,raw,selection,kind,mult)]
        # 选号使用连续数字串；子句前置数量(两单一组)不吞入选号。
        m=re.fullmatch(r'([0-9]{1,9}(?:'+sep+r'[0-9]{1,9})*)\s*('+ALIAS_PATTERN+r'|[一二两三四五六七八九十百]+(?:单|直)|\d+(?:单|直))(.+)',line)
        if not m:
            return None
        selections=re.split(sep,m[1].strip())
        suffix=re.sub(r'\s+','',m[2]+m[3])
        # 独立的已有模板“一单一组金额”在消息入口处理，不在这里猜金额。
        shared=re.fullmatch(r'(组三组六|组六组三)(?:各)?(?:打)?('+MULT+r')倍',suffix)
        operations=[]
        if shared:
            operations=[('组三',self._multiplier(shared[2])),('组六',self._multiplier(shared[2]))]
        else:
            named=re.fullmatch(r'('+MULT+r')(?:单|直)('+MULT+r')(?:组|组选)倍?',suffix)
            if named:
                operations=[('直选',self._multiplier(named[1])),('组选',self._multiplier(named[2]))]
            else:
                pattern=re.compile(r'('+ALIAS_PATTERN+r')(?:各)?(?:打)?('+MULT+r')(倍|元|米|块)?')
                cursor=0
                for part in pattern.finditer(suffix):
                    if part.start()!=cursor:
                        return None
                    kind=ALIAS_LOOKUP[part[1]]
                    operations.append((kind,part[2],part[3]))
                    cursor=part.end()
                if cursor!=len(suffix) or not operations:
                    return None
                if len(operations)>1 and all(op[2] is None for op in operations) and any(op[0] not in ('直选','组选') for op in operations):
                    raise ValueError('多玩法数字未标明倍数或金额单位，需确认下注口径')
        if len(selections)>1 and all(len(s)==1 for s in selections):
            selections=[''.join(selections)]
        result=[]
        for selection in selections:
            for operation in operations:
                kind,value=operation[:2]
                amount_mode=len(operation)==3 and operation[2] in ('元','米','块')
                mult=self._multiplier(str(decimal_number(value,chinese_int))) if isinstance(value,str) else value
                if amount_mode:
                    unit=self._from_kind(lottery,raw,selection,kind,1)[0].principal
                    mult=self._multiplier(str(decimal_number(value,chinese_int)/Decimal(str(unit))))
                items=self._from_kind(lottery,raw,selection,kind,mult)
                if amount_mode:
                    for bet in items:bet.entered_amount_yuan=float(decimal_number(value,chinese_int))
                result.extend(items)
        return result

    def _from_kind(self, lottery, raw, selection, kind, mult):
        if kind == '组选':
            if len(selection) != 3 or len(set(selection)) not in (2, 3):
                raise ValueError('组/组选须为具体三位号码；多码请写组三或组六')
            play = '组三（组选）' if len(set(selection)) == 2 else '组六（组选）'
        elif kind == '组三' and len(selection) == 3 and len(set(selection)) == 2:
            play = '组三（组选）'
        elif kind == '组六' and len(selection) == 3:
            play = '组六（组选）'
        elif kind in ('组三', '组六', '复式'):
            size = len(selection)
            minimum = 2 if kind == '组三' else (4 if kind == '组六' else 3)
            if size not in CN_NUM or size < minimum or len(set(selection)) != size:
                raise ValueError(f'{kind}须使用{minimum}～9个不重复数字；精确组选请写“组”')
            play = CN_NUM[size]+'码'+('复试' if kind == '复式' else kind)
        else:
            play = kind
        return [self._make(lottery, raw, play, selection, mult)]
