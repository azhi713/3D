# -*- coding: utf-8 -*-
"""V2.7.7: read-only, deterministic three-profile hedge candidate optimizer.

Only consumes a *completed* existing risk snapshot; never alters betting data,
settlements, or confirmed actual purchases. Money everywhere in integer cents.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re

_CODES = tuple(f'{n:03d}' for n in range(1000))
_PROFILES = (
    ('cheap', '低成本', Decimal('0.27'), Decimal('0.18')),
    ('balanced', '均衡型', Decimal('0.34'), Decimal('0.42')),
    ('profit', '收益优先', Decimal('0.40'), Decimal('0.78')),
)


def _check_cents(value, field):
    if type(value) is not int or not 0 <= value <= 10**13:
        raise ValueError(field + '必须是合法的非负整数分')
    return value


def _ceil_div(n, d):
    return -(-n // d)


def _build_fixed_point(payouts, income, prizes, unit_cost, max_loss, min_win_profit, max_cost):
    """Find cheapest self-consistent integer plan for one risk ceiling.

    A number is protected iff its unhedged income is worse than -max_loss.
    Protected numbers must earn >= min_win_profit at total-plan cost.
    The cost of hedging every number is included in every outcome's profit.
    """
    cost = 0
    previous = None
    for _ in range(1000):
        bets = {}
        for i, payout in enumerate(payouts):
            if payout - income + cost <= max_loss:
                continue
            needed = min_win_profit - (income - payout - cost)
            k = _ceil_div(needed, prizes[i])
            if k > 0:
                if k > 1_000_000:
                    return None
                bets[_CODES[i]] = k
        new_cost = sum(bets.values()) * unit_cost
        if new_cost > max_cost:
            return None
        if new_cost == cost:
            return (bets, cost)
        if previous is not None and new_cost < cost:
            # Monotone mapping should never decrease; reject rather than guess.
            raise ValueError('优化器固定点不单调')
        previous = cost
        cost = new_cost
    return None


def _metrics(bets, income, payouts, prizes, unit):
    cost = sum(bets.values()) * unit
    profits = [income - payouts[i] - cost + bets.get(code, 0) * prizes[i]
               for i, code in enumerate(_CODES)]
    covered = [profits[i] for i, code in enumerate(_CODES) if code in bets]
    unprotected = [profits[i] for i, code in enumerate(_CODES) if code not in bets]
    deficits = sorted((v for v in profits if v < 0))
    return {
        'cost_cents': cost,
        'covered_count': len(bets),
        'worst_profit_cents': min(profits),
        'uncovered_worst_cents': min(unprotected) if unprotected else None,
        'covered_min_profit_cents': min(covered) if covered else None,
        'loss_count': len(deficits),
        'avg_profit_cents': sum(profits) // 1000,
        'worst_5pct_avg_cents': sum(sorted(profits)[:50]) // 50,
        'no_customer_payout_profit_cents': income-cost,
    }


def optimize_three_profiles(income_cents, payouts_cents, unit_cost_cents,
                            direct_prize_cents, leopard_prize_cents):
    """Return three independently evaluated plans in order low/medium/high.

    Risk optimization is a deterministic integer fixed-point + binary search,
    not a claim of global expected-profit optimality. The preset percentages
    are visible, editable constants in this module. Recalculate when risk changes.
    """
    income = _check_cents(income_cents, '当日收入')
    unit = _check_cents(unit_cost_cents, '补单成本')
    normal = _check_cents(direct_prize_cents, '非豹子返奖')
    leopard = _check_cents(leopard_prize_cents, '豹子返奖')
    if unit <= 0 or normal <= 0 or leopard <= 0:
        raise ValueError('补单每倍成本和返奖必须大于0')
    if not isinstance(payouts_cents, dict):
        raise ValueError('风险赔付必须为号码映射')
    if len(payouts_cents) > 1000 or any(not isinstance(key, str) or not re.fullmatch(r'[0-9]{3}', key)
                                          for key in payouts_cents):
        raise ValueError('赔付号码不是有效的三位号码')
    for amount in payouts_cents.values():
        _check_cents(amount, '号码赔付')
    payouts = tuple(payouts_cents.get(code, 0) for code in _CODES)
    prizes = tuple(leopard if code[0] == code[1] == code[2] else normal
                   for code in _CODES)
    maximum_risk = max(payouts) - income
    results = []
    for key, title, budget_pct, profit_pct in _PROFILES:
        budget = min(income, int(Decimal(income) * budget_pct))
        budget -= budget % unit
        profit_target = int(Decimal(income) * profit_pct)
        if budget < unit or maximum_risk <= 0:
            best_bets = {}
        else:
            # For a fixed risk ceiling, the cost-inclusive solution is monotone
            # in that ceiling. Binary search yields the smallest feasible ceiling
            # without ever exceeding the preset budget.
            lo = 0
            hi = max(0, maximum_risk) + budget
            best = ({}, 0)
            while lo <= hi:
                mid = (lo+hi)//2
                trial = _build_fixed_point(payouts, income, prizes, unit, mid,
                                           profit_target, budget)
                if trial is None:
                    lo = mid+1
                else:
                    best = trial
                    hi = mid-1
            best_bets = best[0]
        metrics = _metrics(best_bets, income, payouts, prizes, unit)
        metrics.update({
            'key':key, 'title':title, 'budget_cap_cents':budget,
            'covered_target_profit_cents': profit_target,
            'bets':best_bets,
        })
        results.append(metrics)
    return results
