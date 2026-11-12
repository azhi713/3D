"""Production currency/plan helpers; no desktop or personal data required."""
import unittest
from dataclasses import FrozenInstanceError
from models import RiskRow
from dashboard_metrics_v280 import (CODES, purchased_metrics, candidate_metrics)
from hedge_edit_v276 import parse_hedge_text
from risk_extension_v2734 import freeze_purchase_snapshot
from plan_snapshot_v283 import *

class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.rows=[RiskRow('935',0,0,2),RiskRow('002',0,0,3),RiskRow('002',0,0,4),RiskRow('000',0,0,1),RiskRow('001',0,0,0)]
        self.payouts=(0,)*1000
    def view(self,**kwargs):
        p=dict(context=('福彩','2026-11-07'),mode='manual',source='手动编辑',source_key=('token',),
               revenue_cents=1000000,payouts_cents=self.payouts,rows=self.rows,settings=(2,1800,1600))
        p.update(kwargs);return build_displayed_plan(**p)
    def test_immutable_view(self):
        view=self.view()
        with self.assertRaises(FrozenInstanceError):view.mode='cheap'
    def test_unique_codes_sum_multiples(self):
        v=self.view();self.assertEqual(v.bets,(('000',1),('002',7),('935',2)))
        self.assertEqual(v.count,3);self.assertEqual(v.cost_cents,2000)
    def test_sorted_number_copy(self):
        self.assertEqual(self.view().numbers_text,'000 002 935\n\n合计：3注')
    def test_full_copy_reparse_matches(self):
        v=self.view();rows,cost=parse_hedge_text(v.full_text,'福彩',2)
        self.assertEqual(aggregate_rows(rows),v.bets);self.assertEqual(cost,v.cost_cents)
    def test_zero_multiplier_excluded(self):self.assertNotIn('001',self.view().numbers_text)
    def test_mutating_source_does_not_change_snapshot(self):
        v=self.view();self.rows[0].plan_multiplier=999
        self.assertEqual(dict(v.bets)['935'],2)
    def test_mutating_returned_rows_cannot_change_snapshot(self):
        v=self.view();rows=v.as_rows();rows[0].plan_multiplier=999
        self.assertEqual(dict(v.bets)['000'],1)
    def test_draft_is_preserved(self):
        v=self.view(pending=True)
        self.assertTrue(v.numbers_text.startswith(DRAFT));self.assertTrue(v.full_text.startswith(DRAFT))
    def test_manual_layout_preserved_cost_updated(self):
        raw='福直选3倍\n002\n福直选4倍\n002\n福直选1倍\n000\n福直选2倍\n935\n合计：999元'
        v=self.view(manual_text=raw)
        self.assertIn('合计：20元',v.display_text);self.assertEqual(v.display_text.count('002'),2)
    def test_three_digit_validation(self):
        with self.assertRaises(ValueError):self.view(rows=[RiskRow('2',0,0,1)])
    def test_no_silent_fraction_truncation(self):
        with self.assertRaises(ValueError):self.view(rows=[RiskRow('002',0,0,.5)])
    def test_empty_plan_statistics_still_valid(self):
        v=self.view(rows=[]);self.assertEqual(v.count,0);self.assertEqual(v.metrics.best_profit_cents,1000000)
    def test_guard_rejects_display_mismatch(self):
        v=self.view();args=dict(mode=v.mode,context=v.context,source_key=v.source_key,rows=v.as_rows(),
              editor_text=v.display_text,count_caption=v.count_caption,cost_caption=v.cost_caption,
              comparison_count=v.count,comparison_cost=format_yuan(v.cost_cents))
        self.assertIs(assert_view_matches(v,**args),v)
        for key,bad in [('mode','balanced'),('source_key',('old',)),('context',('体彩','2026-11-07')),
                        ('count_caption','号码：78组'),('comparison_count',78),('comparison_cost','999'),('editor_text','old text')]:
            with self.subTest(key=key),self.assertRaises(ValueError):assert_view_matches(v,**dict(args,**{key:bad}))

class TargetTests(unittest.TestCase):
    def test_blank_disables(self):self.assertIsNone(parse_profit_target('  '))
    def test_zero_valid(self):self.assertEqual(parse_profit_target('0'),0)
    def test_exact_cents(self):self.assertEqual(parse_profit_target('10000.25'),1000025)
    def test_negative_exact_cents(self):
        self.assertEqual(parse_profit_target('-100'),-10000)
        self.assertEqual(parse_profit_target(' -1000.25 '),-100025)
        self.assertEqual(parse_profit_target('-0.01'),-1)
        self.assertEqual(parse_profit_target('-0'),0)
    def test_reject_invalid_targets(self):
        for x in ['--1','-1.001','-1e4','NaN','Infinity','1/4','abc','1.001','1e4',True,None]:
            with self.subTest(x=x),self.assertRaises(ValueError):parse_profit_target(x)
    def test_negative_target_is_at_least_this_loss_not_at_most(self):
        payouts=[0]*1000;payouts[85]=20000
        m=purchased_metrics(10000,tuple(payouts),None)
        self.assertEqual(target_outcomes(m,-10000),('085',))
        self.assertEqual(target_outcomes(m,-9999),('085',))
        self.assertEqual(len(target_outcomes(m,-10001)),0)
        self.assertEqual(len(target_outcomes(m,0)),999)
        self.assertEqual(len(target_outcomes(m,10001)),0)
    def test_profit_or_loss_10000_yuan_thresholds(self):
        payouts=[0]*1000;payouts[85]=2000000
        m=purchased_metrics(1000000,tuple(payouts),None)
        self.assertEqual(target_outcomes(m,-1000000),('085',))
        self.assertEqual(len(target_outcomes(m,1000000)),999)
    def test_negative_target_uses_purchased_cost_and_returns(self):
        payouts=[0]*1000;payouts[85]=30000
        original=purchased_metrics(10000,tuple(payouts),None)
        purchase=freeze_purchase_snapshot([RiskRow('086',0,0,100)],2,1800,1600)
        bought=purchased_metrics(10000,tuple(payouts),purchase)
        self.assertEqual(original.cost_cents,0)
        self.assertEqual(bought.cost_cents,20000)
        self.assertEqual(target_outcomes(original,-10000),('085',))
        self.assertEqual(len(target_outcomes(bought,-10000)),999)
        self.assertNotIn('086',target_outcomes(bought,-10000))
    def test_all_1000_and_inclusive(self):
        payouts=tuple(20000 if i%2 else 0 for i in range(1000))
        m=purchased_metrics(1000000,payouts,None)
        self.assertEqual(len(target_outcomes(m,1000000)),500)
        self.assertEqual(len(target_outcomes(m,980000)),1000)
        self.assertEqual(len(target_outcomes(m,1000001)),0)
    def test_no_hedge_has_no_cost_or_return(self):
        payouts=[0]*1000;payouts[302]=1080000
        m=purchased_metrics(787800,tuple(payouts),None)
        self.assertEqual(m.at('302')['total_profit_cents'],-292200)
        self.assertEqual(m.cost_cents,0);self.assertEqual(sum(m.returns_cents),0)
    def test_miss_still_deducts_entire_cost(self):
        payouts=[0]*1000;payouts[302]=1080000
        purchase={'rows':[['202',951]],'cost_cents':190200,'direct_prize_cents':180000,'leopard_prize_cents':160000}
        m=purchased_metrics(787800,tuple(payouts),purchase)
        self.assertEqual(m.at('302')['total_profit_cents'],-482400)
    def test_hit_no_double_cost_deduction(self):
        rows=[RiskRow('123',0,0,5),RiskRow('002',0,0,1)]
        payouts=[0]*1000;payouts[123]=1000000
        m=candidate_metrics(1000000,tuple(payouts),rows,2,1800,1600)
        self.assertEqual(m.at('123')['total_profit_cents'],898800)
    def test_leopard_reward(self):
        m=candidate_metrics(1000000,(0,)*1000,[RiskRow('000',0,0,1)],2,1800,1600)
        self.assertEqual(m.at('000')['return_cents'],160000)
    def test_uncovered_profitable_numbers_counted(self):
        m=candidate_metrics(1000000,(0,)*1000,[RiskRow('000',0,0,1)],2,1800,1600)
        self.assertEqual(len(target_outcomes(m,999800)),1000)
    def test_target_changes_never_modify_plan(self):
        rows=[RiskRow('000',0,0,1)];m=candidate_metrics(1000000,(0,)*1000,rows,2,1800,1600)
        before=(rows[0].plan_multiplier,m.cost_cents,m.profits_cents)
        for target in [0,100,1000000,2000000,None]:target_outcomes(m,target)
        self.assertEqual(before,(rows[0].plan_multiplier,m.cost_cents,m.profits_cents))
    def test_actual_goal_includes_full_hedge_cost_and_number_specific_return(self):
        payouts=[0]*1000;payouts[85]=30000
        original=purchased_metrics(10000,tuple(payouts),None)
        purchase=freeze_purchase_snapshot([RiskRow('085',0,0,18)],2,1800,1600)
        bought=purchased_metrics(10000,tuple(payouts),purchase)
        self.assertEqual(original.cost_cents,0)
        self.assertEqual(original.at('085')['total_profit_cents'],-20000)
        self.assertEqual(bought.cost_cents,3600)
        self.assertEqual(bought.at('085')['return_cents'],18*180000)
        self.assertEqual(bought.at('085')['total_profit_cents'],10000-30000-3600+18*180000)
        self.assertEqual(bought.at('086')['total_profit_cents'],10000-3600)
        self.assertEqual(len(target_outcomes(original,10000)),999)
        self.assertEqual(len(target_outcomes(bought,10000)),1)

if __name__=='__main__':unittest.main(verbosity=2)
