# -*- coding: utf-8 -*-
"""V2.7.3.4: 复制阈值号码、冻结实际补单及真实盈亏的纯函数。

不读取数据库、不修改投注；开奖号码与返奖在上层只用于试算。
"""
from decimal import Decimal, InvalidOperation
import re

_NUMBER = re.compile(r'^[0-9]{3}$')


def threshold_number_copy(rows, per_line=9):
    """返回(可复制文本, 去重号码注数)，不受当前排序/搜索/倍数影响。"""
    if not 1 <= int(per_line) <= 30:
        raise ValueError('每行数量无效')
    numbers = sorted({str(row.number) for row in rows
                      if _NUMBER.fullmatch(str(row.number))})
    if not numbers:
        return '', 0
    lines = [' '.join(numbers[i:i+int(per_line)])
             for i in range(0, len(numbers), int(per_line))]
    return '\n'.join(lines) + f'\n\n合计：{len(numbers)}注', len(numbers)


def _to_cents(amount):
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        raise ValueError('实际补单金额配置无效') from None
    if not value.is_finite() or value < 0 or value.as_tuple().exponent < -2:
        raise ValueError('金额须为非负且最多两位小数')
    return int(value * 100)


def normalize_purchase_snapshot(snapshot):
    """从UI JSON安全恢复已购快照；格式错误则按未勾选处理。"""
    if not isinstance(snapshot, dict):
        return None
    try:
        rows = snapshot['rows']
        if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
            return None
        normalized = []
        seen = set()
        for item in rows:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                return None
            number, multiplier = item
            if not isinstance(number, str) or not _NUMBER.fullmatch(number):
                return None
            if type(multiplier) is not int or not 1 <= multiplier <= 1_000_000:
                return None
            if number in seen:
                return None
            seen.add(number)
            normalized.append([number, multiplier])
        normalized.sort(key=lambda entry: entry[0])
        result = {'rows': normalized}
        for key in ('cost_cents','direct_prize_cents','leopard_prize_cents'):
            value = snapshot[key]
            if type(value) is not int or not 0 <= value <= 10**13:
                return None
            result[key] = value
        if not result['direct_prize_cents'] or not result['leopard_prize_cents']:
            return None
        return result
    except (KeyError, ValueError, TypeError):
        return None


def freeze_purchase_snapshot(rows, unit_cost, direct_prize, leopard_prize):
    """用户勾选的那一刻锁定当时实际购买明细，不被后续模拟调整改变。"""
    by_number = {}
    for row in rows:
        number = str(row.number)
        if not _NUMBER.fullmatch(number):
            continue
        multiplier = int(row.plan_multiplier)
        if multiplier <= 0:
            continue
        if not 1 <= multiplier <= 1_000_000:
            raise ValueError('补单倍数超出允许范围')
        if number in by_number:
            raise ValueError('实际补单方案存在重复号码，无法安全确认')
        by_number[number] = multiplier
    if not by_number:
        return None
    unit_cost_cents = _to_cents(unit_cost)
    data = {'rows': [[number, by_number[number]] for number in sorted(by_number)],
            'cost_cents': sum(by_number.values()) * unit_cost_cents,
            'direct_prize_cents': _to_cents(direct_prize),
            'leopard_prize_cents': _to_cents(leopard_prize)}
    normalized = normalize_purchase_snapshot(data)
    if normalized is None:
        raise ValueError('实际补单快照校验失败')
    return normalized


def actual_purchase_financials(snapshot, draw):
    """无实际补单时收入成本都为0；实际补单未中奖仍全额扣成本。"""
    stored = normalize_purchase_snapshot(snapshot)
    if stored is None:
        return {'income_cents': 0, 'cost_cents': 0, 'profit_cents': 0,
                'hit_count': 0}
    cost = stored['cost_cents']
    reward = (stored['leopard_prize_cents'] if len(set(draw)) == 1
              else stored['direct_prize_cents']) if _NUMBER.fullmatch(str(draw)) else 0
    hits = [mult for number, mult in stored['rows'] if number == draw]
    income = sum(hits) * reward
    return {'income_cents': income, 'cost_cents': cost,
            'profit_cents': income-cost, 'hit_count': len(hits)}
