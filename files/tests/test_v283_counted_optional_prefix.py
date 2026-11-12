"""A count-backed direct summary may omit 共, but never omit count proof."""
from collections import Counter
import unittest

from local_blocks_v276 import parse_local_blocks
from parser import BetParser
from shared_orders_v282 import complete_shared_evidence, parse_shared_order
from tests.test_v283_counted_groups import REAL_ORDER


FIRST_POOL = REAL_ORDER.split('\n共515组8倍', 1)[0]
ORDER = (FIRST_POOL + '\n共515组3倍\n'
         '002 003 085 092 095 181 185 192 195 202 203 283 285 291 292 '
         '901 902 905 981 982 983 991 992 993 995\n'
         '共25组6倍\n\n'
         '993 995\n2组30倍\n\n'
         '995\n1组30倍')


class OptionalCountPrefixTests(unittest.TestCase):
    def check_ledger(self, bets):
        self.assertEqual(len(bets), 515 + 25 + 2 + 1)
        self.assertEqual(Counter(b.multiplier for b in bets), {3: 515, 6: 25, 30: 3})
        self.assertEqual(sum(b.principal for b in bets), 3570)
        self.assertTrue(all(b.play == '直选' for b in bets))
        self.assertEqual(sum(b.multiplier for b in bets if b.selection == '995'), 69)
        self.assertEqual(sum(b.multiplier for b in bets if b.selection == '993'), 39)

    def test_original_full_order_through_normal_entry(self):
        parser = BetParser()
        bets, _, error = parser.parse('福彩', ORDER)
        self.assertFalse(error, error)
        self.check_ledger(bets)
        self.assertIsNone(parser.last_claimed)
        self.assertFalse(parser.last_warnings)

    def test_every_group_may_omit_the_prefix_without_changing_purchases(self):
        all_bare = ORDER.replace('共515组3倍', '515组3倍').replace('共25组6倍', '25组6倍')
        bets, _, error = BetParser().parse('福彩', all_bare)
        self.assertFalse(error, error)
        self.check_ledger(bets)

    def test_both_strict_local_and_shared_evidence_engines(self):
        for label, recognizer in (
                ('local', parse_local_blocks), ('shared', parse_shared_order)):
            with self.subTest(engine=label):
                parser = BetParser()
                result = recognizer(parser, '福彩', ORDER)
                self.assertIsNotNone(result)
                bets, _, error = result
                self.assertFalse(error, error)
                self.check_ledger(bets)
                if label == 'shared':
                    self.assertTrue(complete_shared_evidence(ORDER, bets))

    def test_wrong_declared_count_still_rejected(self):
        for wrong in (ORDER.replace('2组30倍', '3组30倍'),
                      ORDER.replace('1组30倍', '2组30倍'),
                      ORDER.replace('共515组3倍', '515组4倍').replace('515组4倍', '516组4倍')):
            with self.subTest(wrong=wrong[-70:]):
                bets, _, error = BetParser().parse('福彩', wrong)
                self.assertEqual(bets, [])
                self.assertIn('组', error)

    def test_unexplained_text_is_never_silently_dropped(self):
        for malformed in (ORDER.replace('2组30倍', '2组30'),
                          ORDER.replace('2组30倍', '2组30倍备注')):
            with self.subTest(malformed=malformed[-55:]):
                bets, _, error = BetParser().parse('福彩', malformed)
                self.assertEqual(bets, [])
                self.assertTrue(error)


if __name__ == '__main__':
    unittest.main()
