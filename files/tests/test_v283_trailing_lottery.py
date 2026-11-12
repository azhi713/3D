"""End-of-block lottery labels must preserve independently priced pools."""
from decimal import Decimal
import unittest

from parser import BetParser, split_lottery_message


FOUR_DIGIT_POOLS = '1358   0358 \n福组六各10🈴20米'
POSITION_POOLS = '百位1358\n十位2468\n个位1357\n福定位半倍🈴64米'


class TrailingLotteryOrders(unittest.TestCase):
    def parse(self, raw, lottery='福彩'):
        parser = BetParser()
        bets, _, error = parser.parse(lottery, raw)
        return parser, bets, error

    def test_two_distinct_four_digit_group_pools_are_not_concatenated(self):
        for default in ('福彩', '体彩'):
            with self.subTest(default=default):
                sections = split_lottery_message(FOUR_DIGIT_POOLS, default)
                self.assertEqual(set(sections), {'福彩'})
                self.assertIn('1358 0358', sections['福彩'])
                parser, bets, error = self.parse(sections['福彩'])
                self.assertFalse(error, error)
                self.assertEqual([(b.play, b.selection, b.multiplier) for b in bets],
                                 [('四码组六', '1358', 1), ('四码组六', '0358', 1)])
                self.assertEqual(sum(Decimal(str(b.principal)) for b in bets), Decimal('20'))
                self.assertEqual(parser.last_claimed, 20)
                self.assertFalse(parser.last_warnings)

    def test_trailing_half_multiplier_creates_one_three_position_bet(self):
        for default in ('福彩', '体彩'):
            with self.subTest(default=default):
                sections = split_lottery_message(POSITION_POOLS, default)
                self.assertEqual(set(sections), {'福彩'})
                parser, bets, error = self.parse(sections['福彩'])
                self.assertFalse(error, error)
                self.assertEqual(len(bets), 1)
                bet = bets[0]
                self.assertEqual((bet.play, bet.selection, bet.multiplier),
                                 ('三码定位', '百:1358|十:2468|个:1357', .5))
                self.assertEqual(Decimal(str(bet.principal)), Decimal('64'))
                self.assertEqual(parser.last_claimed, 64)
                self.assertFalse(parser.last_warnings)
                self.assertEqual(bet.source_span['text'], sections['福彩'])

    def test_position_template_also_parses_without_pre_routing(self):
        parser, bets, error = self.parse(POSITION_POOLS)
        self.assertFalse(error, error)
        self.assertEqual(len(bets), 1)
        self.assertEqual(parser.last_claimed, 64)

    def test_wrong_position_total_is_not_booked(self):
        for reported in ('63', '65'):
            with self.subTest(reported=reported):
                raw = POSITION_POOLS.replace('🈴64米', '🈴'+reported+'米')
                parser, bets, error = self.parse(split_lottery_message(raw, '福彩')['福彩'])
                self.assertEqual(bets, [])
                self.assertIn('定位合计金额不一致', error)
                self.assertEqual(parser.last_claimed, int(reported))

    def test_unexplained_tail_is_not_silently_ignored(self):
        parser, bets, error = self.parse(POSITION_POOLS + '\n说明文字')
        self.assertFalse(bets)
        self.assertTrue(error)

    def test_sports_lottery_trailing_position_directive(self):
        raw = POSITION_POOLS.replace('福定位', '体定位')
        sections = split_lottery_message(raw, '福彩')
        self.assertEqual(set(sections), {'体彩'})
        _, bets, error = self.parse(sections['体彩'], '体彩')
        self.assertFalse(error, error)
        self.assertEqual((bets[0].play, bets[0].multiplier), ('三码定位', .5))


if __name__ == '__main__':
    unittest.main()
