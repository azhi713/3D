"""Single-position 定码 with an explicit total uses the installed 一码定位 cost."""
from decimal import Decimal
import unittest

from parser import BetParser


class FixedDigitCashTests(unittest.TestCase):
    def test_original_tens_position_cash_is_twenty_multiples(self):
        source = '十位定码5  🈴200米'
        parser = BetParser()
        bets, _, error = parser.parse('福彩', source)
        self.assertFalse(error, error)
        self.assertEqual(len(bets), 1)
        self.assertEqual((bets[0].play, bets[0].selection, bets[0].multiplier,
                          bets[0].principal), ('一码定位', '十:5', 20, 200))
        self.assertEqual(parser.last_claimed, 200)
        self.assertEqual(bets[0].source_span['text'], source)

    def test_each_position_and_compatible_color_marker(self):
        samples = [
            ('福彩', '百位定码0 合10元', '百:0', Decimal('1')),
            ('福彩', '个位定码9 合15米', '个:9', Decimal('1.5')),
            ('体彩', '体十位定码5 🈴200米', '十:5', Decimal('20')),
            ('福彩', '福十位 定码 5 🈴200米', '十:5', Decimal('20')),
        ]
        for lottery, text, selection, multiple in samples:
            with self.subTest(text=text):
                parser = BetParser()
                bets, _, error = parser.parse(lottery, text)
                self.assertFalse(error, error)
                self.assertEqual(len(bets), 1)
                self.assertEqual((bets[0].play, bets[0].selection), ('一码定位', selection))
                self.assertEqual(Decimal(str(bets[0].multiplier)), multiple)

    def test_wrong_color_or_missing_receipt_cannot_be_inferred(self):
        for source in ('体十位定码5 合200米', '十位定码5',
                       '十位定码55 合200米', '十位定码5 合0米',
                       '十位定码5 合200米备注'):
            with self.subTest(source=source):
                bets, _, error = BetParser().parse('福彩', source)
                self.assertEqual(bets, [])
                self.assertTrue(error)


if __name__ == '__main__':
    unittest.main()
