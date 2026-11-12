"""Actual user messages: attached count/price after comma or underscore code pools."""
import unittest

from parser import BetParser


RAW_90 = (
    '000,019,028,037,046,055,064,073,091,109,118,127,136,145,154,163,181,190,'
    '208,217,226,235,244,253,271,280,299,307,316,325,334,343,361,370,389,398,'
    '406,415,424,433,451,460,479,488,497,505,514,523,541,550,569,578,587,596,'
    '604,613,631,640,659,668,677,686,695,703,721,730,749,758,767,776,785,794,'
    '811,820,839,848,857,866,875,884,893,901,910,929,938,947,956,965,974,983'
    '福90注直选1倍合计180'
)

RAW_300 = (
    '003_005_007_008_024_025_026_027_029_030_032_033_035_036_037_038_039_040_'
    '042_043_048_049_050_052_054_055_056_060_062_068_069_070_076_085_090_092_'
    '095_098_099_206_207_224_226_228_235_238_239_242_243_248_249_255_256_257_'
    '262_265_269_273_274_275_276_283_284_290_294_295_302_303_305_325_327_332_'
    '334_336_339_340_348_350_353_354_355_357_358_359_360_362_364_366_367_372_'
    '374_375_383_384_385_386_387_390_394_396_398_405_407_420_424_430_433_434_'
    '438_442_443_444_448_452_453_457_459_460_462_465_468_474_478_484_486_492_'
    '493_494_497_498_499_500_507_509_520_527_528_532_537_545_547_549_553_555_'
    '556_557_562_565_566_569_570_574_576_577_579_580_582_584_585_586_587_590_'
    '595_598_600_605_606_608_609_620_622_623_624_625_626_627_629_632_635_638_'
    '639_643_647_650_652_653_658_667_669_672_675_677_678_679_684_685_686_689_'
    '694_696_697_699_700_705_708_722_723_725_727_728_730_734_735_739_740_746_'
    '748_750_753_755_757_764_770_772_773_779_783_786_787_790_793_795_796_798_'
    '800_809_826_830_833_836_838_839_850_852_856_859_860_863_864_868_870_872_'
    '874_876_878_879_880_883_887_889_893_895_896_899_900_903_904_908_920_924_'
    '932_933_934_935_939_942_943_945_948_949_950_952_956_957_959_960_962_963_'
    '966_969_970_975_980_983_985_987_988_992_993_999'
    '福300注0.5倍'
)


class InlineCountedTests(unittest.TestCase):
    def check(self, raw, count, multiple, due):
        parser = BetParser()
        bets, _, error = parser.parse('福彩', raw)
        self.assertEqual(error, '')
        self.assertEqual(len(bets), count)
        self.assertEqual(len(set(b.selection for b in bets)), count)
        self.assertTrue(all(b.play == '直选' for b in bets))
        self.assertTrue(all(b.multiplier == multiple for b in bets))
        self.assertAlmostEqual(sum(b.principal for b in bets), due)
        for bet in bets:
            span = bet.source_span
            self.assertEqual(span['text'], bet.selection)
            self.assertEqual(raw[span['start']:span['end']], bet.selection)
        return parser, bets

    def test_90_comma_separated_joined_lottery_count_and_total(self):
        parser, bets = self.check(RAW_90, 90, 1, 180)
        self.assertEqual(parser.last_claimed, 180)
        self.assertEqual(bets[0].selection, '000')
        self.assertEqual(bets[-1].selection, '983')

    def test_300_underscore_separated_half_multiple_without_play(self):
        parser, bets = self.check(RAW_300, 300, 0.5, 300)
        self.assertIsNone(parser.last_claimed)
        self.assertEqual(bets[0].selection, '003')
        self.assertEqual(bets[-1].selection, '999')

    def test_count_mismatch_does_not_guess_or_post_bets(self):
        bets, _, error = BetParser().parse('福彩', RAW_90.replace('福90注', '福91注'))
        self.assertEqual(bets, [])
        self.assertIn('注数不一致', error)

    def test_opposite_lottery_marker_rejected(self):
        bets, _, error = BetParser().parse('福彩', RAW_90.replace('福90注', '体90注'))
        self.assertEqual(bets, [])
        self.assertIn('彩种', error)

    def test_missing_count_header_is_not_inferred(self):
        bets, _, error = BetParser().parse('福彩', '003_005_007福直选0.5倍')
        self.assertEqual(bets, [])
        self.assertTrue(error)


if __name__ == '__main__':
    unittest.main(verbosity=2)
