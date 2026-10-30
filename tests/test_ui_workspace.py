"""UI工作台重排回归：所有入口仍存在；不触发真实联网或生产数据。"""
import unittest
import tkinter as tk
from tkinter import ttk
from ui import RiskApp
from test_support import TestDirectory


class WorkspaceRedesignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TestDirectory()
        self.app = RiskApp(self.tmp.name)
        self.app.withdraw()
        self.app.controller.settings['wechat']['auto_start'] = False

    def tearDown(self):
        self.app.pool.shutdown(wait=True)
        self.app.close()
        self.tmp.cleanup()

    def test_workspaces_keep_core_controls_and_exports(self):
        nb = self.app.workspace_tabs
        labels = [nb.tab(tab, 'text').strip() for tab in nb.tabs()]
        self.assertEqual(labels, ['投注管理', '实时风险', '开奖结算'])
        for field in ('wechat_toggle_btn', 'ai_toggle_btn', 'history_button',
                      'period_combo', 'input', 'records_tree', 'risks_tree',
                      'sources', 'plan', 'copy_button', 'draw_entry', 'draw_fetch_button',
                      'draw_source_label', 'draw_summary_label', 'draw_winners'):
            self.assertTrue(getattr(self.app, field).winfo_exists(), field)
        self.assertEqual(self.app.export_menu.index('end') + 1, 4)
        for tab in nb.tabs():
            nb.select(tab)
            self.app.update_idletasks()
        self.assertEqual(self.app.workspace_tabs.index('current'), 2)

    def test_inline_winning_details_and_period_change_reset(self):
        self.app.controller.add('福彩', '012单2倍')
        self.app.refresh()
        self.app.draw_number.set('012')
        self.app.calculate_draw()
        rows = self.app.draw_winners.get_children()
        self.assertEqual(len(rows), 1)
        self.assertIn('直选', self.app.draw_winners.item(rows[0], 'values')[0])
        self.app.controller.activate_period('2026-10-21')
        self.app.period.set('2026-10-21')
        self.app.refresh()
        self.assertFalse(self.app.draw_winners.get_children())
        self.assertEqual(self.app.draw_number.get(), '')

    def test_settings_categories_and_cutoff_switch_exist(self):
        self.app.settings_dialog()
        win = next(child for child in self.app.winfo_children() if isinstance(child, tk.Toplevel))
        nb = next(child for child in win.winfo_children() if isinstance(child, ttk.Notebook))
        labels = [nb.tab(tab, 'text') for tab in nb.tabs()]
        self.assertEqual(labels, ['微信监听', 'AI识别', '计算规则', '赔率表',
                                  '自动封盘', '自定义写法', '数据管理'])
        def descendants(obj):
            for child in obj.winfo_children():
                yield child
                yield from descendants(child)
        checkboxes = [w.cget('text') for w in descendants(win) if isinstance(w, ttk.Checkbutton)]
        self.assertIn('启用每日21:14自动封盘（北京时间）', checkboxes)
        win.destroy()

    def test_small_window_minimum_and_panel_visibility(self):
        width, height = self.app.minsize()
        self.assertLessEqual(height, 700)
        self.assertLessEqual(width, 1040)
        self.app.deiconify()
        self.app.geometry(f'{width}x{height}')
        self.app.update_idletasks()
        for idx in range(3):
            self.app.workspace_tabs.select(idx)
            self.app.update_idletasks()
            self.assertGreater(self.app.workspace_tabs.winfo_height(), 190)
        self.app.withdraw()

    def test_draw_page_scrolls_at_short_height(self):
        self.app.deiconify()
        self.app.geometry('1040x650')
        self.app.workspace_tabs.select(2)
        self.app.update_idletasks()
        before = self.app.draw_canvas.yview()
        self.app.draw_canvas.yview_moveto(1.0)
        self.app.update_idletasks()
        after = self.app.draw_canvas.yview()
        self.assertGreater(after[0], before[0])
        self.assertAlmostEqual(after[1], 1.0, places=2)
        self.assertTrue(self.app.draw_winners.winfo_exists())
        self.app.withdraw()


if __name__ == '__main__':
    unittest.main()
