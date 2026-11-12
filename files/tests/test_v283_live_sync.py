"""Headless checks for live purchased-plan replacement; no display needed."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dashboard_v283 import DashboardV283Mixin
from dashboard_metrics_v280 import purchased_metrics
from models import RiskRow
from plan_snapshot_v283 import build_displayed_plan
from risk_extension_v2734 import freeze_purchase_snapshot
from tests.test_v283_ui import UIStateFixture


def view(mode, bets, generation=1):
    rows = [RiskRow(code, 0, 0, multiple) for code, multiple in bets]
    return build_displayed_plan(
        context=('福彩', '2026-11-08'), mode=mode, source='测试方案',
        source_key=('verified', generation, mode), revenue_cents=2081200,
        payouts_cents=(0,) * 1000, rows=rows, settings=(2, 1800, 1600))


class FakeDashboard(DashboardV283Mixin):
    def __init__(self, state, current):
        self._ui_state = state
        self._v283_ready = True
        self._closing = False
        self._v283_live_timer = None
        self._v283_live_syncing = False
        self._v283_live_failed_key = None
        self._v283_live_armed_context = current.context
        self._v283_current = current
        self._plan_mode = current.mode
        self.lottery = SimpleNamespace(get=lambda: '福彩')
        self.controller = SimpleNamespace(period='2026-11-08')
        self.snapshot = SimpleNamespace(pending=0)
        self._v280_eval_cache = {}
        self._idle = None
        self.card_updates = 0

    def _v280_purchase(self):
        return self._ui_state.get_actual_hedge('福彩', '2026-11-08')

    def _purchased_mode(self):
        return self._ui_state.get_purchased_mode('福彩', '2026-11-08')

    def _v280_settings(self):
        return (2, 1800, 1600)

    def _v283_risk_busy(self):
        return False

    def _v283_pending_manual(self):
        return False

    def _v283_checked_view(self):
        return self._v283_current

    def after_idle(self, callback):
        self._idle = callback
        return 'pending-idle'

    def flush_idle(self):
        callback = self._idle
        self._idle = None
        if callback:
            callback()

    def _show_hedge_checkbox(self):
        pass

    def _update_financial_cards(self):
        self.card_updates += 1


class LiveReplacementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        state = UIStateFixture(Path(self.temp.name) / 'state.json')
        original = view('manual', [('085', 12)])
        state.put_purchased_plan('福彩', '2026-11-08',
                                 freeze_purchase_snapshot(original.as_rows(), 2, 1800, 1600), 'manual')
        self.dashboard = FakeDashboard(state, original)

    def tearDown(self):
        self.temp.cleanup()

    def test_switch_replaces_owner_cost_and_reward(self):
        d = self.dashboard
        d._v283_current = view('balanced', [('085', 18), ('836', 5)])
        d._plan_mode = 'balanced'
        d._v283_queue_purchase_sync()
        self.assertIsNotNone(d._idle)
        d.flush_idle()
        self.assertEqual(d._purchased_mode(), 'balanced')
        self.assertEqual(d._v280_purchase()['cost_cents'], (18 + 5) * 200)
        self.assertEqual(dict(d._v280_purchase()['rows']), {'085': 18, '836': 5})
        actual = purchased_metrics(2081200, (0,) * 1000, d._v280_purchase())
        self.assertEqual(actual.profits_cents, d._v283_current.metrics.profits_cents)
        self.assertEqual(d.card_updates, 1)
        d._v283_queue_purchase_sync()
        self.assertIsNone(d._idle)

    def test_opening_saved_period_without_an_edit_does_not_overwrite_history(self):
        d = self.dashboard
        original = d._v280_purchase()
        d._v283_live_armed_context = None
        d._v283_current = view('balanced', [('085', 18)])
        d._plan_mode = 'balanced'
        d._v283_queue_purchase_sync()
        self.assertIsNone(d._idle)
        self.assertEqual(d._v280_purchase(), original)

    def test_cancelled_purchase_is_not_resurrected_by_deferred_sync(self):
        d = self.dashboard
        d._v283_current = view('cheap', [('999', 3)])
        d._plan_mode = 'cheap'
        d._v283_queue_purchase_sync()
        d._ui_state.put_purchased_plan('福彩', '2026-11-08', None, 'manual')
        d.flush_idle()
        self.assertIsNone(d._v280_purchase())

    def test_pending_or_empty_never_erases_real_purchase(self):
        d = self.dashboard
        original = d._v280_purchase()
        d.snapshot.pending = 1
        d._v283_current = view('manual', [('085', 19)], 2)
        d._v283_queue_purchase_sync()
        self.assertIsNone(d._idle)
        d.snapshot.pending = 0
        d._v283_current = view('manual', [], 3)
        d._v283_queue_purchase_sync()
        self.assertIsNone(d._idle)
        self.assertEqual(d._v280_purchase(), original)

    def test_failed_write_retains_old_purchase_and_does_not_retry_forever(self):
        d = self.dashboard
        original = d._v280_purchase()
        d._v283_current = view('manual', [('085', 19)], 2)
        with patch('dashboard_v283.save_purchase_checked', side_effect=OSError('read-only')):
            with patch('dashboard_v283.messagebox.showerror') as warning:
                d._v283_queue_purchase_sync()
                d.flush_idle()
                warning.assert_called_once()
        self.assertEqual(d._v280_purchase(), original)
        d._v283_queue_purchase_sync()
        self.assertIsNone(d._idle)
        d._v283_current = view('manual', [('085', 20)], 3)
        d._v283_queue_purchase_sync()
        d.flush_idle()
        self.assertEqual(dict(d._v280_purchase()['rows'])['085'], 20)


if __name__ == '__main__':
    unittest.main(verbosity=2)
