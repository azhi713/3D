import threading
import time
import unittest
import tkinter as tk
from tkinter import ttk
from unittest.mock import patch
from risk_engine import plan_text

from ui import RiskApp
from test_support import TestDirectory


class FakeAI:
    def __init__(self, candidate='123单3倍', error=None, gate=None):
        self.candidate = candidate
        self.error = error
        self.gate = gate
        self.connected = False
        self.started = threading.Event()

    def parse_unknown(self, lottery, text):
        self.started.set()
        if self.gate is not None and not self.gate.wait(3):
            raise RuntimeError('test gate timed out')
        if self.error:
            raise RuntimeError(self.error)
        self.connected = True
        return self.candidate, '玩法与倍数明确'


class AsyncAITests(unittest.TestCase):
    def setUp(self):
        self.tmp = TestDirectory()
        self.app = RiskApp(self.tmp.name)
        self.app.withdraw()
        self.app.ai_key = 'local-test-only'
        self.app.controller.settings['ai']['enabled'] = True
        self.gates = []

    def tearDown(self):
        for gate in self.gates:
            gate.set()
        self.app.pool.shutdown(wait=True)
        self.app.close()
        self.tmp.cleanup()

    def wait_for(self, predicate):
        deadline = time.monotonic()+4
        while time.monotonic() < deadline:
            self.app.update()
            if predicate():
                return
            time.sleep(.01)
        self.fail('界面未及时处理AI结果')

    def test_response_updates_saved_status_income_and_visible_risks(self):
        self.app.ai = FakeAI()
        record = self.app.controller.add('福彩','123来三单')
        self.app.queue_ai(record)
        self.assertEqual(self.app.store.get(record.record_id).status,'AI识别中')
        self.wait_for(lambda: self.app.store.get(record.record_id).status=='已识别')
        self.assertEqual(self.app.snapshot.principal,600)
        self.assertEqual(self.app.snapshot.pending,0)
        self.assertEqual(self.app.snapshot.payouts['123'],5400)
        self.assertIn('123',self.app.risks_tree.get_children())
        self.assertFalse(self.app.ai_busy)

    def test_settings_save_and_new_window_restores_key_and_model(self):
        self.app.controller.settings['ai']['enabled'] = False
        self.app.settings_dialog()
        self.app.update_idletasks()
        win = next(child for child in self.app.winfo_children() if isinstance(child,tk.Toplevel))
        def descend(parent):
            for child in parent.winfo_children():
                yield child
                yield from descend(child)
        for label,new_value in [('API Key（加密保存）','ui-test-key'),('模型名称','ui-test-model')]:
            label_widget = next(w for w in descend(win) if isinstance(w,ttk.Label) and w.cget('text')==label)
            field = next(w for w in label_widget.master.winfo_children() if isinstance(w,ttk.Entry))
            field.delete(0,'end')
            field.insert(0,new_value)
        save = next(w for w in descend(win) if isinstance(w,ttk.Button) and w.cget('text')=='保存并重算')
        save.invoke()
        self.assertEqual(self.app.store.load_settings()['ai']['api_key'],'ui-test-key')
        self.app.close()
        self.app = RiskApp(self.tmp.name)
        self.app.withdraw()
        self.assertEqual(self.app.ai_key,'ui-test-key')
        self.assertEqual(self.app.ai.api_key,'ui-test-key')
        self.assertEqual(self.app.ai.config['model'],'ui-test-model')

    def test_displayed_plan_and_clipboard_use_direct_word(self):
        self.app.controller.add('福彩','715直选24倍')
        self.app.refresh()
        with patch.object(self.app,'clipboard_clear'), patch.object(self.app,'clipboard_append') as append:
            self.app.copy_plan()
        copied = append.call_args.args[0]
        self.assertEqual(copied,plan_text(self.app.snapshot.risks))
        self.assertIn('715直选24倍',copied)
        self.assertNotIn('x',copied)

    def test_error_and_invalid_response_do_not_get_stuck_or_counted(self):
        for service in (FakeAI(error='AI接口返回HTTP 401'),FakeAI(candidate='123单0倍')):
            self.app.ai = service
            record = self.app.controller.add('福彩','123来三单')
            self.app.queue_ai(record)
            self.wait_for(lambda: not self.app.ai_busy)
            saved = self.app.store.get(record.record_id)
            self.assertEqual(saved.status,'待确认')
            self.assertTrue('401' in saved.note or '校验' in saved.note)
            self.assertEqual(self.app.snapshot.principal,0)

    def test_edit_while_ai_running_never_overwrites_edit(self):
        gate = threading.Event()
        self.gates.append(gate)
        service = FakeAI(gate=gate)
        self.app.ai = service
        record = self.app.controller.add('福彩','123来三单')
        self.app.queue_ai(record)
        self.assertTrue(service.started.wait(1))
        self.app.controller.edit(self.app.store.get(record.record_id),'456单2倍')
        self.app.refresh()
        gate.set()
        self.wait_for(lambda: not self.app.ai_busy)
        saved = self.app.store.get(record.record_id)
        self.assertEqual(saved.raw_text,'456单2倍')
        self.assertEqual(saved.bets[0].selection,'456')
        self.assertEqual(self.app.snapshot.payouts['123'],0)
        self.assertEqual(self.app.snapshot.payouts['456'],3600)

    def test_other_bets_continue_calculating_while_ai_waits(self):
        gate = threading.Event()
        self.gates.append(gate)
        self.app.ai = FakeAI(gate=gate)
        record = self.app.controller.add('福彩','123来三单')
        self.app.queue_ai(record)
        self.app.controller.add('福彩','456单5倍')
        self.app.refresh()
        self.assertEqual(self.app.snapshot.principal,1000)
        self.assertEqual(self.app.snapshot.pending,1)
        self.assertEqual(self.app.snapshot.payouts['456'],9000)
        gate.set()
        self.wait_for(lambda: not self.app.ai_busy)
        self.assertEqual(self.app.snapshot.principal,1600)
        self.assertEqual(self.app.snapshot.pending,0)

    def test_edited_unknown_gets_a_new_ai_request_after_stale_reply(self):
        gate = threading.Event()
        self.gates.append(gate)
        self.app.ai = FakeAI(candidate='123单3倍',gate=gate)
        record = self.app.controller.add('福彩','123来三单')
        self.app.queue_ai(record)
        latest = self.app.store.get(record.record_id)
        self.app.controller.edit(latest,'456来两单')
        self.app.ai = FakeAI(candidate='456单2倍')
        self.app.queue_ai(latest)
        gate.set()
        self.wait_for(lambda: not self.app.ai_busy and self.app.store.get(record.record_id).status=='已识别')
        saved = self.app.store.get(record.record_id)
        self.assertEqual(saved.raw_text,'456来两单')
        self.assertEqual(saved.ai_candidate,'456单2倍')
        self.assertEqual(self.app.snapshot.payouts['123'],0)
        self.assertEqual(self.app.snapshot.payouts['456'],3600)


if __name__ == '__main__':
    unittest.main()
