import itertools
import json
import tempfile
import unittest
import subprocess
import sys
from copy import deepcopy
from contextlib import closing
from pathlib import Path

from config import RULES_BY_LOTTERY, CN_NUM
from models import cents, MessageRecord, RiskRow
from parser import BetParser, chinese_int
from risk_engine import RiskEngine, plan_text
from data_store import DataStore
from controller import Controller
from test_support import TestDirectory


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = BetParser(policy={'position_cost':'combination','span0_wins':True})

    def parse(self, text, lottery='福彩'):
        bets, _, reason = self.parser.parse(lottery, text)
        self.assertTrue(bets, (text, reason))
        return bets

    def test_handover_four_bets_and_multipliers(self):
        bets = self.parse('福293一单一组50倍，095组505组三一倍')
        self.assertEqual([(b.play,b.selection,b.multiplier,b.principal) for b in bets],
                         [('直选','293',50,100),('组六（组选）','293',50,100),('三码组三','095',1,10),('组三（组选）','505',1,2)])

    def test_multiple_bets_without_comma(self):
        bets = self.parse('293单50倍505组1倍0257组六10倍')
        self.assertEqual([b.multiplier for b in bets], [50,1,10])
        self.assertEqual([b.selection for b in bets], ['293','505','0257'])

    def test_all_43_plays(self):
        samples = {'直选':'293单1倍', '组三（组选）':'505组1倍', '组六（组选）':'293组1倍',
                   '独胆':'7独胆1倍','双飞':'27双飞1倍','对子':'55对子1倍','豹子':'豹子1倍',
                   '一码定位':'百8定位1倍','两码定位':'百8十2定位1倍','三码定位':'百8十2个7定位1倍',
                   '全包组六':'全包组六1倍','全包组三':'全包组三1倍'}
        for size in range(4,10):
            samples[CN_NUM[size]+'码组六'] = '012345678'[:size]+'组六1倍'
        for size in range(2,10):
            samples[CN_NUM[size]+'码组三'] = '012345678'[:size]+'组三1倍'
        for size in range(3,10):
            samples[CN_NUM[size]+'码复试'] = '012345678'[:size]+'复式1倍'
        for span in range(10):
            samples['跨度'+str(span)] = f'跨度{span} 1倍'
        self.assertEqual(set(samples), set(RULES_BY_LOTTERY['福彩']))
        for lottery in ('福彩','体彩'):
            for play, sample in samples.items():
                with self.subTest(lottery=lottery,play=play):
                    bets = self.parse(sample,lottery)
                    self.assertEqual(len(bets),1)
                    self.assertEqual(bets[0].play,play)

    def test_fail_closed_no_silent_partial_or_invented_bet(self):
        cases = ['293单0倍','293单-1倍','293单1.5倍','293单','293 50倍','293单50倍，随便再来一点',
                 '358421组10倍','555组三1倍','0123456789组六1倍','全包组六0倍','foo293单50倍',
                 '293单50倍取消','百8百9定位1倍','百99定位2倍','27双飞2倍多一单', '293组1000001倍']
        for sample in cases:
            with self.subTest(sample=sample):
                bets, _, reason = self.parser.parse('福彩',sample)
                self.assertEqual(bets,[])
                self.assertTrue(reason)

    def test_lottery_conflict_and_leading_zero(self):
        self.assertEqual(self.parse('体彩001单10倍','体彩')[0].selection,'001')
        self.assertEqual(self.parser.parse('福彩','体彩001单10倍')[0],[])

    def test_compatibility_all_plays_preserves_cost_and_every_payout(self):
        cases = [
            ('293单2倍', ['直选293 两倍','单选293x2倍','293×2倍直选','2 9 3直2倍','直 选 293 2 倍']),
            ('505组2倍', ['505组三2倍','组三505 两倍','505组3x2倍','组选505 2倍']),
            ('293组2倍', ['组六293 两倍','293组6x2倍','组选293 2倍']),
            ('7独胆2倍', ['独胆7 两倍','毒胆7x2倍','7×2倍独胆']),
            ('27双飞2倍', ['双飞27 两倍','2 7双飞2倍','双 飞 27 2倍']),
            ('55对子2倍', ['对子55 两倍','5 5对子2倍','55x2倍对子']),
            ('豹子2倍', ['豹子x2倍','豹 子 两 倍']),
            ('全包组六2倍', ['组六全包2倍','组6包全两倍','组 六 全 包 2 倍']),
            ('全包组三2倍', ['组三全包2倍','组3全两倍']),
            ('百069十568个14定位2倍', ['百0 6 9十5 6 8个1 4定位2倍','百位0/6/9 十位5/6/8 个位1/4 定位两倍']),
        ]
        for size in range(4,10):
            digits = '012345678'[:size]
            cases.append((digits+'组六2倍', [f'组六{digits} 两倍',f'{digits}{CN_NUM[size]}码组六2倍',f'{CN_NUM[size]}码组6{digits} 2倍',' '.join(digits)+'组六2倍']))
        for size in range(2,10):
            digits = '012345678'[:size]
            cases.append((digits+'组三2倍', [f'组三{digits} 两倍',f'{digits}{CN_NUM[size]}码组三2倍',f'{CN_NUM[size]}码组3{digits} 2倍',' '.join(digits)+'组三2倍']))
        for size in range(3,10):
            digits = '012345678'[:size]
            cases.append((digits+'复试2倍', [f'复式{digits} 两倍',f'{digits}{CN_NUM[size]}码复试2倍',f'{CN_NUM[size]}码复式{digits} 2倍',' '.join(digits)+'复2倍']))
        for size in range(1,4):
            positions = ['百8','十2','个7'][:size]
            plain = ''.join(positions)
            cases.append((plain+'定位2倍', ['定位'+plain+'两倍', ['一码','两码','三码'][size-1]+'定位'+plain+'x2倍', ' '.join(positions)+'定位两倍']))
        for span in range(10):
            cases.append((f'跨度{span} 2倍', [f'跨{span}两倍',f'{span}跨度2倍',f'跨度{span}x2倍']))
        for lottery in ('福彩','体彩'):
            engine = RiskEngine(policy={'span0_wins':True})
            for standard, variants in cases:
                original = self.parse(standard,lottery)
                expected = [(b.play,b.selection,b.multiplier,b.principal) for b in original]
                payouts = engine.calculate(lottery,original,0,1800)[2]
                for variant in variants:
                    with self.subTest(lottery=lottery,variant=variant):
                        bets = self.parse(variant,lottery)
                        self.assertEqual([(b.play,b.selection,b.multiplier,b.principal) for b in bets],expected)
                        self.assertEqual(engine.calculate(lottery,bets,0,1800)[2],payouts)

    def test_compatibility_keeps_bet_boundaries_and_invalid_inputs_pending(self):
        bets = self.parse('293单1倍组六全包2倍定位百8十2两倍')
        self.assertEqual([(b.play,b.multiplier) for b in bets], [('直选',1),('全包组六',2),('两码定位',2)])
        bets = self.parse('541直选10倍组选5倍')
        self.assertEqual([b.multiplier for b in bets], [10,5])
        self.assertEqual(len(self.parse('全包组三组六各2倍')),2)
        self.assertEqual(len(self.parse('直选293 505各10倍')),2)
        for invalid in ('组六35842110倍','复式57811倍','358421四码组六2倍','组六全包0倍','293单1倍撤单','全包组六组六1倍','组三505一倍撤单'):
            self.assertFalse(self.parser.parse('福彩',invalid)[0],invalid)

    def test_chinese_fullwidth_and_multi_shared(self):
        self.assertEqual(self.parse('２９３单五十倍')[0].multiplier,50)
        self.assertEqual(chinese_int('一百零二'),102)
        self.assertEqual(chinese_int('一万零三十'),10030)
        bets = self.parse('578 624 541 581 213 直5组5')
        self.assertEqual(len(bets),10)
        self.assertEqual([b.principal for b in bets],[10]*10)

    def test_claimed_amount_does_not_override(self):
        bets = self.parse('358421组六10倍 合计200元')
        self.assertEqual(bets[0].principal,100)
        self.assertEqual(self.parser.last_claimed,200)
        self.assertEqual(sum(b.principal for b in self.parse('福110直10组5倍30')),30)
        self.assertEqual(self.parser.last_claimed,30)

    def test_ambiguous_policies_hold_only_affected(self):
        parser = BetParser()
        self.assertTrue(parser.parse('福彩','百8定位1倍')[0])
        self.assertFalse(parser.parse('福彩','百069十568个14定位8倍')[0])
        self.assertTrue(parser.parse('福彩','跨度0 2倍')[0])
        self.assertTrue(parser.parse('福彩','跨度1 2倍')[0])
        bets = self.parse('百069十568个14定位8倍')
        self.assertEqual(bets[0].principal,1440)
        whole = BetParser(policy={'position_cost':'whole','span0_wins':True})
        self.assertEqual(whole.parse('福彩','百069十568个14定位8倍')[0][0].principal,80)


class RiskTests(unittest.TestCase):
    parse = ParserTests.parse

    def setUp(self):
        self.parser = BetParser(policy={'position_cost':'combination','span0_wins':True})
        self.engine = RiskEngine(policy={'span0_wins':True})

    def test_six_digit_group_permutations(self):
        bets = self.parse('358421组六10倍')
        _, _, payouts = self.engine.calculate('福彩',bets,0,1800)
        expected = {''.join(result) for result in itertools.permutations('358421',3)}
        self.assertEqual({number for number,payout in payouts.items() if payout},expected)
        self.assertEqual(len(expected),120)
        self.assertTrue(all(payouts[n] == 750 for n in expected))
        self.assertEqual(payouts['355'],0)

    def test_fushi_all_sizes_exclude_pairs_and_triples(self):
        for size in range(3,10):
            bet = self.parse('012345678'[:size]+'复式2倍')[0]
            self.assertEqual(bet.principal,size*(size-1)*(size-2)*2*2)
            _,_,payouts = self.engine.calculate('福彩',[bet],1,1800)
            self.assertEqual(sum(bool(v) for v in payouts.values()), size*(size-1)*(size-2))
            self.assertEqual(payouts['012'],3600)
            self.assertEqual(payouts['000'],0)
            self.assertEqual(payouts['001'],0)

    def test_user_5781_fushi_cost_and_exact_24_outcomes(self):
        expected = {''.join(result) for result in itertools.permutations('5781',3)}
        for lottery in ('福彩','体彩'):
            for alias in ('复试','复式'):
                bet = self.parse('5781'+alias+'一倍', lottery)[0]
                self.assertEqual(bet.principal,48)
                _,_,payouts = self.engine.calculate(lottery,[bet],1,1800)
                self.assertEqual({number for number,payout in payouts.items() if payout}, expected)
                self.assertEqual(len(expected),24)
                self.assertEqual(payouts['578'],1800)
                self.assertEqual(payouts['551'],0)
                self.assertEqual(payouts['777'],0)
        self.assertEqual(self.parse('5781复试10倍')[0].principal,480)

    def test_position_requires_all(self):
        bet = self.parse('百069十568个14定位8倍')[0]
        self.assertEqual(self.engine.payout_for('福彩',bet,'954'),14400)
        self.assertEqual(self.engine.payout_for('福彩',bet,'951'),14400)
        self.assertEqual(self.engine.payout_for('福彩',bet,'854'),0)
        _,_,payouts = self.engine.calculate('福彩',[bet],1,1800)
        self.assertEqual(sum(bool(v) for v in payouts.values()),18)

    def test_group3_exact_and_multi_and_baozi(self):
        exact = self.parse('505组1倍')[0]
        multi = self.parse('095组三1倍')[0]
        self.assertEqual(self.engine.payout_for('福彩',exact,'055'),600)
        self.assertEqual(self.engine.payout_for('福彩',exact,'005'),0)
        self.assertEqual(self.engine.payout_for('福彩',multi,'005'),500)
        self.assertEqual(self.engine.payout_for('福彩',multi,'095'),0)
        self.assertEqual(self.engine.payout_for('福彩',self.parse('跨度0 1倍')[0],'777'),800)
        self.assertEqual(RiskEngine(policy={'span0_wins':False}).payout_for('福彩',self.parse('跨度0 1倍')[0],'777'),800)
        self.assertEqual(self.engine.payout_for('福彩',self.parse('77对子1倍')[0],'777'),0)
        self.assertEqual(self.engine.payout_for('福彩',self.parse('7独胆1倍')[0],'777'),33)
        self.assertEqual(self.engine.payout_for('福彩',self.parse('777单1倍')[0],'777'),1600)

    def test_all_ten_leopards_direct_span_and_separate_baozi(self):
        for lottery in ('福彩','体彩'):
            for digit in '0123456789':
                result = digit*3
                bets = self.parse(f'{result}直选3倍，{digit}独胆2倍，百{digit}十{digit}个{digit}定位1倍，豹子2倍，跨度0 3倍',lottery)
                expected = [4800,66,1800,3200,2400]
                self.assertEqual([self.engine.payout_for(lottery,b,result) for b in bets],expected)
                _,_,payouts = self.engine.calculate(lottery,bets,0,1800)
                self.assertEqual(payouts[result],sum(expected))
                different = str((int(digit)+1)%10)*3
                self.assertEqual(self.engine.payout_for(lottery,bets[0],different),0)
                self.assertEqual(self.engine.payout_for(lottery,self.parse('293单1倍',lottery)[0],'293'),1800)
                for forbidden in ('358421组六2倍','095组三2倍','5781复式2倍','77对子2倍','27双飞2倍','全包组六2倍','全包组三2倍','跨度1 2倍'):
                    self.assertEqual(self.engine.payout_for(lottery,self.parse(forbidden,lottery)[0],result),0)

    def test_leopard_hedge_uses_1600_and_mixed_rewards_cover_total_cost(self):
        bets = self.parse('888直选10倍，293直选10倍')
        _,rows,_ = self.engine.calculate('福彩',bets,1,1800,revenue=0)
        self.assertEqual({r.number:(r.payout,r.plan_multiplier) for r in rows},{'888':(16000,10),'293':(18000,10)})
        _,rows,_ = self.engine.calculate('福彩',bets,1,1800,revenue=0,hedge_cost=True)
        cost = sum(r.plan_multiplier for r in rows)*2
        for row in rows:
            self.assertGreaterEqual(row.plan_multiplier*(1600 if row.number=='888' else 1800),row.net_loss+cost)
        rules = deepcopy(RULES_BY_LOTTERY)
        rules['体彩']['直选']['leopard_prize']=1700
        self.assertEqual(RiskEngine(rules).payout_for('体彩',bets[0],'888'),17000)
        self.assertEqual(RiskEngine(rules).payout_for('福彩',bets[0],'888'),16000)

    def test_threshold_inclusive_metric_and_roundup(self):
        bets = self.parse('293单3倍')
        _,risks,_ = self.engine.calculate('福彩',bets,5400,1800,revenue=6,metric='payout')
        self.assertEqual([r.number for r in risks],['293'])
        self.assertEqual(risks[0].net_loss,5394)
        self.assertEqual(risks[0].plan_multiplier,3)
        self.assertEqual(self.engine.calculate('福彩',bets,5400,1800,metric='net_loss')[1],[])
        rules = deepcopy(RULES_BY_LOTTERY)
        rules['福彩']['直选']['prize']=20000
        row = RiskEngine(rules).calculate('福彩',self.parse('352单1倍'),5000,1800,revenue=2000)[1][0]
        self.assertEqual((row.number,row.net_loss,row.plan_multiplier),('352',18000,10))
        row = RiskEngine(rules).calculate('福彩',self.parse('352单1倍'),5000,1800,revenue=1999.99)[1][0]
        self.assertEqual(row.plan_multiplier,11)

    def test_separate_rules_and_money(self):
        rules = deepcopy(RULES_BY_LOTTERY)
        rules['体彩']['直选']['prize']=1000.01
        bets = self.parse('001单5倍')
        self.assertEqual(RiskEngine(rules).payout_for('体彩',bets[0],'001'),5000.05)
        self.assertEqual(RiskEngine(rules).payout_for('福彩',bets[0],'001'),9000)
        for bad in ['NaN','Infinity',-1,'1.234']:
            with self.assertRaises(ValueError):
                cents(bad)

    def test_plan_layout_and_total_in_yuan(self):
        rows = [RiskRow(f'{i:03d}',10000,10000,m) for i,m in enumerate([10,2,3,1,2,1,1,1])]
        text = plan_text(rows)
        self.assertEqual(len(text.splitlines()),3)
        self.assertEqual(len(text.splitlines()[0].split()),4)
        self.assertTrue(text.endswith('合计：42元'))
        self.assertTrue(text.startswith('000直选10倍'))

    def test_user_plan_exact_format_and_724_yuan_total(self):
        groups = [('715',24),('157 158 175 185 517 518 571 581 751 815 851',14),('541',13),
                  ('178 187 578 587 718 758 781 785 817 857 871 875 110',12),('145 154 415 451 514',3)]
        rows = [RiskRow(number,10000,10000,mult) for numbers,mult in groups for number in numbers.split()]
        text = plan_text(rows)
        self.assertEqual(text.splitlines()[0],'715直选24倍  157直选14倍  158直选14倍  175直选14倍')
        self.assertEqual(text.splitlines()[-2],'415直选3倍  451直选3倍  514直选3倍')
        self.assertEqual(text.splitlines()[-1],'合计：724元')
        self.assertNotIn('x',text)

    def test_total_hedge_cost_in_every_outcome(self):
        bets = self.parse('123单2倍，234单2倍')
        _,rows,_ = self.engine.calculate('福彩',bets,1000,1800,revenue=0,hedge_cost=True)
        total_cost = sum(r.plan_multiplier for r in rows)*2
        self.assertEqual([r.plan_multiplier for r in rows],[3,3])
        for row in rows:
            self.assertGreaterEqual(row.plan_multiplier*1800,row.net_loss+total_cost)
        with self.assertRaises(ValueError):
            self.engine.calculate('福彩',bets,0,1800,revenue=0,hedge_cost=True)

    def test_combinatorial_winning_counts_all_families(self):
        import math
        samples = [('293单1倍',1),('505组1倍',3),('293组1倍',6),('7独胆1倍',271),
                   ('27双飞1倍',54),('55对子1倍',27),('豹子1倍',10),
                   ('百8定位1倍',100),('百8十2定位1倍',10),('百8十2个7定位1倍',1),
                   ('全包组六1倍',720),('全包组三1倍',270)]
        for size in range(4,10):
            samples.append(('012345678'[:size]+'组六1倍',math.comb(size,3)*6))
        for size in range(2,10):
            samples.append(('012345678'[:size]+'组三1倍',size*(size-1)*3))
        for span in range(10):
            samples.append((f'跨度{span} 1倍',10 if span == 0 else 6*span*(10-span)))
        for sample,count in samples:
            with self.subTest(sample=sample):
                _,_,payouts = self.engine.calculate('福彩',self.parse(sample),1,1800)
                self.assertEqual(sum(bool(value) for value in payouts.values()),count)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TestDirectory()
        self.base = Path(self.tmp.name)
        self.store = DataStore(self.base)
        self.controller = Controller(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_store_original_before_parser_error_and_reopen(self):
        def crash(record):
            raise RuntimeError('unexpected parser crash')
        self.controller.parse_record = crash
        with self.assertRaises(RuntimeError):
            self.controller.add('福彩','不认识这笔')
        self.assertEqual(len(self.store.load_records()),1)
        self.assertEqual(self.store.load_records()[0].raw_text,'不认识这笔')

    def test_same_content_allowed_source_id_dedup_and_lottery_separation(self):
        for _ in range(2):
            self.controller.add('福彩','123单1倍')
        self.controller.add('福彩','123单1倍','微信','wx1')
        self.controller.add('福彩','123单1倍','微信','wx2')
        self.assertIsNone(self.controller.add('福彩','123单1倍','微信','wx1'))
        self.controller.add('体彩','123单1倍','微信','wx1')
        self.assertEqual(len(self.controller.records()),5)
        self.assertEqual(self.controller.analysis('福彩').payouts['123'],7200)
        self.assertEqual(self.controller.analysis('体彩').payouts['123'],1800)

    def test_pending_not_counted_receipt_zero_is_real(self):
        a = self.controller.add('福彩','123单3倍 合计50元')
        self.controller.add('福彩','123单50倍，未知投注')
        self.assertIn('差额',a.note)
        self.controller.edit(a,a.raw_text,0)
        analysis = self.controller.analysis('福彩')
        self.assertEqual((analysis.principal,analysis.revenue,analysis.pending),(600,0,1))

    def test_deleted_ai_results_and_revision_are_rejected(self):
        record = self.controller.add('福彩','123来一单')
        self.controller.edit(record,'123单2倍')
        self.assertFalse(self.controller.apply_ai_candidate(record.record_id,0,'123单1倍'))
        record = self.controller.add('福彩','123来一单')
        self.store.delete(record)
        self.assertFalse(self.controller.apply_ai_candidate(record.record_id,0,'123单1倍'))

    def test_ai_valid_candidate_is_counted_immediately_and_after_restart(self):
        record = self.controller.add('福彩','123来一单')
        self.assertTrue(self.controller.apply_ai_candidate(record.record_id,0,'123单1倍'))
        restarted = Controller(self.store)
        self.assertEqual(restarted.records()[0].status,'已识别')
        self.assertEqual(restarted.analysis('福彩').principal,200)
        self.assertEqual(restarted.analysis('福彩').payouts['123'],1800)
        self.assertEqual(restarted.records()[0].raw_text,'123来一单')

    def test_ai_invalid_conflicting_and_unconfirmed_rules_stay_pending(self):
        cases = [('123来一单','123单0倍'), ('体彩123单1倍','123单1倍'),
                 ('百069十568个14定位8倍','123单1倍'),
                 ('358421四码组六2倍','358421组六2倍'), ('123来一单','体彩123单1倍')]
        for raw,candidate in cases:
            record = self.controller.add('福彩',raw)
            self.assertFalse(self.controller.apply_ai_candidate(record.record_id,0,candidate))
            self.assertEqual(self.store.get(record.record_id).status,'待确认')
        self.assertEqual(self.controller.analysis('福彩').principal,0)

    def test_ai_preserves_original_claimed_amount_and_actual_receipt(self):
        record = self.controller.add('福彩','123来一单 合计50元')
        record.received_amount = 40
        self.store.update(record)
        self.assertTrue(self.controller.apply_ai_candidate(record.record_id,0,'123单1倍'))
        saved = self.store.get(record.record_id)
        self.assertEqual((saved.claimed_amount,saved.received_amount),(50,40))
        self.assertIn('差额-48元',saved.note)

    def test_recover_legacy_ai_waiting_and_local_compatibility_on_restart(self):
        for raw,candidate,status in [('组六全包1倍','全包组六1倍','AI待确认'),('复式5781一倍','','待确认'),('123来一单','123单2倍','AI待识别')]:
            record = self.controller.add('福彩',raw)
            record.status,record.ai_candidate,record.bets = status,candidate,[]
            self.store.update(record)
        restarted = Controller(self.store)
        self.assertTrue(all(record.status=='已识别' for record in restarted.records()))
        self.assertEqual(restarted.analysis('福彩').principal,6200)
        audits = self.store.db.execute('SELECT COUNT(*) FROM audit').fetchone()[0]
        Controller(self.store)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM audit').fetchone()[0],audits)

    def test_legacy_import_corrects_wrong_bets_and_is_idempotent(self):
        legacy = MessageRecord('福彩','人工','福293一单一组50倍，095组505组三一倍', period='2026-10-01')
        old = {'date':'2026-10-01','records':[legacy.to_dict()]}
        path = self.store.data_dir / '2026-10-01.json'
        path.write_text(json.dumps(old,ensure_ascii=False),encoding='utf-8')
        controller = Controller(self.store)
        self.assertEqual(len(self.store.load_records('2026-10-01')[0].bets),4)
        Controller(self.store)
        self.assertEqual(len(self.store.load_records('2026-10-01')),1)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')),old)

    def test_corrupt_legacy_is_visible_not_silent_empty(self):
        path = self.store.data_dir / '2026-10-02.json'
        path.write_text('{broken',encoding='utf-8')
        Controller(self.store)
        self.assertTrue(self.store.import_errors)
        self.assertEqual(path.read_text(encoding='utf-8'),'{broken')

    def test_api_settings_key_encrypted_and_recovered_after_restart(self):
        settings = deepcopy(self.controller.settings)
        settings['ai']['api_key']='secret-not-to-save'
        settings['ai'].update(base_url='https://example.invalid/v1',model='saved-model',enabled=True,api_format='chat',json_mode=True)
        self.store.save_settings(settings)
        stored = self.store.db.execute('SELECT payload FROM settings WHERE id=1').fetchone()[0]
        self.assertNotIn('secret-not-to-save',stored)
        self.assertNotIn('"api_key":',stored)
        self.assertIn('api_key_encrypted',stored)
        with closing(DataStore(self.base)) as reopened:
            recovered = Controller(reopened).settings['ai']
            for key in ('api_key','base_url','model','enabled','api_format','json_mode'):
                self.assertEqual(recovered[key],settings['ai'][key])
        # 全新Python进程读取，不依赖父进程的内存。
        probe = "from data_store import DataStore; import sys; s=DataStore(sys.argv[1]); assert s.load_settings()['ai']['api_key']=='secret-not-to-save'; s.close()"
        subprocess.run([sys.executable,'-B','-c',probe,str(self.base)],cwd=Path(__file__).resolve().parent.parent,check=True,capture_output=True)
        self.assertNotIn('secret-not-to-save',self.store.path.read_bytes().decode('latin1'))
        settings['ai']['api_key']=''
        self.store.save_settings(settings)
        self.assertFalse(self.store.load_settings()['ai'].get('api_key'))

    def test_corrupt_encrypted_key_shows_reason_without_losing_settings(self):
        settings = deepcopy(self.controller.settings)
        settings['ai']['api_key']='test-key'
        self.store.save_settings(settings)
        saved = json.loads(self.store.db.execute('SELECT payload FROM settings WHERE id=1').fetchone()[0])
        saved['ai']['api_key_encrypted']='invalid encrypted value'
        self.store.db.execute('UPDATE settings SET payload=? WHERE id=1',(json.dumps(saved),))
        self.store.db.commit()
        loaded = self.store.load_settings()
        self.assertEqual(loaded['ai']['api_key'],'')
        self.assertTrue(self.store.credential_error)
        self.assertEqual(loaded['ai']['model'],settings['ai']['model'])

    def test_old_pending_span0_is_restored_with_latest_leopard_rule(self):
        record = self.controller.add('福彩','跨度0 2倍')
        record.status,record.bets,record.note = '待确认',[],'跨度0遇豹子的中奖规则尚未确认，请到设置选择'
        self.store.update(record)
        restarted = Controller(self.store)
        self.assertEqual(self.store.get(record.record_id).status,'已识别')
        self.assertEqual(restarted.analysis('福彩').payouts['888'],1600)

    def test_unbound_inbox_does_not_starve_bound_messages(self):
        for i in range(120):
            DataStore.enqueue(self.store.path,{'message_id':str(i),'group_id':'unbound','text':'123单1倍','period':self.controller.period})
        self.controller.settings['wechat']['fc_group']='fc'
        DataStore.enqueue(self.store.path,{'message_id':'valid','group_id':'fc','text':'123单1倍','period':self.controller.period})
        records, errors = self.controller.drain_inbox()
        self.assertEqual(len(records),1)
        self.assertFalse(errors)
        self.assertTrue(self.store.inbox())

    def test_period_snapshots_preserved(self):
        self.controller.activate_period('2026-10-01')
        self.controller.add('福彩','123单3倍')
        settings = deepcopy(self.controller.settings)
        rules = deepcopy(self.controller.rules)
        rules['福彩']['直选']['prize']=1000
        self.controller.activate_period('2026-10-03')
        self.controller.save_settings(settings,rules)
        self.controller.add('福彩','123单3倍')
        self.assertEqual(self.controller.analysis('福彩').payouts['123'],3000)
        self.controller.activate_period('2026-10-01')
        self.assertEqual(self.controller.analysis('福彩').payouts['123'],5400)

    def test_correct_saved_fushi_preserving_raw_ids_receipt_and_nonfushi(self):
        record = self.controller.add('福彩','5781复试一倍，293单2倍 合计132元')
        # 模拟旧版本已保存的错误本金。
        record.bets[0].principal = 128
        record.received_amount = 132
        self.store.update(record)
        old_raw,old_id = record.raw_text,record.record_id
        before_audits = self.store.db.execute('SELECT COUNT(*) FROM audit').fetchone()[0]
        Controller(self.store)
        corrected = self.store.get(old_id)
        self.assertEqual([bet.principal for bet in corrected.bets],[48,4])
        self.assertEqual((corrected.raw_text,corrected.record_id,corrected.received_amount),(old_raw,old_id,132))
        self.assertEqual(corrected.claimed_amount,132)
        self.assertIn('应收52元',corrected.note)
        self.assertIn('差额-80元',corrected.note)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM audit').fetchone()[0],before_audits+1)
        Controller(self.store)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM audit').fetchone()[0],before_audits+1)


if __name__ == '__main__':
    unittest.main()
