"""2026-10-31施工单：消息语法、来源复核、新复式、隔离账本和竞争反例。"""
from collections import Counter
from copy import deepcopy
from itertools import permutations, combinations
from pathlib import Path
import unittest
from unittest.mock import patch

from config import RULES_BY_LOTTERY
from controller import Controller
from data_contract import record_to_contract
from data_store import DataStore
from parser import BetParser
from recognition_policy import ai_candidate_validation
from risk_engine import RiskEngine
from test_support import TestDirectory

MULTILINE='福 841.851.857.861.直 2 倍组 1 倍 24\n福 861.直 10 倍组 2 倍 24\n福 15678 组六 6 倍 60 合计 108'
ACCEPTANCE=[('148组六复式',12,1),('1482组六复式',48,1),('51组三复式',12,1),
 ('445-381-731-721-729-003-191-289-135-183-999直选各2倍',44,11),
 ('420.146.114.712直4组1(40米)',40,8),(MULTILINE,108,11),
 ('福定位\n百01\n十2\n个017 1倍12',12,1),('合值10打20元',20,1),('841两单一组',6,2)]


class MessageGrammarTests(unittest.TestCase):
    def test_all_required_cases_both_lotteries(self):
        for lottery in ('福彩','体彩'):
            for raw,principal,count in ACCEPTANCE:
                source=raw if lottery=='福彩' else raw.replace('福','体')
                with self.subTest(lottery=lottery,raw=source):
                    parser=BetParser()
                    bets,_,error=parser.parse(lottery,source)
                    self.assertEqual(error,'')
                    self.assertEqual((sum(b.principal for b in bets),len(bets)),(principal,count))
                    self.assertTrue(all(b.source_span.get('text') for b in bets))

    def test_delimiter_family_and_explicit_duplicate_purchase(self):
        for sep in (' ','.','-',',','，',':','：','、','。','/','\n'):
            source=sep.join(('003','114','999','003'))+'直选各2倍'
            bets,_,error=BetParser().parse('福彩',source)
            self.assertEqual(error,'',(sep,error))
            self.assertEqual([b.selection for b in bets],['003','114','999','003'])
            self.assertEqual(sum(b.principal for b in bets),16)

    def test_multiline_subtotals_and_positions_keep_original_spans(self):
        parser=BetParser()
        bets,_,_=parser.parse('福彩',MULTILINE)
        self.assertEqual(parser.last_claimed,108)
        self.assertEqual([(r['declared'],r['computed']) for r in parser.last_receipts],[(24,24),(24,24),(60,60)])
        self.assertEqual(Counter(b.selection for b in bets)['861'],4)
        self.assertNotIn('108',[b.selection for b in bets])
        for bet in bets:
            span=bet.source_span
            self.assertEqual(MULTILINE[span['start']:span['end']],span['text'])
        raw='福定位\n百01\n十2\n个017 1倍12\n福定位\n百5\n十1\n个2 2倍4\n福148组六复式'
        bets,_,error=parser.parse('福彩',raw)
        self.assertEqual(error,'')
        self.assertEqual([b.principal for b in bets],[12,4,12])
        math_source='福定位\n百069\n十568\n个14\n8倍8*36=288\n福0127组六40倍'
        bets,_,error=parser.parse('福彩',math_source)
        self.assertEqual(error,'')
        self.assertEqual([b.principal for b in bets],[288,400])
        self.assertFalse(parser.parse('福彩',math_source.replace('=288','=289'))[0])

    def test_money_does_not_become_a_number_or_decimal_multiplier(self):
        parser=BetParser()
        for raw,cost,receipt in [('420.146.114.712直4组1(400米)',40,400),
                                 ('福050组选1倍。200',2,200),('293直选1.5倍 合计3元',3,3)]:
            bets,_,error=parser.parse('福彩',raw)
            self.assertEqual(error,'')
            self.assertEqual(sum(b.principal for b in bets),cost)
            self.assertEqual(parser.last_claimed,receipt)
            self.assertNotIn('200',[b.selection for b in bets])
        # 已确认的“一单一组金额”模板不能被通用尾部收款标注拆坏。
        for raw,amount,mult in [('福537一单一组8',8,2),('532 538一单一组4',4,.5)]:
            bets,_,error=parser.parse('福彩',raw)
            self.assertEqual(error,'')
            self.assertEqual(sum(b.principal for b in bets),amount)
            self.assertTrue(all(b.multiplier==mult for b in bets))

    def test_invalid_inputs_stay_invalid(self):
        for raw in ('11组六复式','1481组六复式','511组三复式','5组三复式','01234567890组六复式',
                    '148组六复式0倍','148组六复式-1倍','51组三复式1.001倍',
                    '445--1直选2倍','003，假话，114直选2倍','888组选1倍','222转圈打1倍',
                    '百00十2个3定位1倍','百0百1十2定位1倍','和值28 2倍','合值10打0元'):
            self.assertFalse(BetParser().parse('福彩',raw)[0],raw)

    def test_new_groups_front_alias_spacing_and_ten_digit_boundary(self):
        parser=BetParser()
        for raw in ('组六复式148 两倍','148组 六 复 式两倍','148组六复式x2倍'):
            bets,_,error=parser.parse('福彩',raw)
            self.assertEqual(error,'',raw)
            self.assertEqual(bets[0].principal,24)
        for play,expected in [('组六复式',1440),('组三复式',540)]:
            bet=parser.parse('福彩','0123456789'+play)[0][0]
            self.assertEqual(bet.principal,expected)


class ExpansionPayoutTests(unittest.TestCase):
    def test_exact_expanded_purchase_and_winners_for_every_result(self):
        parser=BetParser()
        engine=RiskEngine()
        for play,pool in [('组六复式','148'),('组六复式','1482'),('组三复式','51'),('组三复式','148')]:
            bet=parser.parse('福彩',pool+play)[0][0]
            tickets=engine.expanded_tickets(bet)
            self.assertEqual(len(tickets),len(set(tickets)))
            self.assertEqual(bet.principal,len(tickets)*2)
            _,_,payouts=engine.calculate('福彩',[bet],0,1800)
            for number,payout in payouts.items():
                # 独立按逐注原始定义计数，避免直接复述引擎的常量。
                matches=sum(sorted(ticket)==sorted(number) for ticket in tickets)
                self.assertEqual(payout,matches*(300 if play=='组六复式' else 600))
            self.assertEqual(payouts['148' if play=='组六复式' else ('511' if pool=='51' else '114')],1800)
            self.assertEqual(payouts['999'],0)
        bet=parser.parse('福彩','51组三复式3倍')[0][0]
        self.assertEqual(bet.principal,36)
        self.assertEqual(engine.payout_for('福彩',bet,'155'),5400)

    def test_existing_retest_and_group_pricing_are_unchanged(self):
        parser=BetParser()
        engine=RiskEngine()
        for raw,principal,result,prize in [('5781复试1倍',10,'551',140),('148组三1倍',10,'114',500),
                                           ('1482组六1倍',10,'148',370),('51组三1倍',10,'511',1500),
                                           ('148组1倍',2,'841',300)]:
            bet=parser.parse('福彩',raw)[0][0]
            self.assertEqual(bet.principal,principal)
            self.assertEqual(engine.payout_for('福彩',bet,result),prize)

    def test_sum_ten_only_has_confirmed_odds(self):
        parser=BetParser()
        engine=RiskEngine()
        bet=parser.parse('福彩','合值10打20元')[0][0]
        self.assertEqual((bet.play,bet.selection,bet.multiplier,bet.principal,bet.entered_amount_yuan),('和值10','10',2,20,20))
        _,_,payouts=engine.calculate('福彩',[bet],0,1800)
        for number,payout in payouts.items():
            self.assertEqual(payout,280 if sum(map(int,number))==10 else 0)


class EvidenceAndPostingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TestDirectory()
        self.store=DataStore(self.tmp.name)
        self.controller=Controller(self.store)
    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def validate(self,raw,candidate):
        bets,_,error=self.controller.parser.parse('福彩',candidate)
        self.assertTrue(bets,error)
        return ai_candidate_validation(raw,candidate,bets,self.controller.parser)[0]

    def test_correct_cross_play_multiline_and_repeats_are_accepted(self):
        for raw,_,_ in ACCEPTANCE:
            self.assertTrue(self.validate(raw,raw),raw)
        self.assertTrue(self.validate('123 123 来一单','123单1倍，123单1倍'))
        self.assertTrue(self.validate('福定位\n百01\n十2\n个017 来1倍','百01十2个017定位1倍'))

    def test_group_permutations_are_legal_source_evidence(self):
        source='148组六复式'
        candidate='，'.join(''.join(p)+'组1倍' for p in permutations('148'))
        self.assertTrue(self.validate(source,candidate))
        self.assertFalse(self.validate(source,candidate+'，148组1倍'))
        source='51组三复式'
        tickets=RiskEngine.expanded_tickets(self.controller.parser.parse('福彩',source)[0][0])
        self.assertTrue(self.validate(source,'，'.join(n+'组1倍' for n in tickets)))

    def test_invention_play_change_money_number_and_wrong_scope_are_blocked(self):
        negatives=[('123来一单','123单2倍'),('123来一单','123组1倍'),
                   ('123来一单','777单1倍'),('123 123 来一单','123单1倍'),
                   ('123来一单','123单1倍，123单1倍'),
                   ('841直2组1合计108','841单2倍，841组1倍，108单1倍'),
                   ('841直2组1\n861直10组2','841单10倍，841组2倍，861单2倍，861组1倍'),
                   ('百01十2个017定位1倍','百01十2个018定位1倍'),
                   ('51组三复式','51组三1倍')]
        for raw,candidate in negatives:
            self.assertFalse(self.validate(raw,candidate),(raw,candidate))

    def test_valid_ai_posts_once_even_reason_contains_uncertain_words(self):
        rec=self.controller.add('福彩','123再来一注')
        self.assertTrue(self.controller.apply_ai_candidate(rec.record_id,rec.revision,'123直选1倍','可能需要核对，结构已明确'))
        self.assertEqual(self.controller.analysis('福彩').principal,200)
        self.assertFalse(self.controller.apply_ai_candidate(rec.record_id,rec.revision,'123直选1倍'))
        self.assertEqual(self.controller.analysis('福彩').payouts['123'],1800)
        self.assertIn('posting_s',self.controller.last_ai_metrics)

    def test_partial_money_and_risk_survive_ai_failure(self):
        rec=self.controller.add('福彩','福定位\n百01\n十2\n个017 1倍12\n未知复杂表达')
        self.assertEqual(rec.status,'部分识别')
        before=self.controller.analysis('福彩')
        self.assertEqual(before.principal,1200)
        self.assertFalse(self.controller.apply_ai_candidate(rec.record_id,rec.revision,'777单1倍'))
        after=self.controller.analysis('福彩')
        self.assertEqual((before.principal,before.payouts),(after.principal,after.payouts))

    def test_deleted_during_post_and_late_result_never_resurrect(self):
        rec=self.controller.add('福彩','123来一单')
        original=self.store.update
        def competing_delete(record,action='更新',**kwargs):
            self.store.delete(record)
            return original(record,action,**kwargs)
        with patch.object(self.store,'update',side_effect=competing_delete):
            self.assertFalse(self.controller.apply_ai_candidate(rec.record_id,rec.revision,'123单1倍'))
        self.assertIsNone(self.store.get(rec.record_id))
        self.assertEqual(self.controller.analysis('福彩').principal,0)

    def test_unknown_rules_never_go_to_ai(self):
        for raw in ('和值28 2倍','12胆345拖2倍','1481组六复式','好👌',
                    '我问一下，等他回复再说','两码定位是10元一倍，中一个的话900','定位是怎么算钱的'):
            rec=self.controller.add('福彩',raw)
            self.assertNotEqual(rec.status,'已识别')
            self.assertEqual(self.controller.ai_request_text(rec),'')

    def test_normal_startup_does_not_rewrite_or_reimport_historical_bets(self):
        import json
        rec=self.controller.add('福彩','5781复试1倍')
        rec.bets[0].principal=128
        rec.engine_version='historical-engine'
        self.store.update(rec)
        payload=self.store.get(rec.record_id).to_dict()
        audit_count=self.store.db.execute('SELECT COUNT(*) FROM audit').fetchone()[0]
        legacy=self.store.data_dir/'2026-10-01.json'
        legacy.write_text(json.dumps({'date':'2026-10-01','records':[payload]},ensure_ascii=False),encoding='utf-8')
        Controller(self.store)
        self.assertEqual(self.store.get(rec.record_id).to_dict(),payload)
        self.assertEqual(self.store.load_records('2026-10-01'),[])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM audit').fetchone()[0],audit_count)

    def test_contract_exports_new_types_and_receipt_metadata(self):
        for raw,_,_ in ACCEPTANCE:
            rec=self.controller.add('福彩',raw)
            self.assertEqual(rec.status,'已识别',rec.note)
            contract=record_to_contract(rec)
            self.assertTrue(all(b['bet_type']!='UNKNOWN' for b in contract['blocks'][0]['bets']))

    def test_offsetting_wrong_subtotals_still_show_an_issue(self):
        rec=self.controller.add('福彩','123单2倍 2\n456单1倍 4合计6')
        self.assertEqual(rec.status,'已识别')
        self.assertEqual(rec.computed_amount_yuan,6)
        self.assertEqual(rec.parse_status,'amount_mismatch')
        self.assertEqual(len([p for p in rec.issues if p['code']=='line_amount_mismatch']),2)

    def test_ai_only_settings_save_does_not_reprice_existing_records(self):
        rec=self.controller.add('福彩','5781复试1倍')
        rec.bets[0].principal=128
        rec.engine_version='historical-engine'
        self.store.update(rec)
        before=self.store.get(rec.record_id).to_dict()
        settings=deepcopy(self.controller.settings)
        settings['ai']['model']='changed-config-only'
        self.controller.save_settings(settings,deepcopy(self.controller.rules))
        self.assertEqual(self.store.get(rec.record_id).to_dict(),before)

    def test_explicit_old_review_uses_new_local_parser_before_ai(self):
        from test_ai_routing_headless import AppHarness
        from ui import RiskApp
        rec=self.controller.add('福彩','148组六复式')
        rec.engine_version='old-engine'
        rec.status='待确认'
        rec.bets=[]
        rec.pending_fragments=[{'text':rec.raw_text,'ai_eligible':True}]
        self.store.update(rec)
        app=AppHarness(self.controller,self.store)
        RiskApp.queue_ai(app,rec)
        self.assertEqual(app.pool.tasks,[])
        self.assertEqual(self.store.get(rec.record_id).status,'已识别')
        self.assertEqual(self.store.get(rec.record_id).computed_amount_yuan,12)


if __name__=='__main__':
    unittest.main()
