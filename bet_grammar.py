"""消息级语法：先区分投注块、号码列表和金额标注，再交给玩法解析器。

这里不决定赔率；保留原文范围，不将金额/合计转换为号码。
"""
from dataclasses import dataclass
from decimal import Decimal
import re
import unicodedata


@dataclass
class SourceBlock:
    text: str
    start: int
    end: int


def message_blocks(text):
    """定位跨行聚合；普通行只在玩法完成后断开，保留重复购买。"""
    raw = str(text or '')
    lines = [SourceBlock(m.group().strip(), m.start(), m.end())
             for m in re.finditer(r'[^\r\n]+',raw) if m.group().strip()]
    result=[]
    pending=None
    for block in lines:
        if pending is None:
            pending=block
            continue
        # 纯号码行与后面的号码/玩法/倍率组成一个批量块。
        number_header=re.fullmatch(r'(?:福|体|排|福彩|体彩|3[Dd])?\s*[:：,，]?\s*[0-9\s.。,:：，、/\-]+',pending.text)
        incomplete_position = (bool(re.search(r'定位|[百十个](?:位)?\s*[:：]?\s*\d',pending.text))
                               and not re.search(r'倍|[元米块]',pending.text))
        lottery_header=re.fullmatch(r'(?:福|体|排|福彩|体彩|3[Dd])\s*[:：,，]?',pending.text)
        position_continuation=bool(re.match(r'^(?:[百十个](?:位)?\s*[:：]?\s*\d|(?:定位)?(?:各|来)?\s*(?:\d|半|[一二两三四五六七八九十]))',block.text))
        if lottery_header or number_header or (incomplete_position and position_continuation):
            pending=SourceBlock(pending.text+'\n'+block.text,pending.start,block.end)
        else:
            result.append(pending)
            pending=block
    if pending:
        result.append(pending)
    return result


def normalize_list(text):
    """仅规范完整选号前缀的分隔符，不修改倍率小数点与负数。"""
    text=unicodedata.normalize('NFKC',text).strip()
    text=re.sub(r'^((?:福彩\s*3[Dd]|福彩|3[Dd]|体彩排列[三3]|体彩|排列[三3]|福|体|排))\s*[:，,.。]\s*',r'\1 ',text)
    words=r'直选|单选|直|单|组选|组|双飞|独胆|对子|豹子|跨度|定位|复试|复式|转圈打'
    text=re.sub(r'(?<=\d)\s*[.。:：,，]\s*(?=(?:'+words+r'))',' ',text)
    text=re.sub(r'((?:'+words+r')(?:各打|各|打)?)\s*[,，:：]\s*(?=\d|半|[一二两三四五六七八九十])',r'\1',text)
    # 找到第一个玩法标记，只处理它前面的选号码区域。
    play=re.search(r'直选|单选|转圈打|组三复式|组六复式|组三|组六|复试|复式|双飞|独胆|对子|豹子|跨度|定位|组选|单|直|组',text)
    end=play.start() if play else len(text)
    prefix=text[:end]
    prefix=re.sub(r'(?<!\d)(\d{2,9})(?:(?:\s*[.,。:：，、/\-]\s*)\d{2,9})+',
                  lambda m: re.sub(r'[.,。:：，、/\-]+',' ',m.group()),prefix)
    return re.sub(r'\s+',' ',prefix)+text[end:]


def strip_receipt(text, number):
    """返回(下注文字, 声明金额)。金额仅在有标签或完整倍数之后提取。"""
    line=unicodedata.normalize('NFKC',text).strip()
    amount=r'(?:\d+(?:\.\d{1,2})?|[零〇一二两三四五六七八九十百千万]+)'
    # 诸如“和值10打20元”是金额下注，不是收款标注。
    if re.fullmatch(r'(?:福|体|排|福彩|体彩)?\s*(?:和值|合值)\s*\d{1,2}\s*(?:打|买|投)\s*'+amount+r'\s*[元米块]',line):
        return line,None
    if re.search(r'(?:直选|单选|直|单|组选|组三|组六|复试|复式|双飞|独胆|对子|豹子|跨度\s*\d)\s*(?:各)?(?:打|买|投)?\s*'+amount+r'\s*[元米块]$',line):
        return line,None
    patterns=[
        r'\s*\(\s*('+amount+r')\s*[元米块]\s*\)\s*$',
        r'(?:[，,。;；\s]*(?:福|体)?\s*(?:合计|总计|金额|共)\s*[:：]?\s*)('+amount+r')\s*[元米块]?\s*$',
        r'(?:[，,。;；\s]+)('+amount+r')\s*[元米块]\s*$',
        r'(?<=倍)\s*('+amount+r')\s*[元米块]?\s*$',
        r'[，,。;；]+\s*('+amount+r')\s*[元米块]?\s*$',
    ]
    for pattern in patterns:
        m=re.search(pattern,line)
        if m:
            return line[:m.start()].rstrip(' ,，。;；'),float(number(m[1]))
    # 中文数量明确的“十单十组40”，最后40才是金额。
    m=re.fullmatch(r'(.*?(?:\d|[一二两三四五六七八九十百])(?:单|直)(?:\d|[一二两三四五六七八九十百])+(?:组|组选))\s*(\d+(?:\.\d{1,2})?)',line)
    if m and not re.search(r'一单一组$',m[1]):
        return m[1],float(number(m[2]))
    return line,None


def decimal_number(value, chinese_int):
    value=str(value)
    return Decimal(value) if re.fullmatch(r'\d+(?:\.\d+)?',value) else Decimal(chinese_int(value))
