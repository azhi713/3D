# -*- coding: utf-8 -*-
"""V2.8.0 read-only, integer-cent dashboard statistics.

This module does not generate a hedge, change odds, write a ledger or choose
an outcome. All 1,000 outcomes are evaluated for the supplied plan only.
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Iterable, Mapping

CODES = tuple(f'{n:03d}' for n in range(1000))
_CODE = re.compile(r'[0-9]{3}\Z')
_LIMIT = 10**16


def amount_cents(value) -> int:
    """Accept exact nonnegative currency, never silently truncate cents."""
    if isinstance(value, bool):
        raise ValueError('金额不能是布尔值')
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError('金额不是有效数字') from exc
    scaled = number * 100
    if not number.is_finite() or number < 0 or scaled != scaled.to_integral_value() or scaled > _LIMIT:
        raise ValueError('金额必须非负且精确到分')
    return int(scaled)


def _integer(value, field, positive=False):
    if type(value) is not int or value < (1 if positive else 0) or value > _LIMIT:
        raise ValueError(field + '须为有效整数')
    return value


def payout_vector(payouts: Mapping[str, object]) -> tuple[int, ...]:
    """RiskEngine supplies a complete mapping. Missing outcomes are NOT zero."""
    if not isinstance(payouts, Mapping) or set(payouts) != set(CODES):
        raise ValueError('赔付数据不完整：必须包含000～999全部1000个号码')
    return tuple(amount_cents(payouts[code]) for code in CODES)


def aggregate_rows(rows: Iterable) -> tuple[tuple[str, int], ...]:
    merged: dict[str, int] = {}
    for row in rows:
        code = str(row.number)
        if not _CODE.fullmatch(code):
            raise ValueError('补单号码必须保留完整三位数字')
        value = Decimal(str(row.plan_multiplier))
        if not value.is_finite() or value < 0 or value != value.to_integral_value() or value > 1_000_000:
            raise ValueError('补单倍数须为0～100万的整数')
        if value:
            merged[code] = merged.get(code, 0) + int(value)
            if merged[code] > 1_000_000:
                raise ValueError('同一号码的累计补单倍数超过100万')
    return tuple(sorted(merged.items()))


@dataclass(frozen=True)
class PlanMetrics:
    cost_cents: int
    covered_count: int
    total_multiples: int
    worst_profit_cents: int
    best_profit_cents: int
    covered_min_profit_cents: int | None
    loss_count: int
    profits_cents: tuple[int, ...]
    returns_cents: tuple[int, ...]

    @property
    def max_loss_cents(self):
        return min(self.worst_profit_cents, 0)

    @property
    def max_gain_cents(self):
        return max(self.best_profit_cents, 0)

    def at(self, draw: str) -> dict:
        if not isinstance(draw, str) or not _CODE.fullmatch(draw):
            raise ValueError('开奖号码须为完整三位数字')
        i = int(draw)
        return {'total_profit_cents': self.profits_cents[i],
                'return_cents': self.returns_cents[i],
                'hedge_profit_cents': self.returns_cents[i] - self.cost_cents}


def evaluate(revenue_cents: int, payouts_cents: tuple[int, ...],
             bets: tuple[tuple[str, int], ...], cost_cents: int,
             normal_prize_cents: int, leopard_prize_cents: int) -> PlanMetrics:
    _integer(revenue_cents, '收单金额')
    _integer(cost_cents, '补单成本')
    _integer(normal_prize_cents, '普通返奖', positive=True)
    _integer(leopard_prize_cents, '豹子返奖', positive=True)
    if len(payouts_cents) != 1000:
        raise ValueError('需要完整1000个赔付结果')
    for val in payouts_cents:
        _integer(val, '客户赔付')
    coverage = {}
    for code, count in bets:
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            raise ValueError('补单号码不是三位数字')
        _integer(count, '补单倍数', positive=True)
        coverage[code] = coverage.get(code, 0) + count
    returns = tuple(coverage.get(code, 0) *
                    (leopard_prize_cents if len(set(code)) == 1 else normal_prize_cents)
                    for code in CODES)
    profits = tuple(revenue_cents - payout - cost_cents + reward
                    for payout, reward in zip(payouts_cents, returns))
    covered = [profits[int(code)] for code in coverage]
    return PlanMetrics(cost_cents, len(coverage), sum(coverage.values()),
                       min(profits), max(profits), min(covered) if covered else None,
                       sum(profit < 0 for profit in profits), profits, returns)


def candidate_metrics(revenue_cents, payouts_cents, rows, unit_cost,
                      normal_prize, leopard_prize):
    bets = aggregate_rows(rows)
    cost = sum(n for _, n in bets) * amount_cents(unit_cost)
    return evaluate(int(revenue_cents), payouts_cents, bets, cost,
                    amount_cents(normal_prize), amount_cents(leopard_prize))


def purchased_metrics(revenue_cents, payouts_cents, purchase):
    """An already bought plan uses its stored total cost and stored prize rules."""
    if purchase is None:
        return evaluate(int(revenue_cents), payouts_cents, (), 0, 1, 1)
    if not isinstance(purchase, dict):
        raise ValueError('已补单记录无效')
    return evaluate(int(revenue_cents), payouts_cents,
                    tuple((code, count) for code, count in purchase['rows']),
                    purchase['cost_cents'], purchase['direct_prize_cents'],
                    purchase['leopard_prize_cents'])


def format_yuan(cents: int, signed=False) -> str:
    if type(cents) is not int:
        raise ValueError('显示金额须为整数分')
    value = Decimal(cents) / 100
    # No floating point; trim only decimal trailing zeroes.
    text = f'{abs(value):,.2f}'.rstrip('0').rstrip('.')
    return ('-' if cents < 0 else '+' if signed and cents > 0 else '') + text


def restored_geometry(data, screen_w, screen_h):
    """Migrate only UI geometry. Never guess a former restore size from zoom."""
    current = data.get('normal_window_geometry', '')
    new_format = bool(current)
    raw = current or data.get('window_geometry', '')
    if not isinstance(raw, str):
        return ''
    match = re.fullmatch(r'(\d{3,5})x(\d{3,5})([+-]\d{1,6})([+-]\d{1,6})', raw)
    if not match:
        return ''
    w,h,x,y = map(int,match.groups())
    if not new_format and data.get('window_zoomed') and (w>=screen_w*.94 or h>=screen_h*.94):
        return ''
    if not min(900,screen_w)<=w<=screen_w or not min(590,screen_h)<=h<=screen_h:
        return ''
    if x < -w+150 or x>screen_w-100 or y < -50 or y>screen_h-100:
        return ''
    return raw
