import json
import unittest
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'modules'))
# The end-to-end harness can also use full project modules when present.
from ai_pipeline_v266 import decode_structured,reconcile
from models import Bet,cents

class Parser:
    @staticmethod
    def _multiplier(x):
        v=Decimal(x)
        if not 0<v<=1000000 or v.as_tuple().exponent < -2:raise ValueError('倍数最多两位小数')
        return float(v)
    @staticmethod
    def _make(lottery,raw,play,code,mult):
        if play not in ('直选','组六（组选）','组三（组选）','三码定位','双飞'):
            raise ValueError('非法玩法：'+play)
        if play=='直选' and not (len(code)==3 and code.isdigit()):raise ValueError('非法直选')
        if play=='组六（组选）' and not (len(code)==3 and len(set(code))==3):raise ValueError('非法组六')
        if play=='组三（组选）' and not (len(code)==3 and len(set(code))==2):raise ValueError('非法组三')
        if play=='双飞' and not (len(code)==2 and code.isdigit()):raise ValueError('非法双飞')
        cost=Decimal(2)
        if play=='三码定位':
            parts=dict(x.split(':') for x in code.split('|'))
            if set(parts)!={'百','十','个'}:raise ValueError('定位位置错误')
            cost=Decimal(2)
            for v in parts.values():cost*=len(v)
        return Bet(play,code,float(mult),float(cost*Decimal(str(mult))),raw)

P=Parser()

def c(kind,codes=None,**kwargs):
    value={'segment_id':'S1','bet_type':kind,
           'bet_multiples':1,'amount_kind':'multiplier','multiple_type':'explicit'}
    if codes is not None:value['ticket_codes']=codes
    value.update(kwargs)
    return value

def run(source,items,local=(),pending=None,needed=False):
    payload=json.dumps({'candidate_bets':items,'needs_review':needed,'uncertainties':[]},ensure_ascii=False)
    return reconcile(source,payload,P,'福彩',list(local),pending if pending is not None else [{'start':0,'end':len(source),'text':source,'reason':'未知'}])

class CandidateTests(unittest.TestCase):
    def test_chinese_multiples_full(self):
        source='815十单十组福40'
        items=[c('直选',['815'],bet_multiples=10), c('组六（组选）',['815'],bet_multiples=10)]
        out=run(source,items)
        self.assertTrue(out.complete,out.detail)
        self.assertEqual(sum(cents(x.principal) for x in out.bets),4000)
        self.assertFalse(out.pending)

    def test_unexplained_always_pending(self):
        source='福17飞10'
        out=run(source,[c('双飞',['17'],bet_multiples=10)])
        self.assertFalse(out.complete)
        self.assertTrue(out.pending)

    def test_fake_code_rejected(self):
        source='福815直2倍'
        out=run(source,[c('直选',['999'],bet_multiples=2)])
        self.assertFalse(out.complete)
        self.assertEqual(len(out.bets),0)
        self.assertIn('号码',out.detail)

    def test_partial_valid_kept(self):
        source='815直2倍，999直3倍'
        items=[c('直选',['815'],bet_multiples=2), c('直选',['123'],bet_multiples=3)]
        out=run(source,items)
        self.assertFalse(out.complete)
        self.assertEqual(len(out.bets),1)
        self.assertEqual(out.bets[0].selection,'815')
        self.assertTrue(out.pending)

    def test_not_automatically_clear_group_wager(self):
        source='福527 653直1组1'
        out=run(source,[c('直选',['527','653'])])
        self.assertFalse(out.complete)

    def test_positions_two_independent_blocks(self):
        source='福定位\n百17\n十56\n个56\n打两倍共32\n百17\n十56\n个6\n打两倍16合计48'
        items=[c('三码定位',None,segment_id=['S1','S2','S3','S4','S5'],
                 position_sets={'百':'17','十':'56','个':'56'},bet_multiples=2),
               c('三码定位',None,segment_id=['S6','S7','S8','S9'],
                 position_sets={'百':'17','十':'56','个':'6'},bet_multiples=2)]
        out=run(source,items)
        self.assertTrue(out.complete,out.detail)
        self.assertEqual(sum(cents(x.principal) for x in out.bets),4800)

    def test_explicit_multiple_not_overwritten_to_match_total(self):
        source='福815直2倍合计2元'
        out=run(source,[c('直选',['815'],bet_multiples=1)])
        self.assertFalse(out.complete)
        self.assertFalse(out.bets)

    def test_duplicate_same_number_in_separate_purchase(self):
        source='815直选2倍\n815直选2倍'
        items=[c('直选',['815'],segment_id='S1',bet_multiples=2),
               c('直选',['815'],segment_id='S2',bet_multiples=2)]
        out=run(source,items)
        self.assertTrue(out.complete,out.detail)
        self.assertEqual(len(out.bets),2)

    def test_amount_per_group_total_will_not_fabricate_multiples(self):
        source='640-660-600-606福1直1组打1米 8'
        items=[c('直选',['640','660','600','606'],bet_multiples=None,
                 declared_amount_yuan=1,amount_kind='amount',multiple_type='missing')]
        out=run(source,items)
        self.assertFalse(out.complete)

if __name__=='__main__':unittest.main()

class BigBatchTests(unittest.TestCase):
    def test_real_515_plus_extra_338(self):
        source=(Path(__file__).parent/'sample_515.txt').read_text(encoding='utf-8')
        codes=source.splitlines()[0].split(' ')
        self.assertEqual(len(codes),515)
        items=[c('直选',codes,segment_id=['S1','S2'],bet_multiples=1),
               c('直选',['338'],segment_id='S3',bet_multiples=5)]
        out=run(source,items)
        self.assertTrue(out.complete,out.detail)
        self.assertEqual(len(out.bets),516)
        self.assertEqual(sum(cents(x.principal) for x in out.bets),104000)

class ReceiptAndSafetyTests(unittest.TestCase):
    def test_colloquial_one_yuan_each_in_large_mixed_order(self):
        numbers='716 176 649 978 290 668 772 929 784 815 826 866 894'.split()
        source=('福'+'-'.join(numbers)+'一米直一米组，039-278组选，共30米')
        items=[]
        for code in numbers:
            items.append(c('直选',[code],bet_multiples=None,
                           declared_amount_yuan=1,amount_kind='amount',multiple_type='missing'))
            play='组三（组选）' if len(set(code))==2 else '组六（组选）'
            items.append(c(play,[code],bet_multiples=None,
                           declared_amount_yuan=1,amount_kind='amount',multiple_type='missing'))
        for code in ('039','278'):
            items.append(c('组六（组选）',[code],bet_multiples=1))
        out=run(source,items)
        self.assertTrue(out.complete,out.detail)
        self.assertEqual(sum(cents(b.principal) for b in out.bets),3000)
        self.assertEqual(len(out.bets),28)

    def test_model_uncertain_does_not_force_success(self):
        source='815直选2倍'
        result=run(source,[c('直选',['815'],bet_multiples=2)],needed=True)
        self.assertTrue(result.pending)
        self.assertFalse(result.complete)
