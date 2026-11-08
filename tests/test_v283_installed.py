"""Executed against a temporary copy of the user's actual installed modules.
No main window, account, clipboard, live data directory, or network is opened.
"""
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from models import RiskRow
from risk_extension_v2734 import freeze_purchase_snapshot
from plan_snapshot_v283 import parse_profit_target, target_outcomes
from dashboard_metrics_v280 import purchased_metrics

class InstalledRuntime(unittest.TestCase):
    def test_ui_adapter_is_active(self):
        from dashboard_v283 import DashboardV283Mixin
        from ui import RiskApp
        self.assertTrue(issubclass(RiskApp, DashboardV283Mixin))
        self.assertEqual(RiskApp._render_plan.__module__,'dashboard_v283')
        self.assertEqual(RiskApp._v282_copy_selected.__module__,'dashboard_v283')
        self.assertEqual(RiskApp._restore_system_plan.__module__,'dashboard_v283')
    def test_goal_state_roundtrip_preserves_existing_records(self):
        from ui_state_v273 import UIStateV273
        with tempfile.TemporaryDirectory(prefix='v283-ui-state-') as td:
            path=Path(td)/'ui_state_v273.json';s=UIStateV273(path)
            purchase=freeze_purchase_snapshot([RiskRow('002',0,0,3)],2,1800,1600)
            s.put_manual_plan('福彩','2026-11-07','福直选3倍\n002')
            s.put_purchased_plan('福彩','2026-11-07',purchase,'manual')
            before=json.loads(path.read_text('utf8'))
            s.data['target_profit_yuan']='10000.25';s.save()
            loaded=UIStateV273(path)
            self.assertEqual(loaded.data['target_profit_yuan'],'10000.25')
            for key in before:
                if key!='target_profit_yuan':self.assertEqual(loaded.data[key],before[key],key)
            loaded.data['target_profit_yuan']='';loaded.save()
            self.assertIsNone(parse_profit_target(UIStateV273(path).data['target_profit_yuan']))
    def test_no_hedge_goal_after_clear(self):
        from ui_state_v273 import UIStateV273
        with tempfile.TemporaryDirectory(prefix='v283-cleared-') as td:
            s=UIStateV273(Path(td)/'state.json')
            p=freeze_purchase_snapshot([RiskRow('002',0,0,3)],2,1800,1600)
            s.put_purchased_plan('福彩','2026-11-07',p,'balanced')
            s.put_purchased_plan('福彩','2026-11-07',None,'balanced')
            m=purchased_metrics(1000000,(0,)*1000,s.get_actual_hedge('福彩','2026-11-07'))
            self.assertEqual(m.cost_cents,0)
            self.assertEqual(len(target_outcomes(m,1000000)),1000)
    def test_previous_recognition_modules_remain_importable(self):
        # Read/import only. This patch does not revise their behavior.
        for module in ('shared_orders_v282','source_context_v281','wechat_text_v281'):
            self.assertIsNotNone(importlib.import_module(module))

if __name__=='__main__':unittest.main(verbosity=2)
