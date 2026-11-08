# -*- coding: utf-8 -*-
"""Must run against the user's complete application in a temporary directory.
No network, no existing data, no UI or clipboard access.
"""
from pathlib import Path
import json
import tempfile
import unittest
from copy import deepcopy
from parser import BetParser
from controller import Controller
from data_store import DataStore
from ai_pipeline_v266 import decode_structured, reconcile
from test_v282_runtime import POOLS,DIRECT,BIG
import test_v282_runtime as cases


class RealApplicationIntegration(unittest.TestCase):
    def test_records_are_local_verified_and_count_warning_is_advisory(self):
        with tempfile.TemporaryDirectory(prefix='v282-records-') as td:
            store=DataStore(Path(td))
            try:
                ctrl=Controller(store)
                ctrl.settings.setdefault('deadline',{})['enabled']=False
                for raw,expected,count in ((POOLS,90,6),(DIRECT,90,45),(BIG,7176,553)):
                    with self.subTest(expected=expected):
                        record=ctrl.add('福彩',raw)
                        self.assertIsNotNone(record)
                        self.assertEqual(record.parse_status,'verified_local',record.note)
                        self.assertEqual(record.status,'已识别',record.note)
                        self.assertFalse(record.pending_fragments,record.note)
                        self.assertEqual(len(record.bets),count)
                        self.assertEqual(record.computed_amount_yuan,expected)
                        self.assertFalse(record.ai_candidate)
                        if expected==7176:
                            self.assertIn('35注',record.note);self.assertIn('33注',record.note)
            finally:store.close()

    def test_actual_ai_entry_checks_shared_source_scope(self):
        payload=cases.SharedAI().candidate()
        parser=BetParser()
        good,items,errors,review=decode_structured(POOLS,json.dumps(payload),parser,'福彩')
        self.assertFalse(errors);self.assertFalse(review);self.assertEqual(len(good),6)
        r=reconcile(POOLS,json.dumps(payload),parser,'福彩',[],[{'start':0,'end':len(POOLS),'text':POOLS,'reason':'旧版识别失败'}],90)
        self.assertTrue(r.complete,r.detail);self.assertEqual(len(r.bets),6);self.assertFalse(r.pending)
        payload['candidate_bets'][0]['declared_amount_yuan']=200
        good,_,errors,review=decode_structured(POOLS,json.dumps(payload),parser,'福彩')
        self.assertFalse(good);self.assertTrue(errors)

    def test_successful_local_reparse_does_not_duplicate_old_ai(self):
        with tempfile.TemporaryDirectory(prefix='v282-reparse-') as td:
            store=DataStore(Path(td))
            try:
                ctrl=Controller(store);ctrl.settings.setdefault('deadline',{})['enabled']=False
                record=ctrl.add('福彩',POOLS)
                record.ai_candidate=json.dumps(cases.SharedAI().candidate())
                ctrl.parse_record(record)
                self.assertEqual(record.parse_status,'verified_local',record.note)
                self.assertEqual(len(record.bets),6)
                self.assertEqual(record.computed_amount_yuan,90)
            finally:store.close()

if __name__=='__main__':unittest.main(verbosity=2)
