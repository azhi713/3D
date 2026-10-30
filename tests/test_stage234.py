"""第二至四阶段：规则违规不外发AI、可追溯数据、截止快照与结算隔离。"""
import unittest
from datetime import date, datetime, time
from time import perf_counter
from unittest.mock import patch

from batch_schedule import is_closed, is_received_before_cutoff
from controller import Controller
from data_contract import record_to_contract
from data_store import DataStore
from models import CN_TIME
from test_support import TestDirectory


class Stage234Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = TestDirectory()
        self.store = DataStore(self.tmp.name)
        self.controller = Controller(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_explicit_violation_never_calls_ai_or_auto_accepts_rewrite(self):
        for raw in ('555转圈打1倍', '222组选10倍', '293单0倍', '293单-1倍'):
            with self.subTest(raw=raw):
                record = self.controller.add('福彩', raw)
                self.assertEqual(record.status, '规则违规', record.note)
                self.assertEqual(record.parse_status, 'rule_violation')
                self.assertEqual(self.controller.ai_text_for_record(record), '')
                self.assertFalse(self.controller.apply_ai_candidate(record.record_id,
                                                                   record.revision, '555单1倍'))
                self.assertEqual(self.store.get(record.record_id).bets, [])

    def test_ambiguous_ai_never_invents_numbers(self):
        record = self.controller.add('福彩', '293单1倍，未知口语，505组1倍')
        self.assertEqual(record.status, '部分识别')
        self.assertEqual(self.controller.ai_text_for_record(record), '未知口语')
        self.assertEqual(self.controller.analysis('福彩').principal, 400)
        self.assertFalse(self.controller.apply_ai_candidate(record.record_id, record.revision, '777单1倍'))
        self.assertEqual(self.controller.analysis('福彩').principal, 400)
        self.assertNotEqual(self.store.get(record.record_id).status, '已识别')
        self.assertEqual(self.controller.ai_text_for_record(self.store.get(record.record_id)), '')
        with self.assertRaises(ValueError):
            self.controller.accept_ai(self.store.get(record.record_id))

    def test_ai_cannot_change_explicit_play_or_money(self):
        wrong_play = self.controller.add('福彩','123来一单')
        self.assertFalse(self.controller.apply_ai_candidate(wrong_play.record_id,
                                                             wrong_play.revision, '123组1倍'))
        self.assertEqual(self.store.get(wrong_play.record_id).bets, [])
        wrong_amount = self.controller.add('福彩','123来一单20元')
        self.assertFalse(self.controller.apply_ai_candidate(wrong_amount.record_id,
                                                             wrong_amount.revision, '123单1倍'))
        self.assertEqual(self.store.get(wrong_amount.record_id).bets, [])
        correct_amount = self.controller.add('福彩','123来一单20元')
        self.assertTrue(self.controller.apply_ai_candidate(correct_amount.record_id,
                                                            correct_amount.revision, '123单10倍'))

    def test_ai_cannot_drop_or_multiply_original_numbers(self):
        from recognition_policy import ai_candidate_safe
        parser = self.controller.parser
        one, _, _ = parser.parse('福彩', '123单1倍')
        two, _, _ = parser.parse('福彩', '123单1倍，123单1倍')
        self.assertFalse(ai_candidate_safe('123 456 来一单', '123单1倍', one))
        self.assertFalse(ai_candidate_safe('123 来一单', '123单1倍，123单1倍', two))
        self.assertTrue(ai_candidate_safe('123 123 来一单', '123单1倍，123单1倍', two))

    def test_amount_mismatch_does_not_call_ai(self):
        record = self.controller.add('福彩', '358421组六10倍 合计200元')
        self.assertEqual(record.status, '已识别')
        self.assertEqual(record.parse_status, 'amount_mismatch')
        self.assertEqual(self.controller.ai_text_for_record(record), '')
        self.assertEqual(record.computed_amount_yuan, 100)
        self.assertEqual(record.declared_total_yuan, 200)

    def test_source_preserved_and_versions_audited(self):
        record = self.controller.add('福彩', '123单1倍')
        self.controller.edit(record, '123单2倍')
        actual = self.store.get(record.record_id)
        self.assertEqual(actual.source_original_text, '123单1倍')
        self.assertEqual(actual.raw_text, '123单2倍')
        self.assertEqual(actual.review_status, 'manually_confirmed')
        self.assertTrue(actual.bets[0].source_span)
        self.assertTrue(actual.rules_version)
        history = self.store.record_history(actual.record_id)
        self.assertTrue(history)
        self.assertEqual(history[-1]['before']['raw_text'], '123单1倍')
        self.assertEqual(history[-1]['after']['raw_text'], '123单2倍')

    def test_unified_contract_has_original_item_and_issue_fields(self):
        record = self.controller.add('福彩','293单1倍，未知口语，505组1倍')
        exported = record_to_contract(record)
        self.assertEqual(exported['schema_version'],'1.0')
        self.assertEqual(exported['raw_text'],'293单1倍，未知口语，505组1倍')
        block = exported['blocks'][0]
        self.assertEqual(block['lottery'],'FC3D')
        self.assertEqual(block['calculated_total_yuan'],'4.00')
        self.assertEqual(block['bets'][0]['ticket_codes'],['293'])
        self.assertEqual(block['unresolved_fragments'][0]['text'],'未知口语')

    def test_deadline_214_freeze_once_and_late_entry_block(self):
        period = self.controller.period
        self.controller.add('福彩', '123单1倍')
        self.controller.add('福彩', '555转圈打1倍')
        self.controller.settings['deadline']['enabled'] = True
        closed_time = datetime.combine(date.fromisoformat(period), time(21,14), CN_TIME)
        self.assertTrue(is_closed(period, closed_time))
        completed = self.controller.tick_deadline(clock=closed_time)
        self.assertEqual(len(completed), 2)
        snapshot = self.store.load_batch(period, '福彩')
        self.assertEqual(snapshot['state'], 'finalized')
        self.assertEqual(snapshot['final']['verified_principal_yuan'], 2)
        self.assertEqual(snapshot['final']['unresolved_messages'], 1)
        self.assertTrue(snapshot['final']['completed_before_21_15'])
        self.assertEqual(self.controller.tick_deadline(clock=closed_time), [])
        with patch('controller.now', return_value=closed_time):
            with self.assertRaisesRegex(ValueError, '21:14已截止'):
                self.controller.add('福彩', '888单1倍')
        self.assertEqual(self.controller.ai_text_for_record(
            self.store.load_records(period)[-1]), '')

    def test_settlement_independent_and_blocks_uncertain_prizes(self):
        period = self.controller.period
        record = self.controller.add('福彩', '123单2倍')
        self.controller.settings['deadline']['enabled'] = True
        at = datetime.combine(date.fromisoformat(period), time(21,14), CN_TIME)
        self.controller.tick_deadline(clock=at)
        settlement = self.controller.record_settlement('福彩', '123')
        self.assertEqual(settlement['total_payout_yuan'], 3600)
        self.assertEqual(record.raw_text, '123单2倍')
        with self.assertRaisesRegex(ValueError, '已经结算'):
            self.controller.record_settlement('福彩', '123')

    def test_source_received_timestamp_boundary(self):
        period = self.controller.period
        base = datetime.combine(date.fromisoformat(period), time(21,13,59), CN_TIME)
        self.assertTrue(is_received_before_cutoff(period, base.isoformat()))
        cutoff = datetime.combine(date.fromisoformat(period), time(21,14), CN_TIME)
        self.assertFalse(is_received_before_cutoff(period, cutoff.isoformat()))

    def test_400_ticket_bottom_deadline_local_benchmark(self):
        period = self.controller.period
        numbers = ' '.join(f'{n:03d}' for n in range(400))
        record = self.controller.add('福彩', numbers+'\n福400注直选1倍合800')
        self.assertEqual((record.status,len(record.bets)), ('已识别',400))
        self.controller.settings['deadline']['enabled'] = True
        cutoff = datetime.combine(date.fromisoformat(period),time(21,14),CN_TIME)
        started = perf_counter()
        summaries = self.controller.tick_deadline(clock=cutoff)
        elapsed = perf_counter() - started
        self.assertLess(elapsed, 60)
        fc = next(item for item in summaries if item['lottery']=='福彩')
        self.assertEqual((fc['verified_bet_count'],fc['verified_principal_yuan']), (400,800))


if __name__ == '__main__':
    unittest.main()
