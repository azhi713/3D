# -*- coding: utf-8 -*-
"""V281 UI corrections. Manual risk formula and automatic optimizer untouched."""
from __future__ import annotations
from copy import deepcopy
from decimal import Decimal
import json
import tkinter as tk
from tkinter import ttk, messagebox
from dashboard_v280 import DashboardV280Mixin, LABELS, MODES, RED, MUTED, INK
from dashboard_metrics_v280 import amount_cents, format_yuan
from risk_extension_v2734 import freeze_purchase_snapshot


def threshold_value(value,positive=False):
    value=amount_cents(str(value).strip())
    if positive and value<=0:raise ValueError('增减金额必须大于0')
    return value


def plain_money(cents):
    return format(Decimal(cents)/100,'f').rstrip('0').rstrip('.') if cents%100 else str(cents//100)


def threshold_move(current,step,direction):
    current=threshold_value(current);step=threshold_value(step,True)
    if direction not in (-1,1):raise ValueError('增减方向必须为+1或-1')
    return plain_money(max(0,current+step*direction))


def save_purchase_checked(state,lottery,period,purchase,mode):
    """One source of truth; failed persistence cannot leave an in-memory success."""
    before=deepcopy(state.data)
    try:
        state.put_purchased_plan(lottery,period,purchase,mode)
        expected=state.get_actual_hedge(lottery,period)
        if purchase is None and expected is not None:raise ValueError('已补单记录未清除')
        if purchase is not None and expected!=purchase:raise ValueError('已补单记录保存结果不一致')
        path=getattr(state,'path',None)
        if path is not None:
            saved=json.loads(path.read_text(encoding='utf-8'))
            key=str(lottery)+'|'+str(period)
            if saved.get('actual_hedges',{}).get(key)!=expected:
                raise OSError('UI状态没有写入成功，请检查目录是否可写')
            stored_mode=saved.get('purchased_hedge_modes',{}).get(key)
            if stored_mode!=(mode if expected is not None else None):
                raise OSError('已补单方案归属未保存成功')
    except Exception:
        state.data=before
        raise


class DashboardV281Mixin(DashboardV280Mixin):
    def _build(self):
        super()._build()
        self._v281_layout_busy=False
        self._v281_install_threshold_controls()
        identity=self.plan_identity_label.master
        self.plan_identity_label.destroy()
        identity_bar=ttk.Frame(identity)
        identity_bar.grid(row=1,column=0,sticky='ew',pady=(0,5))
        self.plan_identity_label=ttk.Label(identity_bar,text='',foreground=MUTED,
            font=('Microsoft YaHei UI',9),anchor='w',width=1)
        self.plan_identity_label.pack(side='left',fill='x',expand=True)
        # Single global escape from every preview mode; no hidden purchased plan.
        self._v281_clear_button=ttk.Button(identity_bar,text='取消本期补单',
            command=self._v281_cancel_current_purchase,style='V280.Small.TButton')
        self._v281_clear_button.pack(side='right',padx=(6,0))
        self._v281_basis_button=ttk.Button(self.plan_adjust_label.master,text='计算依据',
            command=self._v281_show_basis,style='V280.Small.TButton')
        self._v281_basis_button.pack(side='left',padx=(6,0))
        self._show_hedge_checkbox()

    def _v281_install_threshold_controls(self):
        # Locate the existing variable-bound entry, not a positional widget id.
        found=[]
        def visit(widget):
            for child in widget.winfo_children():
                if isinstance(child,(ttk.Entry,tk.Entry)):
                    try:
                        if str(child.cget('textvariable'))==str(self.threshold):found.append(child)
                    except tk.TclError:pass
                visit(child)
        visit(self)
        if len(found)!=1:raise RuntimeError('风险阈值输入框不唯一，停止UI挂接')
        frame=found[0].master
        context=frame.master
        siblings=list(context.pack_slaves())
        if len(siblings)!=3 or frame not in siblings:
            raise RuntimeError('阈值工具栏结构变化，不能安全重排')
        others=[x for x in siblings if x is not frame]
        self._v281_context=context;self._v281_scope=others[0];self._v281_exports=others[1]
        self._v281_filters=frame
        for child in tuple(frame.winfo_children()):child.destroy()
        saved=self._ui_state.data.get('threshold_step_yuan','1000')
        try:saved=plain_money(threshold_value(saved,True))
        except (ValueError,TypeError,ArithmeticError):saved='1000'
        self._v281_step_saved=saved
        self.threshold_step=tk.StringVar(self,value=saved)
        ttk.Label(frame,text='风险筛选').grid(row=0,column=0,padx=(0,4))
        mode=ttk.Combobox(frame,textvariable=self.metric,values=['总赔付','净亏损'],state='readonly',width=6)
        mode.grid(row=0,column=1)
        mode.bind('<<ComboboxSelected>>',self.filter_changed)
        ttk.Label(frame,text='≥').grid(row=0,column=2,padx=3)
        self.threshold_minus_button=ttk.Button(frame,text='−',width=2,
            command=lambda:self._v281_adjust_threshold(-1),style='V280.Small.TButton')
        self.threshold_minus_button.grid(row=0,column=3,padx=(0,3))
        self.threshold_entry=ttk.Entry(frame,textvariable=self.threshold,width=9,justify='right')
        self.threshold_entry.grid(row=0,column=4)
        self.threshold_entry.bind('<KeyRelease>',self.filter_changed)
        self.threshold_entry.bind('<Return>',lambda e:self._v281_apply_threshold_now())
        self.threshold_plus_button=ttk.Button(frame,text='+',width=2,
            command=lambda:self._v281_adjust_threshold(1),style='V280.Small.TButton')
        self.threshold_plus_button.grid(row=0,column=5,padx=(3,0))
        ttk.Label(frame,text='元  增减金额').grid(row=0,column=6,padx=(4,3))
        self.threshold_step_entry=ttk.Entry(frame,textvariable=self.threshold_step,width=7,justify='right')
        self.threshold_step_entry.grid(row=0,column=7)
        self.threshold_step_entry.bind('<FocusOut>',lambda e:self._v281_save_step(False))
        self.threshold_step_entry.bind('<Return>',lambda e:self._v281_save_step(True))
        ttk.Label(frame,text='元').grid(row=0,column=8,padx=(3,0))
        self.threshold_step_note=ttk.Label(frame,text='',foreground=RED,font=('Microsoft YaHei UI',8))
        self.threshold_step_note.grid(row=1,column=0,columnspan=9,sticky='e')
        self.threshold_step_note.grid_remove()
        for widget in siblings:widget.pack_forget()
        context.columnconfigure(0,weight=0);context.columnconfigure(1,weight=1);context.columnconfigure(2,weight=0)
        self._v281_scope.grid(row=0,column=0,sticky='w')
        self._v281_exports.grid(row=0,column=2,sticky='e',padx=(8,0))
        frame.grid(row=0,column=1,sticky='e',padx=(8,0))
        context.bind('<Configure>',self._v281_layout_threshold,add='+')
        self.after_idle(self._v281_layout_threshold)

    def _v281_layout_threshold(self,_event=None):
        if self._v281_layout_busy or self._closing:return
        self._v281_layout_busy=True
        try:
            width=self._v281_context.winfo_width()
            needed=sum(w.winfo_reqwidth() for w in (self._v281_scope,self._v281_filters,self._v281_exports))+20
            if width<needed:
                self._v281_filters.grid_configure(row=1,column=0,columnspan=3,padx=0,pady=(4,0),sticky='e')
            else:
                self._v281_filters.grid_configure(row=0,column=1,columnspan=1,padx=(8,0),pady=0,sticky='e')
        finally:self._v281_layout_busy=False

    def _v281_save_step(self,show_error=False):
        try:
            value=plain_money(threshold_value(self.threshold_step.get(),True))
        except (ValueError,TypeError,ArithmeticError):
            self.threshold_step_note.configure(text='增减金额须为大于0的金额，最多两位小数')
            self.threshold_step_note.grid()
            if show_error:messagebox.showwarning('增减金额无效','请输入大于0的金额，最多两位小数。原阈值未改变。',parent=self)
            return False
        self.threshold_step_note.grid_remove()
        if value!=self._v281_step_saved:
            before=self._ui_state.data.get('threshold_step_yuan','1000')
            self._ui_state.data['threshold_step_yuan']=value
            self._ui_state.save()
            path=getattr(self._ui_state,'path',None)
            try:
                if path is not None and json.loads(path.read_text('utf8')).get('threshold_step_yuan')!=value:
                    raise OSError('配置写入失败')
            except (OSError,ValueError):
                self._ui_state.data['threshold_step_yuan']=before
                self.threshold_step_note.configure(text='增减金额未保存，请检查目录权限');self.threshold_step_note.grid()
                return False
            self._v281_step_saved=value
        # Only the editing field is formatted. Changing step never changes threshold.
        self.threshold_step.set(value)
        return True

    def _v281_apply_threshold_now(self):
        timer=getattr(self,'_threshold_timer',None)
        if timer is not None:
            self.after_cancel(timer);self._threshold_timer=None
        self.apply_filter()
        return 'break'

    def _v281_adjust_threshold(self,direction):
        try:new=threshold_move(self.threshold.get(),self.threshold_step.get(),direction)
        except (ValueError,TypeError,ArithmeticError):
            messagebox.showwarning('阈值或增减金额无效','阈值须非负，增减金额须大于0，最多两位小数。未修改原值。',parent=self)
            return
        if not self._v281_save_step(True):return
        if threshold_value(new)==threshold_value(self.threshold.get()):return
        self.threshold.set(new)
        self._v281_apply_threshold_now()

    def _queue_optimizer(self):
        # A changed threshold affects manual filtering, NOT optimizer inputs.
        try:
            key=self._v280_input_key()
            future=getattr(self,'_v280_optimizer_future',None)
            if key==getattr(self,'_v280_optimizer_key',None):
                if (all(mode in self._auto_optimizer_results for mode in ('cheap','balanced','profit'))
                    or (future is not None and not future.done())):
                    self._update_optimizer_comparison();return
        except (ValueError,TypeError,KeyError,ArithmeticError):pass
        super()._queue_optimizer()

    def _v281_save_purchase(self,purchase,mode):
        try:
            save_purchase_checked(self._ui_state,self.lottery.get(),self.controller.period,purchase,mode)
        except (ValueError,TypeError,KeyError,OSError) as exc:
            messagebox.showerror('已补单状态未保存',str(exc),parent=self)
            self._show_hedge_checkbox();self._update_financial_cards();return False
        self._v280_eval_cache.clear()
        self._show_hedge_checkbox();self._update_financial_cards()
        return True

    def _toggle_actual_hedge(self):
        previous=self._v280_purchase();owner=self._purchased_mode()
        desired=bool(self.hedge_confirmed.get())
        if desired:
            if not self.snapshot or self.snapshot.pending or self._plan_edit_error or not self._plan_rows:
                self._show_hedge_checkbox()
                messagebox.showwarning('不能勾选已补单','当前方案尚未完成或有未确认投注。',parent=self);return
            if previous is not None and owner!=self._plan_mode:
                if not messagebox.askyesno('切换本期计入方案','改按「'+LABELS[self._plan_mode]+'」计入成本和返奖？这是替换，不是追加购买。',parent=self):
                    self._show_hedge_checkbox();return
            try:purchase=freeze_purchase_snapshot(self._plan_rows,*self._v280_settings())
            except (ValueError,TypeError,KeyError) as exc:
                self._show_hedge_checkbox();messagebox.showwarning('不能勾选',str(exc),parent=self);return
            if purchase is not None:self._v281_save_purchase(purchase,self._plan_mode)
        elif previous is not None and owner==self._plan_mode:
            self._v281_cancel_current_purchase()
        else:self._show_hedge_checkbox();self._update_financial_cards()

    def _v281_cancel_current_purchase(self):
        previous=self._v280_purchase();owner=self._purchased_mode() or 'manual'
        if previous is None:
            self._show_hedge_checkbox();self._update_financial_cards();return
        text=('本期当前计入「'+LABELS.get(owner,'人工方案')+'」，成本'+format_yuan(previous['cost_cents'])+'元。\n'
              '取消后，顶部按未抛单计算：成本0、返奖0。候选方案和原投注不删除。\n确认取消本期补单？')
        if not messagebox.askyesno('取消本期补单',text,parent=self):
            self._show_hedge_checkbox();return
        self._v281_save_purchase(None,owner)

    def _show_hedge_checkbox(self):
        super()._show_hedge_checkbox()
        button=getattr(self,'_v281_clear_button',None)
        if button is not None:button.configure(state='normal' if self._v280_purchase() is not None else 'disabled')
        if hasattr(self,'_v281_basis_button'):
            self._v281_basis_button.configure(state='normal' if self._plan_mode=='manual' else 'disabled')

    def _update_financial_cards(self):
        super()._update_financial_cards()
        if not hasattr(self,'_v280_badges') or not self.snapshot or self._v280_error:return
        purchase=self._v280_purchase()
        if purchase is None:
            self._v280_badges['hedge_cost'].configure(text='本期未抛单')
            # Even without a draw, no purchased plan means exact zero hedge P&L.
            self.cards['scheme_profit'].configure(text='0元',fg=INK)
            self._v280_badges['scheme_profit'].configure(text='本期未抛单')
        else:
            self._v280_badges['hedge_cost'].configure(text='计入：'+LABELS.get(self._purchased_mode(),'人工方案'))

    def _v281_show_basis(self):
        settings=self.controller.settings
        source='手动文本（不会按阈值重新分配倍数）' if self._plan_manual_mode else '系统阈值实时计算'
        lines=['当前人工来源：'+source,
               '风险筛选：'+self.metric.get()+' ≥ '+self.threshold.get()+'元',
               '系统全局调整：'+str(self._plan_adjustment)+'倍',
               '倍数计算计入全部补单成本：'+('是' if settings.get('include_hedge_cost') else '否'),
               '每倍成本/普通返奖/豹子返奖：'+' / '.join(map(str,self._v280_settings())),
               '今日收入：'+(format_yuan(int(self.snapshot.revenue))+'元' if self.snapshot else '等待数据'),
               '', '基础倍数：按原风险引擎计算；本次没有修改公式。',
               '系统方案使用基础倍数 + 已保存的全局调整量。',
               '手动文本仅核算已有号码/倍数；恢复系统方案才重新按阈值生成。',
               '三种自动方案不使用人工阈值或全局调整量。']
        messagebox.showinfo('人工计算依据', '\n'.join(lines),parent=self)

    def close(self):
        if hasattr(self,'threshold_step'):self._v281_save_step(False)
        super().close()
