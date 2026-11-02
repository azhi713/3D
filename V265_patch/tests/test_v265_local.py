# -*- coding: utf-8 -*-
"""Run against the real project parser + user-configured base pricing in stage."""
import unittest
from parser import BetParser
from recognition_v265 import parse_v265
from models import cents


class LocalRecognitionTests(unittest.TestCase):
    def setUp(self):self.p=BetParser()

    def test_515_codes_plus_explicit_extra_bet(self):
        # Generated codes (not a user's real wagers). 338 exists in both sets.
        codes=['%03d'%i for i in range(515)]
        source=' '.join(codes)+'\n共515注1倍\n\n338直选5倍\n\n合1040米'
        bets,_,error=self.p.parse('福彩',source)
        self.assertFalse(error)
        self.assertEqual(len(bets),516)
        self.assertEqual([x.multiplier for x in bets if x.selection=='338'],[1,5])
        self.assertEqual(sum(cents(x.principal) for x in bets),104000)
        self.assertEqual(self.p.last_claimed,1040)

    def test_number_count_mismatch_does_not_auto_book(self):
        bets,_,error=self.p.parse('福彩','000 001 002\n共4注1倍\n合8米')
        self.assertFalse(bets)
        self.assertIn('注数',error)

    def test_provisional_total_conflict_not_silently_repaired(self):
        bets,_,error=self.p.parse('福彩','000 001 002\n共3注1倍\n合4米')
        # Parser can parse codes, but controller must reject claim != total.
        self.assertEqual(len(bets),3)
        self.assertEqual(sum(cents(x.principal) for x in bets),600)
        self.assertEqual(self.p.last_claimed,4)

    def test_two_position_blocks_both_verified(self):
        source='福定位\n百17\n十56\n个56\n打两倍共32\n\n百17\n十56\n个6\n打两倍16合计48'
        bets,_,error=self.p.parse('福彩',source)
        self.assertFalse(error)
        self.assertEqual(len(bets),2)
        self.assertEqual([cents(x.principal) for x in bets],[3200,1600])
        self.assertEqual(self.p.last_claimed,48)

    def test_short_chinese_multipliers(self):
        for original,total,count in [('815十单十组福40',40,2),('福206两单一组，356一组 8',8,3)]:
            with self.subTest(original=original):
                p=BetParser()
                bets,_,error=p.parse('福彩',original)
                self.assertFalse(error)
                self.assertEqual(len(bets),count)
                self.assertEqual(sum(cents(b.principal) for b in bets),cents(total))
                self.assertEqual(p.last_claimed,total)

    def test_uncertain_short_not_auto_booked(self):
        self.assertIsNone(parse_v265(self.p,'福彩','福17飞10'))
        self.assertIsNone(parse_v265(self.p,'福彩','815十单十组福30'))
        self.assertIsNone(parse_v265(self.p,'福彩','福206两单一组，356一组 5'))


if __name__=='__main__':unittest.main()
