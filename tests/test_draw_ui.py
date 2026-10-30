"""开奖界面：手动试算、模拟官网回填、切换期次清理旧号码。"""
import time
import gc
import tkinter as tk
import unittest
from unittest.mock import patch
from draw_service import DrawResult
from ui import RiskApp
from test_support import TestDirectory

class DrawUiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TestDirectory()
        self.app = RiskApp(self.tmp.name)
        self.app.withdraw()
        self.app.controller.settings['wechat']['auto_start'] = False

    def tearDown(self):
        self.app.pool.shutdown(wait=True)
        self.app.close()
        self.app = None
        gc.collect()  # Tk对象须在主线程回收，避免测试后台线程的析构触发Tcl错误。
        self.tmp.cleanup()

    def test_manual_draw_preview_and_detail_dialog(self):
        self.app.controller.add('福彩','012单1倍')
        self.app.refresh()
        self.app.draw_number.set('012')
        self.app.calculate_draw()
        self.assertEqual(self.app._draw_preview['payout_yuan'],1800)
        self.assertIn('1800',self.app.draw_summary_label.cget('text'))
        with patch('ui.messagebox.showwarning') as warn:
            self.app.save_draw_settlement()
        self.assertTrue(warn.called)
        self.assertIsNone(self.app.store.load_settlement(self.app.controller.period,'福彩'))
        self.app.show_draw_details()
        popup = next(widget for widget in self.app.winfo_children() if isinstance(widget,tk.Toplevel))
        self.assertIn('中奖明细',popup.title())
        popup.destroy()

    def test_fetched_draw_and_period_switch_reset(self):
        period=self.app.controller.period
        draw=DrawResult('福彩', period, '26289', '009','官网模拟','https://www.scflcp.com.cn/3dinfo.jhtml')
        with patch('ui.fetch_draw',return_value=draw):
            self.app.lookup_draw()
            deadline=time.monotonic()+3
            while time.monotonic()<deadline and self.app._draw_online is None:
                if not self.app.events.empty():
                    event = self.app.events.get_nowait()
                    if event[0] == 'draw_lookup':
                        self.app._apply_draw_lookup(*event[1:])
                self.app.update_idletasks()
                time.sleep(.01)
        self.assertIsNotNone(self.app._draw_online)
        self.assertEqual(self.app.draw_number.get(),'009')
        self.assertIn('官网',self.app.draw_source_label.cget('text'))
        self.app.controller.activate_period('2026-10-28')
        self.app.period.set('2026-10-28')
        self.app.refresh()
        self.assertEqual(self.app.draw_number.get(),'')
        self.assertIsNone(self.app._draw_online)

    def test_online_failure_keeps_manual_value_without_false_verified(self):
        self.app.draw_number.set('456')
        with patch('ui.fetch_draw',side_effect=RuntimeError('连接超时')):
            self.app.lookup_draw()
            deadline=time.monotonic()+3
            while time.monotonic()<deadline and self.app._draw_busy:
                if not self.app.events.empty():
                    event = self.app.events.get_nowait()
                    if event[0] == 'draw_lookup':
                        self.app._apply_draw_lookup(*event[1:])
                self.app.update_idletasks()
                time.sleep(.01)
        self.assertFalse(self.app._draw_busy)
        self.assertEqual(self.app.draw_number.get(),'456')
        self.assertIsNone(self.app._draw_online)
        self.assertIn('连接超时',self.app.draw_source_label.cget('text'))

if __name__=='__main__':unittest.main()
