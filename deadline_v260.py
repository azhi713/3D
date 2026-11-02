"""Separate receiving cutoff from settlement deadline (Asia/Shanghai +08:00).

Never treat a late API response as a new order. Work started before cutoff
may finish only before the second, 21:15, deadline.
"""
from datetime import datetime
from batch_schedule import deadlines, is_received_before_cutoff
from models import now, CN_TIME


def final_deadline_reached(period, clock=None):
    try:
        check=clock or now()
        if check.tzinfo is None:check=check.replace(tzinfo=CN_TIME)
        return check>=deadlines(period)[1]
    except (ValueError, TypeError):return False


def valid_pre_cutoff_receipt(period,created_at,clock=None):
    """Only message timestamps consistent with this session's clock qualify."""
    clock=clock or now()
    if not created_at or not is_received_before_cutoff(period,created_at):return False
    try:
        source=datetime.fromisoformat(created_at)
        if source.tzinfo is None:return False
        return source<=clock
    except (ValueError,TypeError):return False


def allow_late_inbox(period,source,created_at,clock=None):
    clock=clock or now()
    if source!='微信' or final_deadline_reached(period,clock):return False
    return valid_pre_cutoff_receipt(period,created_at,clock)


def allow_candidate(controller,record, clock=None):
    if not controller.deadline_enabled():return True
    clock=clock or now()
    batch=controller.store.load_batch(record.period,record.lottery)
    if batch and batch['state']=='finalized':return False
    if final_deadline_reached(record.period,clock):return False
    if not controller.cutoff_applies(record.period,clock):return True
    # A request queued before cutoff is already committed; no new bet is
    # created by its late response.
    if record.ai_request_state in ('queued','started','responded') and valid_pre_cutoff_receipt(record.period,record.created_at,clock):
        return True
    # Delayed inbox ingestion within the grace period is permitted for
    # verifiably earlier received messages, not user-created post-cutoff bets.
    return allow_late_inbox(record.period,record.source,record.created_at,clock)
