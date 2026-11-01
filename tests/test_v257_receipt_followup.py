"""End-to-end regression for AI returning 'ambiguous' despite an exact total.

No API key or real network is used. Tests are run inside the user's existing
project after installation, using actual BetParser, AIService, and evidence.
"""
import json
import unittest
from decimal import Decimal
from ai_service import AIService
from parser import BetParser
from candidate_v257 import decode_candidate_v257
from evidence_v257 import source_segments, validate_full
from receipt_inference_v257 import infer_receipt_candidates
from models import cents

RAW = '福定位\n百7\n十12\n个017   1倍12\n福0127组六40组三20合计72'


def request(raw=RAW, local_amount=12, segments=None, pending=None):
    segments = segments if segments is not None else source_segments(raw)
    pending = pending if pending is not None else [{
        'text': segments[-1]['text'],
        'segment_ids': [segments[-1]['segment_id']],
        'reason': '缺少独立明确的玩法或倍数',
    }]
    return {
        'lottery': '福彩', 'raw_text': raw, 'source_segments': segments,
        'unresolved_segments': pending,
        'local_confirmed': [{'play': '三码定位', 'selection': '百:7|十:12|个:017', 'multiplier': 1}],
        'local_confirmed_count': 1,
        'local_confirmed_amount_yuan': local_amount,
    }


class ReceiptFollowup(unittest.TestCase):
    def test_receipt_matched_builds_candidate_without_network(self):
        class NoNetwork(AIService):
            def _request(self, messages):
                raise AssertionError('准确的合计不应该再次调用AI')
        app = NoNetwork()
        candidate, _ = app.parse_unknown('福彩', json.dumps(request(), ensure_ascii=False))
        obj = json.loads(candidate)
        self.assertEqual(len(obj['candidate_bets']), 2)
        self.assertEqual([x['declared_amount_yuan'] for x in obj['candidate_bets']], [40, 20])
        self.assertEqual([x['bet_multiples'] for x in obj['candidate_bets']], [None, None])
        bets, _, err = decode_candidate_v257(candidate, BetParser(), '福彩', RAW)
        self.assertFalse(err, err)
        self.assertEqual([b.play for b in bets], ['四码组六', '四码组三'])
        self.assertEqual([b.multiplier for b in bets], [4, 2])
        self.assertEqual([cents(b.principal) for b in bets], [4000, 2000])
        local = BetParser().parse('福彩', '百7 十12 个017 1倍12')[0]
        self.assertEqual([cents(b.principal) for b in local], [1200])
        self.assertTrue(validate_full(RAW, local + bets)[0])

    def test_controller_supplies_actual_confirmed_amount(self):
        from controller import Controller
        from types import SimpleNamespace
        local = BetParser().parse('福彩', '百7 十12 个017 1倍12')[0]
        s = source_segments(RAW)[-1]
        record = SimpleNamespace(
            raw_text=RAW, lottery='福彩', bets=local,
            pending_fragments=[{'start': s['start'], 'end': s['end'],
                                'text': s['text'], 'reason': '待解释',
                                'ai_eligible': True}],
        )
        fake_controller = SimpleNamespace(
            ai_text_for_record=lambda rec, allow_existing_candidate=False: s['text'])
        outgoing = json.loads(Controller.ai_request_text(fake_controller, record))
        self.assertEqual(outgoing['local_confirmed_amount_yuan'], 12)
        self.assertEqual(outgoing['unresolved_segments'][0]['segment_ids'], ['S5'])
        self.assertIsNotNone(infer_receipt_candidates(outgoing))

    def test_uncertain_or_inconsistent_cases_go_to_ai(self):
        invalid = [
            request(local_amount=11),
            request(raw=RAW.replace('合计72', '合计73')),
            request(raw=RAW.replace('合计72', '合计720')),
            request(raw=RAW.replace('组六40', '组六40倍')),
            request(raw=RAW.replace('合计72', '')),  # no receipt
            request(raw=RAW.replace('组三20', '组三25')),
        ]
        for case in invalid:
            with self.subTest(raw=case['raw_text'], amount=case['local_confirmed_amount_yuan']):
                self.assertIsNone(infer_receipt_candidates(case))

    def test_other_ambiguity_preserved(self):
        raw = '福01268 / 01468 组六30组三10'
        context = request(raw=raw, local_amount=0)
        self.assertIsNone(infer_receipt_candidates(context))

    def test_multiple_unresolved_and_repeated_play_rejected(self):
        self.assertIsNone(infer_receipt_candidates(request(pending=[{'text': 'A', 'segment_ids': ['S5']}, {'text': 'B', 'segment_ids': ['S5']}])))
        raw = RAW.replace('组三20', '组六20')
        self.assertIsNone(infer_receipt_candidates(request(raw=raw)))

    def test_pending_not_falsely_resolved_by_partial_amount(self):
        raw = RAW.replace('合计72', '合计82')
        self.assertIsNone(infer_receipt_candidates(request(raw=raw)))

if __name__ == '__main__':
    unittest.main()
