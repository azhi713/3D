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

    def test_explicit_multiple_with_customer_total(self):
        from models import MessageRecord
        with tempfile.TemporaryDirectory() as directory:
            store=DataStore(Path(directory))
            try:
                ctrl=Controller(store);ctrl.activate_period('2026-11-15')
                ctrl.settings['deadline']['enabled']=False
                ctrl.settings['recognition_outer_compat']=True
                for sep in (',','，','\n'):
                    raw='福689直选1倍'+sep+'合计5元'
                    rec=ctrl.add('福彩',raw)
                    self.assertEqual(rec.computed_amount_yuan,2)
                    self.assertEqual(rec.claimed_amount,5)
                    self.assertIsNone(rec.received_amount)
                    self.assertEqual(rec.parse_status,'verified_receipt_discrepancy')
                    self.assertEqual(rec.raw_text,raw)
                    ctrl.parse_record(rec)
                    self.assertEqual(len(rec.bets),1)
                    self.assertEqual(rec.bets[0].multiplier,1)
            finally:store.close()

    def test_explicit_inline_list_keeps_duplicates_and_lottery(self):
        for raw,codes,multiple in [('福101 102 103直3倍',['101','102','103'],3),
                                   ('福001 001 005直选1倍',['001','001','005'],1)]:
            bets,_,_,_=recover(BetParser(),'福彩',raw)
            self.assertEqual([b.selection for b in bets],codes)
            self.assertEqual([b.multiplier for b in bets],[multiple]*3)
            self.assertIsNone(recover(BetParser(),'体彩',raw))
