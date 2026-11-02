# -*- coding: utf-8 -*-
from copy import deepcopy

APP_NAME = "彩票实时风险预警"
APP_VERSION = "V2.6.2-客户测试版"
ENGINE_VERSION = "V2.6.1-RC1-verified-grammar-shortcuts"
RULES_VERSION = "2026-10-26-catalog+2026-10-31-overlay-v2"

LOTTERY_FC = "福彩"
LOTTERY_TC = "体彩"
LOTTERIES = (LOTTERY_FC, LOTTERY_TC)

DEFAULT_AI_PROMPT = r'''你是福彩3D和体彩排列三投注信息识别器，只返回一个JSON对象，不要Markdown和解释。
只识别unresolved_segments；必须结合raw_text、local_confirmed和合计，但绝不能把已确认的订单再输出一遍。
格式：{"candidate_bets":[{"segment_id":"S1","bet_type":"四码组六","digit_set":"0127","declared_amount_yuan":40,"bet_multiples":null,"amount_kind":"amount","amount_type":"inferred_from_total","multiple_type":"missing"}],"needs_review":false,"uncertainties":[],"brief_reason":""}。
每笔必须提供原文对应的segment_id、准确玩法、号码（ticket_codes或digit_set或position_sets），以及金额或倍数。号码必须是字符串，保留0和重复购买；支持同一个号码分别购买直、组三、组六。多码组选写成四码组六/四码组三等原本的包码玩法；只有原文明确写“组六复式/组三复式”时，才允许返回排列复式。
元/米/块代表金额，“倍”代表倍数；两者同时存在要都返回，倍数字段只提取原文明确的倍数。没有明确倍数必须返回null，不准由AI自行反推。无单位数字可借助原文合计推定为金额，此时amount_type=inferred_from_total。没有足够依据就放入uncertainties，不准猜测。
不计算本金、组合数、倍数、奖金或赔付，全部交由本地收费规则。合计/福家/体家是校验数，不是投注号码；不得虚构玩法、号码、金额、倍数。'''

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
