
# -*- coding: utf-8 -*-
import unittest
from models import RiskRow
from risk_engine import adjust_plan_rows, plan_financials, calculate_total_profit_cents

class PlanSimulationV272(unittest.TestCase):
    def setUp(self):
        self.rows = [
            RiskRow('072', 0, 0, 35), RiskRow('172', 0, 0, 35),
            RiskRow('084', 0, 0, 33), RiskRow('085', 0, 0, 33),
            RiskRow('104', 0, 0, 33),
            RiskRow('002', 0, 0, 13), RiskRow('004', 0, 0, 13),
            RiskRow('185', 0, 0, 13),
            RiskRow('702', 0, 0, 5),
            *[RiskRow(n, 0, 0, 3) for n in (
                '027','107','120','202','204','210','217','270','272',
                '275','285','604','672','674','684','685','701','710',
                '712','720','775','784','802','805','872','882','885'
            )]
        ]

    def test_today_plan_example_185(self):
        result = plan_financials(self.rows, '185', 2, 1800, 1600)
        self.assertEqual(result['cost_cents'], 58800)
        self.assertEqual(result['income_cents'], 2340000)
        self.assertEqual(result['profit_cents'], 2281200)

    def test_global_plus_one_increases_profit_by_1728(self):
        adjusted = adjust_plan_rows(self.rows, 1)
        result = plan_financials(adjusted, '185', 2, 1800, 1600)
        self.assertEqual(result['cost_cents'], 66000)
        self.assertEqual(result['income_cents'], 2520000)
        self.assertEqual(result['profit_cents'], 2454000)
        base = plan_financials(self.rows, '185', 2, 1800, 1600)
        self.assertEqual(result['profit_cents'] - base['profit_cents'], 172800)

    def test_new_risk_number_inherits_current_global_adjustment(self):
        extended = self.rows + [RiskRow('999', 0, 0, 4)]
        adjusted = adjust_plan_rows(extended, 2)
        self.assertEqual(adjusted[-1].plan_multiplier, 6)

    def test_minus_never_creates_zero_or_negative_suggested_group(self):
        rows = [RiskRow('001', 0, 0, 1), RiskRow('002', 0, 0, 3),
                RiskRow('003', 0, 0, 0)]
        adjusted = adjust_plan_rows(rows, -5)
        self.assertEqual([x.plan_multiplier for x in adjusted], [0, 0, 0])

    def test_leopard_uses_configured_leopard_prize(self):
        rows = [RiskRow('111', 0, 0, 2)]
        result = plan_financials(rows, '111', 2, 1800, 1600)
        self.assertEqual(result['income_cents'], 320000)
        self.assertEqual(result['cost_cents'], 400)
        self.assertEqual(result['profit_cents'], 319600)

    def test_no_draw_has_no_simulated_income_or_profit(self):
        result = plan_financials(self.rows, '', 2, 1800, 1600)
        self.assertEqual(result['cost_cents'], 58800)
        self.assertIsNone(result['income_cents'])
        self.assertIsNone(result['profit_cents'])

    def test_total_profit_formula(self):
        plan = plan_financials(self.rows, '185', 2, 1800, 1600)
        total = calculate_total_profit_cents(
            today_income_cents=262000,
            actual_payout_cents=2520000,
            plan_result=plan
        )
        self.assertEqual(total, 23200)  # +232元

if __name__ == '__main__':
    unittest.main()
