# -*- coding: utf-8 -*-
"""V2.6.8 regressions: real user examples; no network or DB writes."""
import json
import unittest
from decimal import Decimal
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from ai_pipeline_v266 import reconcile
from test_ai_pipeline_v266 import run,c
from models import Bet,cents
from amount_audit_v264 import reconcile_terminal_shared_total
from risk_engine import plan_text


class UserCases(unittest.TestCase):
    def test_grouped_plan_has_requested_labels_total(self):
        rows=[SimpleNamespace(number=number,plan_multiplier=multiple) for number,multiple in
             [('369',5),('766',5),('156',4),('166',4),('756',4),('689',4)]]
        for lottery,prefix in [('福彩','福'),('体彩','体')]:
            output=plan_text(rows,2,lottery)
            self.assertIn(prefix+'直选5倍\n369  766',output)
            self.assertIn(prefix+'直选4倍\n156  166  756  689',output)
            self.assertIn('\n\n合计：52元',output)
            self.assertNotIn('369直选5倍  ',output)

    def test_grouped_plan_empty_uses_existing_message(self):
        self.assertEqual(plan_text([],2),'暂无需要补单的号码')

    def test_total_scope_reclassifies_716_when_entire_receipt_matches(self):
        source='福716-176一米直一米组，039-278组选，共8米'
        bets=[Bet('直选','716',.5,1,source,source_span={'start':0,'end':17}),
              Bet('组六（组选）','716',.5,1,source,source_span={'start':0,'end':17}),
              Bet('直选','176',.5,1,source,source_span={'start':0,'end':17}),
              Bet('组六（组选）','176',.5,1,source,source_span={'start':0,'end':17}),
              Bet('组六（组选）','039',1,2,source,source_span={'start':18,'end':len(source)}),
              Bet('组六（组选）','278',1,2,source,source_span={'start':18,'end':len(source)})]
        receipts=[{'declared':8,'computed':4,'text':'039-278组选,共8米'}]
        claimed,remain,changed=reconcile_terminal_shared_total(source,bets,8,receipts)
        self.assertTrue(changed)
        self.assertEqual(claimed,8)
        self.assertEqual(remain,[])
        claimed2,remain2,changed2=reconcile_terminal_shared_total(source,bets,7,receipts)
        self.assertFalse(changed2)
        self.assertEqual(remain2,receipts)

    def test_mismatching_grand_total_does_not_launder_bad_receipt(self):
        source='福716-176一米直一米组，039-278组选，共30米'
        bets=[Bet('直选','716',.5,1,source,source_span={'start':0,'end':17}),
              Bet('组六（组选）','039',1,2,source,source_span={'start':18,'end':len(source)})]
        receipts=[{'declared':30,'computed':2,'text':'039-278组选,共30米'}]
        declared,kept,changed=reconcile_terminal_shared_total(source,bets,30,receipts)
        self.assertFalse(changed)
        self.assertEqual(kept,receipts)

    def test_single_group_count_is_not_reclassified(self):
        source='福039-278组选，共4米'
        bets=[Bet('组六（组选）','039',1,2,source,source_span={'start':0,'end':len(source)}),
              Bet('组六（组选）','278',1,2,source,source_span={'start':0,'end':len(source)})]
        receipts=[{'declared':4,'computed':4,'text':source}]
        declared,kept,changed=reconcile_terminal_shared_total(source,bets,None,receipts)
        self.assertFalse(changed)

    def test_four_bets_declared_three_numbers_ai_must_remain_pending(self):
        source='福689直选1倍\n101 102 103直4注8元'
        items=[c('直选',['689'],segment_id='S1',bet_multiples=1),
               c('直选',['101','102','103'],segment_id='S2',bet_multiples=None,
                 declared_amount_yuan=8,amount_kind='amount',multiple_type='inferred')]
        out=run(source,items)
        self.assertFalse(out.complete)
        self.assertEqual([(x.play,x.selection) for x in out.bets],[('直选','689')])
        self.assertIn('声明4注',out.detail)
        self.assertTrue(out.pending)

    def test_ai_three_single_rows_must_not_bypass_count_conflict(self):
        source='福689直选1倍\n101 102 103直4注8元'
        items=[c('直选',['689'],segment_id='S1',bet_multiples=1)]
        for code in ['101','102','103']:
            items.append(c('直选',[code],segment_id='S2',bet_multiples=1))
        out=run(source,items)
        self.assertEqual([x.selection for x in out.bets],['689'])
        self.assertTrue(out.pending)

    def test_real_duplicate_purchase_remains_two(self):
        source='815直选2倍\n815直选2倍'
        items=[c('直选',['815'],segment_id='S1',bet_multiples=2),
               c('直选',['815'],segment_id='S2',bet_multiples=2)]
        out=run(source,items)
        self.assertEqual(len(out.bets),2)

class ExactShorthandOrders(unittest.TestCase):
    class FakeParser:
        @staticmethod
        def normalize_text(text):return text
        @staticmethod
        def _multiplier(value):
            mult=Decimal(str(value))
            if mult<=0 or mult.as_tuple().exponent < -2:
                raise ValueError('倍率格式错误')
            return float(mult)
        @staticmethod
        def _from_kind(lot,raw,selection,play,mult):
            if play == '直选' and len(selection)==3:target='直选';unit=2
            elif play == '组选' and len(selection)==3 and len(set(selection))>1:
                target='组三（组选）' if len(set(selection))==2 else '组六（组选）';unit=2
            elif play=='组六' and len(selection)==4:target='四码组六';unit=10
            elif play=='双飞' and len(selection)==2:target='双飞';unit=10
            else:raise ValueError('不支持的测试玩法')
            return [Bet(target,selection,float(mult),float(Decimal(unit)*Decimal(str(mult))),raw)]

    def test_640_full_receipt(self):
        from recognition_amount_v264 import parse_amount_v264
        text='640-660-600-606福1直1组打1米 8'
        bets,_,err=parse_amount_v264(self.FakeParser(),'福彩',text)
        self.assertFalse(err)
        self.assertEqual(len(bets),8)
        self.assertEqual(sum(cents(b.principal) for b in bets),800)
        self.assertEqual(sorted(b.principal for b in bets),[1]*8)

    def test_640_mismatched_receipt_not_auto_accepted(self):
        from recognition_amount_v264 import parse_amount_v264
        self.assertIsNone(parse_amount_v264(self.FakeParser(),'福彩',
                            '640-660-600-606福1直1组打1米 9'))

    def test_369_fly_with_total_80_exact(self):
        from recognition_amount_v264 import parse_amount_v264
        text='福369四单一组1369组六30。39 79 双飞20合计80'
        bets,_,err=parse_amount_v264(self.FakeParser(),'福彩',text)
        self.assertFalse(err)
        self.assertEqual(len(bets),5)
        self.assertEqual(sum(cents(b.principal) for b in bets),8000)
        self.assertEqual([b.principal for b in bets],[8,2,30,20,20])

    def test_shorthand_never_crosses_lottery(self):
        from recognition_amount_v264 import parse_amount_v264
        self.assertIsNone(parse_amount_v264(self.FakeParser(),'体彩',
                            '640-660-600-606福1直1组打1米 8'))
        self.assertIsNone(parse_amount_v264(self.FakeParser(),'体彩',
                            '福369四单一组1369组六30。39 79 双飞20合计80'))

    def test_fly_without_context_or_wrong_total_not_accepted(self):
        from recognition_amount_v264 import parse_amount_v264
        self.assertIsNone(parse_amount_v264(self.FakeParser(),'福彩','福17飞10'))
        self.assertIsNone(parse_amount_v264(self.FakeParser(),'福彩',
                          '福369四单一组1369组六30。39 79 双飞20合计81'))

class RealParserRegression(unittest.TestCase):
    def test_parser_proves_716_shared_total_when_project_present(self):
        try:from parser import BetParser
        except ImportError:self.skipTest('real project parser unavailable offline')
        parser=BetParser()
        source='福716-176-649-978-290-668-772-929-784-815-826-866-894一米直一米组，039-278组选，共30米'
        bets,_,_=parser.parse('福彩',source)
        self.assertEqual(len(bets),28)
        self.assertEqual(sum(cents(b.principal) for b in bets),3000)
        claimed,receipts,changed=reconcile_terminal_shared_total(
            source,bets,parser.last_claimed,parser.last_receipts)
        self.assertTrue(changed,'最后共30米必须被重分类为整单总额')
        self.assertEqual(claimed,30)
        self.assertFalse(receipts)

    def test_640_and_369_real_parser_when_project_present(self):
        try:from parser import BetParser
        except ImportError:self.skipTest('real project parser unavailable offline')
        for source,n,amount in [
            ('640-660-600-606福1直1组打1米 8',8,800),
            ('福369四单一组1369组六30。39 79 双飞20合计80',5,8000)]:
            parser=BetParser()
            bets,_,reason=parser.parse('福彩',source)
            self.assertEqual(len(bets),n,reason)
            self.assertEqual(sum(cents(b.principal) for b in bets),amount)
            self.assertEqual(cents(parser.last_claimed),amount)

class SourceBehavior(unittest.TestCase):
    def test_local_recheck_never_calls_ai(self):
        from patch_logic_v268 import LOCAL_RECHECK_METHOD
        self.assertIn('self.controller.parse_record(current)',LOCAL_RECHECK_METHOD)
        self.assertNotIn('self.queue_ai(',LOCAL_RECHECK_METHOD)

    def test_copy_draft_safeguard_patched(self):
        from patch_logic_v268 import UI_COPY_HANDLER,UI_REFRESH_AFTER
        self.assertIn("self.plan.get('1.0', 'end-1c')",UI_COPY_HANDLER)
        self.assertIn("'[仅供人工核对 / 不可作为正式补单依据]'",UI_COPY_HANDLER)
        self.assertIn('不可作为正式补单依据',UI_REFRESH_AFTER)

    def test_diagnostics_export_excludes_settings_api_key(self):
        from patch_logic_v268 import DIAGNOSTIC_METHOD
        self.assertIn('ai_history',DIAGNOSTIC_METHOD)
        self.assertIn('record_revisions',DIAGNOSTIC_METHOD)
        self.assertNotIn("self.controller.settings",DIAGNOSTIC_METHOD)
        self.assertNotIn('api_key',DIAGNOSTIC_METHOD.lower())

    def test_unchanged_review_keeps_candidate_and_does_not_reparse(self):
        from patch_logic_v268 import CONTROLLER_EDIT_AFTER
        first=CONTROLLER_EDIT_AFTER.split('        record.raw_text = text')[0]
        self.assertIn("if record.raw_text == text and record.received_amount == received",first)
        self.assertIn('return',first)
        self.assertNotIn('self.parse_record',first)

if __name__=='__main__':unittest.main()
