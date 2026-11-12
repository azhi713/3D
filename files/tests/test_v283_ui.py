"""Real Tk integration of the entire shipped dashboard adapter chain.

Uses a legacy-widget/storage/Analysis harness, not a full Windows/WeChat session.
No mock of the rendering, comparison, switching, target or clipboard methods.
Must run on an isolated display; installer never runs this on the user's desktop.
"""
import json, queue, tempfile, unittest
from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import ttk
from unittest.mock import patch
from dashboard_v283 import DashboardV283Mixin, PlanComparisonV283
from dashboard_v280 import LABELS, MODES
from dashboard_metrics_v280 import CODES, aggregate_rows, purchased_metrics
from risk_engine import plan_text, adjust_plan_rows
from risk_extension_v2734 import freeze_purchase_snapshot
from hedge_edit_v276 import parse_hedge_text
from models import RiskRow
from plan_snapshot_v283 import target_outcomes
from dashboard_v283 import _v283_format_target_numbers, _v283_loss_outcomes

class UIStateFixture:
    def __init__(self,path):
        self.path=path;self.data={'actual_hedges':{},'purchased_hedge_modes':{},'manual_hedge_plans':{},'hedge_modes':{},'scopes':{}}
        self.save()
    def save(self):self.path.write_text(json.dumps(self.data,ensure_ascii=False),'utf-8')
    def get_actual_hedge(self,*scope):return deepcopy(self.data['actual_hedges'].get('|'.join(scope)))
    def get_purchased_mode(self,*scope):return self.data['purchased_hedge_modes'].get('|'.join(scope))
    def put_purchased_plan(self,lot,period,value,mode):
        key=lot+'|'+period
        if value is None:
            self.data['actual_hedges'].pop(key,None);self.data['purchased_hedge_modes'].pop(key,None)
        else:self.data['actual_hedges'][key]=value;self.data['purchased_hedge_modes'][key]=mode
        self.save()
    def put_manual_plan(self,lot,period,value):
        if value is None:self.data['manual_hedge_plans'].pop(lot+'|'+period,None)
        else:self.data['manual_hedge_plans'][lot+'|'+period]=value
        self.save()
    def put_hedge_mode(self,lot,period,mode):self.data['hedge_modes'][lot+'|'+period]=mode;self.save()
    def put_scope(self,lot,period,draw,adjust):self.data['scopes'][lot+'|'+period]={'draw':draw,'adjustment':adjust};self.save()
    def get_manual_plan(self,lot,period):return self.data['manual_hedge_plans'].get(lot+'|'+period)
    def get_scope(self,lot,period):return self.data['scopes'].get(lot+'|'+period,{'draw':'','adjustment':0})

class LegacyWidgets(tk.Tk):
    def _build(self):
        style=ttk.Style(self);style.theme_use('clam')
        controls=ttk.Frame(self);controls.pack(fill='x')
        switches=ttk.Frame(controls);switches.pack(side='left')
        ttk.Button(switches,text='微信监听：关').pack(side='left')
        ttk.Button(switches,text='AI识别：关').pack(side='left')
        self.history_button=ttk.Button(switches,text='读取历史消息');self.history_button.pack(side='left')
        context=ttk.Frame(self);context.pack(fill='x')
        scope=ttk.Frame(context);scope.pack(side='left');ttk.Label(scope,text='福彩 2026-11-07').pack()
        exports=ttk.Frame(context);exports.pack(side='right');ttk.Button(exports,text='数据导出').pack()
        filters=ttk.Frame(context);filters.pack(side='right');ttk.Entry(filters,textvariable=self.threshold).pack()
        metrics=ttk.Frame(self);metrics.pack(fill='x');card=tk.Frame(metrics);card.pack()
        self.cards={'income':tk.Label(card,text='—')};self.cards['income'].pack()
        self._v283_distribution_host=ttk.Frame(self)
        self._v283_distribution_host.pack(fill='x')
        self.warning=ttk.Label(self,text='测试数据');self.warning.pack(fill='x')
        panel=ttk.Frame(self);panel.pack(fill='both',expand=True);panel.rowconfigure(0,weight=1)
        left=ttk.Frame(panel);left.grid(row=0,column=0,sticky='nsew')
        self.copy_risk_numbers_button=ttk.Button(left,text='复制号码',command=self.copy_risk_numbers)
        self.copy_risk_numbers_button.pack()
        box=ttk.LabelFrame(panel,text='建议补单方案');box.grid(row=0,column=1,sticky='nsew')
        self.plan=tk.Text(box);self.plan.pack()
    def _adjusted_plan_rows(self):return adjust_plan_rows(self.snapshot.risks,self._plan_adjustment) if self.snapshot else []
    def _render_risk_rows(self):self.risk_render_count+=1
    def _draw_edited(self,event=None):self._update_financial_cards()
    def lookup_draw(self):pass
    def show_draw_lookup_error(self):pass
    def _save_ui_scope(self):self._ui_state.put_scope(self.lottery.get(),self.controller.period,self.draw_number.get(),self._plan_adjustment)
    def filter_changed(self,event=None):
        if self._threshold_timer:self.after_cancel(self._threshold_timer)
        self._threshold_timer=self.after(300,self.apply_filter)
    def apply_filter(self):self._threshold_timer=None;self._render_plan();self._update_financial_cards()
    def _start_risk_job(self):self._risk_running=bool(self._risk_next);self._risk_next=None
    def _apply_risk_snapshot(self,snapshot,error=''):
        self.snapshot=snapshot;self._queue_optimizer();self._render_plan();self._update_financial_cards()
    def _sync_draw_context(self):
        self._draw_context=(self.lottery.get(),self.controller.period)
        self._plan_mode='manual';self.plan_mode_choice.set('人工方案')
        self._plan_manual_text=self._ui_state.get_manual_plan(*self._draw_context)
        self._plan_manual_mode=self._plan_manual_text is not None
        self._plan_adjustment=0;self.snapshot=None;self._auto_optimizer_results={}
        self._render_plan();self._update_financial_cards()

class Harness(DashboardV283Mixin,LegacyWidgets):
    def __init__(self, legacy_target=None):
        tk.Tk.__init__(self);self.geometry('1400x900');self._temp=tempfile.TemporaryDirectory()
        self.reported_errors=[];self.report_callback_exception=lambda *a:self.reported_errors.append(a)
        self._ui_state=UIStateFixture(Path(self._temp.name)/'state.json')
        if legacy_target is not None:
            self._ui_state.data['target_profit_yuan']=legacy_target
            self._ui_state.save()
        self._closing=False;self._plan_mode='manual';self._plan_manual_mode=True;self._plan_adjustment=0
        self._plan_edit_timer=None;self._plan_editor_block=False;self._plan_edit_error='';self._plan_rows=[]
        self._last_plan_signature=None;self._copy_timer=None;self._threshold_timer=None;self._risk_running=False;self._risk_next=None;self._risk_generation=1
        self._draw_preview=None;self._risk_detail_number=None;self._draw_context=('福彩','2026-11-07')
        self._auto_optimizer_error='';self._auto_optimizer_generation=1;self.events=queue.Queue();self.risk_render_count=0
        self.optimizer_pool=ThreadPoolExecutor(max_workers=1)
        self.lottery=tk.StringVar(self,value='福彩');self.draw_number=tk.StringVar(self,value='123')
        self.plan_mode_choice=tk.StringVar(self,value='人工方案');self.hedge_confirmed=tk.BooleanVar(self,value=False)
        self.threshold=tk.StringVar(self,value='30200');self.metric=tk.StringVar(self,value='总赔付')
        self.controller=SimpleNamespace(period='2026-11-07',settings={'include_hedge_cost':False,
          'risk_threshold':30200,'threshold_metric':'payout',
          'direct_unit_cost':{'福彩':2,'体彩':2},'external_direct_prize':{'福彩':1800,'体彩':1800},
          'external_leopard_direct_prize':{'福彩':1600,'体彩':1600}})
        self.snapshot=SimpleNamespace(revenue=1760850,payouts={c:((int(c)%7)*5000) for c in CODES},pending=0,
          risks=[RiskRow(f'{i:03d}',30200,12591.5,10) for i in range(700,778)])
        self.snapshot.payouts['123']=18000
        manual=[RiskRow(f'{i:03d}',0,0,13 if i<12 else 12) for i in range(56)]
        self._plan_manual_text=plan_text(manual,2,'福彩');self._ui_state.put_manual_plan(*self._draw_context,self._plan_manual_text)
        self._auto_optimizer_results={
          'cheap':{'bets':{f'{i:03d}':8 for i in range(100,228)}},
          'balanced':{'bets':{f'{i:03d}':15 if i<262 else 14 for i in range(200,328)}},
          'profit':{'bets':{f'{i:03d}':20 for i in range(300,428)}}}
        self._build()
        self._v280_optimizer_key=self._v280_input_key()
        self._render_plan();self._update_financial_cards();self.update()
    def cleanup(self):
        self._closing=True;self.optimizer_pool.shutdown(wait=True,cancel_futures=True)
        for timer in self.tk.call('after','info'):self.tk.call('after','cancel',timer)
        self.destroy();self._temp.cleanup()

class RealUI(unittest.TestCase):
    def setUp(self):
        self.dialogs=[patch('tkinter.messagebox.askyesno',return_value=True),patch('tkinter.messagebox.showwarning'),patch('tkinter.messagebox.showerror')]
        for p in self.dialogs:p.start()
        self.app=Harness()
    def tearDown(self):
        errors=list(self.app.reported_errors);self.app.cleanup()
        for p in self.dialogs:p.stop()
        self.assertEqual(errors,[],errors)
    def switch(self,mode):
        self.app.plan_mode_choice.set(LABELS[mode]);self.app._switch_plan_mode();self.app.update_idletasks()
    def assert_current(self,count):
        a=self.app;v=a._v283_current
        self.assertEqual(v.count,count);self.assertEqual(a.plan_count_label['text'],f'号码：{count}组')
        self.assertEqual(a.optimizer_compare_table.cells[a._plan_mode,1]['text'],str(count))
        self.assertTrue(a.copy_current_plan_numbers());self.assertTrue(a.clipboard_get().endswith(f'合计：{count}注'))
        self.assertTrue(a.copy_plan());rows,cost=parse_hedge_text(a.clipboard_get(),a.lottery.get(),2)
        self.assertEqual(aggregate_rows(rows),v.bets);self.assertEqual(cost,v.cost_cents)
    def test_screenshot_manual56_threshold78(self):
        self.assertEqual(self.app.cards['risk']['text'],'78组');self.assert_current(56)
        self.assertEqual(self.app.plan_cost_label['text'],'成本：1,368元')
        self.app.copy_risk_numbers();self.assertTrue(self.app.clipboard_get().endswith('合计：78注'))
    def test_balanced128_not_threshold78(self):
        self.switch('balanced');self.assert_current(128)
        self.assertEqual(self.app.plan_cost_label['text'],'成本：3,708元')
        self.assertEqual(self.app._v283_current.bets[0][0],'200')
    def test_repeated_four_way_switches(self):
        for mode,count in [('cheap',128),('balanced',128),('profit',128),('manual',56)]*3:
            self.switch(mode);self.assert_current(count)
    def test_restore_renamed_and_consistent(self):
        a=self.app;self.assertEqual(a.restore_system_plan_button['text'],'按当前阈值重算')
        a.restore_system_plan_button.invoke();self.assertFalse(a._plan_manual_mode);self.assert_current(78)
        self.assertIn('阈值计算',a.plan_identity_label['text'])
    def test_restore_preserves_adjustment_and_replaces_purchase(self):
        a=self.app;a._plan_adjustment=-1
        a.hedge_confirmed.set(True);a._toggle_actual_hedge();before=a._v280_purchase()
        a._restore_system_plan();a.update_idletasks()
        self.assertEqual(a._plan_adjustment,-1);self.assertNotEqual(a._v280_purchase(),before)
        self.assertEqual(a._v280_purchase(),freeze_purchase_snapshot(a._v283_current.as_rows(),*a._v280_settings()))
        self.assertEqual(dict(a._v283_current.bets)['700'],9)
    def test_zero_multiple_threshold_difference(self):
        a=self.app;a.snapshot.risks[0].plan_multiplier=1;a._plan_adjustment=-1
        a._restore_system_plan();self.assert_current(77);self.assertEqual(a.cards['risk']['text'],'78组')
    def test_mode_switch_overwrites_actual_purchase_but_copies_current_view(self):
        a=self.app;a.hedge_confirmed.set(True);a._toggle_actual_hedge();saved=a._v280_purchase()
        self.switch('balanced');self.assert_current(128)
        self.assertNotEqual(a._v280_purchase(),saved)
        self.assertEqual(a._v280_purchase(),freeze_purchase_snapshot(a._v283_current.as_rows(),*a._v280_settings()))
        self.assertEqual(a._purchased_mode(),'balanced')
        self.assertTrue(a.hedge_confirmed.get())
        self.assertEqual(a.cards['hedge_cost']['text'],'3,708元')
    def test_badge_mismatch_blocks_without_clearing(self):
        a=self.app;a.clipboard_clear();a.clipboard_append('unchanged');a.plan_count_label.configure(text='号码：78组')
        self.assertFalse(a.copy_current_plan_numbers());self.assertEqual(a.clipboard_get(),'unchanged')
        a.update_idletasks();self.assert_current(56)
    def test_editor_mismatch_cannot_copy_automatic(self):
        self.switch('balanced');a=self.app;a.plan.configure(state='normal');a.plan.delete('1.0','end');a.plan.insert('1.0','人工旧单')
        a.plan.edit_modified(False);a.clipboard_clear();a.clipboard_append('unchanged')
        self.assertFalse(a.copy_plan());self.assertEqual(a.clipboard_get(),'unchanged')
    def test_stale_rows_not_silently_ignored(self):
        a=self.app;a._plan_rows=[RiskRow('999',0,0,999)]
        self.assertFalse(a.copy_plan());a.update_idletasks();self.assert_current(56)
    def test_auto_result_changes_requires_display_refresh(self):
        self.switch('balanced');a=self.app;a._auto_optimizer_results['balanced']['bets']['999']=2
        self.assertFalse(a.copy_plan());a.update_idletasks();self.assert_current(129)
    def test_only_financial_update_syncs_view_too(self):
        self.switch('balanced');a=self.app;a._auto_optimizer_results['balanced']['bets']['999']=2
        a._update_financial_cards();self.assert_current(129)
    def test_busy_risk_no_copy(self):
        a=self.app;a._risk_running=True;a._render_plan()
        self.assertFalse(a.copy_plan());self.assertIn('disabled',a.copy_current_numbers_button.state())
        a._risk_running=False;a._render_plan();self.assert_current(56)
    def test_unfinished_auto_does_not_fallback_manual(self):
        self.switch('balanced');a=self.app;a._auto_optimizer_results={};a._render_plan()
        self.assertIsNone(a._v283_current);self.assertFalse(a.copy_plan())
    def test_stale_auto_source_rejected(self):
        self.switch('balanced');a=self.app;a.snapshot.revenue+=100;a._render_plan()
        self.assertIsNone(a._v283_current);self.assertFalse(a.copy_plan())
    def test_stale_async_generation_ignored(self):
        self.switch('balanced');a=self.app;before=a._v283_current
        a._apply_optimizer_ready(0,a._draw_context,[{'key':'balanced','bets':{'999':1}}],'')
        self.assertIs(a._v283_current,before);self.assert_current(128)
    def test_manual_edit_before_debounce(self):
        a=self.app;a.plan.delete('1.0','end');a.plan.insert('1.0','福直选4倍\n003 001')
        self.assertTrue(a.copy_plan());self.assert_current(2)
        self.assertEqual(a._v283_current.bets,(('001',4),('003',4)))
    def test_manual_invalid_text_no_old_numbers(self):
        a=self.app;a.plan.delete('1.0','end');a.plan.insert('1.0','福直选4倍\n003\n未知内容')
        self.assertFalse(a.copy_plan());self.assertIsNone(a._v283_current)
    def test_manual_stays_when_switching_auto_back(self):
        a=self.app;before=a._plan_manual_text;self.switch('balanced');self.switch('manual')
        self.assertEqual(a._plan_manual_text,before);self.assert_current(56)
    def test_plus_updates_text_badges_comparison_copy(self):
        a=self.app;before=a._v283_current.cost_cents;a.adjust_plan(1)
        self.assertEqual(a._v283_current.cost_cents,before+56*200);self.assert_current(56)
    def test_plus_replaces_purchased_cost_and_outcome_returns(self):
        a=self.app;a.hedge_confirmed.set(True);a._toggle_actual_hedge();before=a._v280_purchase()
        a.adjust_plan(1);a.update_idletasks()
        current=a._v280_purchase()
        self.assertEqual(current['cost_cents'],before['cost_cents']+56*200)
        self.assertEqual(dict(current['rows'])['000'],dict(before['rows'])['000']+1)
        self.assertEqual(a.cards['hedge_cost']['text'],format(current['cost_cents']//100,',')+'元')
    def test_valid_manual_edit_replaces_purchase_invalid_edit_does_not(self):
        a=self.app;a.hedge_confirmed.set(True);a._toggle_actual_hedge();before=a._v280_purchase()
        a.plan.delete('1.0','end');a.plan.insert('1.0','福直选4倍\n003\n未知内容')
        a._v283_flush_manual();a.update_idletasks()
        self.assertEqual(a._v280_purchase(),before)
        a.plan.delete('1.0','end');a.plan.insert('1.0','福直选4倍\n003 001')
        a._v283_flush_manual();a.update_idletasks()
        self.assertEqual(a._v280_purchase(),freeze_purchase_snapshot(a._v283_current.as_rows(),*a._v280_settings()))
        self.assertEqual(a._v280_purchase()['cost_cents'],1600)
    def test_threshold_waits_for_worker_then_replaces_actual_purchase(self):
        a=self.app;a._restore_system_plan();a.hedge_confirmed.set(True);a._toggle_actual_hedge()
        before=a._v280_purchase();a._risk_running=True
        a.snapshot.risks=a.snapshot.risks[:10]
        a._render_plan();a.update_idletasks()
        self.assertEqual(a._v280_purchase(),before)
        a._risk_running=False;a._render_plan();a.update_idletasks()
        self.assertEqual(a._v280_purchase(),freeze_purchase_snapshot(a._v283_current.as_rows(),*a._v280_settings()))
        self.assertEqual(a._v280_purchase()['cost_cents'],10*10*200)
    def test_auto_optimizer_ready_replaces_actual_purchase(self):
        self.switch('balanced');a=self.app
        a.hedge_confirmed.set(True);a._toggle_actual_hedge()
        before=a._v280_purchase()
        changed=deepcopy(a._auto_optimizer_results)
        changed['balanced']['bets']['999']=3
        a._apply_optimizer_ready(a._auto_optimizer_generation,a._draw_context,list(changed.values()),'')
        a.update_idletasks()
        self.assertNotEqual(before,a._v280_purchase())
        self.assertEqual(a._v280_purchase(),freeze_purchase_snapshot(a._v283_current.as_rows(),*a._v280_settings()))
    def test_cancel_during_mode_switch_does_not_restore_purchase(self):
        a=self.app;a.hedge_confirmed.set(True);a._toggle_actual_hedge()
        a.plan_mode_choice.set(LABELS['balanced']);a._switch_plan_mode()
        self.assertTrue(a.hedge_confirmed.get())
        a.hedge_confirmed.set(False);a._toggle_actual_hedge();a.update_idletasks()
        self.assertIsNone(a._v280_purchase());self.assertFalse(a.hedge_confirmed.get())
    def test_goal_empty_no_zero_assumption(self):
        a=self.app;self.assertEqual(a.target_profit_count['text'],'盈利达标：—')
        self.assertEqual(a.target_loss_count['text'],'亏损达标：—')
        self.assertEqual(a.optimizer_compare_table.cells['manual',7]['text'],'—')
        self.assertEqual(a.optimizer_compare_table.cells['manual',8]['text'],'—')
        self.assertNotEqual(a._v283_tier_labels[1000][0]['text'], '—')
    def test_goal_all_rows_and_actual(self):
        a=self.app;a.target_profit.set('10000');a._v283_commit_target()
        actual=purchased_metrics(a.snapshot.revenue,a._v280_inputs()[1],None)
        self.assertEqual(a.target_profit_count['text'],f'盈利达标：{len(target_outcomes(actual,1000000))}组')
        self.assertEqual(a.target_loss_count['text'],f'亏损达标：{len(_v283_loss_outcomes(actual,1000000))}组')
        for mode in MODES:
            view=a._v283_current if mode==a._plan_mode else a._v283_views[mode]
            self.assertEqual(a.optimizer_compare_table.cells[mode,7]['text'],str(len(target_outcomes(view.metrics,1000000))))
            self.assertEqual(a.optimizer_compare_table.cells[mode,8]['text'],str(len(_v283_loss_outcomes(view.metrics,1000000))))
    def test_goal_switch_preview_not_actual(self):
        a=self.app;a.target_profit.set('10000');a._v283_commit_target();before=a.target_profit_count['text']
        self.switch('balanced');self.assertEqual(a.target_profit_count['text'],before)
    def test_goal_purchase_then_cancel_matches_no_hedge(self):
        a=self.app;a.target_profit.set('10000');a._v283_commit_target();before=a.target_profit_count['text']
        a.hedge_confirmed.set(True);a._toggle_actual_hedge()
        m=purchased_metrics(a.snapshot.revenue,a._v280_inputs()[1],a._v280_purchase())
        self.assertEqual(a.target_profit_count['text'],f'盈利达标：{len(target_outcomes(m,1000000))}组')
        self.switch('balanced');a._v281_cancel_current_purchase()
        self.assertEqual(a.target_profit_count['text'],before);self.assertEqual(a.cards['hedge_cost']['text'],'0元')
    def test_goal_after_auto_replacement_matches_selected_actual_and_partitions_1000(self):
        a=self.app;a.target_profit.set('1');a._v283_commit_target()
        unhedged=a.target_profit_count['text']
        a.hedge_confirmed.set(True);a._toggle_actual_hedge();self.switch('balanced')
        active=purchased_metrics(a.snapshot.revenue,a._v280_inputs()[1],a._v280_purchase())
        count=len(target_outcomes(active,100))
        self.assertEqual(a.target_profit_count['text'],f'盈利达标：{count}组')
        self.assertEqual(a.optimizer_compare_table.cells['balanced',7]['text'],str(count))
        neutral=sum(0<=p<100 for p in active.profits_cents)
        self.assertEqual(active.loss_count+neutral+count,1000)
        a.hedge_confirmed.set(False);a._toggle_actual_hedge();a.update_idletasks()
        self.assertEqual(a.target_profit_count['text'],unhedged)
    def test_goal_persist_invalid_not_saved(self):
        a=self.app;a.target_profit.set('10000.25');a._v283_commit_target()
        self.assertEqual(json.loads(a._ui_state.path.read_text())['target_goal_yuan'],'10000.25')
        a.target_profit.set('-100');a._v283_commit_target()
        self.assertEqual(a._ui_state.data['target_goal_yuan'],'10000.25')
        self.assertEqual(a.target_profit_count['text'],'目标待核对')
    def test_shared_goal_saved_and_both_counts_updated(self):
        a=self.app;a.target_profit.set('100');a._v283_commit_target()
        self.assertEqual(a._v283_loss_target,10000)
        self.assertEqual(a._v283_target,10000)
        self.assertEqual(json.loads(a._ui_state.path.read_text())['target_goal_yuan'],'100')
        self.assertEqual(a.optimizer_compare_table.loss_heading['text'],'亏损≥100元')
        actual=purchased_metrics(a.snapshot.revenue,a._v280_inputs()[1],a._v280_purchase())
        self.assertEqual(a.target_loss_count['text'],f'亏损达标：{len(_v283_loss_outcomes(actual,10000))}组')
    def test_old_signed_target_migrates_to_positive_shared_field(self):
        self.app.cleanup();self.app=Harness(legacy_target='-10000')
        a=self.app
        self.assertEqual(a.target_profit.get(),'10000')
        self.assertEqual(a.target_loss.get(),'10000')
        self.assertEqual(a._v283_loss_target,1000000)
        a._v283_commit_target()
        saved=json.loads(a._ui_state.path.read_text())
        self.assertEqual(saved['target_goal_yuan'],'10000')
    def test_shared_goal_and_loss_dialog_copies_only_losses(self):
        a=self.app;a.snapshot.payouts['085']=a.snapshot.revenue+5000
        a.target_profit.set('0');a._v283_commit_target()
        a._update_financial_cards()
        actual=purchased_metrics(a.snapshot.revenue,a._v280_inputs()[1],None)
        self.assertEqual(a._v283_target_numbers,target_outcomes(actual,0))
        self.assertEqual(a._v283_loss_numbers,_v283_loss_outcomes(actual,0))
        self.assertEqual(a._v283_loss_numbers,('085',))
        a._v283_open_target_dialog('profit')
        self.assertIn('盈利≥0元',a._v283_target_dialog_header['text'])
        a._v283_open_target_dialog('loss')
        self.assertIn('亏损≥0元',a._v283_target_dialog_header['text'])
        self.assertEqual(a._v283_target_dialog_text.get('1.0','end-1c'),'085')
        a._v283_copy_target_numbers()
        self.assertEqual(a.clipboard_get(),'085')
        a.target_profit.set('-100');a._v283_commit_target()
        self.assertEqual(a.target_profit_count['text'],'目标待核对')
        self.assertIsNone(a._v283_loss_numbers)
        self.assertEqual(a.target_loss_count['text'],'目标待核对')
    def test_loss_goal_follows_actual_purchase_and_cancel(self):
        a=self.app;a.target_profit.set('100');a._v283_commit_target()
        before=a.target_loss_count['text']
        a.hedge_confirmed.set(True);a._toggle_actual_hedge()
        actual=purchased_metrics(a.snapshot.revenue,a._v280_inputs()[1],a._v280_purchase())
        self.assertEqual(a.target_loss_count['text'],f'亏损达标：{len(_v283_loss_outcomes(actual,10000))}组')
        a.hedge_confirmed.set(False);a._toggle_actual_hedge();a.update_idletasks()
        self.assertEqual(a.target_loss_count['text'],before)
    def test_target_tooltip_dialog_copy_all_and_clears_stale_numbers(self):
        a=self.app
        self.assertEqual(a.target_profit_view_button['state'],'disabled')
        a.target_profit.set('0');a._v283_commit_target()
        actual=purchased_metrics(a.snapshot.revenue,a._v280_inputs()[1],a._v280_purchase())
        expected=target_outcomes(actual,0)
        self.assertEqual(a._v283_target_numbers,expected)
        self.assertEqual(a.target_profit_view_button['state'],'normal')
        a._v283_show_target_tip()
        self.assertIsNotNone(a._v283_target_tip)
        def text_widget(parent):
            for child in parent.winfo_children():
                if isinstance(child,tk.Text):return child
                found=text_widget(child)
                if found is not None:return found
            return None
        tooltip_area=text_widget(a._v283_target_tip)
        self.assertIsNotNone(tooltip_area)
        self.assertEqual(tooltip_area.get('1.0','end-1c'),_v283_format_target_numbers(expected))
        a._v283_open_target_dialog()
        self.assertIsNotNone(a._v283_target_dialog)
        self.assertIsNone(a._v283_target_tip)
        self.assertEqual(a._v283_target_dialog_text.get('1.0','end-1c'),_v283_format_target_numbers(expected))
        a._v283_copy_target_numbers()
        self.assertEqual(a.clipboard_get().replace('\r\n','\n'),_v283_format_target_numbers(expected))
        a.target_profit.set('');a._v283_commit_target()
        self.assertIsNone(a._v283_target_numbers)
        self.assertEqual(a.target_profit_view_button['state'],'disabled')
        self.assertEqual(a._v283_target_dialog_copy['state'],'disabled')
        self.assertIn('等待最新达标统计',a._v283_target_dialog_text.get('1.0','end-1c'))
    def test_loss_number_list_tracks_purchased_result(self):
        a=self.app;a.target_profit.set('100');a._v283_commit_target()
        original=purchased_metrics(a.snapshot.revenue,a._v280_inputs()[1],None)
        self.assertEqual(a._v283_loss_numbers,_v283_loss_outcomes(original,10000))
        a.hedge_confirmed.set(True);a._toggle_actual_hedge()
        purchased=purchased_metrics(a.snapshot.revenue,a._v280_inputs()[1],a._v280_purchase())
        self.assertEqual(a._v283_loss_numbers,_v283_loss_outcomes(purchased,10000))
        a.hedge_confirmed.set(False);a._toggle_actual_hedge();a.update_idletasks()
        self.assertEqual(a._v283_loss_numbers,_v283_loss_outcomes(original,10000))
    def test_zero_target_counts_break_even(self):
        a=self.app;a.target_profit.set('0');a._v283_commit_target()
        for mode in MODES:
            v=a._v283_current if mode==a._plan_mode else a._v283_views[mode]
            self.assertEqual(a.optimizer_compare_table.cells[mode,7]['text'],str(1000-v.metrics.loss_count))
    def test_target_does_not_reoptimize_or_change_manual(self):
        a=self.app;before=(a._plan_manual_text,a._plan_adjustment,deepcopy(a._auto_optimizer_results),a.threshold.get())
        with patch.object(a.optimizer_pool,'submit',side_effect=AssertionError('not allowed')):
            a.target_profit.set('10000');a._v283_commit_target()
        self.assertEqual(before,(a._plan_manual_text,a._plan_adjustment,a._auto_optimizer_results,a.threshold.get()))
    def test_threshold_does_not_reoptimize_auto(self):
        a=self.app;before=deepcopy(a._auto_optimizer_results);a.threshold.set('60000')
        with patch.object(a.optimizer_pool,'submit',side_effect=AssertionError('not allowed')):a._queue_optimizer()
        self.assertEqual(a._auto_optimizer_results,before)
    def test_target_never_depends_on_actual_draw(self):
        a=self.app;a.target_profit.set('10000');a._v283_commit_target();before=a.target_profit_count['text']
        a.draw_number.set('999');a._update_financial_cards();self.assertEqual(a.target_profit_count['text'],before)
    def test_narrow_layout_table_no_wrapping(self):
        a=self.app;a.geometry('1040x720');a.update()
        self.assertEqual(len(a.optimizer_compare_table.cells),32)
        self.assertGreater(a.plan.winfo_height(),60)
        self.assertTrue(a.target_profit_entry.winfo_ismapped())
    def test_target_count_text_never_moves_following_controls(self):
        a=self.app;a.update_idletasks()
        trailing=(a.target_profit_view_button,a.target_loss_count,a.target_loss_view_button)
        before=[widget.winfo_rootx() for widget in trailing]
        for profit,loss in (('盈利达标：输入中','亏损达标：计算中'),
                            ('盈利达标：0组','亏损达标：1000组'),
                            ('盈利达标：1000组 *','亏损达标：0组')):
            a.target_profit_count.configure(text=profit)
            a.target_loss_count.configure(text=loss)
            a.update_idletasks()
            self.assertEqual([widget.winfo_rootx() for widget in trailing],before)
    def test_scope_switch_never_copies_previous(self):
        a=self.app;a.controller.period='2026-11-08';self.assertFalse(a.copy_plan())
        a._sync_draw_context();self.assertIsNone(a._v283_current)

    def test_goal_source_label_follows_purchase_not_preview(self):
        a=self.app;a.target_profit.set('10000');a._v283_commit_target()
        self.assertEqual(a.target_profit_source['text'],'〔未补单·不计成本及返奖〕')
        self.switch('balanced');a.hedge_confirmed.set(True);a._toggle_actual_hedge()
        self.assertEqual(a.target_profit_source['text'],'〔计入：均衡型·含成本及返奖〕')
        self.switch('manual');self.assertEqual(a.target_profit_source['text'],'〔计入：人工方案·含成本及返奖〕')
    def test_comparison_visible_cell_mismatch_stops_copy(self):
        a=self.app;a.optimizer_compare_table.cells['manual',1].configure(text='78')
        self.assertFalse(a.copy_plan());a.update_idletasks();self.assert_current(56)
    def test_invalid_display_cannot_be_marked_purchased(self):
        a=self.app;a.plan_count_label.configure(text='号码：78组')
        a.hedge_confirmed.set(True);a._toggle_actual_hedge();self.assertIsNone(a._v280_purchase())

if __name__=='__main__':unittest.main(verbosity=2)
