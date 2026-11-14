import tempfile, unittest
from pathlib import Path
from controller import Controller
from data_store import DataStore
from recognition_outer_compat import recover
from parser import BetParser

class OuterCompatibility(unittest.TestCase):
    def test_positive_original_offsets_and_duplicates(self):
        raw='005、005、012\n共3注直选 20 倍'
        for lottery in ('福彩','体彩'):
            bets,_,_,_ = recover(BetParser(),lottery,raw)
            self.assertEqual([b.selection for b in bets],['005','005','012'])
            self.assertEqual(sum(b.principal for b in bets),120)
            self.assertEqual([b.source_span['start'] for b in bets],[0,4,8])
    def test_reject_incomplete_or_ambiguous_input(self):
        for raw in ('005、012\n共3注直选20倍','005、12\n共2注直选20倍',
                    '005、012\n共2注直选20倍合计5元','005、012\n共2注直选0倍',
                    '005、012\n共2注直选20倍\n撤单'):
            self.assertIsNone(recover(BetParser(),'福彩',raw))
    def test_switch_and_reparse_do_not_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            store=DataStore(Path(directory))
            try:
                ctrl=Controller(store);ctrl.activate_period('2026-11-15')
                ctrl.settings['deadline']['enabled']=False
                raw='157、975、381、338、875\n共5注直选 20 倍'
                before=ctrl.add('福彩',raw)
                self.assertFalse(before.bets)
                ctrl.settings['recognition_outer_compat']=True
                rec=ctrl.add('福彩',raw)
                self.assertEqual(len(rec.bets),5)
                ctrl.parse_record(rec);ctrl.parse_record(rec)
                self.assertEqual(len(rec.bets),5)
                self.assertEqual(sum(b.principal for b in rec.bets),200)
                self.assertEqual(rec.raw_text,raw)
                self.assertEqual(rec.source_original_text,raw)
            finally:store.close()
