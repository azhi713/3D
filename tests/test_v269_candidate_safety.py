# -*- coding: utf-8 -*-
"""Conservative AI candidate and money evidence regression tests."""
import json
import unittest
from ai_pipeline_v266 import decode_structured, reconcile
from parser import BetParser


class CandidateSafety(unittest.TestCase):
    def setUp(self):
        self.parser=BetParser()

    @staticmethod
    def payload(items):
        return json.dumps({'candidate_bets':items,'needs_review':False,
                           'uncertainties':[]},ensure_ascii=False)

    def item(self, code, seg, mult=1, money=None):
        row={'lottery':'福彩','segment_id':seg,'bet_type':'直选',
             'ticket_codes':[code], 'bet_multiples':mult,
             'amount_kind':'multiplier','multiple_type':'explicit'}
        if money is not None:row['declared_amount_yuan']=money
        return row

    def test_explicit_multiplier_and_inconsistent_amount_rejected(self):
        text='福689直选1倍，合计5元'
        candidate=self.payload([self.item('689','S1',1,5)])
        bets,items,errors,pending=decode_structured(text,candidate,self.parser,'福彩')
        self.assertEqual(bets,[])
        self.assertTrue(any('金额' in x and '不一致' in x for x in errors),errors)

    def test_explicit_multiplier_and_matching_amount_accepted(self):
        text='福689直选1倍，合计2元'
        candidate=self.payload([self.item('689','S1',1,2)])
        bets,items,errors,pending=decode_structured(text,candidate,self.parser,'福彩')
        self.assertEqual(len(bets),1,errors)
        self.assertEqual(bets[0].selection,'689')

    def test_bad_row_does_not_erase_safe_other_span(self):
        text='福689直选1倍\n101 102 103直4注8元'
        rows=[self.item('689','S1',1),
              {'lottery':'福彩','segment_id':'S2','bet_type':'直选',
               'ticket_codes':['101','102','103'],'bet_multiples':None,
               'declared_amount_yuan':8,'amount_kind':'amount',
               'multiple_type':'inferred'}]
        result=reconcile(text,self.payload(rows),self.parser,'福彩',[],
                         [{'start':0,'end':len(text),'text':text,'reason':'待复核'}])
        self.assertFalse(result.complete)
        self.assertTrue(result.pending)
        self.assertTrue(any(b.selection=='689' for b in result.bets))
        self.assertFalse(any(b.selection in ('101','102','103') for b in result.bets))

    def test_amount_does_not_fabricate_multiplier(self):
        text='福689直选1倍 合计5元'
        candidate=self.payload([self.item('689','S1',1,5)])
        result=reconcile(text,candidate,self.parser,'福彩',[],
                         [{'start':0,'end':len(text),'text':text,'reason':'金额不符'}])
        self.assertFalse(result.complete)
        self.assertEqual(result.bets,[])


if __name__=='__main__':unittest.main()
