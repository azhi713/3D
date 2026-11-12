# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from datetime import date
from ui_state_v273 import UIStateV273, visible_periods
from risk_engine import adjust_plan_rows, plan_financials, calculate_total_profit_cents
from models import RiskRow

class UIStateTests(unittest.TestCase):
    def test_shared_target_and_record_split_round_trip_without_reviving_old_goal(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'ui.json'
            current=UIStateV273(path)
            self.assertIsNone(current.data['target_goal_yuan'])
            current.data['target_profit_yuan']='10000'
            current.data['target_loss_yuan']='5000'
            current.data['target_goal_yuan']=''
            current.data['records_split']={'pane_width':1200,'sash_x':480,'ratio':0.4}
            current.save()
            restored=UIStateV273(path)
            self.assertEqual(restored.data['target_goal_yuan'],'')
            self.assertEqual(restored.data['target_profit_yuan'],'10000')
            self.assertEqual(restored.data['records_split'],
                             {'pane_width':1200,'sash_x':480,'ratio':0.4})

    def test_per_lottery_and_date_persistence(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'ui.json'
            a=UIStateV273(p)
            a.put_scope('福彩', '2026-11-03', '185', 2)
            a.put_scope('体彩', '2026-11-03', '777', -1)
            b=UIStateV273(p)
            self.assertEqual(b.get_scope('福彩','2026-11-03'), {'draw':'185','adjustment':2})
            self.assertEqual(b.get_scope('体彩','2026-11-03'), {'draw':'777','adjustment':-1})
            self.assertEqual(b.get_scope('福彩','2026-11-04'), {'draw':'','adjustment':0})
    def test_geometry_invalid_locations_safe(self):
        with tempfile.TemporaryDirectory() as td:
            a=UIStateV273(Path(td)/'ui.json')
            a.put_window_geometry('1200x850+100+80')
            self.assertEqual(UIStateV273(a.path).get_window_geometry(2560,1440),'1200x850+100+80')
            self.assertEqual(UIStateV273(a.path).get_window_geometry(900,700),'')
    def test_old_dates_hidden_or_range_limited(self):
        dates=['2026-11-04','2026-11-03','2026-10-01']
        self.assertEqual(visible_periods(dates,'2026-11-04','2026-11-04',set(),'7'),
                         ['2026-11-04','2026-11-03'])
        self.assertEqual(visible_periods(dates,'2026-11-04','2026-11-04',{'2026-11-03'},'all'),
                         ['2026-11-04','2026-10-01'])
    def test_original_36_number_plus_one(self):
        rows=[RiskRow('185',0,0,13)] + [RiskRow(f'{i:03d}',0,0,1) for i in range(35)]
        plus=adjust_plan_rows(rows,1)
        x=plan_financials(rows,'185',2,1800,1600)
        y=plan_financials(plus,'185',2,1800,1600)
        self.assertEqual(y['profit_cents']-x['profit_cents'],172800)
    def test_total_profit_accounting(self):
        rows=[RiskRow('185',0,0,13)]
        p=plan_financials(rows,'185',2,1800,1600)
        self.assertEqual(calculate_total_profit_cents(268000,2520000,p),
                         268000-2520000+p['income_cents']-p['cost_cents'])

if __name__=='__main__': unittest.main()
