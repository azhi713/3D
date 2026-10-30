"""V2.5.6：独立价格/赔付期望、证据防伪及状态边界。"""
import json,unittest,socket
from unittest.mock import patch
from decimal import Decimal
from parser import BetParser
from risk_engine import RiskEngine
from ai_candidates import decode_candidate
from bet_evidence import validate_evidence,independent_source_bets
from controller import Controller
from data_store import DataStore
from test_support import TestDirectory
from ai_service import AIService,AIError

class CatalogTests(unittest.TestCase):
    def test_every_sum_all_1000_outcomes_and_both_lotteries(self):
        prices=(8000,3000,1500,900,600,430,320,250,200,160,140,130,120,120,120,120,130,140,160,200,250,320,430,600,900,1500,3000,8000)
        for lottery in ('福彩','体彩'):
            for value,prize in enumerate(prices):
                b=BetParser().parse(lottery,f'和值{value} 2倍')[0][0]
                self.assertEqual(b.principal,20)
                engine=RiskEngine()
                table=engine.calculate(lottery,[b],10**12,1800)[2]
                for n in range(1000):
                    code=f'{n:03}'
                    expected=prize*2 if sum(map(int,code))==value else 0
                    self.assertEqual(engine.payout_for(lottery,b,code),expected)
                    self.assertEqual(table[code],expected)

    def test_all_dantuo_tiers_nonoverlap_and_outcome_shape(self):
        for shape,prizes in ((6,(1500,500,250,150,100,70,50,40)),(3,(750,500,370,300,250,200,185,160))):
            for size,prize in enumerate(prizes,2):
                text='0胆'+('123456789'[:size])+f'拖组{shape}2倍'
                bets,_,error=BetParser().parse('福彩',text)
                self.assertFalse(error,text);b=bets[0]
                self.assertEqual(b.principal,20)
                for n in range(1000):
                    code=f'{n:03}';digits=set(code)
                    hit=(len(digits)==shape//3+1 and '0' in digits and (digits-{'0'})<=set('123456789'[:size]))
                    self.assertEqual(RiskEngine().payout_for('福彩',b,code),prize*2 if hit else 0)
        for raw in ('0胆012拖组六10元','0胆112拖组三10元','01胆234拖10元'):
            self.assertFalse(BetParser().parse('福彩',raw)[0])

    def test_permutation_counts_and_old_pool_separation(self):
        for raw,price,winning in [('148组六复式1倍',12,1800),('1482组六复式1倍',48,1800),
                                  ('51组三复式1倍',12,1800),('1482组六1倍',10,370),('51二码组三1倍',10,1500)]:
            b=BetParser().parse('福彩',raw)[0][0]
            code='148' if '148' in raw else '551'
            self.assertEqual(b.principal,price)
            self.assertEqual(RiskEngine().payout_for('福彩',b,code),winning)

    def test_bare_unit_full_context_and_explicit_multiplier(self):
        for raw,expected in [('123直22倍\n32-14-双飞各20\n合计84',84),
                             ('123直22倍\n32-14-双飞各20倍\n合计84',444),
                             ('福0789组六50组三30合计80',80),('293单',2)]:
            bets,_,error=BetParser().parse('福彩',raw)
            self.assertFalse(error,raw)
            self.assertEqual(sum(b.principal for b in bets),expected)
        self.assertFalse(BetParser().parse('福彩','32-14-双飞各20')[0])

class CandidateTests(unittest.TestCase):
    def candidate(self,raw,code='420',multiple=2,start=0,end=None):
        end=len(raw) if end is None else end
        return json.dumps({'candidate_bets':[{'lottery':'福彩','bet_type':'直选',
                          'ticket_codes':[code],'bet_multiples':multiple,'amount_kind':'multiplier',
                          'evidence_text':raw[start:end],'source_start':start,'source_end':end}],
                          'needs_review':False},ensure_ascii=False)

    def test_structured_candidate_and_independent_source_proof(self):
        raw='请把420各买2倍直选'
        parser=BetParser()
        with patch.object(BetParser,'parse',return_value=([],'','模拟公共解析器未支持口语')):
            bets,_,error=decode_candidate(self.candidate(raw),parser,'福彩',raw)
            self.assertFalse(error)
            self.assertEqual(bets[0].principal,4)
            self.assertTrue(validate_evidence(raw,'候选',bets,parser)[0])

    def test_invented_code_quantity_offset_and_duplicates_rejected(self):
        raw='请把420各买2倍直选'
        for candidate in (self.candidate(raw,'421'),self.candidate(raw,multiple=20),self.candidate(raw,start=1)):
            self.assertFalse(decode_candidate(candidate,BetParser(),'福彩',raw)[0])
        obj=json.loads(self.candidate(raw));obj['candidate_bets']*=2
        self.assertFalse(decode_candidate(json.dumps(obj),BetParser(),'福彩',raw)[0])

    def test_total_label_never_proves_a_number(self):
        raw='420直2倍 合计108'
        bets=BetParser().parse('福彩','108直2倍')[0]
        self.assertFalse(validate_evidence(raw,'108直2倍',bets)[0])

    def test_read_timeout_is_unknown_billing_without_hidden_retry(self):
        class Response:
            status=200
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):raise socket.timeout('read timed out')
        service=AIService({'base_url':'https://example.invalid','model':'test','timeout':60},'synthetic-only')
        with patch('ai_service.urllib.request.urlopen',return_value=Response()) as request:
            with self.assertRaisesRegex(AIError,'可能已经产生模型费用'):
                service.parse_unknown('福彩','123来一注')
            self.assertEqual(request.call_count,1)
        self.assertEqual(service.last_metrics['timeout_stage'],'read_body')
        self.assertEqual(service.last_metrics['billing_state'],'unknown_after_send')

    def test_full_context_resolves_bare_doublefly_amount(self):
        raw='123直22倍\n32-14-双飞各20\n合计84'
        evidence='32-14-双飞各20';start=raw.index(evidence)
        obj={'candidate_bets':[{'bet_type':'双飞','digit_set':code,'amount_kind':'amount',
                              'declared_amount_yuan':20,'source_start':start,'source_end':start+len(evidence),
                              'evidence_text':evidence} for code in ('32','14')]}
        bets,_,error=decode_candidate(json.dumps(obj),BetParser(),'福彩',raw)
        self.assertFalse(error)
        self.assertEqual([b.multiplier for b in bets],[2,2])
        for b in obj['candidate_bets']:
            b.update(amount_kind='multiplier',bet_multiples=20)
        self.assertFalse(decode_candidate(json.dumps(obj),BetParser(),'福彩',raw)[0])

    def test_rule_pending_keeps_risk_incomplete_and_settlement_blocked(self):
        tmp=TestDirectory()
        try:
            store=DataStore(tmp.name)
            try:
                ctrl=Controller(store);ctrl.add('福彩','0胆123拖组三10元')
                self.assertEqual(ctrl.analysis('福彩').pending,1)
                preview=ctrl.preview_draw('福彩','001')
                self.assertFalse(preview['complete'])
                self.assertTrue(any('胆拖' in x for x in preview['blockers']))
            finally:store.close()
        finally:tmp.cleanup()

    def test_structured_partial_merge_preserves_other_unknown_spans(self):
        tmp=TestDirectory();store=DataStore(tmp.name)
        try:
            ctrl=Controller(store)
            raw='123直1倍；请把420各买2倍直选；未知内容'
            rec=ctrl.add('福彩',raw)
            evidence='请把420各买2倍直选';start=raw.index(evidence)
            obj=json.loads(self.candidate(raw,start=start,end=start+len(evidence)))
            ctrl.apply_ai_candidate(rec.record_id,rec.revision,json.dumps(obj,ensure_ascii=False))
            saved=store.get(rec.record_id)
            self.assertEqual(saved.status,'部分识别')
            self.assertEqual(saved.computed_amount_yuan,6)
            self.assertTrue(any('未知内容' in f['text'] for f in saved.pending_fragments))
            self.assertEqual(ctrl.analysis('福彩').pending,1)
            ctrl.apply_ai_candidate(rec.record_id,rec.revision,json.dumps(obj,ensure_ascii=False))
            self.assertEqual(store.get(rec.record_id).computed_amount_yuan,6)
        finally:store.close();tmp.cleanup()

    def test_unknown_billing_survives_restart_and_requires_explicit_retry(self):
        tmp=TestDirectory();store=DataStore(tmp.name)
        try:
            ctrl=Controller(store);rec=ctrl.add('福彩','123再来一注')
            rec.ai_request_state='unknown_billing';store.update(rec)
            ctrl=Controller(store)
            saved=store.get(rec.record_id)
            self.assertEqual(ctrl.ai_request_text(saved),'')
            self.assertTrue(ctrl.ai_request_text(saved,allow_existing_candidate=True))
        finally:store.close();tmp.cleanup()

if __name__=='__main__':unittest.main()
