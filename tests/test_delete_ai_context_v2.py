"""新增测试：批量删除无残留；AI全原文语境、完整回填与自动二次验证。"""
import unittest
import tkinter as tk
from unittest.mock import patch
from controller import Controller
from data_store import DataStore
from ui import RiskApp
from test_support import TestDirectory


class DeleteAndAIContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TestDirectory()
        self.store = DataStore(self.tmp.name)
        self.controller = Controller(self.store)
    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_delete_many_transaction_and_no_ai_resurrection(self):
        records = [self.controller.add('福彩',f'{n:03d}单1倍') for n in range(10,30)]
        self.assertEqual(len(self.controller.records()),20)
        removed = self.store.delete_many([r.record_id for r in records])
        self.assertEqual(removed,20)
        self.assertEqual(self.controller.records(),[])
        self.assertEqual(self.store.delete_many([r.record_id for r in records]),0)
        self.assertFalse(self.controller.apply_ai_candidate(records[0].record_id,0,'010单1倍'))
        self.assertEqual(Controller(self.store).records(),[])
        self.assertEqual(self.controller.analysis('福彩').principal,0)
        count = self.store.db.execute("SELECT COUNT(*) FROM audit WHERE action='删除'").fetchone()[0]
        self.assertEqual(count,20)

    def test_context_for_partial_does_not_replace_known_bets(self):
        raw='293单1倍，123再来一注，505组1倍'
        rec=self.controller.add('福彩',raw)
        self.assertEqual(rec.status,'部分识别',rec.note)
        self.assertIn('293单1倍', self.controller.ai_request_text(rec))
        self.assertIn('123再来一注',self.controller.ai_request_text(rec))
        self.assertTrue(self.controller.apply_ai_candidate(rec.record_id,0,
             '293单1倍，123单1倍，505组1倍'),self.store.get(rec.record_id).note)
        stored=self.store.get(rec.record_id)
        self.assertEqual(stored.status,'已识别')
        self.assertEqual(len(stored.bets),3)
        self.assertEqual([b.selection for b in stored.bets],['293','505','123'])
        self.assertEqual(stored.computed_amount_yuan,6)
        self.assertEqual([b.parse_source for b in stored.bets],['local','local','ai_assisted'])

    def test_candidates_cannot_invent_bet_or_twice_count_confirmed(self):
        raw='293单1倍，123再来一注，505组1倍'
        rec=self.controller.add('福彩',raw)
        self.assertFalse(self.controller.apply_ai_candidate(rec.record_id,0,
                    '293单1倍，777单1倍，505组1倍'))
        self.assertEqual(self.controller.analysis('福彩').principal,400)
        # 模型只回传原来两笔，不能把疑难片段标为已完成。
        rec2=self.controller.add('福彩',raw)
        self.assertFalse(self.controller.apply_ai_candidate(rec2.record_id,0,
                    '293单1倍，505组1倍'))
        self.assertEqual(len(self.store.get(rec2.record_id).bets),2)

    def test_illegal_ai_and_lone_title_blocked(self):
        for raw in ('555转圈打1倍','222组1倍','4','定位'):
            rec=self.controller.add('福彩',raw)
            self.assertEqual(self.controller.ai_request_text(rec),'')
            self.assertFalse(self.controller.apply_ai_candidate(rec.record_id,0,'123单1倍'))

    def test_full_position_context_instead_of_header_only(self):
        raw='定位\n百069\n十568\n个14\n来8倍'
        rec=self.controller.add('福彩',raw)
        self.assertIn('百069',self.controller.ai_request_text(rec))
        self.assertTrue(self.controller.apply_ai_candidate(rec.record_id,0,
            '百069十568个14定位8倍'),self.store.get(rec.record_id).note)
        self.assertEqual(self.store.get(rec.record_id).bets[0].principal,288)


class DeleteWidgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TestDirectory()
        self.app=RiskApp(self.tmp.name)
        self.app.withdraw()
    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def test_select_all_delete_and_canvas_scroll_reset(self):
        for n in range(100,118):
            self.app.controller.add('福彩',f'{n:03d}单1倍')
        self.app.refresh()
        self.app.records_tree.select_all()
        self.assertEqual(len(self.app.records_tree.selection()),18)
        self.app.records_tree.canvas.yview_moveto(1.0)
        with patch('ui.messagebox.askyesno',return_value=True):
            self.app.delete_selected()
        self.assertEqual(self.app.controller.records(),[])
        self.assertEqual(self.app.records_tree.get_children(),())
        self.assertEqual(self.app.records_tree.selection(),())
        self.assertEqual(self.app.records_tree.canvas.yview()[0],0)
        self.assertIn('今日0条',self.app.records_summary.cget('text'))
        self.app.controller.add('福彩','009单1倍')
        self.app.refresh()
        self.assertEqual(len(self.app.records_tree.get_children()),1)
        self.assertEqual(self.app.records_tree.canvas.yview()[0],0)

    def test_deleted_ai_event_does_not_come_back(self):
        rec=self.app.controller.add('福彩','123来一单')
        self.app.refresh()
        self.app.records_tree.select_all()
        with patch('ui.messagebox.askyesno',return_value=True):
            self.app.delete_selected()
        self.app.events.put(('ai',rec.record_id,0,'123单1倍',''))
        self.app.pump()
        self.assertEqual(self.app.controller.records(),[])
        self.assertEqual(self.app.records_tree.get_children(),())

if __name__=='__main__':
    unittest.main()

class AIAutoRetryUITests(unittest.TestCase):
    class DeferredPool:
        def __init__(self):
            self.jobs=[]
        def submit(self, work):
            self.jobs.append(work)
        def shutdown(self, wait=False, cancel_futures=False):
            return

    class SequenceAI:
        def __init__(self, results):
            self.results=list(results)
            self.sent=[]
        def parse_unknown(self, lottery, text):
            self.sent.append((lottery,text))
            return self.results.pop(0), '标准化完成'

    def setUp(self):
        self.tmp=TestDirectory()
        self.app=RiskApp(self.tmp.name)
        self.app.withdraw()
        self.app.pool.shutdown(wait=True)
        self.app.pool=self.DeferredPool()
        self.app.controller.settings['ai']['enabled']=True
        self.app.controller.settings['ai']['model']='mock'
        self.app.ai_key='test-only'
    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def test_invalid_first_then_valid_second_auto_repair(self):
        self.app.ai=self.SequenceAI(['777单1倍','123单1倍'])
        rec=self.app.controller.add('福彩','123来一单')
        self.app.queue_ai(rec)
        self.assertEqual(len(self.app.pool.jobs),1)
        self.app.pool.jobs[0]()
        self.app.pump()
        self.assertEqual(len(self.app.pool.jobs),2)
        mid=self.app.store.get(rec.record_id)
        self.assertEqual(mid.status,'AI识别中')
        self.assertEqual(sum(h.get('action')=='auto_repair' for h in mid.ai_history),1)
        self.app.pool.jobs[1]()
        self.app.pump()
        final=self.app.store.get(rec.record_id)
        self.assertEqual(final.status,'已识别',final.note)
        self.assertEqual([b.selection for b in final.bets],['123'])
        self.assertEqual(final.computed_amount_yuan,2)
        self.assertIn('上次本地复核拒绝原因',self.app.ai.sent[1][1])

    def test_two_invalid_candidates_stop_without_loop(self):
        self.app.ai=self.SequenceAI(['777单1倍','888单1倍'])
        rec=self.app.controller.add('福彩','123来一单')
        self.app.queue_ai(rec)
        self.app.pool.jobs[0]();self.app.pump()
        self.app.pool.jobs[1]();self.app.pump()
        self.assertEqual(len(self.app.pool.jobs),2)
        saved=self.app.store.get(rec.record_id)
        self.assertEqual(saved.status,'待确认')
        self.assertIn('AI复核未通过',saved.note)
        self.assertEqual(self.app.controller.analysis('福彩').principal,0)

    def test_manual_ai_allowed_even_if_auto_toggle_off(self):
        self.app.controller.settings['ai']['enabled']=False
        self.app.ai=self.SequenceAI(['123单1倍'])
        rec=self.app.controller.add('福彩','123来一单')
        self.app.queue_ai(rec,force=True)
        self.app.pool.jobs[0]();self.app.pump()
        self.assertEqual(self.app.store.get(rec.record_id).status,'已识别')
