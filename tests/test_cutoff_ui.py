"""设置页21:14开关UI测试，需要有可用桌面环境（Windows或Xvfb）。"""
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from ui import RiskApp
from test_support import TestDirectory


class CutoffCheckboxUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = TestDirectory()
        try:
            self.app = RiskApp(self.tmp.name)
        except tk.TclError as exc:
            self.tmp.cleanup()
            self.skipTest('无图形显示环境：'+str(exc))
        self.app.withdraw()

    def tearDown(self):
        if hasattr(self, 'app'):
            self.app.pool.shutdown(wait=True)
            self.app.close()
            self.tmp.cleanup()

    @staticmethod
    def descend(parent):
        for child in parent.winfo_children():
            yield child
            yield from CutoffCheckboxUITests.descend(child)

    def test_checkbox_default_off_save_on_and_off(self):
        self.assertFalse(self.app.controller.deadline_enabled())
        for enabled in (True, False):
            self.app.settings_dialog()
            self.app.update_idletasks()
            dialog = next(w for w in self.app.winfo_children() if isinstance(w, tk.Toplevel))
            controls = list(self.descend(dialog))
            checkbox = next(w for w in controls if isinstance(w, ttk.Checkbutton)
                            and '21:14自动封盘' in w.cget('text'))
            self.assertEqual(bool(checkbox.instate(['selected'])), not enabled)
            checkbox.invoke()
            save = next(w for w in controls if isinstance(w, ttk.Button) and w.cget('text')=='保存并重算')
            with patch('ui.messagebox.askyesno', return_value=True):
                save.invoke()
            self.assertEqual(self.app.controller.deadline_enabled(), enabled)
            self.assertEqual(self.app.store.load_settings()['deadline']['enabled'], enabled)
        self.assertIn('截止：关', self.app.status.cget('text'))


if __name__ == '__main__':
    unittest.main()
