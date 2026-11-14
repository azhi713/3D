from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import uuid

CN_TIME = timezone(timedelta(hours=8))


def now():
    return datetime.now(CN_TIME)


def cents(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('金额必须是数字') from None
    if not number.is_finite() or number < 0 or number.as_tuple().exponent < -2:
        raise ValueError('金额必须非负，且最多两位小数')
    return int(number * 100)


def money(value):
    return f'{value / 100:.2f}'.rstrip('0').rstrip('.')


@dataclass
class Bet:
    play: str
    selection: str
    multiplier: int | float
    principal: float
    raw: str = ''
    unit_price_yuan: float | None = None
    entered_amount_yuan: float | None = None
    source_span: dict = field(default_factory=dict)
    parse_source: str = 'local'
    rules_version: str = ''
    settlement_rule_pending: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, obj):
        return cls(**{key: obj[key] for key in cls.__dataclass_fields__ if key in obj})


@dataclass
class MessageRecord:
    lottery: str
    source: str
    raw_text: str
    normalized_text: str = ''
    status: str = '待确认'
    bets: list = field(default_factory=list)
    note: str = ''
    created_at: str = field(default_factory=lambda: now().isoformat(timespec='seconds'))
    record_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    source_message_id: str = ''
    sender: str = ''
    group_id: str = ''
    period: str = field(default_factory=lambda: now().date().isoformat())
    received_amount: float | None = None
    claimed_amount: float | None = None
    revision: int = 0
    ai_candidate: str = ''
    pending_fragments: list = field(default_factory=list)  # 原文位置与未识别原因，仅供AI/人工处理
    source_original_text: str = ''  # 接收时原文，人工改写raw_text时仍保留
    parse_status: str = 'needs_review'
    review_status: str = 'not_manually_reviewed'
    issues: list = field(default_factory=list)
    declared_total_yuan: float | None = None
    computed_amount_yuan: float | None = None
    rules_version: str = ''
    engine_version: str = ''
    deadline_state: str = ''
    ai_history: list = field(default_factory=list)
    ai_request_state: str = ''  # started/unknown_billing survives restart; never blindly re-submit.

    def to_dict(self):
        obj = asdict(self)
        obj['bets'] = [b.to_dict() for b in self.bets]
        return obj

    @classmethod
    def from_dict(cls, obj):
        data = {key: val for key, val in obj.items() if key in cls.__dataclass_fields__}
        data['bets'] = [Bet.from_dict(b) for b in obj.get('bets', [])]
        return cls(**data)


@dataclass
class RiskRow:
    number: str
    payout: float
    net_loss: float
    plan_multiplier: int


@dataclass
class Analysis:
    principal: int
    revenue: int
    risks: list
    payouts: dict
    pending: int
    unconfirmed_receipts: int
