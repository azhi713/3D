# -*- coding: utf-8 -*-
"""Immutable displayed plan and target-profit statistics; no optimizer or ledger writes."""
from __future__ import annotations
from dataclasses import dataclass
import re
from typing import Any
from dashboard_metrics_v280 import (PlanMetrics, amount_cents, aggregate_rows,
                                     candidate_metrics, format_yuan)
from models import RiskRow
from risk_engine import plan_text
from hedge_edit_v276 import update_display_total

DRAFT = '[仅供人工核对 / 不可作为正式补单依据]'
MODES = ('manual', 'cheap', 'balanced', 'profit')


def parse_profit_target(value: str) -> int | None:
    """Blank disables the target; zero means break-even or better. Unit: cents."""
    if not isinstance(value, str):
        raise ValueError('目标盈利须为非负金额，最多两位小数')
    text = value.strip()
    if not text:
        return None
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?', text):
        raise ValueError('目标盈利须为非负金额，最多两位小数')
    return amount_cents(text)


def target_outcomes(metrics: PlanMetrics, target_cents: int | None) -> tuple[str, ...] | None:
    if target_cents is None:
        return None
    if type(target_cents) is not int or target_cents < 0:
        raise ValueError('目标盈利须为非负整数分')
    if len(metrics.profits_cents) != 1000:
        raise ValueError('目标盈利需要完整1000个开奖结果')
    return tuple(f'{n:03d}' for n, profit in enumerate(metrics.profits_cents)
                 if profit >= target_cents)


@dataclass(frozen=True)
class DisplayedPlan:
    """Only this immutable object supplies the editor, badge, comparison and copy."""
    context: tuple[str, str]
    mode: str
    source: str
    source_key: tuple[Any, ...]
    bets: tuple[tuple[str, int], ...]
    metrics: PlanMetrics
    display_text: str
    full_text: str
    numbers_text: str
    pending: bool

    @property
    def count(self):
        return len(self.bets)

    @property
    def cost_cents(self):
        return self.metrics.cost_cents

    @property
    def count_caption(self):
        return f'号码：{self.count}组'

    @property
    def cost_caption(self):
        return '成本：' + format_yuan(self.cost_cents) + '元'

    def as_rows(self):
        # A new mutable RiskRow list for legacy rendering, never shared with the snapshot.
        return [RiskRow(code, 0., 0., multiple) for code, multiple in self.bets]


def build_displayed_plan(context, mode, source, source_key, revenue_cents,
                         payouts_cents, rows, settings, *, manual_text=None, pending=False):
    if mode not in MODES or len(context) != 2 or context[0] not in ('福彩', '体彩'):
        raise ValueError('方案身份无效')
    bets = aggregate_rows(rows)
    canonical_rows = [RiskRow(code, 0., 0., multiple) for code, multiple in bets]
    metrics = candidate_metrics(revenue_cents, payouts_cents, canonical_rows, *settings)
    full = plan_text(canonical_rows, settings[0], context[0])
    # Preserve manual editing layout; copied complete plan is canonical and sorted.
    display = update_display_total(manual_text, metrics.cost_cents) if manual_text is not None else full
    codes = [code for code, _ in bets]
    numbers = '\n'.join(' '.join(codes[i:i+12]) for i in range(0, len(codes), 12))
    numbers += ('\n\n' if codes else '') + f'合计：{len(codes)}注'
    if pending:
        full = DRAFT + '\n' + full
        numbers = DRAFT + '\n' + numbers
        if manual_text is None:
            display = DRAFT + '\n当前投注尚未完整确认。\n\n' + display
    return DisplayedPlan(tuple(context), mode, source, tuple(source_key), bets, metrics,
                         display, full, numbers, bool(pending))


def assert_view_matches(view, *, mode, context, source_key, rows, editor_text,
                        count_caption, cost_caption, comparison_count, comparison_cost):
    """Fail closed before clipboard clear; never silently substitute a different plan."""
    if not isinstance(view, DisplayedPlan) or not view.bets:
        raise ValueError('当前没有已完成的有效补单方案')
    if (view.mode != mode or view.context != tuple(context) or view.source_key != tuple(source_key)):
        raise ValueError('当前方案已变化，等待显示和统计同步后再复制')
    if aggregate_rows(rows) != view.bets or editor_text != view.display_text:
        raise ValueError('方案文本与显示结果尚未同步，未复制旧数据')
    if count_caption != view.count_caption or cost_caption != view.cost_caption:
        raise ValueError('号码数或成本与当前方案不一致，未复制')
    if str(comparison_count) != str(view.count) or str(comparison_cost) != format_yuan(view.cost_cents):
        raise ValueError('方案对比行尚未同步，未复制')
    return view
