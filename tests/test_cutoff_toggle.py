"""21:14自动封盘按开关生效；不影响原文保护、AI违规拦截和旧快照。"""
import unittest
from copy import deepcopy
from datetime import date, datetime, time
from unittest.mock import patch

from controller import Controller
from data_store import DataStore
from models import CN_TIME
from test_support import TestDirectory


class CutoffToggleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TestDirectory()
        self.store = DataStore(self.tmp.name)
        self.controller = Controller(self.store)
        self.period = self.controller.period
        self.after_cutoff = datetime.combine(date.fromisoformat(self.period), time(21, 14, 3), CN_TIME)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_default_off_works_after_cutoff_and_never_freezes(self):
        self.assertFalse(self.controller.deadline_enabled())
        with patch('controller.now', return_value=self.after_cutoff):
            self.assertIsNotNone(self.controller.add('福彩', '123单1倍'))
            self.assertEqual(self.controller.tick_deadline(), [])
        self.assertIsNone(self.store.load_batch(self.period, '福彩'))
        self.assertIsNone(self.store.load_batch(self.period, '体彩'))
        self.assertEqual(self.controller.analysis('福彩').principal, 200)

    def test_enabled_blocks_new_bets_and_generates_final_snapshot(self):
        self.controller.add('福彩', '123单1倍')
        self.controller.settings['deadline']['enabled'] = True
        with patch('controller.now', return_value=self.after_cutoff):
            self.assertEqual(len(self.controller.tick_deadline()), 2)
            with self.assertRaisesRegex(ValueError, '21:14已截止'):
                self.controller.add('福彩', '234单1倍')
        self.assertEqual(self.store.load_batch(self.period, '福彩')['final']['verified_principal_yuan'], 2)
        self.assertEqual(self.controller.tick_deadline(self.after_cutoff), [])

    def test_disable_after_cutoff_allows_new_bets_and_preserves_archived_snapshot(self):
        self.controller.add('福彩', '123单1倍')
        self.controller.settings['deadline']['enabled'] = True
        self.controller.tick_deadline(self.after_cutoff)
        frozen = deepcopy(self.store.load_batch(self.period, '福彩'))
        with patch('controller.now', return_value=self.after_cutoff):
            settings = deepcopy(self.controller.settings)
            settings['deadline']['enabled'] = False
            self.controller.save_settings(settings, deepcopy(self.controller.rules))
            self.controller.add('福彩', '234单1倍')
            self.assertEqual(self.controller.tick_deadline(), [])
        self.assertEqual(self.controller.analysis('福彩').principal, 400)
        self.assertEqual(self.store.load_batch(self.period, '福彩'), frozen)
        with self.assertRaisesRegex(ValueError, '未启用'):
            self.controller.record_settlement('福彩', '123')

    def test_reopen_restores_persisted_choice(self):
        settings = deepcopy(self.controller.settings)
        settings['deadline']['enabled'] = True
        self.controller.save_settings(settings, deepcopy(self.controller.rules))
        restarted = Controller(self.store)
        self.assertTrue(restarted.deadline_enabled())
        settings = deepcopy(restarted.settings)
        settings['deadline']['enabled'] = False
        restarted.save_settings(settings, deepcopy(restarted.rules))
        self.assertFalse(Controller(self.store).deadline_enabled())

    def test_disabled_cutoff_does_not_override_rule_violation_ai_filter(self):
        with patch('controller.now', return_value=self.after_cutoff):
            ambiguous = self.controller.add('福彩', '123来一单')
            illegal = self.controller.add('福彩', '555转圈打1倍')
            self.assertTrue(self.controller.ai_text_for_record(ambiguous))
            self.assertFalse(self.controller.ai_text_for_record(illegal))

    def test_enabled_cutoff_locks_financial_edits_but_can_be_turned_off(self):
        self.controller.settings['deadline']['enabled'] = True
        with patch('controller.now', return_value=self.after_cutoff):
            settings = deepcopy(self.controller.settings)
            settings['risk_threshold'] += 100
            with self.assertRaisesRegex(ValueError, '21:14已截止'):
                self.controller.save_settings(settings, deepcopy(self.controller.rules))
            settings = deepcopy(self.controller.settings)
            settings['deadline']['enabled'] = False
            self.controller.save_settings(settings, deepcopy(self.controller.rules))
            self.assertFalse(self.controller.deadline_enabled())


if __name__ == '__main__':
    unittest.main()
