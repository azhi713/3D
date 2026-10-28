"""以分计算金额；只枚举真实开奖号码，组六的排列不会重复赔付。"""
from functools import lru_cache
from fractions import Fraction
from config import RULES_BY_LOTTERY
from models import RiskRow, cents, money

RESULTS = tuple(f'{i:03d}' for i in range(1000))


@lru_cache(maxsize=1024)
def winning_indices(play, selection, span0_wins):
    out = []
    for index, result in enumerate(RESULTS):
        unique = set(result)
        size = len(unique)
        if size == 1 and play not in ('直选', '独胆', '一码定位', '两码定位', '三码定位', '豹子', '跨度0'):
            continue
        win = False
        if play == '直选':
            win = result == selection
        elif play in ('组三（组选）', '组六（组选）'):
            win = size == (2 if play.startswith('组三') else 3) and sorted(result) == sorted(selection)
        elif play == '独胆':
            win = selection in result
        elif play == '双飞':
            win = set(selection).issubset(unique)
        elif play == '对子':
            win = result.count(selection[0]) == 2
        elif play == '豹子':
            win = size == 1
        elif play.endswith('码定位'):
            parts = [part.split(':') for part in selection.split('|')]
            win = all(result['百十个'.index(position)] in numbers for position, numbers in parts)
        elif play == '全包组六':
            win = size == 3
        elif play == '全包组三':
            win = size == 2
        elif play.endswith('码组六'):
            win = size == 3 and unique.issubset(set(selection))
        elif play.endswith('码组三'):
            win = size == 2 and unique.issubset(set(selection))
        elif play.endswith('码复试'):
            # 复试按规则表：所选数字池覆盖组三+组六（包含对子），豹子按总规则全杀。
            win = size in (2, 3) and unique.issubset(set(selection))
        elif play.startswith('跨度'):
            win = max(map(int, result))-min(map(int, result)) == int(play[-1])
        if win:
            out.append(index)
    return tuple(out)


class RiskEngine:
    def __init__(self, rules=None, policy=None):
        self.rules = rules or RULES_BY_LOTTERY
        self.policy = policy or {}

    def payout_for(self, lottery, bet, result):
        if result not in RESULTS:
            raise ValueError('开奖号码须为三位数字')
        wins = winning_indices(bet.play, bet.selection, bool(self.policy.get('span0_wins')))
        if int(result) not in wins:
            return 0
        return int(round(self.prize_cents(lottery, bet, result) * float(bet.multiplier))) / 100

    def prize_cents(self, lottery, bet, result):
        rule = self.rules[lottery][bet.play]
        return cents(rule.get('leopard_prize',1600) if bet.play == '直选' and len(set(result)) == 1 else rule['prize'])

    def calculate(self, lottery, bets, threshold, external_direct_prize, revenue=None,
                  metric='payout', hedge_cost=None, unit_cost=2, leopard_direct_prize=1600):
        threshold_cents = cents(threshold)
        reward = cents(external_direct_prize)
        leopard_reward = cents(leopard_direct_prize)
        unit = cents(unit_cost)
        if min(reward,leopard_reward) <= 0 or (hedge_cost and min(reward,leopard_reward) <= unit):
            raise ValueError('外部直选奖金须大于零，计入补单成本时须高于每倍成本')
        if metric not in ('payout', 'net_loss'):
            raise ValueError('未知风险筛选方式')
        total_principal = sum(cents(b.principal) for b in bets)
        revenue_cents = total_principal if revenue is None else cents(revenue)
        payouts = [0] * 1000
        for bet in bets:
            prize = int(round(self.prize_cents(lottery, bet, bet.selection) * float(bet.multiplier)))
            for index in winning_indices(bet.play, bet.selection, bool(self.policy.get('span0_wins'))):
                payouts[index] += prize
        rows = []
        for result, payout in zip(RESULTS, payouts):
            loss = payout - revenue_cents
            result_reward = leopard_reward if len(set(result)) == 1 else reward
            if (payout if metric == 'payout' else loss) >= threshold_cents:
                rows.append(RiskRow(result, payout/100, loss/100, max(0, (max(loss, 0)+result_reward-1)//result_reward)))
        # 严格成本模式：所有补单共用的总成本也纳入每一种开奖情形。
        # 最小整数固定点，不能只扣中奖号码自己的补单成本。
        if hedge_cost:
            rewards = [leopard_reward if len(set(row.number)) == 1 else reward for row in rows]
            if sum((Fraction(unit, r) for r in rewards), Fraction()) >= 1:
                raise ValueError('风险号码过多，计入全部补单成本后无法生成有效覆盖方案')
            multipliers = [r.plan_multiplier for r in rows]
            for _ in range(10000):
                total_cost = sum(multipliers) * unit
                updated = [max(0, (int(round(row.net_loss*100))+total_cost+row_reward-1)//row_reward) for row,row_reward in zip(rows,rewards)]
                if updated == multipliers:
                    break
                multipliers = updated
            else:
                raise ValueError('补单成本计算未收敛，请调整阈值')
            for row, multiplier in zip(rows, multipliers):
                row.plan_multiplier = multiplier
        rows.sort(key=lambda row: (-row.net_loss, row.number))
        return total_principal/100, rows, dict(zip(RESULTS, (p/100 for p in payouts)))

    def explain(self, lottery, bets, result):
        rows = []
        for bet in bets:
            payout = self.payout_for(lottery, bet, result)
            if payout:
                rows.append({**bet.to_dict(), 'payout': payout})
        return sorted(rows, key=lambda row: -row['payout'])


def plan_text(rows, unit_cost=2):
    items = [f'{row.number}直选{row.plan_multiplier}倍' for row in rows if row.plan_multiplier > 0]
    if not items:
        return '暂无需要补单的号码'
    lines = ['  '.join(items[i:i+4]) for i in range(0, len(items), 4)]
    lines.append('合计：'+money(sum(row.plan_multiplier for row in rows)*cents(unit_cost))+'元')
    return '\n'.join(lines)
