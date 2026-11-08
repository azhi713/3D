# -*- coding: utf-8 -*-
"""Clipboard tests use actual Tk widgets; no live account or application data."""
from copy import deepcopy
from types import SimpleNamespace
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch
from models import RiskRow
from dashboard_v282 import DashboardV282Mixin, copy_payload
from dashboard_v280 import LABELS
from dashboard_metrics_v280 import CODES
from hedge_edit_v276 import parse_hedge_text


class CopyFunctions(unittest.TestCase):
    def test_numbers_sorted_and_zero_padded(self):
        rows=[RiskRow(c,0,0,m) for c,m in [('935',3),('002',1),('013',2),('001',0)]]
        text,n,cost=copy_payload(rows,2,'福彩',True)
        self.assertEqual(text,'002 013 935\n\n合计：3注');self.assertEqual((n,cost),(3,1200))

    def test_duplicates_only_deduped_in_number_view(self):
        rows=[RiskRow('002',0,0,1),RiskRow('002',0,0,2)]
        text,n,cost=copy_payload(rows,2,'福彩',True)
        self.assertEqual((n,cost),(1,600));self.assertEqual(text,'002\n\n合计：1注')
        full,_,_=copy_payload(rows,2,'福彩')
        parsed,amount=parse_hedge_text(full,'福彩',2)
        self.assertEqual(sum(r.plan_multiplier for r in parsed),3);self.assertEqual(amount,600)

    def test_only_positive_multiples(self):
        with self.assertRaises(ValueError):copy_payload([RiskRow('002',0,0,0)],2,'福彩')

    def test_invalid_code_not_copied(self):
        with self.assertRaises(ValueError):copy_payload([RiskRow('2',0,0,1)],2,'福彩')

    def test_draft_marker_preserved(self):
        text,n,_=copy_payload([RiskRow('002',0,0,1)],2,'福彩',True,True)
        self.assertTrue(text.startswith('[仅供人工核对'))


class Harness(DashboardV282Mixin,tk.Tk):
    def __init__(self):
        tk.Tk.__init__(self)
        self.withdraw()
        self.geometry('650x250')
        self._closing=False;self._plan_mode='manual';self._plan_manual_mode=False
        self._plan_manual_text=None;self._plan_edit_timer=None;self._copy_timer=None
        self._v282_copy_timer=None;self._v282_copy_ready=True
        self._plan_edit_error='';self._plan_rows=[]
        self.lottery=tk.StringVar(self,value='福彩')
        self.plan_mode_choice=tk.StringVar(self,value='人工方案')
        self.controller=SimpleNamespace(period='2026-11-07',settings={
          'direct_unit_cost':{'福彩':2},'external_direct_prize':{'福彩':1800},
          'external_leopard_direct_prize':{'福彩':1600}})
        self._draw_context=('福彩','2026-11-07');self._plan_adjustment=0
        self._risk_running=False;self._risk_next=None
        self.snapshot=SimpleNamespace(revenue=10000,payouts=dict.fromkeys(CODES,0),pending=0,
             risks=[RiskRow('999',0,0,8),RiskRow('001',0,0,1)])
        self._auto_optimizer_results={'cheap':{'bets':{'030':2,'003':1}},
          'balanced':{'bets':{'020':4,'002':3}},'profit':{'bets':{'040':6,'004':5}}}
        self._auto_optimizer_error='';self._v280_optimizer_key=self._v280_input_key()
        self._ui_state=SimpleNamespace(data={'actual_hedges':{'福彩|2026-11-07':{'rows':[['777',99]]}},
                                            'purchased_hedge_modes':{'福彩|2026-11-07':'manual'}},
                                        put_hedge_mode=lambda *a:None)
        self.plan=tk.Text(self);self.plan.pack()
        self.copy_button=ttk.Button(self,command=self.copy_plan);self.copy_button.pack()
        self.copy_current_numbers_button=ttk.Button(self,command=self.copy_current_plan_numbers);self.copy_current_numbers_button.pack()
        self.copy_risk_numbers_button=ttk.Button(self,command=self.copy_risk_numbers);self.copy_risk_numbers_button.pack()
        self._v282_copy_identity=ttk.Label(self);self._v282_copy_identity.pack()
        self._render_plan()
        self.update_idletasks()
    def _adjusted_plan_rows(self):
        return self.snapshot.risks
    def _render_plan(self):
        try:self._plan_rows=self._v280_candidate_rows(self._plan_mode);self._plan_edit_error=''
        except Exception as e:self._plan_rows=[];self._plan_edit_error=str(e)
        self._v282_refresh_copy_controls()
    def _render_risk_rows(self):pass
    def _update_financial_cards(self):self._v282_refresh_copy_controls()
    def _commit_manual_plan(self):
        self._plan_edit_timer=None;self._plan_manual_text=self.plan.get('1.0','end-1c');self._render_plan()
    def cleanup(self):
        self._closing=True
        for timer in self.tk.call('after','info'):
            self.tk.call('after','cancel',timer)
        self.destroy()


class RealClipboard(unittest.TestCase):
    def setUp(self):
        self.app=Harness()
        self.warning=patch('dashboard_v282.messagebox.showwarning').start()
    def tearDown(self):
        self.app.cleanup();patch.stopall()
    def switch(self,mode):
        self.app.plan_mode_choice.set(LABELS[mode]);self.app._switch_plan_mode()

    def test_build_installs_separate_copy_controls(self):
        from dashboard_v281 import DashboardV281Mixin
        a=self.app
        def baseline_build(app):
            box=ttk.LabelFrame(app)
            app.optimizer_compare_table=ttk.Frame(box)
            bar=ttk.Frame(box)
            app.copy_button.destroy()
            app.copy_button=ttk.Button(bar,text='一键复制')
        with patch.object(DashboardV281Mixin,'_build',baseline_build):
            DashboardV282Mixin._build(a)
        self.assertEqual(a.copy_button['text'],'复制完整方案')
        self.assertEqual(a.copy_current_numbers_button['text'],'复制当前方案号码')
        self.assertEqual(a.copy_risk_numbers_button['text'],'复制阈值号码')
        self.switch('balanced')
        a.copy_current_numbers_button.invoke()
        self.assertTrue(a.clipboard_get().startswith('002 020'))

    def test_all_four_modes_real_clipboard(self):
        a=self.app
        expected={'manual':'001 999','cheap':'003 030','balanced':'002 020','profit':'004 040'}
        for mode in ('manual','balanced','cheap','profit','manual'):
            self.switch(mode)
            self.assertTrue(a.copy_current_plan_numbers())
            self.assertTrue(a.clipboard_get().startswith(expected[mode]),(mode,a.clipboard_get()))
            self.assertTrue(a.copy_plan())
            rows,cost=parse_hedge_text(a.clipboard_get(),'福彩',2)
            self.assertEqual({r.number for r in rows},set(expected[mode].split()))

    def test_stale_cached_plan_rows_ignored(self):
        self.switch('balanced');self.app._plan_rows=[RiskRow('777',0,0,99)]
        self.assertTrue(self.app.copy_plan());self.assertNotIn('777',self.app.clipboard_get())
        self.assertIn('002',self.app.clipboard_get())

    def test_purchased_manual_not_used(self):
        before=deepcopy(self.app._ui_state.data);self.switch('balanced')
        self.app.copy_plan();self.assertNotIn('777',self.app.clipboard_get())
        self.assertEqual(self.app._ui_state.data,before)

    def test_threshold_numbers_independent(self):
        self.switch('balanced');self.app.copy_risk_numbers()
        self.assertTrue(self.app.clipboard_get().startswith('001 999'))
        self.assertNotIn('002',self.app.clipboard_get())

    def test_pending_auto_does_not_copy_manual(self):
        self.switch('balanced');self.app.clipboard_clear();self.app.clipboard_append('old clipboard')
        self.app._auto_optimizer_results={}
        self.assertFalse(self.app.copy_plan());self.assertEqual(self.app.clipboard_get(),'old clipboard')
        self.warning.assert_called_once()

    def test_stale_auto_input_rejected(self):
        self.switch('balanced');self.app.snapshot.revenue+=100
        self.assertFalse(self.app.copy_plan())

    def test_switch_scope_rejected(self):
        self.app.controller.period='2026-11-08';self.assertFalse(self.app.copy_plan())

    def test_risk_job_running_rejected(self):
        self.app._risk_running=True;self.assertFalse(self.app.copy_plan())

    def test_wrong_combobox_state_rejected(self):
        self.app.plan_mode_choice.set('均衡型');self.assertFalse(self.app.copy_plan())

    def test_invalid_manual_edit_rejected(self):
        self.app._plan_manual_mode=True;self.app._plan_manual_text='福直选3倍\n002\n未知内容'
        self.app.plan.insert('1.0',self.app._plan_manual_text)
        self.assertFalse(self.app.copy_plan())

    def test_pending_manual_edit_flushed_before_copy(self):
        a=self.app;a._plan_manual_mode=True;a.plan.delete('1.0','end');a.plan.insert('1.0','福直选4倍\n003')
        a._plan_manual_text='福直选1倍\n999';a._plan_edit_timer=a.after(5000,lambda:None)
        self.assertTrue(a.copy_plan());self.assertIn('003',a.clipboard_get());self.assertNotIn('999',a.clipboard_get())


    def test_manual_edit_before_debounce_not_stale(self):
        a=self.app;a._plan_manual_mode=True
        a._plan_manual_text='福直选1倍\n999'
        a.plan.delete('1.0','end');a.plan.insert('1.0','福直选4倍\n003')
        self.assertTrue(a.copy_plan());self.assertIn('003',a.clipboard_get());self.assertNotIn('999',a.clipboard_get())

    def test_buttons_disabled_during_recompute(self):
        self.switch('balanced');a=self.app;a._auto_optimizer_results={};a._v282_refresh_copy_controls()
        self.assertIn('disabled',a.copy_button.state());self.assertIn('disabled',a.copy_current_numbers_button.state())

    def test_feedback_resets_to_explicit_name(self):
        self.app._reset_risk_copy_label();self.assertEqual(self.app.copy_risk_numbers_button['text'],'复制阈值号码')
        self.app._v282_restore_copy_labels();self.assertEqual(self.app.copy_button['text'],'复制完整方案')

if __name__=='__main__':unittest.main(verbosity=2)
