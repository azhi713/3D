"""Headless checks for the V283 target-number display and copy payload."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dashboard_v283 import (DashboardV283Mixin, PlanComparisonV283, INPUT_IDLE_MS, INPUT_DELETE_IDLE_MS,
                            _v283_format_target_numbers,
                            _v283_parse_unsigned_target, _v283_migrate_signed_target,
                            _v283_loss_outcomes, _v283_tier_counts)
from dashboard_v282 import DashboardV282Mixin
from dashboard_metrics_v280 import purchased_metrics


class TargetNumberViewTests(unittest.TestCase):
    def test_typing_goal_resets_idle_timer_without_intermediate_calculation(self):
        scheduled = {}
        cancelled = []
        committed = []
        delays = []
        labels = {}
        value = {'profit': '15000'}
        def after(ms, callback):
            delays.append(ms)
            number = len(cancelled) + len(scheduled) + 1
            scheduled[number] = callback
            return number
        def after_cancel(number):
            cancelled.append(number)
            scheduled.pop(number, None)
        fixture = SimpleNamespace(
            _closing=False, _v283_goal_timer=None,
            _v283_previous_goal_text='15000',
            _v283_set_target_numbers=lambda *_: None,
            after=after, after_cancel=after_cancel,
            target_profit=SimpleNamespace(get=lambda: value['profit']),
            target_profit_count=SimpleNamespace(configure=lambda **kw: labels.update(profit=kw['text'])),
            target_loss_count=SimpleNamespace(configure=lambda **kw: labels.update(loss=kw['text'])),
            target_profit_source=SimpleNamespace(configure=lambda **kw: None),
            _v283_commit_target=lambda: committed.append(value['profit']),
        )
        for partial in ('15000', '5000', '25000'):
            value['profit'] = partial
            DashboardV283Mixin._v283_target_changed(fixture)
            self.assertFalse(committed)
            self.assertEqual(len(scheduled), 1)
        self.assertEqual(len(cancelled), 2)
        self.assertEqual(delays, [INPUT_IDLE_MS, INPUT_DELETE_IDLE_MS, INPUT_IDLE_MS])
        self.assertIn('输入中', labels['profit'])
        self.assertIn('输入中', labels['loss'])
        next(iter(scheduled.values()))()
        self.assertEqual(committed, ['25000'])

    def test_ten_profit_loss_tiers_each_use_a_single_complete_actual_vector(self):
        profits = [500000] * 996 + [-100000, -200000, -1000000, 1000000]
        tiers = _v283_tier_counts(profits)
        self.assertEqual(tuple(row[0] for row in tiers), tuple(range(1000, 10001, 1000)))
        self.assertEqual(tiers[0], (1000, 997, 3))
        self.assertEqual(tiers[4], (5000, 997, 1))
        self.assertEqual(tiers[-1], (10000, 1, 1))
        for _yuan, profit, loss in tiers:
            self.assertLessEqual(profit + loss, 1000)
        with self.assertRaises(ValueError):
            _v283_tier_counts(profits[:-1])

    def test_typing_threshold_waits_then_applies_and_focus_out_commits(self):
        scheduled = {}
        events = []
        delays = []
        def after(ms, callback):
            delays.append(ms)
            scheduled['pending'] = callback
            return 'pending'
        def cancel(timer):
            events.append(('cancel', timer))
            scheduled.pop(timer, None)
        fixture = SimpleNamespace(
            _closing=False, _threshold_timer=None,
            after=after, after_cancel=cancel,
            apply_filter=lambda: events.append(('apply',)),
            _v281_apply_threshold_now=lambda: events.append(('enter/focus',)),
        )
        DashboardV283Mixin._v283_threshold_input(fixture, SimpleNamespace(keysym='BackSpace'))
        DashboardV283Mixin._v283_threshold_input(fixture, SimpleNamespace(keysym='2'))
        self.assertEqual(events, [('cancel', 'pending')])
        self.assertEqual(delays, [INPUT_DELETE_IDLE_MS, INPUT_IDLE_MS])
        self.assertEqual(len(scheduled), 1)
        DashboardV283Mixin._v283_threshold_input(fixture, SimpleNamespace(keysym='Return'))
        self.assertEqual(len(scheduled), 1)
        DashboardV283Mixin._v283_threshold_focus_out(fixture)
        self.assertEqual(events[-1], ('enter/focus',))
        self.assertNotIn(('apply',), events)

    def test_manual_plan_uses_same_idle_delay_before_replacing_purchase(self):
        class Fake(DashboardV283Mixin):
            def __init__(self):
                self._v283_ready = True
                self._plan_edit_timer = None
                self._plan_manual_text = ''
                self.next_text = '12345'
                self._v283_current = object()
                self._v283_views = {'manual': object()}
                self.plan_count_label = SimpleNamespace(configure=lambda **_: None)
                self.optimizer_compare_table = SimpleNamespace(cells={
                    ('manual', column): SimpleNamespace(configure=lambda **_: None)
                    for column in range(1, 9)})
                self.jobs = {}
                self.cancelled = []
                self.committed = []
                self.ui_updates = []
            def after(self, ms, callback):
                job = max(self.jobs.keys(), default=0) + 1
                self.jobs[job] = (ms, callback)
                return job
            def after_cancel(self, job):
                self.cancelled.append(job)
                self.jobs.pop(job, None)
            def _v283_pending_manual(self):
                return self._plan_edit_timer is not None
            def _v283_show_comparison(self):
                self.ui_updates.append('expensive-comparison')
            def _v283_target_status(self, status):
                self.ui_updates.append(status)
            def _v282_refresh_copy_controls(self):
                self.ui_updates.append('copy-status')
            def _show_hedge_checkbox(self):
                self.ui_updates.append('purchase-status')
            def _commit_manual_plan(self):
                self.committed.append(True)
        fixture = Fake()
        def original_short_timer(self, event):
            if self._plan_edit_timer is not None:
                self.after_cancel(self._plan_edit_timer)
            self._plan_manual_text = self.next_text
            self._plan_edit_timer = self.after(170, self._commit_manual_plan)
        with patch.object(DashboardV282Mixin, '_on_plan_text_modified', original_short_timer):
            fixture._on_plan_text_modified()
            self.assertEqual([ms for ms, _ in fixture.jobs.values()], [INPUT_IDLE_MS])
            self.assertEqual(fixture.committed, [])
            fixture.next_text = '123'
            fixture._on_plan_text_modified()
            self.assertEqual([ms for ms, _ in fixture.jobs.values()], [INPUT_DELETE_IDLE_MS])
            self.assertEqual(fixture.committed, [])
        self.assertEqual(fixture.ui_updates, ['方案编辑中', 'copy-status', 'purchase-status'])

    def test_two_actual_counts_follow_purchase_state_together(self):
        class Label:
            def __init__(self):self.text=''
            def configure(self,**kwargs):self.text=kwargs.get('text',self.text)
            def cget(self,_name):return self.text
        payouts=[0]*1000;payouts[85]=2000000
        retained={}
        source=Label();profit_label=Label();loss_label=Label()
        tier_labels={yuan:(Label(),Label()) for yuan in range(1000,10001,1000)}
        fixture=SimpleNamespace(
            _v283_ready=True,_v283_target=10000,_v283_loss_target=10000,
            _v283_target_error='',_v283_loss_target_error='',_v283_goal_timer=None,
            _v283_risk_busy=lambda:False,_v283_pending_manual=lambda:False,
            _v283_current=object(),_v283_live_timer=None,_v283_live_syncing=False,
            _v283_live_failed_key=None,
            _v280_inputs=lambda:(1000000,tuple(payouts)),
            _v280_purchase=lambda:None,_purchased_mode=lambda:'manual',
            snapshot=SimpleNamespace(pending=0),target_profit_source=source,
            target_profit_count=profit_label,target_loss_count=loss_label,
            _v283_tier_labels=tier_labels,
            _v283_set_target_numbers=lambda numbers,kind='profit':retained.__setitem__(kind,numbers),
        )
        DashboardV283Mixin._v283_update_actual_target(fixture)
        self.assertEqual(len(retained['profit']),999)
        self.assertEqual(retained['loss'],('085',))
        self.assertEqual(loss_label.text,'亏损达标：1组')
        self.assertEqual(tier_labels[1000][0].text, '999组')
        self.assertEqual(tier_labels[1000][1].text, '1组')
        purchase={'rows':[('085',10)],'cost_cents':2000,
                  'direct_prize_cents':180000,'leopard_prize_cents':160000}
        fixture._v280_purchase=lambda:purchase
        DashboardV283Mixin._v283_update_actual_target(fixture)
        self.assertEqual(len(retained['profit']),1000)
        self.assertEqual(retained['loss'],())
        self.assertEqual(profit_label.text,'盈利达标：1000组')
        self.assertEqual(loss_label.text,'亏损达标：0组')
        self.assertEqual(tier_labels[1000][0].text, '1000组')
        self.assertEqual(tier_labels[1000][1].text, '0组')

    def test_comparison_headings_show_both_thresholds(self):
        labels=[]
        view=PlanComparisonV283.__new__(PlanComparisonV283)
        view.target_heading=SimpleNamespace(configure=lambda **k: labels.append(k['text']))
        view.loss_heading=SimpleNamespace(configure=lambda **k: labels.append(k['text']))
        view.set_target(1000000,False,1000000,False)
        self.assertEqual(labels,['盈利≥10,000元','亏损≥10,000元'])
        labels.clear()
        view.set_target(None,False,0,False)
        self.assertEqual(labels,['盈利达标','亏损≥0元'])

    def test_loss_and_profit_thresholds_use_opposite_comparisons(self):
        payouts=[0]*1000;payouts[85]=20000;payouts[86]=10000
        metrics=purchased_metrics(10000,tuple(payouts),None)
        self.assertEqual(_v283_loss_outcomes(metrics,0),('085','086'))
        self.assertEqual(_v283_loss_outcomes(metrics,1),('085',))
        self.assertEqual(_v283_loss_outcomes(metrics,10000),('085',))
        self.assertEqual(_v283_loss_outcomes(metrics,10001),())
    def test_split_nonnegative_targets_and_migrate_old_negative(self):
        self.assertEqual(_v283_migrate_signed_target('-10000.25', ''), ('', '10000.25'))
        self.assertEqual(_v283_migrate_signed_target('-100', '500'), ('', '500'))
        self.assertEqual(_v283_migrate_signed_target('300', '500'), ('300', '500'))
        self.assertEqual(_v283_parse_unsigned_target('10000.25', '目标盈利'), 1000025)
        self.assertEqual(_v283_parse_unsigned_target('0', '目标亏损'), 0)
        self.assertIsNone(_v283_parse_unsigned_target('', '目标亏损'))
        with self.assertRaises(ValueError):
            _v283_parse_unsigned_target('-100', '目标亏损')
    def test_all_1000_numbers_are_present_and_keep_leading_zeroes(self):
        numbers = tuple(f'{i:03d}' for i in range(1000))
        rendered = _v283_format_target_numbers(numbers)
        self.assertEqual(rendered.split(), list(numbers))
        self.assertEqual(len(rendered.splitlines()), 84)
        self.assertTrue(rendered.startswith('000 001 002'))
        self.assertTrue(rendered.endswith('996 997 998 999'))

    def test_unavailable_differs_from_valid_empty_result(self):
        controls = []
        refreshes = []
        closes = []
        fixture = SimpleNamespace(
            _v283_target_numbers=None, _v283_target_detail=None,
            _v283_loss_numbers=None, _v283_loss_detail=None,
            _v283_target_tip_kind='profit', _v283_target_dialog_kind='profit',
            _v283_target=10000, lottery=SimpleNamespace(get=lambda: '福彩'),
            _v283_loss_target=10000,
            controller=SimpleNamespace(period='2026-11-10'),
            target_profit_source=SimpleNamespace(cget=lambda name: '〔未补单〕'),
            target_profit_view_button=SimpleNamespace(configure=lambda **kw: controls.append(kw['state'])),
            target_loss_view_button=SimpleNamespace(configure=lambda **kw: controls.append('loss:'+kw['state'])),
            _v283_hide_target_tip=lambda: closes.append(True),
            _v283_refresh_target_dialog=lambda: refreshes.append(True),
        )
        setter = DashboardV283Mixin._v283_set_target_numbers
        setter(fixture, ())
        self.assertEqual(fixture._v283_target_numbers, ())
        self.assertEqual(controls[-1], 'normal')  # 0 matches is a valid completed calculation.
        setter(fixture, ('000', '085'))
        self.assertEqual(fixture._v283_target_numbers, ('000', '085'))
        self.assertEqual(controls[-1], 'normal')
        setter(fixture, None)
        self.assertIsNone(fixture._v283_target_numbers)
        self.assertEqual(controls[-1], 'disabled')
        self.assertEqual(len(refreshes), 3)
        self.assertEqual(len(closes), 3)
        setter(fixture, ('000',), 'loss')
        self.assertEqual(fixture._v283_loss_numbers, ('000',))
        self.assertEqual(fixture._v283_loss_detail[2], 10000)
        self.assertEqual(controls[-1], 'loss:normal')
        self.assertEqual(len(refreshes), 3)  # Profit dialog is not refreshed by loss changes.
        self.assertIsNone(fixture._v283_target_numbers)


if __name__ == '__main__':
    unittest.main()
