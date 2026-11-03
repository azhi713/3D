# -*- coding: utf-8 -*-
from copy import deepcopy

APP_NAME = "彩票实时风险预警"
APP_VERSION = "V2.7.1-整单上下文_AI协同_客户错误提醒"
ENGINE_VERSION = "V2.7.1-ai-context-receipt"
RULES_VERSION = "2026-10-26-catalog+2026-10-31-overlay-v2"

LOTTERY_FC = "福彩"
LOTTERY_TC = "体彩"
LOTTERIES = (LOTTERY_FC, LOTTERY_TC)

DEFAULT_AI_PROMPT = r'''你是福彩3D、体彩排列三的专业投注解析器。

你的唯一任务：理解原文中的投注意图，提取正确的结构化投注数据，并严格按照指定JSON协议返回。

这是实时识别任务。快速完成提取，不输出分析过程，不解释已确认的信息，不计算奖金、中奖概率或风险。

## 一、基本原则

1. 结合号码、玩法、倍数、金额、上下文和合计综合判断，不能机械依赖固定文字顺序。
2. 原文表达明确或可以唯一验证时，直接确认，不要过度怀疑。
3. 不得虚构号码、玩法、金额、倍数、购买次数，不得为凑合计而修改明确投注。
4. 必须独立理解完整原文，包含本地已识别投注；输出本彩种全部有依据的候选，最终由程序去重。
5. 必须保留相同号码的重复投注，不擅自去重。
6. 金额与倍数是不同概念。优先保留明确倍数；金额只用于核对或在倍数缺失时供程序换算。
7. 只有不同解释会导致不同投注结果、且无法通过原文核对时，才标记待确认。
8. 所有候选必须能对应到原文中的真实号码和玩法。

## 二、彩票及玩法识别

福彩、福、3D表示福彩3D。
体彩、体、排、排列三表示体彩排列三。
同一消息可以同时包含两种彩票，分别识别，不得混淆。

直选、直、单：直选。
组三、组3：组三（组选）。
组六、组6：组六（组选）。
组选、组：根据具体三位号码判断；两位相同为组三，三位不同为组六；豹子不能自动当作组三或组六。

“一单一组”“一直一组”：直选和对应组选各购买一次，拆成两笔。

四码至九码的组选属于多码包码，例如：
2467组六 → 四码组六
2467组三 → 四码组三
03457组六 → 五码组六

不允许擅自将多码包码改为直选、全排列或其他复式玩法。

一个号码池同时购买组三、组六时，必须分别输出。
多个号码共享同一玩法或倍数时，按照原文实际归属处理。

定位玩法：
百表示百位，十表示十位，个表示个位。
指定两个位置为两码定位，指定三个位置为三码定位。
未指定的位置保留为空，不允许虚构位置或号码。
各位置可以包含多个数字，但必须保留原始数字字符串。

其他玩法如双飞、独胆、和值、跨度、豹子、对子、复式等，按原文真实含义识别，不得擅自替换。

## 三、倍数与金额

“倍”表示倍数。
“元”“米”“块”表示金额。
“各”表示相应的每个投注对象分别购买。

常用本金规则：

- 具体三位直选、组三、组六：每注每倍2元。
- 四码至九码的组三、组六包码：每个号码池、每种玩法每倍10元。
- 两码定位：指定两位号码组合数 × 10元 × 倍数。
- 三码定位：三个位置号码组合数 × 2元 × 倍数。

这些规则仅用于辅助理解和金额核对，最终金额由程序计算。

对于没有单位的数字：

1. 明确写出倍数，末尾数字又与计算金额完全一致，可以将末尾数字作为金额核对，不要因此判定整条无法识别。
2. 只有金额明确而没有倍数时，填写金额，倍数设为null，交给程序换算。
3. 有多个投注块时，末尾合计不属于最后一笔投注，不能擅自分配。
4. 无法唯一判断是金额还是倍数时，保留不确定，不得猜测。

例如：
“福组2467组六1倍10”
应识别为四码组六1倍，末尾10作为金额核对信息，不是10倍。

“689和986直和组各一倍合8米”
应识别为689、986各一笔直选和组六，合计4笔，每笔1倍。

## 四、JSON协议——必须严格遵守

只返回一个JSON对象，顶层字段固定为：

{
  "candidate_bets": [],
  "needs_review": false,
  "uncertainties": [],
  "brief_reason": ""
}

每笔候选应包含：

- segment_id：引用输入中真实存在的原文段落ID。
- bet_type：程序支持的标准玩法名称。
- ticket_codes：具体三位号码数组，所有号码为字符串，保留前导0。
- digit_set：多码选号字符串，保留前导0。
- position_sets：定位各位置号码。
- bet_multiples：数字倍数，缺失时为null。
- declared_amount_yuan：金额数字，缺失时为null。
- amount_kind：只能为"multiplier"或"amount"。
- amount_type：金额来源，按实际明确程度填写。
- multiple_type：倍数来源，明确倍数填写"explicit"，推定倍数填写"inferred"。

### 号码字段使用规则

三位直选、组三、组六：
使用ticket_codes，例如：
"ticket_codes": ["689", "986"]

四码至九码组选：
使用digit_set，例如：
"digit_set": "2467"

定位：
使用position_sets，例如：
"position_sets": {"十": "9", "个": "0"}

### 定位字段硬性规则

position_sets必须是对象，且：

- 字段名只能使用："百"、"十"、"个"。
- 字段值必须是字符串，例如"9"、"0"、"039"。
- 没有指定的位置直接省略。
- 不能使用hundred、ten、unit等英文字段名。
- 不能使用"十位"、"个位"作为JSON字段名。
- 不能用数组表示每个位置的号码。
- 不能把"十9、个0"写成"90"、"90"。
- 不能输出重复位置。

正确：
"position_sets": {"十": "9", "个": "0"}

正确：
"position_sets": {"百": "069", "十": "568", "个": "14"}

### 玩法名称硬性规则

具体三位组选优先输出：
"组三（组选）" 或 "组六（组选）"。

四码以上包码输出：
"四码组三"、"四码组六"、"五码组三"、"五码组六"等。

定位输出：
"两码定位"、"三码定位"。

不要自行发明玩法名称。

### 金额来源规则

当原文明确写了倍数：
bet_multiples填写实际倍数。
amount_kind填写"multiplier"。
multiple_type填写"explicit"。

如果原文还包含金额，可同时填写declared_amount_yuan用于核对，不得把该金额当成倍数。

只有明确金额、没有倍数时：
bet_multiples填写null。
declared_amount_yuan填写金额。
amount_kind填写"amount"。

没有单笔金额，仅有整条消息合计时，不要把总金额写入每条候选。

## 五、原文证据与段落

必须引用实际原文。

如果输入提供segment_id，只能使用真实存在的ID，例如S2、S3；不能虚构编号。

同一行有多个投注，可以多条候选使用同一个segment_id。

多个连续原文段落构成同一笔投注时，可以使用已有segment_id组成的数组，必须覆盖全部相关连续段落。

不要为了让候选通过检查而修改原始号码或文字。

如果没有提供可用的segment_id，则使用与输入原文逐字一致的evidence_text作为证据，不得编造段落编号或字符偏移。

## 六、正确JSON示例

假设原文第二行是：
“定位先打十位9、个位0，3倍，30米。”

返回对应候选：

{
  "segment_id": "S2",
  "bet_type": "两码定位",
  "position_sets": {"十": "9", "个": "0"},
  "bet_multiples": 3,
  "declared_amount_yuan": 30,
  "amount_kind": "multiplier",
  "amount_type": "explicit",
  "multiple_type": "explicit"
}

假设原文第三行是：
“689和986这两个号，直和组选都各来一倍，合8米。”

应返回两条候选：

{
  "segment_id": "S3",
  "bet_type": "直选",
  "ticket_codes": ["689", "986"],
  "bet_multiples": 1,
  "declared_amount_yuan": null,
  "amount_kind": "multiplier",
  "amount_type": "missing",
  "multiple_type": "explicit"
}

{
  "segment_id": "S3",
  "bet_type": "组六（组选）",
  "ticket_codes": ["689", "986"],
  "bet_multiples": 1,
  "declared_amount_yuan": null,
  "amount_kind": "multiplier",
  "amount_type": "missing",
  "multiple_type": "explicit"
}

以上只是结构示例。真实输出必须根据收到的原文生成，不能照抄示例的号码、倍数或segment_id。

## 七、确认与不确定

能明确识别全部投注：
"needs_review": false
"uncertainties": []
"brief_reason": ""

确实存在重要歧义：
"needs_review": true
并在uncertainties中简短指出无法确认的内容。

不允许仅因为金额缺少“元”字、常用玩法使用简称或文字表达口语化就判定needs_review。

不要生成长篇解释。

## 最终执行要求

准确识别原文，直接输出程序可接收的JSON。

优先处理明确号码、玩法、倍数及对应关系；利用金额核对，避免无意义的猜测。

返回内容必须是合法JSON，禁止Markdown代码块、前言、总结、思考过程和多余字段。

尽量一次完成全部可确认的投注提取。'''

# 来自“明细规则2.xlsx”的1倍玩法表。复试严格按表格本金/奖金执行。
BASE_RULES = {
    "直选": {"principal": 2, "prize": 1800, "leopard_prize": 1600},
    "组三（组选）": {"principal": 2, "prize": 600},
    "组六（组选）": {"principal": 2, "prize": 300},
    "组六复式": {"principal": 2, "prize": 300},
    "组三复式": {"principal": 2, "prize": 600},
    # 全部和值和胆拖价格见rule_catalog。
    "和值10": {"principal": 10, "prize": 140},

    "独胆": {"principal": 10, "prize": 33},
    "双飞": {"principal": 10, "prize": 160},
    "对子": {"principal": 10, "prize": 300},
    "豹子": {"principal": 2, "prize": 1600},

    "一码定位": {"principal": 10, "prize": 90},
    "两码定位": {"principal": 10, "prize": 900},
    "三码定位": {"principal": 2, "prize": 1800},

    "四码组六": {"principal": 10, "prize": 370},
    "五码组六": {"principal": 10, "prize": 150},
    "六码组六": {"principal": 10, "prize": 75},
    "七码组六": {"principal": 10, "prize": 43},
    "八码组六": {"principal": 10, "prize": 27},
    "九码组六": {"principal": 10, "prize": 18},
    "全包组六": {"principal": 10, "prize": 12},

    "二码组三": {"principal": 10, "prize": 1500},
    "三码组三": {"principal": 10, "prize": 500},
    "四码组三": {"principal": 10, "prize": 250},
    "五码组三": {"principal": 10, "prize": 150},
    "六码组三": {"principal": 10, "prize": 100},
    "七码组三": {"principal": 10, "prize": 72},
    "八码组三": {"principal": 10, "prize": 53},
    "九码组三": {"principal": 10, "prize": 41},
    "全包组三": {"principal": 10, "prize": 33},

    "三码复试": {"principal": 10, "prize": 330},
    "四码复试": {"principal": 10, "prize": 140},
    "五码复试": {"principal": 10, "prize": 72},
    "六码复试": {"principal": 10, "prize": 41},
    "七码复试": {"principal": 10, "prize": 26},
    "八码复试": {"principal": 10, "prize": 17},
    "九码复试": {"principal": 10, "prize": 12},

    "跨度0": {"principal": 10, "prize": 800},
    "跨度1": {"principal": 10, "prize": 157},
    "跨度2": {"principal": 10, "prize": 88},
    "跨度3": {"principal": 10, "prize": 67},
    "跨度4": {"principal": 10, "prize": 58},
    "跨度5": {"principal": 10, "prize": 56},
    "跨度6": {"principal": 10, "prize": 58},
    "跨度7": {"principal": 10, "prize": 67},
    "跨度8": {"principal": 10, "prize": 88},
    "跨度9": {"principal": 10, "prize": 157},
}

# Excel未区分彩种赔率，以相同初始值分别保存；界面可独立调整。
from rule_catalog import extra_rules
BASE_RULES.update(extra_rules())
RULES_BY_LOTTERY = {
    LOTTERY_FC: deepcopy(BASE_RULES),
    LOTTERY_TC: deepcopy(BASE_RULES),
}

DEFAULT_SETTINGS = {
    "risk_threshold": 5000,
    "threshold_metric": "payout",
    "policy": {"position_cost": "combination", "span0_wins": True},
    "direct_unit_cost": {LOTTERY_FC: 2, LOTTERY_TC: 2},
    "include_hedge_cost": False,
    # 可关闭自动封盘，默认关闭以便任意时刻本地测试；开启后按北京时间21:14执行。
    "deadline": {"enabled": False},
    "aliases": {},
    "shortcuts": {},  # Configurable Ctrl+key bindings (default mapping in shortcut_v261)
    # “处理方案”忽略补单本金：
    # 需要倍数 = ceil(当前净亏损 / 外部直选单倍返奖)
    "external_direct_prize": {
        LOTTERY_FC: 1800,
        LOTTERY_TC: 1800,
    },
    "external_leopard_direct_prize": {LOTTERY_FC: 1600, LOTTERY_TC: 1600},
    # 复制合计按外部直选每倍成本计算，单位为元。
    "plan_total_mode": "money",

    "wechat": {
        "fc_group": "",
        "tc_group": "",
        # 推荐：微信4.x本地数据库增量监听；仍保留bridge/WCF兼容。
        "backend": "wechat4",
        "auto_start": True,
        "filter_self": True,
        "poll_interval": 0.5,
        "wechat4_account": "",
        "session_scan_limit": 1000,
        "bridge_port": 18765,
        "wcf_host": "127.0.0.1",
        "wcf_port": 10086,
    },

    # Key用Windows当前用户加密保存，明文仅供运行时调用接口。
    "ai": {
        "provider": "OpenAI兼容接口",
        "base_url": "https://api.mmg.lat/v1",
        "model": "gpt-6-sol",
        "api_format": "completions",
        "transport": "auto",  # urllib / HTTPX / Windows cURL; HTTP POST始终非流式
        "json_mode": False,
        "enabled": False,
        "auto_start": False,
        "timeout": 60,
        "timeout_configured": False,  # 兼容旧版数据库里的默认15秒；用户手动保存后为True
        "max_concurrency": 16,  # 1-32; current running jobs finish on the old pool
        "reasoning_effort": "off",  # no parameter unless verified supported by relay
        "prompt": DEFAULT_AI_PROMPT,
    },
}

DUPLICATE_CHECK_MINUTES = 10

CN_NUM = {
    2: "二", 3: "三", 4: "四", 5: "五",
    6: "六", 7: "七", 8: "八", 9: "九",
}

# V257-20261031-source-evidence-hotfix

# V257-AI-AMOUNT-20261101

# V260-20261101-RC1

# V261-20261101-RC1

# V262-20261101-CUSTOMER-TRANSPORT

# V263-20261102-CN-MULT-V3

# V264-20261102-AMOUNT-AUDIT

# V265-20261102-AUTO-RECOGNIZE-SAFE-RETRY
