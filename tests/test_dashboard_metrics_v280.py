# -*- coding: utf-8 -*-
import unittest
from types import SimpleNamespace as Row
from dashboard_metrics_v280 import *


class DashboardMetricsTests(unittest.TestCase):
    def rows(self, *pairs):
        return [Row(number=code,plan_multiplier=n) for code,n in pairs]

    def vector(self, **overrides):
        data=dict.fromkeys(CODES,0)
        data.update(overrides)
        return payout_vector(data)

    def test_full_1000_required(self):
        with self.assertRaises(ValueError): payout_vector({'001':0})

    def test_unique_count_duplicate_stakes_retained(self):
        stats=candidate_metrics(10000,self.vector(),self.rows(('002',3),('002',2),('020',1)),2,1800,1600)
        self.assertEqual((stats.covered_count,stats.total_multiples,stats.cost_cents),(2,6,1200))
        self.assertEqual(stats.at('002')['return_cents'],900000)

    def test_no_draw_needed_for_range(self):
        m=candidate_metrics(10000,self.vector(**{'001':200}),self.rows(('001',1)),2,1800,1600)
        self.assertEqual(m.worst_profit_cents,9800)
        self.assertEqual(m.best_profit_cents,169800)

    def test_loss_count_includes_uncovered_and_non_threshold(self):
        m=candidate_metrics(10000,self.vector(**{'001':200,'002':110}),self.rows(('001',1)),2,1800,1600)
        self.assertEqual(m.loss_count,1)
        self.assertEqual(m.worst_profit_cents,-1200)

    def test_even_not_loss(self):
        m=purchased_metrics(10000,self.vector(**{'001':100}),None)
        self.assertEqual(m.loss_count,0)

    def test_no_purchase_zero_hedge_cost(self):
        m=purchased_metrics(10000,self.vector(**{'001':200}),None)
        self.assertEqual((m.cost_cents,m.covered_count,m.covered_min_profit_cents),(0,0,None))
        self.assertEqual(m.at('001'),{'total_profit_cents':-10000,'return_cents':0,'hedge_profit_cents':0})

    def test_miss_deducts_whole_plan(self):
        m=candidate_metrics(10000,self.vector(),self.rows(('001',2),('002',3)),2,1800,1600)
        self.assertEqual(m.at('999')['hedge_profit_cents'],-1000)
        self.assertEqual(m.at('999')['total_profit_cents'],9000)

    def test_cost_not_subtracted_twice(self):
        m=candidate_metrics(262000,self.vector(**{'072':64800}),self.rows(('072',35),('073',259)),2,1800,1600)
        self.assertEqual(m.cost_cents,58800)
        self.assertEqual(m.at('072')['total_profit_cents'],23200)

    def test_leopard_prize_is_distinct(self):
        m=candidate_metrics(0,self.vector(),self.rows(('000',2),('001',2)),2,1800,1600)
        self.assertEqual(m.at('000')['return_cents'],320000)
        self.assertEqual(m.at('001')['return_cents'],360000)

    def test_min_covered_is_whole_day_net(self):
        m=candidate_metrics(10000,self.vector(**{'001':1900}),self.rows(('001',1),('002',1)),2,1800,1600)
        self.assertEqual(m.covered_min_profit_cents,-400)

    def test_actual_uses_stored_rules(self):
        p={'rows':[['001',2]],'cost_cents':700,'direct_prize_cents':190000,'leopard_prize_cents':150000}
        m=purchased_metrics(10000,self.vector(),p)
        self.assertEqual((m.cost_cents,m.at('001')['return_cents']),(700,380000))

    def test_empty_plan_is_valid(self):
        m=candidate_metrics(10000,self.vector(),[],2,1800,1600)
        self.assertEqual((m.cost_cents,m.covered_count,m.covered_min_profit_cents),(0,0,None))

    def test_all_loss_does_not_fake_max_profit(self):
        m=purchased_metrics(10000,payout_vector(dict.fromkeys(CODES,200)),None)
        self.assertEqual((m.max_loss_cents,m.max_gain_cents),(-10000,0))

    def test_all_profit_does_not_fake_max_loss(self):
        m=purchased_metrics(10000,self.vector(),None)
        self.assertEqual((m.max_loss_cents,m.max_gain_cents),(0,10000))

    def test_money_fraction_exact(self):
        self.assertEqual(amount_cents('13853.50'),1385350)
        with self.assertRaises(ValueError):amount_cents('0.001')
        with self.assertRaises(ValueError):amount_cents(float('inf'))
        with self.assertRaises(ValueError):amount_cents(True)

    def test_invalid_code_or_multiple_rejected(self):
        for code,count in [('2',1),('１２３',1),('001',0.5),('001',-1)]:
            with self.assertRaises(ValueError):aggregate_rows(self.rows((code,count)))

    def test_format_zero_and_thousands(self):
        self.assertEqual(format_yuan(10000,True),'+100')
        self.assertEqual(format_yuan(-1385350,True),'-13,853.5')
        self.assertEqual(format_yuan(0,True),'0')

    def test_invalid_draw_not_guessed(self):
        m=purchased_metrics(10000,self.vector(),None)
        with self.assertRaises(ValueError):m.at('01')

    def test_restore_geometry_state_separate(self):
        d={'normal_window_geometry':'1200x800+20+30','window_geometry':'1920x1080+0+0','window_zoomed':True}
        self.assertEqual(restored_geometry(d,1920,1080),'1200x800+20+30')

    def test_old_maximized_geometry_falls_back(self):
        self.assertEqual(restored_geometry({'window_geometry':'1920x1040+0+0','window_zoomed':True},1920,1080),'')

    def test_offscreen_geometry_rejected(self):
        self.assertEqual(restored_geometry({'normal_window_geometry':'1200x800+3000+500'},1920,1080),'')

    def test_compare_uniform_all_outcomes(self):
        p=self.vector(**{'000':3200,'001':4000,'099':3000})
        rows=self.rows(('000',2),('001',2))
        m=candidate_metrics(100000,p,rows,2,1800,1600)
        brute=[100000-p[n]-800+(320000 if n==0 else 360000 if n==1 else 0) for n in range(1000)]
        self.assertEqual(m.profits_cents,tuple(brute))
        self.assertEqual(m.loss_count,sum(x<0 for x in brute))
        self.assertEqual(m.covered_min_profit_cents,min(brute[:2]))


if __name__=='__main__':unittest.main(verbosity=2)
