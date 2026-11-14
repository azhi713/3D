"""Offline-only: never drives the real WeChat client or sends any message."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wechat_groups_v288 import bound_groups, group_lotteries, normalize_bindings
from wechat_notice_v288 import NoticeLedger, due_send, due_reminder, send_notice
from config import RULES_BY_LOTTERY
from models import Bet
from risk_engine import RiskEngine

CN = timezone(timedelta(hours=8))


class GroupTests(unittest.TestCase):
    def test_legacy_binding_is_kept(self):
        wc = {'fc_group': 'a@chatroom', 'tc_group': 'b@chatroom'}
        self.assertEqual(bound_groups(wc, '福彩'), ['a@chatroom'])
        self.assertEqual(normalize_bindings(wc)['fc_groups'], ['a@chatroom'])

    def test_explicit_empty_does_not_restore_legacy(self):
        self.assertEqual(bound_groups({'fc_groups': [], 'fc_group': 'old@chatroom'}, '福彩'), [])

    def test_multiples_dedupe_and_prevent_cross_lottery(self):
        wc = {'fc_groups': ['a', 'a', 'b'], 'tc_groups': ['c']}
        normalized = normalize_bindings(wc)
        self.assertEqual(group_lotteries(normalized), {'a': '福彩', 'b': '福彩', 'c': '体彩'})
        with self.assertRaises(ValueError):
            normalize_bindings({'fc_groups': ['a'], 'tc_groups': ['a']})
        with self.assertRaises(ValueError):
            normalize_bindings({'fc_groups': [str(i) for i in range(21)]})


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.config = {'send_stop_notice': True, 'special_day_reminder': True,
                       'fc_groups': ['f1', 'f2'], 'tc_groups': ['t1']}

    def test_saturday_early_and_thursday_tc_early(self):
        sat = datetime(2026, 11, 14, 21, 10, tzinfo=CN)
        thu = datetime(2026, 11, 12, 21, 20, tzinfo=CN)
        self.assertEqual([x[0] for x in due_reminder(sat, self.config)], ['福彩'])
        self.assertEqual([x[0] for x in due_reminder(thu, self.config)], ['体彩'])
        self.assertEqual(len(due_send(sat.replace(minute=13), self.config)), 2)
        self.assertEqual(len(due_send(thu.replace(minute=23), self.config)), 1)
        self.assertEqual(due_send(sat.replace(minute=14), self.config), [])
        self.assertEqual(due_send(thu.replace(minute=24), self.config), [])

    def test_independent_switches_and_no_catchup(self):
        sat = datetime(2026, 11, 14, 21, 13, tzinfo=CN)
        self.assertEqual(due_send(sat, {**self.config, 'send_stop_notice': False}), [])
        self.assertEqual(due_reminder(sat.replace(minute=10), {**self.config, 'special_day_reminder': False}), [])
        self.assertEqual(due_send(sat.replace(hour=21, minute=15), self.config), [])

    def test_crash_safe_claim_and_no_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'send.json'
            dt = datetime(2026, 11, 14, 21, 13, tzinfo=CN)
            ledger = NoticeLedger(path)
            self.assertTrue(ledger.reserve(dt.date().isoformat(), '福彩', 'f1', dt))
            self.assertFalse(NoticeLedger(path).reserve(dt.date().isoformat(), '福彩', 'f1', dt))
            ledger.finish(dt.date().isoformat(), '福彩', 'f1', 'unknown', '未验证', dt)
            self.assertFalse(NoticeLedger(path).reserve(dt.date().isoformat(), '福彩', 'f1', dt))

    def test_sender_never_confirms_none_or_collision(self):
        calls = []
        def fake(text, who, verify=False):
            calls.append((text, who, verify))
        self.assertEqual(send_notice('wechat4', 'f1', '彩友群', '福停止收单', gui_send=fake)[0], 'unknown')
        self.assertEqual(calls, [('福停止收单', '彩友群', True)])
        with self.assertRaises(RuntimeError):
            send_notice('wechat4', 'f2', '未命名群 f2', '福停止收单', gui_send=fake)
        self.assertEqual(len(calls), 1)


class LowThresholdTests(unittest.TestCase):
    def test_payout_filter_one_keeps_1000_results_without_plan(self):
        # Every number matches at least one "独胆" ticket.  All-result hedge
        # costs cannot converge; that must not erase payouts or income.
        bets = [Bet('独胆', str(i), 1, 10) for i in range(10)]
        engine = RiskEngine(RULES_BY_LOTTERY, {'span0_wins': True})
        principal, risks, payouts = engine.calculate('福彩', bets, 1, 1800,
            revenue=100, metric='payout', hedge_cost=True,
            unit_cost=2, leopard_direct_prize=1600)
        self.assertEqual(principal, 100)
        self.assertEqual(len(risks), 1000)
        self.assertEqual(len(payouts), 1000)
        self.assertTrue(engine.plan_warning)
        self.assertTrue(all(row.plan_multiplier == 0 for row in risks))


if __name__ == '__main__':
    unittest.main()
