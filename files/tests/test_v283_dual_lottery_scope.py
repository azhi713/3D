"""Shared terminal 直选各N倍 must reach both explicitly named lotteries."""
from copy import deepcopy
from decimal import Decimal
import unittest

from parser import BetParser, split_lottery_message
from routing_v260 import split_shared_total
from context_receipts_v271 import parse_shared_context


RAW = ('福637  387   765   658\n'
       '体314   318   194   328\n'
       '直选各2倍🈴32')


class DualLotterySharedScopeTests(unittest.TestCase):
    def parse_parts(self, raw, default='福彩', rules=None):
        routed = split_lottery_message(raw, default)
        result = {}
        for lot, text in routed.items():
            parser = BetParser(rules)
            bets, _, error = parser.parse(lot, text)
            self.assertFalse(error, (lot, error))
            result[lot] = bets
        return routed, result

    def test_original_cross_colour_shared_multiplier_and_receipt(self):
        for default in ('福彩', '体彩'):
            with self.subTest(default=default):
                sections, results = self.parse_parts(RAW, default)
                self.assertEqual(set(sections), {'福彩', '体彩'})
                self.assertIn('直选各2倍', sections['福彩'])
                self.assertIn('直选各2倍', sections['体彩'])
                self.assertEqual([b.selection for b in results['福彩']],
                                 ['637', '387', '765', '658'])
                self.assertEqual([b.selection for b in results['体彩']],
                                 ['314', '318', '194', '328'])
                self.assertTrue(all(b.play == '直选' and b.multiplier == 2
                                    for bets in results.values() for b in bets))
                self.assertEqual({lot:sum(Decimal(str(b.principal)) for b in bets)
                                  for lot,bets in results.items()},
                                 {'福彩':Decimal(16), '体彩':Decimal(16)})
                self.assertEqual(split_shared_total(RAW,default)[1],Decimal(32))

    def test_wrong_global_receipt_does_not_change_explicit_multipliers(self):
        raw = RAW.replace('🈴32', '🈴31')
        _, results = self.parse_parts(raw)
        stated = split_shared_total(raw, '福彩')[1]
        calculated = sum((Decimal(str(b.principal)) for rows in results.values()
                          for b in rows), Decimal(0))
        self.assertEqual(calculated,Decimal(32))
        self.assertEqual(stated,Decimal(31))
        self.assertNotEqual(stated,calculated)  # Controller must retain amount_mismatch.

    def test_only_exact_two_pool_scope_is_shared(self):
        for body in (
            '福637 387\n体314 318\n直选1倍\n直选各2倍合16',
            '福637 387\n体314 318\n说明\n直选各2倍合16',
            '福637 387\n体314 318\n组选各2倍合16',
            '福637 387\n福314 318\n直选各2倍合16',
        ):
            with self.subTest(body=body):
                parsed=split_shared_total(body,'福彩')
                self.assertTrue(parsed is None or
                                not all('直选各2倍' in value for value in parsed[0].values()))

    def test_explicit_prices_are_taken_from_current_rules(self):
        rules=deepcopy(BetParser().rules)
        rules['福彩']['直选']['principal']=3
        rules['体彩']['直选']['principal']=4
        _,results=self.parse_parts(RAW,rules=rules)
        self.assertEqual(sum(Decimal(str(b.principal)) for b in results['福彩']),Decimal(24))
        self.assertEqual(sum(Decimal(str(b.principal)) for b in results['体彩']),Decimal(32))

    def test_saved_old_routing_can_reparse_from_unchanged_full_source(self):
        # Both historical fragments were committed before the routing fix.
        old_parts = {
            '福彩': '福637  387   765   658',
            '体彩': '体314   318   194   328\n直选各2倍',
        }
        for lottery, old_text in old_parts.items():
            with self.subTest(lottery=lottery):
                parser = BetParser()
                restored = parse_shared_context(parser, lottery, old_text, RAW)
                self.assertIsNotNone(restored)
                bets, normalized, note = restored
                self.assertEqual(len(bets), 4)
                self.assertEqual([b.multiplier for b in bets], [2] * 4)
                self.assertEqual(sum(Decimal(str(b.principal)) for b in bets), Decimal(16))
                self.assertEqual(parser.last_claimed, None)
                self.assertTrue(all(b.source_span['reference'] == 'source_original_text'
                                    and b.source_span['text'] == RAW for b in bets))
                self.assertIn('直选各2倍', normalized)
                self.assertIn('原文', note)

    def test_old_record_recovery_rejects_manual_changes_and_wrong_total(self):
        old = '福637  387   765   658'
        self.assertIsNone(parse_shared_context(BetParser(), '福彩',
                                               old.replace('637', '638'), RAW))
        self.assertIsNone(parse_shared_context(BetParser(), '福彩', old,
                                               RAW.replace('🈴32', '🈴31')))
        self.assertIsNone(parse_shared_context(BetParser(), '福彩', old,
                                               RAW.replace('直选各2倍', '组选各2倍')))


if __name__=='__main__':
    unittest.main()
