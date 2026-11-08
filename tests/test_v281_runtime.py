# -*- coding: utf-8 -*-
"""Acceptance tests run against the installed project's actual parser in TEMP."""
from __future__ import annotations
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from pathlib import Path
import hashlib
import tempfile
import unittest
from unittest.mock import patch

from parser import BetParser, split_lottery_message
from models import RiskRow
from source_context_v281 import normalize_layout, parse_context
from wechat_text_v281 import split_sender, normalize_payload, repair_record
from dashboard_v281 import threshold_move, threshold_value, save_purchase_checked
from dashboard_metrics_v280 import CODES, candidate_metrics, purchased_metrics
from ui_state_v273 import UIStateV273
from risk_extension_v2734 import freeze_purchase_snapshot


class ContextTests(unittest.TestCase):
    def parse(self,text,lottery='福彩'):
        p=BetParser();bets,norm,reason=p.parse(lottery,text)
        self.assertFalse(reason,(text,reason))
        self.assertTrue(bets,text)
        return p,bets

    def test_user_group_crossline_bare_cash(self):
        _,b=self.parse('13589\n福 组六200')
        self.assertEqual([(x.play,x.selection,x.multiplier,x.principal) for x in b],
                         [('五码组六','13589',20,200.0)])

    def test_four_digit_receipt(self):
        _,b=self.parse('福1479四码组六100🈴100')
        self.assertEqual((b[0].play,b[0].multiplier,b[0].principal),('四码组六',10,100.0))

    def test_bare_value_disambiguated_as_multiple(self):
        _,b=self.parse('福1479四码组六10合100')
        self.assertEqual((b[0].multiplier,b[0].principal),(10,100.0))

    def test_explicit_twohundred_multiples_not_money(self):
        _,b=self.parse('13589\n福组六200倍')
        self.assertEqual((b[0].multiplier,b[0].principal),(200,2000.0))

    def test_aliases_currency_and_multiples(self):
        for tail,mult,amount in [('0.25倍',.25,2.5),('1/4倍',.25,2.5),
                                ('四分之一倍',.25,2.5),('3/2倍',1.5,15),
                                ('二分之三倍',1.5,15),('¼倍',.25,2.5),('0.5米',.05,.5),
                                ('半米',.05,.5),('5毛',.05,.5),('一块',.1,1)]:
            with self.subTest(tail=tail):
                _,b=self.parse('1479\n福四码组六'+tail)
                self.assertEqual((b[0].multiplier,b[0].principal),(mult,amount))

    def test_reversed_fraction_requires_exact_receipt(self):
        _,b=self.parse('福123直选4/1倍合0.5')
        self.assertEqual((b[0].multiplier,b[0].principal),(.25,.5))
        result=parse_context(BetParser(),'福彩','福123直选4/1倍')
        self.assertFalse(result[0]);self.assertTrue(result[2])

    def test_direct_cash_is_not_half_multiple(self):
        _,b=self.parse('123\n福直选0.5米')
        self.assertEqual((b[0].multiplier,b[0].principal),(.25,.5))

    def test_all_pool_sizes_explicit_units(self):
        for n in range(4,10):
            digits='012345678'[:n]
            for kind in ('组三','组六','复试'):
                for price,mult in [('20元',2),('0.5倍',.5)]:
                    with self.subTest(n=n,kind=kind,price=price):
                        _,b=self.parse(digits+'\n福'+kind+price)
                        self.assertEqual(len(b),1)
                        self.assertEqual(b[0].selection,digits)
                        self.assertEqual(b[0].multiplier,mult)
                        self.assertEqual(b[0].principal,10*mult)

    def test_runtime_price_not_hardcoded(self):
        p=BetParser();rules=deepcopy(p.rules);rules['福彩']['五码组六']['principal']=20
        p=BetParser(rules)
        b,_,reason=p.parse('福彩','13589\n福组六200元')
        self.assertFalse(reason);self.assertEqual((b[0].multiplier,b[0].principal),(10,200.0))

    def test_repeated_purchase_not_deduplicated(self):
        _,b=self.parse('13589\n福组六200元\n13589\n福组六200元')
        self.assertEqual(len(b),2);self.assertEqual(sum(x.principal for x in b),400)

    def test_invalid_size_and_repeated_digits_rejected(self):
        for s in ('福13589四码组六200元','福13558五码组六200元','福13589组六0元',
                  '福1479四码组六1/3倍','福1479四码组六100合200'):
            with self.subTest(s=s):
                p=BetParser();result=parse_context(p,'福彩',s)
                self.assertIsNotNone(result);self.assertFalse(result[0]);self.assertTrue(result[2])

    def test_cancel_or_unrecognized_not_eaten(self):
        p=BetParser()
        for s in ('13589\n福组六200元\n取消','13589\n说明文字\n福组六200元',
                  '13589\n02468\n福组六200元','foo:\n13589\n福组六200元'):
            with self.subTest(s=s):self.assertIsNone(parse_context(p,'福彩',s))

    def test_wrong_total_preserves_explicit_multiple(self):
        p,b=self.parse('福1479四码组六10倍合90')
        self.assertEqual(b[0].principal,100);self.assertTrue(p.last_warnings)

    def test_crosslottery_tail_routing(self):
        sections=split_lottery_message('13589\n体组六200元','福彩')
        self.assertEqual(set(sections),{'体彩'})
        _,b=self.parse(sections['体彩'],'体彩');self.assertEqual(b[0].principal,200)

    def test_two_lottery_pools_route_separately(self):
        sections=split_lottery_message('13589\n福组六200元\n1479\n体组六100元','福彩')
        self.assertEqual(set(sections),{'福彩','体彩'})
        for lot,amount in [('福彩',200),('体彩',100)]:
            _,b=self.parse(sections[lot],lot);self.assertEqual(sum(x.principal for x in b),amount)

    def test_conflicting_lotteries_preserved(self):
        text='福13589\n体组六200元'
        self.assertEqual(normalize_layout(text),text)
        result=parse_context(BetParser(),'福彩',text)
        self.assertIsNone(result)

    def test_old_multiline_direct_still_works(self):
        for text in ('3D\n104 302 303 305\n直选一倍',
                     '福直选2倍\n002 003\n福直选3倍\n006'):
            with self.subTest(text=text):
                p=BetParser();b,_,reason=p.parse('福彩',text)
                self.assertFalse(reason);self.assertEqual(sum(x.principal for x in b),8 if '一倍' in text else 14)


class WeChatTests(unittest.TestCase):
    body='008 015\n福2注1米合2'
    def test_user_full_216_number_wechat_order(self):
        raw = 'lvoexiyong:\n008 015 023 024 028 029 032 038 042 045 047 048 051 054 058 068 069 074 078 080 082 083 084 085 086 087 088 092 096 105 124 127 129 142 144 145 148 149 150 154 172 178 184 187 192 194 230 239 240 241 244 248 249 271 280 284 289 290 291 294 298 308 320 329 345 347 354 359 374 379 380 392 395 397 399 405 407 408 414 415 418 419 420 421 424 428 429 435 437 441 442 445 446 450 451 453 454 456 458 464 465 468 470 473 478 480 481 482 485 486 487 489 491 492 498 501 504 508 510 514 534 539 540 541 543 544 546 548 564 580 584 593 644 645 648 654 680 684 689 690 698 704 708 718 721 734 739 740 743 748 780 781 784 789 793 798 800 804 805 806 807 808 814 817 820 824 829 830 840 841 842 845 846 847 849 850 854 860 864 869 870 871 874 879 880 892 894 896 897 899 906 914 920 921 923 924 928 932 935 937 939 941 942 948 953 960 968 973 978 982 984 986 987 989 993 998\n福216注一块🈴216'
        clean=split_sender(raw,'','123@chatroom',True)
        self.assertEqual(clean.sender,'lvoexiyong')
        p=BetParser();bets,_,reason=p.parse('福彩',clean.text)
        self.assertFalse(reason,reason)
        self.assertEqual(len(bets),216)
        self.assertEqual(sum(b.principal for b in bets),216)
        self.assertTrue(all(b.multiplier==.5 for b in bets))
        self.assertEqual(clean.original,raw)

    def test_native_custom_username(self):
        x=split_sender('lvoexiyong:\n'+self.body,'','123@chatroom',True)
        self.assertEqual((x.text,x.sender),(self.body,'lvoexiyong'))
        self.assertTrue(x.proof);self.assertTrue(x.original.startswith('lvoexiyong:'))

    def test_custom_id_from_metadata(self):
        x=split_sender('lvoexiyong:\r\n'+self.body,'lvoexiyong','123@chatroom')
        self.assertEqual(x.text,self.body)

    def test_fullwidth_colon(self):
        x=split_sender('lvoexiyong：\n'+self.body,'lvoexiyong','123@chatroom')
        self.assertEqual(x.text,self.body)

    def test_mismatched_sender_is_not_stripped(self):
        x=split_sender('someone_else:\n'+self.body,'actual_sender','123@chatroom',True)
        self.assertFalse(x.prefix)

    def test_private_or_untrusted_prefix_untouched(self):
        for group,trusted in [('wxid_person',True),('123@chatroom',False)]:
            x=split_sender('lvoexiyong:\n'+self.body,'',group,trusted)
            self.assertFalse(x.prefix)

    def test_legitimate_headers_untouched(self):
        for head in ('福彩','定位','百位','方案一','13589','合计'):
            x=split_sender(head+':\n'+self.body,'','123@chatroom',True)
            self.assertFalse(x.prefix,head)

    def test_same_line_colon_not_deleted(self):
        self.assertFalse(split_sender('lvoexiyong:008直选1倍','lvoexiyong','123@chatroom',True).prefix)

    def test_native_same_line_wxid_compatibility(self):
        text='wxid_example:008直选1倍'
        x=split_sender(text,'','123@chatroom',True)
        self.assertEqual((x.text,x.sender),('008直选1倍','wxid_example'))
        self.assertEqual(x.original,text)
        self.assertFalse(split_sender(text,'other_person','123@chatroom',True).prefix)
        self.assertFalse(split_sender(text,'','123@chatroom',False).prefix)

    def test_payload_keeps_original_envelope(self):
        x=normalize_payload(dict(text='lvoexiyong:\n'+self.body,sender='',group_id='123@chatroom',transport_source='wechat4',message_id='abc'))
        self.assertEqual(x['message_id'],'abc');self.assertEqual(x['text'],self.body)
        self.assertEqual(x['transport_original_text'],'lvoexiyong:\n'+self.body)

    def test_reparse_existing_message_audited(self):
        raw='lvoexiyong:\n'+self.body
        r=SimpleNamespace(source='微信',raw_text=raw,source_original_text=raw,sender='',group_id='123@chatroom',ai_history=[])
        self.assertTrue(repair_record(r));self.assertEqual(r.raw_text,self.body)
        self.assertEqual(r.source_original_text,raw);self.assertEqual(len(r.ai_history),1)
        self.assertFalse(repair_record(r));self.assertEqual(len(r.ai_history),1)

    def test_manual_record_not_sanitized(self):
        r=SimpleNamespace(source='人工',raw_text='lvoexiyong:\n'+self.body,sender='',group_id='123@chatroom',ai_history=[])
        self.assertFalse(repair_record(r))

    def test_native_callback_and_stable_id(self):
        from wechat_service import WeChatService
        import hashlib
        svc=WeChatService('unused-test-only',{'filter_self':False})
        captured=[];svc._persist=lambda p:captured.append(p) or True
        raw='lvoexiyong:\n'+self.body
        msg={'type':'text','content':raw,'create_time':'2026-11-06T12:00:00+08:00'}
        self.assertTrue(svc._wechat4_callback('123@chatroom',msg))
        got=captured[-1]
        self.assertEqual(got['text'],self.body)
        old_hash=hashlib.sha256(raw.encode('utf8')).hexdigest()[:16]
        self.assertEqual(got['message_id'],f"wx4:123@chatroom:{msg['create_time']}::{old_hash}")
        self.assertTrue(svc._wechat4_callback('123@chatroom',msg,True))
        self.assertEqual(got['message_id'],captured[-1]['message_id'])
        self.assertTrue(captured[-1]['history'])

    def test_sender_filter_still_applies(self):
        from wechat_service import WeChatService
        svc=WeChatService('unused-test-only',{'filter_self':True})
        svc.self_wxid='lvoexiyong';called=[];svc._persist=lambda p:called.append(p)
        self.assertFalse(svc._wechat4_callback('123@chatroom',dict(type='text',content='lvoexiyong:\n'+self.body)))
        self.assertFalse(called)

    def test_persist_retains_transport_audit(self):
        from wechat_service import WeChatService
        svc=WeChatService('unused-test-only',{'filter_self':False})
        raw='lvoexiyong:\n'+self.body
        payload=dict(message_id='id1',text=raw,sender='',group_id='123@chatroom',transport_source='wechat4')
        captured=[]
        with patch('wechat_service.DataStore.enqueue',side_effect=lambda db,p:captured.append(p) or True):
            self.assertTrue(svc._persist(payload))
        self.assertEqual(captured[0]['text'],self.body)
        self.assertEqual(captured[0]['transport_original_text'],raw)
        self.assertEqual(captured[0]['sender'],'lvoexiyong')



class ThresholdAndAccountingTests(unittest.TestCase):
    def test_plus_minus_and_floor(self):
        self.assertEqual(threshold_move('27000','1000',1),'28000')
        self.assertEqual(threshold_move('27000','1000',-1),'26000')
        self.assertEqual(threshold_move('27000','500',1),'27500')
        self.assertEqual(threshold_move('200','1000',-1),'0')
        self.assertEqual(threshold_move('0','1000',-1),'0')
        self.assertEqual(threshold_move('0.20','0.10',1),'0.3')

    def test_invalid_steps(self):
        for step in ('','0','-1','NaN','Infinity','abc','0.001'):
            with self.subTest(step=step):
                with self.assertRaises((ValueError,ArithmeticError)):threshold_move('27000',step,1)

    def test_unhedged_range_excludes_all_hedge(self):
        payouts=tuple(1080000 if code=='302' else 15660000 if code=='202' else 0 for code in CODES)
        p=purchased_metrics(787800,payouts,None)
        self.assertEqual(p.cost_cents,0);self.assertEqual(p.at('302')['total_profit_cents'],-292200)
        self.assertEqual(p.at('302')['hedge_profit_cents'],0)
        self.assertEqual(p.worst_profit_cents,787800-15660000);self.assertEqual(p.best_profit_cents,787800)

    def test_purchased_miss_keeps_whole_cost(self):
        payouts=tuple(1080000 if c=='302' else 0 for c in CODES)
        purchase={'rows':[['202',84]],'cost_cents':190200,'direct_prize_cents':180000,'leopard_prize_cents':160000}
        p=purchased_metrics(787800,payouts,purchase)
        self.assertEqual(p.at('302')['total_profit_cents'],-482400)
        self.assertEqual(p.at('302')['hedge_profit_cents'],-190200)

    def test_cancel_persists_on_reopen(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'ui.json';s=UIStateV273(path)
            p=freeze_purchase_snapshot([RiskRow('202',0,0,84)],2,1800,1600)
            save_purchase_checked(s,'福彩','2026-11-06',p,'balanced')
            self.assertIsNotNone(UIStateV273(path).get_actual_hedge('福彩','2026-11-06'))
            save_purchase_checked(s,'福彩','2026-11-06',None,'balanced')
            fresh=UIStateV273(path)
            self.assertIsNone(fresh.get_actual_hedge('福彩','2026-11-06'))
            self.assertIsNone(fresh.get_purchased_mode('福彩','2026-11-06'))

    def test_step_saved_independent_of_actual_and_scopes(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'ui.json';s=UIStateV273(path)
            s.put_scope('福彩','2026-11-06','302',1)
            s.data['threshold_step_yuan']='500';s.save()
            fresh=UIStateV273(path)
            self.assertEqual(fresh.data['threshold_step_yuan'],'500')
            self.assertEqual(fresh.get_scope('福彩','2026-11-06')['adjustment'],1)
            self.assertEqual(fresh.get_scope('福彩','2026-11-06')['draw'],'302')

    def test_no_reset_manual_state(self):
        with tempfile.TemporaryDirectory() as td:
            s=UIStateV273(Path(td)/'state.json')
            s.put_manual_plan('福彩','2026-11-06','福直选3倍\n002')
            s.put_scope('福彩','2026-11-06','002',4)
            s.data['threshold_step_yuan']='100';s.save()
            self.assertEqual(s.get_manual_plan('福彩','2026-11-06'),'福直选3倍\n002')
            self.assertEqual(s.get_scope('福彩','2026-11-06')['adjustment'],4)

    def test_failed_save_rolls_back_memory(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'ui.json';s=UIStateV273(path);s.save()
            old=deepcopy(s.data)
            p=freeze_purchase_snapshot([RiskRow('202',0,0,2)],2,1800,1600)
            with patch.object(s,'save',return_value=None):
                with self.assertRaises(OSError):save_purchase_checked(s,'福彩','d',p,'cheap')
            self.assertEqual(s.data,old)


if __name__=='__main__':unittest.main(verbosity=2)
