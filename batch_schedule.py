"""中国北京时间21:14停止新单；21:15为最终统计截止点。"""
from datetime import date, datetime, time, timedelta

from models import CN_TIME, now

RECEIVE_CUTOFF = time(21, 14)
SUMMARY_DEADLINE = time(21, 15)


def deadlines(period):
    day = date.fromisoformat(period)
    return (datetime.combine(day, RECEIVE_CUTOFF, CN_TIME),
            datetime.combine(day, SUMMARY_DEADLINE, CN_TIME))


def is_closed(period, clock=None):
    clock = clock or now()
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=CN_TIME)
    # 期号不是ISO日期时不自动推断截止日期，交由人工设置期次。
    try:
        cutoff, _ = deadlines(period)
    except ValueError:
        return False
    return clock >= cutoff


def is_received_before_cutoff(period, received_at):
    """只信任带时区且属于当期的实际接收时间。"""
    if not received_at:
        return False
    try:
        received = datetime.fromisoformat(received_at)
        cutoff, _ = deadlines(period)
    except ValueError:
        return False
    if received.tzinfo is None:
        received = received.replace(tzinfo=CN_TIME)
    return received < cutoff


def finalized_on_time(period, finished_at):
    _, deadline = deadlines(period)
    if finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=CN_TIME)
    return finished_at < deadline
