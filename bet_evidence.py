"""AI复核的来源证据账本。按来源块/玩法/倍数复核，不统计三位字符串出现次数。"""
from collections import Counter
from decimal import Decimal
import re
import unicodedata

from models import cents
from bet_grammar import SourceBlock, message_blocks, normalize_list, strip_receipt, decimal_number


def ledger(bets):
    """精确组选以组合身份比较；逐注复式按合法展开比较，保留所有购买次数。"""
    from risk_engine import RiskEngine
    result=Counter()
    for bet in bets:
        mult=str(Decimal(str(bet.multiplier)).normalize())
        if bet.play in ('组六复式','组三复式'):
            kind='组六（组选）' if bet.play=='组六复式' else '组三（组选）'
            for code in RiskEngine.expanded_tickets(bet):
                result[(kind,''.join(sorted(code)),mult)]+=1
        else:
            selection=''.join(sorted(bet.selection)) if bet.play in ('组三（组选）','组六（组选）') else bet.selection
            result[(bet.play,selection,mult)]+=1
    return result


def contextual_standard(text):
    """只把有明确来源的口语变为可验证语法。无规则/无倍数依据不补写。"""
    from parser import MULT, LOTTERY_TOKEN
    line=unicodedata.normalize('NFKC',text).strip()
    # 原文“来N注/单”有购买次数依据；金额下注则按本金换算，不能当倍率。
    order=re.fullmatch(r'(?:'+LOTTERY_TOKEN+r'\s*)?(\d{3}(?:\s+\d{3})*)\s*(?:再来|来|买|要|打|投)\s*('+MULT+r')(?:单|注)\s*(?:(\d+(?:\.\d{1,2})?)\s*[元米块])?',line)
    if order:
        if order[3]:
            mult=Decimal(order[3])/(Decimal(2)*len(order[1].split()))
        else:
            from parser import chinese_int
            mult=Decimal('0.5') if order[2]=='半' else Decimal(order[2]) if order[2][0].isdigit() else Decimal(chinese_int(order[2]))
        return order[1]+'直'+str(mult)+'倍'
    # “定位...来8倍”：保留所有位置、号码、数量，只去掉下单动词。
    if re.search(r'[百十个](?:位)?\s*\d',line):
        return re.sub(r'(?:来|买|要|打)\s*('+MULT+r')\s*倍',r'\1倍',line)
    # 常见完整口语“请把420和146各买2倍直选”仍须完整数字、玩法及倍率。
    order=re.fullmatch(r'(?:请|麻烦|帮我|给我|请把|麻烦把|帮我把)\s*(\d{3}(?:\s*(?:和|、|及|\s)\s*\d{3})*)\s*(?:各|每个)?\s*(?:来|买|要|打)?\s*('+MULT+r')\s*倍\s*(直选|单选|组选)',line)
    if order:
        numbers=' '.join(re.findall(r'\d{3}',order[1]))
        return numbers+order[3]+order[2]+'倍'
    return line


def source_bets(parser,lottery,text):
    """完整本地语法优先，其次逐块提取有语法证据的口语，不忽略未知语义。"""
    from parser import BetParser
    probe=BetParser(parser.rules,parser.policy)
    bets,_,_=probe.parse(lottery,text)
    if bets:
        return bets,True
    blocks=message_blocks(text)
    if len(blocks)==1 and not re.search(r'定位|[百十个](?:位)?\s*\d',text):
        normalized=normalize_list(text)
        blocks=[SourceBlock(m.group(),m.start(),m.end()) for m in re.finditer(r'[^，,;；。\r\n]+',normalized)]
    found=[]
    unresolved=False
    for block in blocks:
        from parser import chinese_int
        cleaned,_=strip_receipt(block.text,lambda x:decimal_number(x,chinese_int))
        standard=contextual_standard(cleaned)
        if re.fullmatch(r'(?:合计|总计|金额)\s*\d+(?:\.\d{1,2})?[元米块]?',standard):
            continue
        sub=BetParser(parser.rules,parser.policy)
        chunk,_,_=sub.parse(lottery,standard)
        if chunk:
            found.extend(chunk)
        else:
            unresolved=True
    return found,not unresolved


def validate_evidence(source,candidate,bets,parser=None,lottery='福彩'):
    from parser import BetParser
    from recognition_policy import classify_fragment
    from risk_engine import RiskEngine
    if not source or not candidate or not bets:
        return False,'原文或AI候选缺少有效投注'
    violation=classify_fragment(source)
    if not violation['ai_eligible']:
        return False,'原文不允许AI修改：'+violation['reason']
    parser=parser or BetParser()
    expected,complete=source_bets(parser,lottery,source)
    required=ledger(expected)
    actual=ledger(bets)
    if actual-required:
        return False,'AI新增号码、玩法或修改倍数，未找到对应来源证据'
    if required-actual:
        return False,'AI漏掉原文投注或改变了重复购买次数'
    if not expected:
        return False,'原文不足以确认号码、玩法和倍数，不能自动入账'
    if sum(cents(b.principal) for b in expected)!=sum(cents(b.principal) for b in bets):
        return False,'AI金额与原文结构复算不一致'
    # 只写“20元”但未给倍率时，来源口语转换已按明确本金计算；总额标签仍需核对。
    # 声明合计用于报差，不反推或修改原文明示倍数；用户写错合计不删除正确投注。
    # 合法展开在自定义赔率下也必须等价，防止结构等价而实际赔付发生变化。
    if any(b.play in ('组三复式','组六复式') for b in expected+bets):
        engine=RiskEngine(parser.rules,parser.policy)
        left=engine.calculate(lottery,expected,10**20,1800)[2]
        right=engine.calculate(lottery,bets,10**20,1800)[2]
        if left!=right:
            return False,'AI展开后的赔付与原玩法规则不一致'
    if not complete:
        return False,'AI未能为原文所有疑难内容提供可验证的投注依据'
    return True,''
