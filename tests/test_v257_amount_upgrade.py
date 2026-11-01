"""V2.5.7 structured AI amount/multiple regression cases."""
import json
import unittest
from parser import BetParser
from models import cents
from candidate_v257 import decode_candidate_v257
from evidence_v257 import validate_full


def proposal(raw,items):
    return json.dumps({'candidate_bets':items,'needs_review':False,'uncertainties':[]},ensure_ascii=False)


class V257AmountUpgrade(unittest.TestCase):
    def decode(self,raw,items):
        bets,_,err=decode_candidate_v257(proposal(raw,items),BetParser(),'福彩',raw)
        self.assertFalse(err,err)
        return bets

    def test_two_explicit_hundred_yuan_pools(self):
        raw='0357组六100米组三100米福合计200'
        items=[{'segment_id':'S1','bet_type':p,'digit_set':'0357',
                'declared_amount_yuan':100,'bet_multiples':None,'amount_kind':'amount'}
               for p in ('GROUP6_PERM','GROUP3_PERM')]
        bets=self.decode(raw,items)
        self.assertEqual([b.play for b in bets],['四码组六','四码组三'])
        self.assertEqual([cents(b.principal) for b in bets],[10000,10000])
        self.assertEqual([b.multiplier for b in bets],[10,10])
        self.assertTrue(validate_full(raw,bets)[0])

    def test_partial_local_position_and_two_bare_amounts(self):
        raw='福定位\n百7\n十12\n个017   1倍12\n福0127组六40组三20合计72'
        items=[{'segment_id':'S5','bet_type':p,'digit_set':'0127',
                'declared_amount_yuan':amount,'bet_multiples':None,'amount_kind':'amount',
                'amount_type':'inferred_from_total'}
               for p,amount in [('四码组六',40),('四码组三',20)]]
        bets=self.decode(raw,items)
        self.assertEqual([b.multiplier for b in bets],[4,2])
        self.assertEqual([cents(b.principal) for b in bets],[4000,2000])
        local=BetParser().parse('福彩','百7 十12 个017 1倍12')[0]
        self.assertEqual(len(local),1)
        self.assertEqual(cents(local[0].principal),1200)
        self.assertTrue(validate_full(raw,local+bets)[0])

    def test_bare_amount_without_receipt_must_not_auto_accept(self):
        raw='0127组六40组三20'
        items=[{'segment_id':'S1','bet_type':p,'digit_set':'0127',
                'declared_amount_yuan':n,'bet_multiples':None,'amount_kind':'amount'}
               for p,n in [('四码组六',40),('四码组三',20)]]
        _,_,error=decode_candidate_v257(proposal(raw,items),BetParser(),'福彩',raw)
        self.assertTrue(error)

    def test_invented_mult_not_backfilled_from_money(self):
        raw='0357组六100米'
        items=[{'segment_id':'S1','bet_type':'四码组六','digit_set':'0357',
                'declared_amount_yuan':100,'bet_multiples':100,'amount_kind':'multiplier'}]
        _,_,error=decode_candidate_v257(proposal(raw,items),BetParser(),'福彩',raw)
        self.assertTrue(error)

if __name__=='__main__':unittest.main()
