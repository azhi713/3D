"""Spaced position pools and repeated selections preserve real purchase counts."""
import unittest

from parser import BetParser
from risk_engine import RiskEngine


SPACED = ('百位  2 3 5 7 2\n'
          '十位  1 3 5 6 3 5\n'
          '个位  4 5 8 9 5\n'
          '福直选定位1倍')
SPACED_TOTAL = ('百位  2 3 5 7\n'
                '十位  1 3 5 6\n'
                '个位  4 5 8 9\n'
                '福直选定位1倍🈴128米')
COMPACT_HALF = ('百位3579\n十位0246\n个位1357\n'
                '福直选定位半倍🈴64')


class SpacedPositionsTests(unittest.TestCase):
    def test_spaced_pools_count_repeated_tickets(self):
        parser=BetParser()
        bets, _, error=parser.parse('福彩', SPACED)
        self.assertFalse(error, error)
        self.assertEqual(len(bets),1)
        self.assertEqual((bets[0].play,bets[0].selection,bets[0].multiplier,
                          bets[0].principal),
                         ('三码定位','百:23572|十:135635|个:45895',1,300))
        risk=RiskEngine(parser.rules)
        self.assertEqual(risk.payout_for('福彩',bets[0],'235'),8*1800)
        self.assertEqual(risk.payout_for('福彩',bets[0],'555'),4*1600)
        revenue,_,payouts=risk.calculate('福彩',bets,0,1800)
        self.assertEqual(revenue,300)
        self.assertEqual(payouts['235'],8*1800)
        self.assertEqual(payouts['555'],4*1600)

    def test_spaced_and_compact_with_receipts(self):
        for source,expected_cost,expected_multiple in ((SPACED_TOTAL,128,1),
                                                       (COMPACT_HALF,64,.5)):
            with self.subTest(source=source):
                parser=BetParser()
                bets,_,error=parser.parse('福彩',source)
                self.assertFalse(error,error)
                self.assertEqual(len(bets),1)
                self.assertEqual(bets[0].principal,expected_cost)
                self.assertEqual(bets[0].multiplier,expected_multiple)
                self.assertEqual(parser.last_claimed,expected_cost)

    def test_bad_receipt_and_missing_position_do_not_post(self):
        for source in (SPACED_TOTAL.replace('128米','129米'),
                       COMPACT_HALF.replace('个位1357\n',''),
                       SPACED_TOTAL.replace('福直选','体直选')):
            with self.subTest(source=source):
                bets,_,error=BetParser().parse('福彩',source)
                self.assertEqual(bets,[])
                self.assertTrue(error)


if __name__=='__main__':
    unittest.main()
