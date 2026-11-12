"""2026-11-09 user's private photo: supported plays only, future periods only."""
from copy import deepcopy
import unittest

from config import RULES_BY_LOTTERY, LOTTERIES
from rule_catalog import PHOTO_CHANGES, SUM_PRIZES, migrate_photo_defaults_for_new_period
from models import Bet
from risk_engine import RiskEngine


PHOTO_EXISTING = {
    '四码组六': (10, 360),
    '七码复试': (10, 25),
    '跨度0': (10, 800),
    '跨度1': (10, 165),
    '跨度2': (10, 92),
    '跨度3': (10, 72),
    '跨度4': (10, 62),
    '跨度5': (10, 60),
    '跨度6': (10, 63),
    '跨度7': (10, 72),
    '跨度8': (10, 93),
    '跨度9': (10, 170),
    '和值0': (10, 7500),
    '和值27': (10, 7500),
}


class PhotoOddsTests(unittest.TestCase):
    def test_photo_values_for_both_lotteries(self):
        for lottery in LOTTERIES:
            for play, (principal, prize) in PHOTO_EXISTING.items():
                with self.subTest(lottery=lottery, play=play):
                    self.assertEqual(RULES_BY_LOTTERY[lottery][play]['principal'], principal)
                    self.assertEqual(RULES_BY_LOTTERY[lottery][play]['prize'], prize)
        self.assertEqual((SUM_PRIZES[0], SUM_PRIZES[27]), (7500, 7500))

    def test_old_saved_default_settings_rebase_only_known_values(self):
        rules = deepcopy(RULES_BY_LOTTERY)
        for lottery in LOTTERIES:
            for play, (principal, old, new) in PHOTO_CHANGES.items():
                rules[lottery][play] = {'principal': principal, 'prize': old}
        rules['体彩']['跨度9']['prize'] = 999  # Custom value must survive.
        archived = deepcopy(rules)  # Same shape as an immutable saved-period snapshot.
        migrate_photo_defaults_for_new_period(rules)
        for lottery in LOTTERIES:
            for play, (_, old, new) in PHOTO_CHANGES.items():
                actual = rules[lottery][play]['prize']
                self.assertEqual(actual, 999 if lottery == '体彩' and play == '跨度9' else new)
        self.assertEqual(archived['福彩']['跨度1']['prize'], 157)
        self.assertEqual(archived['体彩']['跨度9']['prize'], 999)

    def test_wrong_principal_is_not_silently_overwritten(self):
        rules = {'福彩': {'四码组六': {'principal': 20, 'prize': 370}}}
        migrate_photo_defaults_for_new_period(rules)
        self.assertEqual(rules['福彩']['四码组六'], {'principal': 20, 'prize': 370})

    def test_photo_prizes_are_used_by_existing_winning_engine(self):
        engine = RiskEngine()
        samples = [
            ('四码组六', '1234', '123', 360),
            ('七码复试', '0123456', '012', 25),
            ('跨度9', '9', '909', 170),
            ('和值0', '0', '000', 7500),
            ('和值27', '27', '999', 7500),
        ]
        for play, choice, draw, prize in samples:
            with self.subTest(play=play):
                bet = Bet(play, choice, 1, 10)
                self.assertEqual(engine.payout_for('福彩', bet, draw), prize)


if __name__ == '__main__':
    unittest.main(verbosity=2)
