# -*- coding: utf-8 -*-
"""V2.8.0 dashboard/UI adapter for the existing RiskApp.

No changes to parsing, customer payout rules, manual threshold calculation or
optimize_three_profiles. Preview, purchased snapshot and editable text are
separate. Installation subclasses RiskApp so the legacy business methods stay
intact and rollback is byte-for-byte.
"""
from __future__ import annotations
from decimal import Decimal
import re
import tkinter as tk
from tkinter import ttk, messagebox, font as tkfont
from models import RiskRow
from risk_engine import plan_text, adjust_plan_rows
from hedge_edit_v276 import parse_hedge_text, update_display_total
from risk_extension_v2734 import freeze_purchase_snapshot
from dashboard_metrics_v280 import (
    CODES, amount_cents, aggregate_rows, payout_vector, candidate_metrics,
    purchased_metrics, format_yuan,
)

INK = '#123050'
MUTED = '#61758b'
RED = '#c3324c'
GREEN = '#16824c'
BG = '#f3f5fa'
SELECTED = '#e6effd'
MODES = ('manual', 'cheap', 'balanced', 'profit')
LABELS = {'manual':'人工方案', 'cheap':'低成本', 'balanced':'均衡型', 'profit':'收益优先'}
_NUMBER = re.compile(r'[0-9]{3}\Z')
_GEOMETRY = re.compile(r'(\d{3,5})x(\d{3,5})([+-]\d{1,6})([+-]\d{1,6})\Z')


class PlanComparisonTable(ttk.Frame):
    """Four rows, individually coloured cells, no wrapped multi-plan text."""
    headers = ('方案', '号码数', '成本/元', '亏损组数', '最差盈亏/元', '补中最低/元', '开奖号盈亏/元')
    minimum_widths = (85, 52, 73, 65, 100, 104, 106)

    def __init__(self, parent, choose):
        super().__init__(parent)
        self.choose = choose
        self.cells = {}
        self.values = {}
        self.selected = 'manual'
        self._sizing = False
        self.cell_font = tkfont.Font(family='Microsoft YaHei UI', size=9)
        self.row_height = max(28, self.cell_font.metrics('linespace')+12)
        self.canvas = tk.Canvas(self, height=self.row_height*5, bg='white',
                                highlightthickness=1, highlightbackground='#c3cfdf', bd=0)
        self.canvas.grid(row=0, column=0, sticky='ew')
        self.columnconfigure(0, weight=1)
        self.body = tk.Frame(self.canvas, bg='#c3cfdf')
        self.window = self.canvas.create_window(0, 0, window=self.body, anchor='nw')
        self.scroll = ttk.Scrollbar(self, orient='horizontal', command=self.canvas.xview)
        self.canvas.configure(xscrollcommand=self.scroll.set)
        for j, text in enumerate(self.headers):
            self.body.columnconfigure(j, weight=1 if j>0 else 0, minsize=self.minimum_widths[j])
            label = tk.Label(self.body, text=text, font=self.cell_font,
                             bg='#eaf0f8', fg=INK, anchor='w' if j==0 else 'e', padx=5)
            label.grid(row=0, column=j, sticky='nsew', padx=(0,1), pady=(0,1))
        for i, mode in enumerate(MODES, 1):
            self.body.rowconfigure(i, minsize=self.row_height)
            for j in range(7):
                label = tk.Label(self.body, text='—', font=self.cell_font,
                                 anchor='w' if j==0 else 'e', padx=5, bg='white', fg=INK)
                label.grid(row=i, column=j, sticky='nsew', padx=(0,1), pady=(0,1))
                label.bind('<Button-1>', lambda _e, key=mode: self.choose(key))
                self.cells[mode,j] = label
        self.body.rowconfigure(0, minsize=self.row_height)
        self.canvas.bind('<Configure>', self._resize)
        self.body.bind('<Configure>', self._resize)
        self._resize()

    def _resize(self, _event=None):
        if self._sizing:
            return
        self._sizing=True
        try:
            width=max(self.canvas.winfo_width(),sum(self.minimum_widths))
            self.canvas.itemconfigure(self.window, width=width, height=self.row_height*5)
            self.canvas.configure(scrollregion=(0,0,width,self.row_height*5))
            if self.canvas.winfo_width()+2 < sum(self.minimum_widths):
                self.scroll.grid(row=1,column=0,sticky='ew')
            else:
                self.scroll.grid_remove()
        finally:
            self._sizing=False

    def set_compact(self, compact):
        height=max(24 if compact else 28, self.cell_font.metrics('linespace')+(6 if compact else 12))
        if self.row_height==height:return
        self.row_height=height
        for row in range(5):self.body.rowconfigure(row,minsize=height)
        self.canvas.configure(height=height*5)
        self._resize()

    def show(self, data, selected, owner):
        self.selected=selected
        self.values=data
        for mode in MODES:
            values,colours=data[mode]
            name=LABELS[mode]+(' ✓' if owner==mode else '')
            values=(name,)+tuple(values)
            background=SELECTED if mode==selected else ('#f7f9fc' if MODES.index(mode)%2 else 'white')
            for j,value in enumerate(values):
                self.cells[mode,j].configure(text=value,bg=background,
                                             fg=INK if j==0 else colours[j-1])


class DashboardV280Mixin:
    """Placed before the pre-patch RiskApp in MRO; original formulas unchanged."""
    _OPT_MODE_LABELS = LABELS

    def _build(self):
        self._v280_eval_cache={}
        self._v280_error=''
        self._v280_preserve_editor=False
        self._v280_switching=False
        self._v280_scroll_request=True
        self._v280_optimizer_future=None
        self._v280_optimizer_key=None
        self._v280_context_syncing=False
        self._v280_compact=False
        self._v280_tail_follow=True
        self._v280_layout_timer=None
        super()._build()
        self._v280_rebuild_cards()
        self._v280_rebuild_plan_panel()
        self._v280_install_window_tracking()
        self.bind('<Configure>',self._v280_schedule_layout,add='+')
        self.after_idle(self._v280_apply_compact_layout)

    def _v280_rebuild_cards(self):
        old=self.cards['income'].master.master
        old.destroy()
        metrics=ttk.Frame(self)
        metrics.pack(fill='x',padx=14,pady=(0,8),before=self.warning)
        self._v280_metric_frame=metrics
        self.cards={}
        self._v280_badges={}
        self._v280_titles={}
        specs=(('income','今日收单'),('hedge_cost','抛单金额'),('risk','阈值组数'),
               ('total_profit','今日总盈亏'),('payout','今日赔付'),('scheme_profit','抛单盈亏'),
               ('range','今日最大盈亏'),('draw','开奖号码'))
        self._v280_card_boxes=[]
        line=tkfont.Font(family='Microsoft YaHei UI',size=10).metrics('linespace')
        height=max(90,line*4+16)
        for i,(key,title) in enumerate(specs):
            row,col=divmod(i,4)
            metrics.columnconfigure(col,weight=1,uniform='v280_metric')
            background='#eaf7ef' if key=='total_profit' else 'white'
            box=tk.Frame(metrics,bg=background,bd=1,relief='solid',height=height,padx=10,pady=6)
            box.grid(row=row,column=col,sticky='nsew',padx=4,pady=(0,5))
            box.pack_propagate(False)
            self._v280_card_boxes.append(box)
            titlebar=tk.Frame(box,bg=background)
            titlebar.pack(fill='x')
            self._v280_titles[key]=tk.Label(titlebar,text=title,bg=background,fg=MUTED,
                                              font=('Microsoft YaHei UI',10))
            self._v280_titles[key].pack(side='left')
            badge=tk.Label(titlebar,text='',bg=background,fg=MUTED,font=('Microsoft YaHei UI',8))
            badge.pack(side='right')
            self._v280_badges[key]=badge
            if key=='range':
                for labelkey,caption,colour in (('loss','最大亏损 —',RED),('best_profit','最大盈利 —',GREEN)):
                    value=tk.Label(box,text=caption,bg=background,fg=colour,
                                   anchor='w',font=('Microsoft YaHei UI',11,'bold'))
                    value.pack(fill='x',pady=(1,0))
                    self.cards[labelkey]=value
            elif key=='draw':
                control=tk.Frame(box,bg=background)
                control.pack(anchor='w',pady=(3,0))
                self.draw_entry=ttk.Entry(control,textvariable=self.draw_number,width=5,
                                          font=('Consolas',16,'bold'),justify='center')
                self.draw_entry.pack(side='left')
                self.draw_entry.bind('<KeyRelease>',self._draw_edited)
                self.draw_fetch_button=ttk.Button(control,text='联网获取',command=self.lookup_draw,
                                                  style='V280.Small.TButton')
                self.draw_fetch_button.pack(side='left',padx=(7,0))
                self.draw_error_more=ttk.Button(control,text='详情',command=self.show_draw_lookup_error,
                                                style='V280.Small.TButton')
                # Existing look-up callbacks retain these status objects. Short
                # status is shown by the panel identity, not a tall card paragraph.
                self.draw_source_label=ttk.Label(box,text='')
                self.draw_summary_label=ttk.Label(box,text='')
            else:
                value=tk.Label(box,text='—',bg=background,fg=INK,
                               font=('Microsoft YaHei UI',17,'bold'),anchor='w')
                value.pack(fill='x',pady=(4,0))
                self.cards[key]=value
        metrics.bind('<Configure>',self._v280_card_fonts,add='+')

    def _v280_card_fonts(self,_event=None):
        try:
            width=self._v280_metric_frame.winfo_width()/4-30
            for key,label in self.cards.items():
                if key in ('loss','best_profit'):
                    continue
                size=15 if self._v280_compact else 17
                font=tkfont.Font(family='Microsoft YaHei UI',size=size,weight='bold')
                while size>11 and font.measure(str(label.cget('text')))>width:
                    size-=1; font.configure(size=size)
                label.configure(font=('Microsoft YaHei UI',size,'bold'))
        except tk.TclError:
            pass

    def _v280_tick_style(self):
        style=ttk.Style(self)
        style.configure('V280.Small.TButton',padding=(7,4),font=('Microsoft YaHei UI',9))
        style.configure('V280.TCheckbutton',background=BG,foreground=INK,padding=2,
                        font=('Microsoft YaHei UI',9))
        # Native Checkbutton state machine, keyboard and BooleanVar, with a tick
        # image indicator (not a square character placed in a push-button).
        side=max(16,round(16*float(self.tk.call('tk','scaling'))/1.3333))
        def indicator(checked,disabled=False):
            image=tk.PhotoImage(master=self,width=side,height=side)
            image.put('#ffffff',to=(1,1,side-1,side-1))
            edge='#b6bdc6' if disabled else '#76859a'
            for x in range(side):
                image.put(edge,(x,0)); image.put(edge,(x,side-1))
                image.put(edge,(0,x)); image.put(edge,(side-1,x))
            if checked:
                points=((.20,.51),(.43,.73),(.82,.25))
                colour='#a8b2bd' if disabled else GREEN
                for (x0,y0),(x1,y1) in zip(points,points[1:]):
                    x0,y0,x1,y1=[round(v*(side-1)) for v in (x0,y0,x1,y1)]
                    steps=max(abs(x1-x0),abs(y1-y0),1)
                    for step in range(steps+1):
                        x=round(x0+(x1-x0)*step/steps); y=round(y0+(y1-y0)*step/steps)
                        image.put(colour,to=(max(1,x-1),max(1,y-1),min(side-1,x+2),min(side-1,y+2)))
            return image
        self._v280_check_images=[indicator(False),indicator(True),indicator(False,True),indicator(True,True)]
        element='V280.Check.indicator'
        if element not in style.element_names():
            off,on,disoff,dison=self._v280_check_images
            style.element_create(element,'image',off,('disabled','selected',dison),
                                 ('selected',on),('disabled',disoff),sticky='')
        style.layout('V280.TCheckbutton', [('Checkbutton.padding', {'sticky':'nswe','children':[
            (element,{'side':'left','sticky':''}),('Checkbutton.label',{'side':'left','sticky':'nswe'})]})])

    def _v280_rebuild_plan_panel(self):
        box=self.plan.master
        # Supported baseline stores Text directly inside its LabelFrame.
        if not isinstance(box,ttk.LabelFrame):
            raise RuntimeError('补单面板结构不符合V277/V279基线')
        panel=box.master
        panel.columnconfigure(0,weight=4,minsize=350)
        panel.columnconfigure(1,weight=6,minsize=510)
        for child in tuple(box.winfo_children()):
            child.destroy()
        for row in range(10):
            box.rowconfigure(row,weight=0,minsize=0)
        box.configure(text='补单方案',padding=(8,7))
        box.rowconfigure(3,weight=1)
        box.columnconfigure(0,weight=1)
        self._v280_tick_style()
        bar=ttk.Frame(box)
        bar.grid(row=0,column=0,sticky='ew',pady=(0,4))
        ttk.Label(bar,text='方案',font=('Microsoft YaHei UI',9)).grid(row=0,column=0,padx=(0,3))
        self.plan_mode_combo=ttk.Combobox(bar,textvariable=self.plan_mode_choice,
                values=list(LABELS.values()),state='readonly',width=9,font=('Microsoft YaHei UI',9))
        self.plan_mode_combo.grid(row=0,column=1)
        self.plan_mode_combo.bind('<<ComboboxSelected>>',self._switch_plan_mode)
        self.plan_count_label=ttk.Label(bar,text='号码：—',foreground=INK,font=('Microsoft YaHei UI',9,'bold'))
        self.plan_count_label.grid(row=0,column=2,padx=(8,3))
        self.plan_cost_label=ttk.Label(bar,text='成本：—',foreground=INK,font=('Microsoft YaHei UI',9,'bold'))
        self.plan_cost_label.grid(row=0,column=3,padx=(3,8),sticky='w')
        bar.columnconfigure(3,weight=1)
        self.actual_hedge_checkbox=ttk.Checkbutton(bar,text='已补单',style='V280.TCheckbutton',
            variable=self.hedge_confirmed,command=self._toggle_actual_hedge,takefocus=True)
        self.actual_hedge_checkbox.grid(row=0,column=4,padx=(0,4))
        self.plan_plus_button=ttk.Button(bar,text='+',width=2,command=lambda:self.adjust_plan(1),style='V280.Small.TButton')
        self.plan_plus_button.grid(row=0,column=5,padx=(0,3))
        self.plan_minus_button=ttk.Button(bar,text='−',width=2,command=lambda:self.adjust_plan(-1),style='V280.Small.TButton')
        self.plan_minus_button.grid(row=0,column=6)
        self.plan_identity_label=ttk.Label(box,text='',foreground=MUTED,font=('Microsoft YaHei UI',9),anchor='w',width=1)
        self.plan_identity_label.grid(row=1,column=0,sticky='ew',pady=(0,5))
        self.optimizer_compare_table=PlanComparisonTable(box,self._v280_choose_mode)
        self.optimizer_compare_table.grid(row=2,column=0,sticky='ew',pady=(0,7))
        holder=ttk.Frame(box)
        holder.grid(row=3,column=0,sticky='nsew')
        holder.rowconfigure(0,weight=1); holder.columnconfigure(0,weight=1)
        self.plan=tk.Text(holder,height=6,wrap='word',bg='white',relief='solid',bd=1,
                          fg=INK,font=('Microsoft YaHei UI',11),padx=5,pady=4)
        self.plan.grid(row=0,column=0,sticky='nsew')
        scrollbar=ttk.Scrollbar(holder,orient='vertical',command=self._v280_scroll_plan)
        scrollbar.grid(row=0,column=1,sticky='ns')
        self.plan.configure(yscrollcommand=scrollbar.set)
        self.plan.bind('<Configure>',self._v280_plan_resized,add='+')
        for sequence in ('<MouseWheel>','<Button-4>','<Button-5>','<Button-1>','<Prior>','<Next>','<Up>','<Down>'):
            self.plan.bind(sequence,self._v280_stop_tail_follow,add='+')
        self.plan.bind('<<Modified>>',self._on_plan_text_modified)
        self.plan.edit_modified(False)
        actions=ttk.Frame(box)
        actions.grid(row=4,column=0,sticky='ew',pady=(6,0))
        self.plan_adjust_label=ttk.Label(actions,text='',foreground=MUTED,font=('Microsoft YaHei UI',9))
        self.plan_adjust_label.pack(side='left')
        self.copy_button=ttk.Button(actions,text='一键复制',command=self.copy_plan,style='V280.Small.TButton')
        self.copy_button.pack(side='right')
        self.sort_plan_button=ttk.Button(actions,text='排序整理',command=self._sort_plan_editor,style='V280.Small.TButton')
        self.sort_plan_button.pack(side='right',padx=(0,5))
        self.restore_system_plan_button=ttk.Button(actions,text='恢复系统方案',command=self._restore_system_plan,style='V280.Small.TButton')
        self.restore_system_plan_button.pack(side='right',padx=(0,5))
        self.plan_note=ttk.Label(box,text='',foreground=MUTED,font=('Microsoft YaHei UI',9),anchor='w')
        self.plan_note.grid(row=5,column=0,sticky='ew',pady=(3,0))
        # Compatibility objects for an older error handler; intentionally hidden.
        self.plan_simulation_label=ttk.Label(box,text='')
        self.optimizer_compare_label=ttk.Label(box,text='')
        if hasattr(self,'adopt_auto_button'):
            del self.adopt_auto_button

    def _v280_schedule_layout(self,event=None):
        if event is not None and getattr(event,'widget',None) is not self:return
        if self._v280_layout_timer is not None:return
        self._v280_layout_timer=self.after_idle(self._v280_apply_compact_layout)

    def _v280_apply_compact_layout(self):
        self._v280_layout_timer=None
        compact=self.winfo_height()<830
        self._v280_compact=compact
        line=tkfont.Font(family='Microsoft YaHei UI',size=10).metrics('linespace')
        for box in self._v280_card_boxes:box.configure(height=max(76,line*3+16) if compact else max(90,line*4+16))
        self.optimizer_compare_table.set_compact(compact)
        if compact:self.plan_note.grid_remove()
        else:self.plan_note.grid()
        self._v280_card_fonts()
        self._v280_plan_resized()

    def _v280_stop_tail_follow(self,_event=None):
        self._v280_tail_follow=False

    def _v280_scroll_plan(self,*args):
        self._v280_tail_follow=False
        self.plan.yview(*args)

    def _v280_plan_resized(self,_event=None):
        if self._v280_tail_follow and not self._v280_preserve_editor:
            self.after_idle(self._v280_see_tail)

    def _v280_see_tail(self):
        if not self._closing and self._v280_tail_follow and not self._v280_preserve_editor:
            self.plan.see('end')

    def _v280_settings(self):
        lottery=self.lottery.get(); s=self.controller.settings
        return (s['direct_unit_cost'][lottery],s['external_direct_prize'][lottery],
                s['external_leopard_direct_prize'][lottery])

    def _v280_inputs(self):
        if not self.snapshot:
            raise ValueError('等待风险数据')
        return int(self.snapshot.revenue),payout_vector(self.snapshot.payouts)

    def _v280_input_key(self):
        income,payouts=self._v280_inputs()
        return (self.lottery.get(),self.controller.period,income,payouts,
                tuple(map(str,self._v280_settings())))

    def _v280_candidate_rows(self,mode):
        if not self.snapshot:
            raise ValueError('等待数据')
        if mode=='manual':
            if self._plan_manual_mode:
                text=self._plan_manual_text if self._plan_manual_text is not None else ''
                return parse_hedge_text(text,self.lottery.get(),self._v280_settings()[0])[0]
            return self._adjusted_plan_rows()
        if self._auto_optimizer_error:
            raise ValueError('计算失败')
        item=self._auto_optimizer_results.get(mode)
        if item is None:
            raise ValueError('计算中')
        if self._v280_optimizer_key is not None and self._v280_optimizer_key!=self._v280_input_key():
            raise ValueError('更新中')
        return [RiskRow(number,float(self.snapshot.payouts[number]),
                        float(self.snapshot.payouts[number])-self.snapshot.revenue/100,int(count))
                for number,count in sorted(item['bets'].items()) if count>0]

    def _v280_candidate_metric(self,mode):
        rows=self._v280_candidate_rows(mode)
        income,payouts=self._v280_inputs()
        settings=self._v280_settings()
        key=('preview',income,payouts,aggregate_rows(rows),tuple(map(str,settings)))
        if key not in self._v280_eval_cache:
            if len(self._v280_eval_cache)>12:
                self._v280_eval_cache.clear()
            self._v280_eval_cache[key]=candidate_metrics(income,payouts,rows,*settings)
        return self._v280_eval_cache[key]

    def _v280_purchase(self):
        return self._ui_state.get_actual_hedge(self.lottery.get(),self.controller.period)

    def _purchased_mode(self):
        return self._ui_state.get_purchased_mode(self.lottery.get(),self.controller.period)

    def _v280_draw(self):
        typed=self.draw_number.get().strip()
        if _NUMBER.fullmatch(typed):
            return typed,False
        preview=getattr(self,'_draw_preview',None)
        hypothetical=str(preview.get('draw','')) if preview else ''
        return (hypothetical,True) if _NUMBER.fullmatch(hypothetical) else ('',True)

    def _update_optimizer_comparison(self):
        if not isinstance(getattr(self,'optimizer_compare_table',None),PlanComparisonTable):
            return
        draw,hypothetical=self._v280_draw()
        data={}
        for mode in MODES:
            try:
                item=self._v280_candidate_metric(mode)
                minimum=item.covered_min_profit_cents
                at=item.at(draw)['total_profit_cents'] if draw else None
                values=(str(item.covered_count),format_yuan(item.cost_cents),str(item.loss_count),
                        format_yuan(item.worst_profit_cents,True),
                        format_yuan(minimum,True) if minimum is not None else '—',
                        format_yuan(at,True) if at is not None else '—')
                colours=(INK,INK,RED if item.loss_count else INK,
                         RED if item.worst_profit_cents<0 else GREEN if item.worst_profit_cents>0 else INK,
                         RED if minimum is not None and minimum<0 else GREEN if minimum is not None and minimum>0 else INK,
                         RED if at is not None and at<0 else GREEN if at is not None and at>0 else INK)
            except (ValueError,TypeError,KeyError,ArithmeticError):
                status=('待核对' if mode=='manual' and self.snapshot else
                        '计算失败' if self._auto_optimizer_error else '计算中' if self.snapshot else '等待数据')
                values=('—',status,'—','—','—','—');colours=(MUTED,RED if status in ('待核对','计算失败') else MUTED,MUTED,MUTED,MUTED,MUTED)
            data[mode]=(values,colours)
        self.optimizer_compare_table.show(data,self._plan_mode,self._purchased_mode())
        self._show_hedge_checkbox()

    def _update_financial_cards(self):
        if not hasattr(self,'_v280_metric_frame'):
            return
        self._v280_error=''
        if not self.snapshot:
            for badge in self._v280_badges.values():badge.configure(text='')
            for key,label in self.cards.items():
                label.configure(text='最大亏损 —' if key=='loss' else '最大盈利 —' if key=='best_profit' else '—')
            self._update_optimizer_comparison()
            return
        try:
            income,payouts=self._v280_inputs()
            actual=purchased_metrics(income,payouts,self._v280_purchase())
            self.cards['income'].configure(text=format_yuan(income)+'元',fg=INK)
            self.cards['hedge_cost'].configure(text=format_yuan(actual.cost_cents)+'元',fg=INK)
            self.cards['risk'].configure(text=str(len(self.snapshot.risks))+'组',fg=INK)
            self.cards['loss'].configure(text='最大亏损 '+format_yuan(actual.max_loss_cents,True)+'元',fg=RED if actual.max_loss_cents<0 else MUTED)
            self.cards['best_profit'].configure(text='最大盈利 '+format_yuan(actual.max_gain_cents,True)+'元',fg=GREEN if actual.max_gain_cents>0 else MUTED)
            self._v280_badges['range'].configure(text='全部结果亏损' if actual.best_profit_cents<0 else
                                                    '全部结果不亏' if actual.worst_profit_cents>=0 else '')
            draw,hypothetical=self._v280_draw()
            incomplete=bool(self.snapshot.pending)
            for key in ('income','hedge_cost','risk'):
                self._v280_badges[key].configure(text='数据未全' if incomplete else '')
            if draw:
                one=actual.at(draw)
                self.cards['payout'].configure(text=format_yuan(payouts[int(draw)])+'元',fg=RED)
                for key,value in (('scheme_profit',one['hedge_profit_cents']),('total_profit',one['total_profit_cents'])):
                    self.cards[key].configure(text=format_yuan(value,True)+'元',fg=GREEN if value>0 else RED if value<0 else INK)
                for key in ('payout','scheme_profit','total_profit'):
                    self._v280_badges[key].configure(text=('模拟 '+draw if hypothetical else '')+(' *' if incomplete else ''))
            else:
                for key in ('payout','scheme_profit','total_profit'):
                    self.cards[key].configure(text='—',fg=MUTED)
                    self._v280_badges[key].configure(text='待输入号码')
        except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
            self._v280_error=str(exc)
            for key,label in self.cards.items():
                label.configure(text='待核对',fg=RED)
            self.plan_note.configure(text='统计未完成：'+str(exc)[:80],foreground=RED)
        self._v280_card_fonts()
        self._update_optimizer_comparison()

    def _show_hedge_checkbox(self):
        if not hasattr(self,'plan_identity_label'):
            return
        purchase=self._v280_purchase()
        owner=self._purchased_mode()
        checked=purchase is not None and owner==self._plan_mode
        self.hedge_confirmed.set(checked)
        stale=False
        if checked and self.snapshot and self._plan_rows and not self._plan_edit_error:
            try:
                now=freeze_purchase_snapshot(self._plan_rows,*self._v280_settings())
                stale=now!=purchase
            except (ValueError,TypeError,KeyError):
                stale=True
        identity='查看：'+LABELS[self._plan_mode]+'  ｜  计入：'+(LABELS.get(owner,'人工方案') if purchase else '未抛单')
        draw,hypothetical=self._v280_draw()
        if draw:
            identity+='  ｜  '+('假设开' if hypothetical else '开奖号')+draw
        if stale:
            identity+='  · 已购与当前推荐不同'
        self.plan_identity_label.configure(text=identity)
        enabled=(checked or (self.snapshot and not self.snapshot.pending and any(r.plan_multiplier>0 for r in self._plan_rows) and not self._plan_edit_error))
        self.actual_hedge_checkbox.configure(state='normal' if enabled else 'disabled')

    def _toggle_actual_hedge(self):
        scope=(self.lottery.get(),self.controller.period)
        previous=self._v280_purchase(); owner=self._purchased_mode()
        desired=bool(self.hedge_confirmed.get())
        if desired:
            if not self.snapshot or self.snapshot.pending or self._plan_edit_error or not self._plan_rows:
                self._show_hedge_checkbox()
                messagebox.showwarning('不能勾选已补单','当前方案不完整或存在待确认投注。',parent=self)
                return
            if previous is not None and owner!=self._plan_mode:
                if not messagebox.askyesno('切换计入方案','将用「'+LABELS[self._plan_mode]+'」替换当前已补单方案；不是追加购买。确认吗？',parent=self):
                    self._show_hedge_checkbox(); return
            try:
                purchase=freeze_purchase_snapshot(self._plan_rows,*self._v280_settings())
                if purchase is None: raise ValueError('没有有效补单号码')
                self._ui_state.put_purchased_plan(*scope,purchase,self._plan_mode)
            except (ValueError,TypeError,KeyError) as exc:
                self._show_hedge_checkbox();messagebox.showwarning('补单记录未保存',str(exc),parent=self);return
        elif previous is not None and owner==self._plan_mode:
            if not messagebox.askyesno('取消已补单','取消后本期不再计入这份补单的成本和返奖。确认吗？',parent=self):
                self._show_hedge_checkbox();return
            self._ui_state.put_purchased_plan(*scope,None,self._plan_mode)
        self._show_hedge_checkbox();self._update_financial_cards()

    def _update_actual_hedge_from_plan(self):
        if (self._plan_mode!='manual' or self._purchased_mode()!='manual' or not self.hedge_confirmed.get()
                or not self.snapshot or self._plan_edit_error or not self._plan_rows):
            return
        purchase=freeze_purchase_snapshot(self._plan_rows,*self._v280_settings())
        if purchase and purchase!=self._v280_purchase():
            self._ui_state.put_purchased_plan(self.lottery.get(),self.controller.period,purchase,'manual')

    def _on_manual_purchase_text_committed(self):
        self._update_actual_hedge_from_plan()

    def _set_editable_plan_text(self,value):
        value=str(value)
        current=self.plan.get('1.0','end-1c')
        changed=current!=value
        preserve=self._v280_preserve_editor or (self._plan_mode=='manual' and self._plan_manual_mode and self._plan_edit_timer is not None)
        if changed:
            self._plan_editor_block=True
            try:
                view=self.plan.yview(); cursor=self.plan.index('insert')
                self.plan.configure(state='normal')
                self.plan.delete('1.0','end');self.plan.insert('1.0',value)
                self.plan.edit_modified(False)
                if preserve:
                    self.plan.mark_set('insert',cursor)
                    self.plan.yview_moveto(view[0])
            finally:
                self._plan_editor_block=False
        if (changed or self._v280_scroll_request) and not preserve:
            self._v280_tail_follow=True
            self.plan.see('end')
            self.after_idle(self._v280_see_tail)
        self._v280_scroll_request=False

    def _render_plan(self):
        if not hasattr(self,'plan_count_label'):
            return
        self._plan_edit_error=''
        text='等待风险数据…'; rows=[]
        try:
            rows=self._v280_candidate_rows(self._plan_mode)
            if self._plan_mode=='manual' and self._plan_manual_mode:
                text=self._plan_manual_text or ''
                cost=sum(n for _,n in aggregate_rows(rows))*amount_cents(self._v280_settings()[0])
                adjusted=update_display_total(text,cost)
                if text!=adjusted:
                    text=adjusted;self._plan_manual_text=adjusted
                    if self._draw_context and not self._v280_context_syncing:
                        self._ui_state.put_manual_plan(*self._draw_context,adjusted)
            else:
                text=plan_text(rows,self._v280_settings()[0],self.lottery.get())
                if self.snapshot and self.snapshot.pending:
                    text='[仅供人工核对 / 不可作为正式补单依据]\n当前投注尚未完整确认。\n\n'+text
            grouped=aggregate_rows(rows)
            cost=sum(n for _,n in grouped)*amount_cents(self._v280_settings()[0])
            self.plan_count_label.configure(text=f'号码：{len(grouped)}组')
            self.plan_cost_label.configure(text='成本：'+format_yuan(cost)+'元',foreground=INK)
        except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
            self._plan_edit_error=str(exc)
            text=(self._plan_manual_text or '') if self._plan_mode=='manual' and self._plan_manual_mode else (
                '自动方案正在计算…' if self.snapshot and not self._auto_optimizer_error else
                '自动计算失败：'+self._auto_optimizer_error if self._auto_optimizer_error else '等待风险数据…')
            self.plan_count_label.configure(text='号码：—')
            self.plan_cost_label.configure(text='成本：待核对' if self.snapshot and self._plan_mode=='manual' else '成本：—',foreground=RED if self._plan_mode=='manual' and self.snapshot else MUTED)
        self._plan_rows=rows
        self._set_editable_plan_text(text)
        manual=self._plan_mode=='manual'
        self.plan.configure(state='normal' if manual else 'disabled')
        self.sort_plan_button.configure(state='normal' if manual and rows and not self._plan_edit_error else 'disabled')
        self.restore_system_plan_button.configure(state='normal' if manual else 'disabled')
        self.plan_plus_button.configure(state='normal' if manual and self.snapshot and rows and not self._plan_edit_error else 'disabled')
        self.plan_minus_button.configure(state='normal' if manual and not self._plan_edit_error and any(r.plan_multiplier>1 for r in rows) else 'disabled')
        self.copy_button.configure(state='normal' if any(r.plan_multiplier>0 for r in rows) and not self._plan_edit_error else 'disabled',
                                   text='复制核对草稿' if self.snapshot and self.snapshot.pending else '一键复制')
        if manual:
            source='手动编辑' if self._plan_manual_mode else f'系统阈值 · 全局{self._plan_adjustment:+d}倍'
            cost_mode=self.controller.settings.get('include_hedge_cost',False)
            note='倍数计算：计入全部补单成本' if cost_mode else '倍数计算：未计补单成本'
            if self._plan_manual_mode: note='保留手动号码与倍数；新投注只更新盈亏统计'
        else:
            source=LABELS[self._plan_mode]+' · 自动计算';note='对比表按各方案假设购买；顶部卡片只按已计入方案'
        self.plan_adjust_label.configure(text=source)
        self.plan_note.configure(text=('方案待核对：'+self._plan_edit_error[:65]) if self._plan_edit_error and self.snapshot and manual else note,
                                  foreground=RED if self._plan_edit_error and self.snapshot and manual else MUTED)
        self._update_optimizer_comparison()

    def _on_plan_text_modified(self,event=None):
        if not self.plan.edit_modified(): return
        self.plan.edit_modified(False)
        if self._plan_editor_block or self._closing or self._plan_mode!='manual': return
        self._v280_tail_follow=False
        self._plan_manual_mode=True
        self._plan_manual_text=self.plan.get('1.0','end-1c')
        if self._plan_edit_timer is not None:self.after_cancel(self._plan_edit_timer)
        self.plan_cost_label.configure(text='成本：核对中',foreground=MUTED)
        self._plan_edit_timer=self.after(170,self._commit_manual_plan)

    def _commit_manual_plan(self):
        self._plan_edit_timer=None
        if self._closing or not self._draw_context or self._plan_mode!='manual': return
        self._plan_manual_mode=True
        self._plan_manual_text=self.plan.get('1.0','end-1c')
        self._ui_state.put_manual_plan(*self._draw_context,self._plan_manual_text)
        self._v280_preserve_editor=True
        try:
            self._render_plan()
            self._update_actual_hedge_from_plan()
            self._render_risk_rows();self._update_financial_cards()
        finally:
            self._v280_preserve_editor=False

    def _v280_choose_mode(self,mode):
        if mode not in MODES:return
        self.plan_mode_choice.set(LABELS[mode]);self._switch_plan_mode()

    def _select_compare_mode(self,event=None):
        # Row labels call _v280_choose_mode directly; no virtual selection loop.
        return

    def _switch_plan_mode(self,event=None):
        selected={v:k for k,v in LABELS.items()}.get(self.plan_mode_choice.get(),'manual')
        if selected==self._plan_mode:return
        if self._plan_edit_timer is not None:
            self.after_cancel(self._plan_edit_timer);self._plan_edit_timer=None
            if self._plan_mode=='manual': self._commit_manual_plan()
        self._plan_mode=selected;self._last_plan_signature=None
        self._ui_state.put_hedge_mode(self.lottery.get(),self.controller.period,selected)
        self._v280_scroll_request=True
        self._render_plan();self._render_risk_rows();self._update_financial_cards()

    def adjust_plan(self,delta):
        if self._plan_mode!='manual' or not self.snapshot or not self._plan_rows or self._plan_edit_error:return
        direction=1 if int(delta)>0 else -1
        if direction<0 and not any(r.plan_multiplier>1 for r in self._plan_rows):return
        if self._plan_manual_mode:
            rows=[RiskRow(r.number,r.payout,r.net_loss,max(0,int(r.plan_multiplier)+direction)) for r in self._plan_rows]
            self._plan_manual_text=plan_text(rows,self._v280_settings()[0],self.lottery.get())
            self._ui_state.put_manual_plan(self.lottery.get(),self.controller.period,self._plan_manual_text)
        else:
            self._plan_adjustment+=direction;self._save_ui_scope()
        self._v280_scroll_request=True
        self._render_plan();self._update_actual_hedge_from_plan()
        self._render_risk_rows();self._update_financial_cards()

    def _restore_system_plan(self):
        if self._plan_mode!='manual':return
        if self._plan_manual_mode and not messagebox.askyesno('恢复系统方案',
                '用当前阈值和全局倍数调整重新生成方案？只替换人工编辑文本，不修改已补单快照。',parent=self):return
        if self._plan_edit_timer is not None:
            self.after_cancel(self._plan_edit_timer);self._plan_edit_timer=None
        self._plan_manual_mode=False;self._plan_manual_text=None;self._plan_edit_error=''
        self._ui_state.put_manual_plan(self.lottery.get(),self.controller.period,None)
        self._v280_scroll_request=True
        self._render_plan();self._render_risk_rows();self._update_financial_cards()

    def _sort_plan_editor(self):
        if self._plan_mode!='manual':return
        try:
            rows,_=parse_hedge_text(self.plan.get('1.0','end-1c'),self.lottery.get(),self._v280_settings()[0])
            self._plan_manual_text=plan_text(rows,self._v280_settings()[0],self.lottery.get())
            self._plan_manual_mode=True
            self._ui_state.put_manual_plan(self.lottery.get(),self.controller.period,self._plan_manual_text)
            self._v280_scroll_request=True
            self._render_plan();self._update_actual_hedge_from_plan()
            self._render_risk_rows();self._update_financial_cards()
        except (ValueError,TypeError,KeyError) as exc:
            messagebox.showwarning('无法排序','方案未完整识别：'+str(exc),parent=self)

    def _queue_optimizer(self):
        self._auto_optimizer_generation+=1
        generation=self._auto_optimizer_generation
        self._auto_optimizer_results={};self._auto_optimizer_error=''
        if self._v280_optimizer_future is not None:self._v280_optimizer_future.cancel()
        self._v280_optimizer_key=None
        if not self.snapshot or self._closing:
            self._update_optimizer_comparison();return
        context=(self.lottery.get(),self.controller.period)
        try:
            income,payouts=self._v280_inputs()
            unit,normal,leopard=map(amount_cents,self._v280_settings())
            self._v280_optimizer_key=self._v280_input_key()
        except (ValueError,KeyError,TypeError) as exc:
            self._auto_optimizer_error=str(exc);self._update_optimizer_comparison();return
        self._update_optimizer_comparison()
        queue_ref=self.events
        def work():
            from hedge_optimizer_v277 import optimize_three_profiles
            try:
                result=optimize_three_profiles(income,dict(zip(CODES,payouts)),unit,normal,leopard)
                queue_ref.put(('optimizer_ready',generation,context,result,''))
            except Exception as exc:
                queue_ref.put(('optimizer_ready',generation,context,None,str(exc)))
        self._v280_optimizer_future=self.optimizer_pool.submit(work)

    def _apply_optimizer_ready(self,generation,context,output,error):
        if generation!=self._auto_optimizer_generation or context!=(self.lottery.get(),self.controller.period) or self._closing:return
        self._auto_optimizer_error=error or ''
        self._auto_optimizer_results={r['key']:r for r in (output or [])} if not error else {}
        if self._plan_mode!='manual':self._v280_scroll_request=True
        self._render_plan();self._render_risk_rows();self._update_financial_cards()

    def _apply_risk_snapshot(self,snapshot,error=''):
        # Existing handler keeps source links, cutoff flags and risk-table logic.
        preserve=self._plan_mode=='manual' and self._plan_manual_mode and self._plan_edit_timer is not None
        self._v280_preserve_editor=preserve
        try:
            super()._apply_risk_snapshot(snapshot,error)
            self._render_plan();self._update_financial_cards()
        finally:
            self._v280_preserve_editor=False

    def _sync_draw_context(self):
        context=(self.lottery.get(),self.controller.period)
        if self._draw_context==context:return
        old=self._draw_context
        if old and self._plan_mode=='manual' and self._plan_manual_mode:
            self._ui_state.put_manual_plan(*old,self.plan.get('1.0','end-1c'))
        if self._plan_edit_timer is not None:
            self.after_cancel(self._plan_edit_timer);self._plan_edit_timer=None
        # Legacy code would otherwise store visible AUTO text into manual cache.
        self._plan_manual_mode=False
        self._v280_optimizer_key=None;self._v280_eval_cache.clear()
        self._v280_scroll_request=True
        self._v280_context_syncing=True
        try:
            super()._sync_draw_context()
        finally:
            self._v280_context_syncing=False
        self._show_hedge_checkbox()

    def copy_plan(self):
        if self._plan_edit_timer is not None:
            self.after_cancel(self._plan_edit_timer);self._plan_edit_timer=None;self._commit_manual_plan()
        if not self.snapshot or self._plan_edit_error or not self._plan_rows:return
        text=plan_text(self._plan_rows,self._v280_settings()[0],self.lottery.get())
        if self.snapshot.pending:
            text='[仅供人工核对 / 不可作为正式补单依据]\n当前投注尚未完整确认。\n\n'+text
        self.clipboard_clear();self.clipboard_append(text)
        self.copy_button.configure(text='已复制')
        if self._copy_timer:
            try:self.after_cancel(self._copy_timer)
            except tk.TclError:pass
        self._copy_timer=self.after(1400,self._v280_restore_copy_label)

    def _v280_restore_copy_label(self):
        self._copy_timer=None
        if not self._closing:
            self.copy_button.configure(text='复制核对草稿' if self.snapshot and self.snapshot.pending else '一键复制')

    def _v280_install_window_tracking(self):
        self._v280_window_timer=None
        self._normal_window_geometry=getattr(self,'_normal_window_geometry',None) or self._ui_state.data.get('normal_window_geometry') or self._ui_state.data.get('window_geometry') or '1280x800+60+60'
        self._last_window_was_zoomed=bool(self._ui_state.data.get('window_zoomed'))
        self._window_tracking_ready=False
        self.bind('<Configure>',self._remember_normal_window,add='+')
        self.after(40,self._capture_initial_normal_window)
        self.after(300,self._enable_window_tracking)

    def _capture_initial_normal_window(self):
        try:
            if self.state()=='normal' and _GEOMETRY.fullmatch(self.geometry()):
                self._normal_window_geometry=self.geometry()
        except tk.TclError:pass

    def _enable_window_tracking(self):
        self._window_tracking_ready=True
        self._remember_normal_window()

    def _remember_normal_window(self,event=None):
        if event is not None and getattr(event,'widget',None) is not self:return
        if not getattr(self,'_window_tracking_ready',False):return
        timer=getattr(self,'_v280_window_timer',None)
        if timer:
            try:self.after_cancel(timer)
            except tk.TclError:pass
        self._v280_window_timer=self.after(160,self._v280_capture_stable_window)

    def _v280_capture_stable_window(self):
        self._v280_window_timer=None
        try:
            state=self.state()
            if state=='normal':
                geometry=self.geometry()
                if _GEOMETRY.fullmatch(geometry):self._normal_window_geometry=geometry
                self._last_window_was_zoomed=False
            elif state=='zoomed':self._last_window_was_zoomed=True
        except tk.TclError:pass

    def _v280_restore_zoom(self):
        try:self.state('zoomed')
        except tk.TclError:
            try:self.attributes('-zoomed',True)
            except tk.TclError:pass

    def _v280_store_window_state(self):
        state=self.state()
        if state=='normal' and _GEOMETRY.fullmatch(self.geometry()):
            self._normal_window_geometry=self.geometry()
        zoomed=state=='zoomed' or (state=='iconic' and self._last_window_was_zoomed)
        self._ui_state.data['normal_window_geometry']=self._normal_window_geometry
        self._ui_state.data['window_geometry']=self._normal_window_geometry
        self._ui_state.data['window_zoomed']=zoomed
        self._ui_state.data['window_geometry_schema']=2
        self._ui_state.save()

    def close(self):
        if self._closing:return
        # Same guard as scope switching: closing an automatic preview must not
        # persist it as user-authored manual text.
        if self._plan_mode!='manual':
            self._plan_manual_mode=False
        elif self._plan_edit_timer is not None:
            self.after_cancel(self._plan_edit_timer)
            self._plan_edit_timer=None
            self._commit_manual_plan()
        super().close()
