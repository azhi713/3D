import csv
import json
import os
import queue
import re
import sqlite3
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from copy import deepcopy
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import perf_counter

from config import APP_NAME, APP_VERSION, LOTTERIES, DEFAULT_AI_PROMPT, ENGINE_VERSION
from models import cents, money, now
from data_store import DataStore
from controller import Controller
from batch_schedule import is_closed
from ai_service import AIService, AIError, effective_timeout, _redact
from ai_diagnostics import AIDiagnostics, SAMPLE as AI_DIAG_SAMPLE
from parser import BetParser
from wechat_service import WeChatService
from risk_engine import plan_text
from data_contract import record_to_contract
from draw_service import fetch_draw, DrawLookupError
from ui_diff import sync_tree

BG = '#f3f5fa'
INK = '#17243b'
MUTED = '#62718b'
BLUE = '#2563eb'
RED = '#be3345'
ORANGE = '#c46a14'
GRID = '#d7deea'


def yuan_fmt(value):
    # RiskRow及payout_for返回的是“元”，与money(分)不能混用。
    return f'{Decimal(str(value)):,.2f}'.rstrip('0').rstrip('.')


class RecordTable(tk.Frame):
    """支持多选、多行原文、网格线和右键菜单的投注记录表。"""
    def __init__(self, parent, columns):
        super().__init__(parent, bg='white', bd=1, relief='solid', highlightthickness=0)
        self.columns = columns
        self._rows = {}
        self._selected = []
        self._callbacks = {}
        self._tags = {}
        self._vars = {}
        self.on_selection_change = None
        self._weights = {'raw': 1}
        self._mins = {key: max(55, int(width)) for key, _title, width in columns}

        # 表头和数据区共用同一网格列；滚动条独占右侧一列。
        # 这样滚动条出现后，表头不会比数据区多出一截宽度。
        table = tk.Frame(self, bg='white')
        table.pack(fill='both', expand=True)
        table.columnconfigure(0, weight=1)
        table.rowconfigure(1, weight=1)

        header = tk.Frame(table, bg='#eaf0fa')
        self.header = header
        header.grid(row=0, column=0, sticky='ew')
        self._header_select = tk.BooleanVar(value=False)
        chk = tk.Checkbutton(header, variable=self._header_select, command=self._toggle_all_from_header,
                             bg='#eaf0fa', activebackground='#eaf0fa', bd=0)
        chk.grid(row=0, column=0, sticky='nsew', padx=2)
        header.columnconfigure(0, minsize=34)
        for i, (key, title, _width) in enumerate(columns, start=1):
            lbl = tk.Label(header, text=title, bg='#eaf0fa', fg=INK,
                           font=('Microsoft YaHei UI',10,'bold'), bd=1, relief='solid', padx=6, pady=7)
            lbl.grid(row=0, column=i, sticky='nsew')
            header.columnconfigure(i, weight=self._weights.get(key, 0), minsize=self._mins[key])

        self.canvas = tk.Canvas(table, bg='white', bd=0, highlightthickness=0, height=150)
        self.vs = ttk.Scrollbar(table, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vs.set)
        self.canvas.grid(row=1, column=0, sticky='nsew')
        self.vs.grid(row=1, column=1, sticky='ns')
        self.inner = tk.Frame(self.canvas, bg='white')
        # Individual canvas windows avoid a >32767px native child window on Windows.
        self._layout_timer=None
        self.canvas.bind('<Configure>', self._schedule_layout)
        self.canvas.bind('<MouseWheel>', self._wheel)
        self.inner.bind('<MouseWheel>', self._wheel)
        for widget in (header, self.canvas, self.inner):
            widget.bind('<Button-3>', self._area_right, add='+')
        for widget in header.winfo_children():
            widget.bind('<Button-3>', self._area_right, add='+')

    def _wheel(self, event):
        if event.delta:
            self.canvas.yview_scroll(int(-event.delta/120), 'units')
        return 'break'

    def _schedule_layout(self,event=None):
        if not getattr(self.winfo_toplevel(),'_closing',False):
            self._layout_rows()

    def _layout_rows(self):
        if self._layout_timer:
            self.after_cancel(self._layout_timer)
        self._layout_timer=None
        width=max(1,self.canvas.winfo_width())
        top=0
        for row in self._rows.values():
            height=max(1,row['frame'].winfo_reqheight())
            self.canvas.coords(row['window'],0,top)
            self.canvas.itemconfigure(row['window'],width=width,height=height)
            row['top']=top;row['height']=height
            top+=height
        self.canvas.configure(scrollregion=(0,0,width,top))

    def destroy(self):
        if self._layout_timer:
            self.after_cancel(self._layout_timer)
            self._layout_timer=None
        super().destroy()

    @staticmethod
    def _preview(text):
        lines = str(text or '').replace('\r\n','\n').replace('\r','\n').split('\n')
        shown = []
        for line in lines[:3]:
            shown.append(line if len(line) <= 80 else line[:77]+'…')
        if len(lines) > 3:
            shown[-1] = (shown[-1][:76] if len(shown[-1]) > 76 else shown[-1]) + ' …'
        return '\n'.join(shown)

    def _toggle_all_from_header(self):
        if self._header_select.get():
            self.select_all()
        else:
            self.clear_selection()

    def bind(self, sequence=None, func=None, add=None):
        if sequence in ('<Double-1>','<Button-3>'):
            self._callbacks[sequence] = func
            return None
        return super().bind(sequence, func, add)

    def tag_configure(self, tag, **kwargs):
        self._tags[tag] = kwargs

    def get_children(self):
        return tuple(self._rows.keys())

    def exists(self, iid):
        return iid in self._rows

    def selection(self):
        return tuple(self._selected)

    def selection_set(self, items):
        if isinstance(items, str):
            items = (items,)
        self._selected = [iid for iid in items if iid in self._rows]
        self._sync_checks()

    def clear_selection(self):
        self._selected = []
        self._sync_checks()

    def select_all(self):
        self._selected = list(self._rows.keys())
        self._sync_checks()

    def invert_selection(self):
        selected = set(self._selected)
        self._selected = [iid for iid in self._rows if iid not in selected]
        self._sync_checks()

    def select_where(self, predicate):
        self._selected = [iid for iid, row in self._rows.items() if predicate(iid, row['values'])]
        self._sync_checks()

    def _sync_checks(self):
        selected = set(self._selected)
        for iid, var in self._vars.items():
            if var.get() != (iid in selected):
                var.set(iid in selected)
        self._header_select.set(bool(self._rows) and len(selected) == len(self._rows))
        for iid, row in self._rows.items():
            self._paint(iid, iid in selected)
        if callable(self.on_selection_change):
            self.on_selection_change()

    def _paint(self, iid, selected=False):
        row = self._rows.get(iid)
        if not row:
            return
        tags = row['tags']
        pending = 'pending' in tags
        state=(pending,selected)
        if row.get('paint_state')==state:return
        row['paint_state']=state
        bg = '#dce8ff' if selected else ('#fff7ed' if pending else 'white')
        fg = RED if pending else INK
        row['frame'].configure(bg=bg)
        for w in row['widgets']:
            if isinstance(w, tk.Checkbutton):
                w.configure(bg=bg, activebackground=bg)
            else:
                w.configure(bg=bg, fg=fg if w._name.endswith('status') else INK)

    def _area_right(self, event):
        cb = self._callbacks.get('<Button-3>')
        if cb:
            event.iid = None
            cb(event)
        return 'break'

    def _row_right(self, iid, event):
        # 右键只弹菜单，绝不改变当前勾选状态。
        cb = self._callbacks.get('<Button-3>')
        if cb:
            event.iid = iid
            cb(event)
        return 'break'

    def _row_double(self, iid, event):
        # 数据行只在双击时切换选择；单击行本身不做选择。
        if iid in self._selected:
            self._selected.remove(iid)
        else:
            self._selected.append(iid)
        self._sync_checks()
        return 'break'

    def insert(self, parent, index, iid=None, values=(), tags=()):
        iid = str(iid)
        if iid in self._rows:
            self.update_row(iid,values,tags)
            return iid
        frame = tk.Frame(self.canvas, bg='white')
        var = tk.BooleanVar(value=False)
        chk = tk.Checkbutton(frame, variable=var, command=lambda i=iid:self._checkbox_toggle(i), bg='white', activebackground='white', bd=0)
        chk.grid(row=0,column=0,sticky='nsew',padx=2)
        widgets=[chk]
        keys=[c[0] for c in self.columns]
        for col, (key, _title, _width) in enumerate(self.columns, start=1):
            value = values[col-1] if col-1 < len(values) else ''
            text = self._preview(value) if key == 'raw' else str(value)
            name = 'status' if key == 'status' else key
            lbl = tk.Label(frame, name=name, text=text, bg='white', fg=INK, bd=1, relief='solid', padx=6, pady=5, anchor='w' if key=='raw' else 'center', justify='left', wraplength=720 if key=='raw' else 0)
            lbl.grid(row=0,column=col,sticky='nsew')
            frame.columnconfigure(col, weight=self._weights.get(key,0), minsize=self._mins[key])
            widgets.append(lbl)
        frame.columnconfigure(0,minsize=34)
        frame.bind('<Double-1>', lambda e,i=iid:self._row_double(i,e))
        frame.bind('<Button-3>', lambda e,i=iid:self._row_right(i,e))
        for w in widgets:
            # 复选框只走自身 command，避免“勾上后又被行点击逻辑取消”。
            if not isinstance(w, tk.Checkbutton):
                w.bind('<Double-1>', lambda e,i=iid:self._row_double(i,e))
            w.bind('<Button-3>', lambda e,i=iid:self._row_right(i,e))
            w.bind('<MouseWheel>', self._wheel)
        window=self.canvas.create_window((0,0),window=frame,anchor='nw',width=max(1,self.canvas.winfo_width()))
        self._rows[iid]={'frame':frame,'widgets':widgets,'values':tuple(values),'tags':tuple(tags),
                         'window':window,'top':0,'height':1}
        self._vars[iid]=var
        self._paint(iid,False)
        return iid

    def update_row(self,iid,values,tags=()):
        row=self._rows[iid]
        old_values=row['values']
        for index,((key,_title,_width),widget) in enumerate(zip(self.columns,row['widgets'][1:])):
            value=values[index] if index<len(values) else ''
            old=old_values[index] if index<len(old_values) else ''
            if value!=old:widget.configure(text=self._preview(value) if key=='raw' else str(value))
        changed_tags=row['tags']!=tuple(tags)
        row['values'],row['tags']=tuple(values),tuple(tags)
        if changed_tags:self._paint(iid,iid in self._selected)

    def sync_rows(self,rows):
        before=list(self._rows)
        desired=[str(r[0]) for r in rows]
        wanted=set(desired)
        top=self.canvas.canvasy(0)
        anchor=None;offset=0
        for iid,row in self._rows.items():
            y=row.get('top',0)
            if y<=top:anchor=iid;offset=top-y
        counts={'insert':0,'update':0,'delete':0,'move':0}
        removed=[iid for iid in before if iid not in wanted]
        if removed:
            self.delete(*removed)
            counts['delete']=len(removed)
        for iid,values,tags in rows:
            iid=str(iid)
            if iid not in self._rows:self.insert('','end',iid=iid,values=values,tags=tags);counts['insert']+=1
            elif self._rows[iid]['values']!=tuple(values) or self._rows[iid]['tags']!=tuple(tags):
                self.update_row(iid,values,tags);counts['update']+=1
        if list(self._rows)!=desired:
            self._rows={iid:self._rows[iid] for iid in desired}
            counts['move']=len(desired)
        if any(counts.values()):
            self._layout_rows()
            self.update_idletasks()
            bounds=self.canvas.bbox('all') or (0,0,0,0)
            self.canvas.configure(scrollregion=bounds)
            if self._rows and anchor in self._rows:
                target=self._rows[anchor]['top']+offset
                self.canvas.yview_moveto(max(0,target)/max(1,bounds[3]))
            elif not self._rows:self.canvas.yview_moveto(0)
        self.last_diff=counts
        return counts

    def _checkbox_toggle(self, iid):
        if self._vars[iid].get():
            if iid not in self._selected:
                self._selected.append(iid)
        elif iid in self._selected:
            self._selected.remove(iid)
        self._sync_checks()

    def delete(self, *items):
        for iid in items:
            iid=str(iid)
            row=self._rows.pop(iid,None)
            if row:
                self.canvas.delete(row['window'])
                row['frame'].destroy()
            self._vars.pop(iid,None)
            if iid in self._selected:
                self._selected.remove(iid)
        if not self._rows:
            # Canvas滚动坐标不会随最后一行销毁自动清零；不归零就会形成
            # 顶部大片空白、仅一条旧行停在底部的“删除未清干净”假象。
            self.canvas.yview_moveto(0)
            self.canvas.configure(scrollregion=(0, 0, 0, 0))
        self._sync_checks()

    def reset_scroll(self):
        """批量刷新数据后同步滚动范围，删除最后一行也保持空表正常布局。"""
        self._layout_rows()
        self.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox('all') or (0, 0, 0, 0))
        self.canvas.yview_moveto(0)


class RiskGridTable(ttk.Treeview):
    """原生风险表：轻分隔线、列排序、选择定位，避免粗黑网格。"""
    def __init__(self, parent, columns):
        keys = [c[0] for c in columns]
        super().__init__(parent, columns=keys, show='headings', selectmode='browse',
                         height=12, style='Risk.Treeview')
        for key, title, width in columns:
            self.heading(key, text=title)
            self.column(key, width=width, minwidth=max(65, width // 2),
                        stretch=True, anchor='center' if key == 'number' else 'e')
        self.tag_configure('odd', background='#f7f9fd')
        self.tag_configure('negative', foreground='#bd3148')


class RiskApp(tk.Tk):
    def __init__(self, base_dir):
        super().__init__()
        self.base_dir = Path(base_dir)
        self.title(APP_NAME+' · '+APP_VERSION)
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        # 小屏幕优先给正文留出空间，不允许默认高度超出物理屏幕。
        width = min(1440, max(960, screen_w-80))
        height = min(980, max(630, screen_h-85))
        self.geometry(f'{width}x{height}')
        self.minsize(min(1040, max(900, screen_w-80)), min(700, max(590, screen_h-85)))
        self.configure(bg=BG)
        self.store = DataStore(base_dir)
        self.controller = Controller(self.store)
        self.lottery = tk.StringVar(value='福彩')
        self.period = tk.StringVar(value=self.controller.period)
        self.threshold = tk.StringVar(value=str(self.controller.settings['risk_threshold']))
        self.metric = tk.StringVar(value='总赔付' if self.controller.settings['threshold_metric'] == 'payout' else '净亏损')
        self.receipt = tk.StringVar()
        self.draw_number = tk.StringVar()
        self._draw_context = None
        self._draw_online = None
        self._draw_preview = None
        self._draw_busy = False
        self._draw_lookup_request = 0
        self.events = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='bet-ai')
        self.risk_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='risk-calc')
        self._risk_generation = 0
        self._risk_key = None
        self._risk_running = False
        self._risk_next = None
        self._records_cache = (None, [])
        self.ai_key = self.controller.settings['ai'].get('api_key', '')
        self.controller.settings['ai']['enabled'] = bool(self.controller.settings['ai'].get('auto_start', self.controller.settings['ai'].get('enabled', False)))
        self.ai = AIService(self.controller.settings['ai'], self.ai_key, self.store.data_dir / 'ai_requests')
        self.ai_busy = set()
        self._manual_ai = set()
        self.wechat = WeChatService(self.store.path, self.controller.settings['wechat'])
        self.wechat.groups.update(dict(self.store.known_groups()))
        self.snapshot = None
        self.error = ''
        self.last_day = now().date().isoformat()
        self._closing = False
        self._timer = None
        self._threshold_timer = None
        self._wechat_start_timer = None
        self._history_busy = False
        self._context_record_id = None
        self._copy_timer = None
        self._risk_sort_key = 'loss'
        self._risk_sort_reverse = True
        self._risk_detail_number = None
        self._risk_source_items = {}
        self._draw_lookup_error_detail = ''
        self._style()
        self._build()
        self.refresh()
        self.protocol('WM_DELETE_WINDOW', self.close)
        self._timer = self.after(200, self.pump)
        if self.store.import_errors:
            self.after(300, lambda: messagebox.showwarning('旧数据未完整迁入', '\n'.join(self.store.import_errors), parent=self))
        if self.store.credential_error:
            self.after(300, lambda: messagebox.showwarning('AI Key需要重新填写', self.store.credential_error, parent=self))
        if self.ai_key and self.controller.settings['ai'].get('enabled'):
            self.after(300, self.resume_pending_ai)
        if self.controller.settings.get('wechat', {}).get('auto_start', True):
            self._wechat_start_timer = self.after(500, self.start_saved_wechat)

    def resume_pending_ai(self):
        if not self._closing:
            for record in self.controller.records():
                if record.engine_version==ENGINE_VERSION:
                    self.queue_ai(record)

    def start_saved_wechat(self):
        self._wechat_start_timer = None
        if self._closing or self.wechat.listening:
            return
        try:
            self.wechat.start()
            self.update_status()
        except Exception as exc:
            self.wechat.status = '微信监听启动失败：'+str(exc)

    def _style(self):
        self.option_add('*Font', ('Microsoft YaHei UI', 10))
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('TFrame', background=BG)
        style.configure('TLabel', background=BG, foreground=INK)
        style.configure('TButton', padding=(12, 7), font=('Microsoft YaHei UI', 10))
        style.configure('Accent.TButton', background=BLUE, foreground='white')
        style.map('Accent.TButton', background=[('active', '#1d4ed8')])
        style.configure('Treeview', rowheight=34, background='white', fieldbackground='white', borderwidth=1, relief='solid', font=('Microsoft YaHei UI', 10))
        style.configure('Treeview.Heading', background='#eaf0fa', foreground=INK, padding=(6, 9), font=('Microsoft YaHei UI', 10, 'bold'))
        style.map('Treeview', background=[('selected', '#dce8ff')], foreground=[('selected', INK)])
        style.configure('Risk.Treeview', rowheight=32, background='white', fieldbackground='white',
                        borderwidth=0, font=('Microsoft YaHei UI', 10))
        style.map('Risk.Treeview', background=[('selected', '#dce8ff')], foreground=[('selected', INK)])
        style.configure('On.TButton', background='#e8f7ee', foreground='#167647')
        style.configure('Off.TButton', background='#fff0f1', foreground=RED)
        style.configure('Warn.TButton', background='#fff5e8', foreground=ORANGE)
        style.configure('TNotebook.Tab', padding=(17, 9), font=('Microsoft YaHei UI', 10, 'bold'))

    def _build(self):
        """三区工作台：常驻信息 + 投注录入 / 实时风险 / 开奖结算。只变更布局。"""
        pad = 18
        # 顶部品牌栏仅保留页面标题和设置，不再混放状态及业务按钮。
        header = tk.Frame(self, bg=INK)
        header.pack(fill='x')
        title_area = tk.Frame(header, bg=INK)
        title_area.pack(side='left', padx=pad, pady=10)
        tk.Label(title_area, text=APP_NAME, bg=INK, fg='white',
                 font=('Microsoft YaHei UI', 17, 'bold')).pack(side='left')
        tk.Label(title_area, text='  /  实时风险工作台', bg=INK,
                 fg='#a8bddb').pack(side='left', padx=(6, 0))
        ttk.Button(header, text='设置', command=self.settings_dialog).pack(side='right', padx=pad)

        # 运行开关集中在一行。长群名、连接日志放到窗口底部状态栏。
        controls = tk.Frame(self, bg='white', bd=1, relief='solid')
        controls.pack(fill='x', padx=pad, pady=(10, 7))
        switches = ttk.Frame(controls)
        switches.pack(side='left', padx=11, pady=8)
        self.wechat_toggle_btn = ttk.Button(switches, text='微信监听：关', command=self.toggle_wechat)
        self.wechat_toggle_btn.pack(side='left', padx=(0, 8))
        self.ai_toggle_btn = ttk.Button(switches, text='AI识别：关', command=self.toggle_ai)
        self.ai_toggle_btn.pack(side='left', padx=(0, 8))
        self.history_button = ttk.Button(switches, text='读取历史消息', command=self.history_dialog)
        self.history_button.pack(side='left')
        self.pending_label = tk.Label(controls, text='待处理：0', bg='white', fg=INK,
                                      font=('Microsoft YaHei UI', 10, 'bold'))
        self.pending_label.pack(side='right', padx=(10, 15))

        # 当前统计范围、筛选条件和导出操作三组明确分区。
        context = ttk.Frame(self)
        context.pack(fill='x', padx=pad, pady=(0, 7))
        scope = ttk.Frame(context)
        scope.pack(side='left')
        for lottery, text in [('福彩', '福彩3D'), ('体彩', '体彩排列三')]:
            ttk.Radiobutton(scope, text=text, value=lottery, variable=self.lottery,
                            command=self.refresh).pack(side='left', padx=(0, 13))
        ttk.Label(scope, text='日期 / 期号').pack(side='left', padx=(3, 5))
        self.period_combo = ttk.Combobox(scope, textvariable=self.period, width=12)
        self.period_combo.pack(side='left')
        self.period_combo.bind('<<ComboboxSelected>>', self.switch_period)
        self.period_combo.bind('<Return>', self.switch_period)

        exports = ttk.Frame(context)
        exports.pack(side='right', padx=(12, 0))
        self.export_menu = tk.Menu(self, tearoff=False)
        for text, action in [
            ('当前数据 CSV', self.export_csv),
            ('投注明细 CSV', self.export_detail_csv),
            ('结构化 JSON', self.export_contract_json),
            ('截止汇总 JSON', self.export_batch_json),
        ]:
            self.export_menu.add_command(label=text, command=action)
        ttk.Menubutton(exports, text='数据导出 ▾', menu=self.export_menu).pack(side='right')

        filters = ttk.Frame(context)
        filters.pack(side='right', padx=(12, 0))
        ttk.Label(filters, text='风险筛选').pack(side='left', padx=(0, 5))
        metric_box = ttk.Combobox(filters, textvariable=self.metric,
                                  values=['总赔付', '净亏损'], width=7, state='readonly')
        metric_box.pack(side='left')
        metric_box.bind('<<ComboboxSelected>>', self.filter_changed)
        ttk.Label(filters, text='≥').pack(side='left', padx=(5, 3))
        threshold = ttk.Entry(filters, textvariable=self.threshold, width=8)
        threshold.pack(side='left')
        threshold.bind('<KeyRelease>', self.filter_changed)
        ttk.Label(filters, text='元').pack(side='left', padx=(4, 0))

        metrics = ttk.Frame(self)
        metrics.pack(fill='x', padx=pad, pady=(0, 8))
        self.cards = {}
        for i, (key, label) in enumerate([
            ('due', '有效投注应收'), ('income', '计入收入'),
            ('risk', '达到阈值的号码'), ('loss', '当前最大净亏损'),
        ]):
            metrics.columnconfigure(i, weight=1, uniform='metric')
            card = tk.Frame(metrics, bg='white', bd=1, relief='solid', padx=13, pady=8)
            card.grid(row=0, column=i, sticky='ew', padx=(0, 8 if i < 3 else 0))
            tk.Label(card, text=label, bg='white', fg=MUTED).pack(anchor='w')
            value = tk.Label(card, text='—', bg='white',
                             fg=RED if key == 'loss' else INK,
                             font=('Microsoft YaHei UI', 18, 'bold'))
            value.pack(anchor='w', pady=(1, 0))
            self.cards[key] = value

        self.warning = tk.Label(self, bg=BG, fg=RED, anchor='w', justify='left',
                                wraplength=1040)
        self.warning.pack(fill='x', padx=pad, pady=(0, 5))

        # 连接信息不占用任务工具栏，也不与当前彩种、日期争抢空间。
        footer = tk.Frame(self, bg='#eaf0f7', bd=1, relief='solid')
        footer.pack(side='bottom', fill='x')
        footer.columnconfigure(0, weight=2)
        footer.columnconfigure(1, weight=3)
        self.group_label = tk.Label(footer, text='', bg='#eaf0f7', fg=MUTED,
                                    anchor='w', justify='left', wraplength=400)
        self.group_label.grid(row=0, column=0, sticky='ew', padx=(pad, 10), pady=6)
        self.status = tk.Label(footer, text='', bg='#eaf0f7', fg=MUTED,
                               anchor='e', justify='right', wraplength=670)
        self.status.grid(row=0, column=1, sticky='ew', padx=(0, pad), pady=6)

        self.workspace_tabs = ttk.Notebook(self)
        self.workspace_tabs.pack(fill='both', expand=True, padx=pad, pady=(0, 8))
        records_page = ttk.Frame(self.workspace_tabs)
        risks_page = ttk.Frame(self.workspace_tabs)
        draws_page = ttk.Frame(self.workspace_tabs)
        self.workspace_tabs.add(records_page, text='  投注管理  ')
        self.workspace_tabs.add(risks_page, text='  实时风险  ')
        self.workspace_tabs.add(draws_page, text='  开奖结算  ')

        # 第一页：录入区固定在上，记录表独占剩余全部空间。
        input_box = ttk.LabelFrame(records_page, text='手动补录', padding=(12, 9))
        input_box.pack(fill='x', pady=(9, 8))
        input_row = ttk.Frame(input_box)
        input_row.pack(fill='x')
        self.input = tk.Text(input_row, height=3, wrap='word', relief='solid', bd=1,
                             bg='white', fg=INK)
        self.input.pack(side='left', fill='x', expand=True)
        self.input.bind('<Return>', self.add_event)
        self.input.bind('<Shift-Return>', lambda event: None)
        side = ttk.Frame(input_row)
        side.pack(side='right', padx=(10, 0))
        ttk.Button(side, text='添加投注', style='Accent.TButton', command=self.add_manual).pack(fill='x')
        ttk.Label(side, text='Shift+Enter 换行', foreground=MUTED).pack(pady=(6, 0))
        receipt_row = ttk.Frame(input_box)
        receipt_row.pack(fill='x', pady=(7, 0))
        ttk.Label(receipt_row, text='已确认实收').pack(side='left')
        ttk.Entry(receipt_row, textvariable=self.receipt, width=10).pack(side='left', padx=6)
        ttk.Label(receipt_row, text='元（留空按应收）').pack(side='left')
        ttk.Label(receipt_row, text='例：358421组六10倍，293单组各50倍',
                  foreground=MUTED).pack(side='right')
        self.add_feedback = ttk.Label(input_box, text='', foreground='#167647')
        self.add_feedback.pack(fill='x', anchor='w', pady=(4, 0))

        notebook = ttk.Notebook(records_page)
        notebook.pack(fill='both', expand=True)
        records_tab, guide_tab = ttk.Frame(notebook), ttk.Frame(notebook)
        notebook.add(records_tab, text='投注记录')
        notebook.add(guide_tab, text='下单模板')
        records_tab.rowconfigure(1, weight=1)
        records_tab.columnconfigure(0, weight=1)
        self.records_summary = ttk.Label(records_tab, text='今日0条 · 已识别0 · 未识别0 · 已选0')
        self.records_summary.grid(row=0, column=0, sticky='ew', pady=(7, 5))
        self.records_tree = RecordTable(records_tab, [
            ('time', '时间', 75), ('source', '来源', 70), ('raw', '投注原文', 420),
            ('amount', '应收', 75), ('status', '状态', 95),
        ])
        self.records_tree.grid(row=1, column=0, sticky='nsew')
        self.records_tree.tag_configure('pending', foreground=RED)
        self.records_tree.bind('<Button-3>', self.record_context_menu)
        self.records_tree.on_selection_change = self._update_record_selection_count
        actions = ttk.Frame(records_tab)
        actions.grid(row=2, column=0, sticky='ew', pady=(7, 3))
        for title, command in [
            ('查看', self.view_record), ('修改／实收', self.edit_record),
            ('重新识别', self.reparse_selected), ('AI转换', self.ai_selected),
            ('删除', self.delete_selected),
        ]:
            ttk.Button(actions, text=title, command=command).pack(side='left', padx=(0, 6))
        ttk.Label(records_tab, text='单击复选框或双击行选中；右键可全选、反选和删除当前数据',
                  foreground=MUTED).grid(row=3, column=0, sticky='w', pady=(1, 3))
        guide = tk.Text(guide_tab, wrap='word', bg='white', relief='solid', bd=1)
        guide.pack(fill='both', expand=True, padx=2, pady=6)
        guide.insert('1.0', self.template_text())
        guide.configure(state='disabled')

        # 第二页：左侧可拖拽调整的风险表/来源详情，右侧保留处理方案。
        risk_panel = ttk.Frame(risks_page)
        risk_panel.pack(fill='both', expand=True, pady=(9, 0))
        risk_panel.rowconfigure(0, weight=1)
        risk_panel.columnconfigure(0, weight=3, minsize=535)
        risk_panel.columnconfigure(1, weight=2, minsize=280)
        risk_box = ttk.LabelFrame(risk_panel, text='高风险号码 · 点击表头排序', padding=8)
        risk_box.grid(row=0, column=0, sticky='nsew', padx=(0, 9))
        risk_box.rowconfigure(1, weight=1)
        risk_box.columnconfigure(0, weight=1)

        self.risk_info = ttk.Label(risk_box, text='点击号码查看命中明细。原始投注倍数与建议补单倍数分别展示。',
                                   foreground=MUTED)
        self.risk_info.grid(row=0, column=0, sticky='ew', pady=(0, 6))
        # 纵向拉动分隔线，可自行调整排行榜与明细的高度。
        self.risk_pane = tk.PanedWindow(risk_box, orient='vertical', bd=0, sashwidth=9,
                                       sashrelief='flat', showhandle=True, bg=GRID)
        self.risk_pane.grid(row=1, column=0, sticky='nsew')
        risk_top = ttk.Frame(self.risk_pane)
        risk_top.columnconfigure(0, weight=1)
        risk_top.rowconfigure(0, weight=1)
        self.risks_tree = RiskGridTable(risk_top, [
            ('number', '开奖号码', 76), ('payout', '预计赔付(元)', 130),
            ('loss', '净亏损(元)', 130), ('plan', '建议补单(倍)', 118),
        ])
        self.risks_tree.grid(row=0, column=0, sticky='nsew')
        risk_scroll = ttk.Scrollbar(risk_top, orient='vertical', command=self.risks_tree.yview)
        risk_scroll.grid(row=0, column=1, sticky='ns')
        self.risks_tree.configure(yscrollcommand=risk_scroll.set)
        self.risks_tree.bind('<<TreeviewSelect>>', self.show_sources)
        for key, title in [('number', '开奖号码'), ('payout', '预计赔付(元)'),
                           ('loss', '净亏损(元)'), ('plan', '建议补单(倍)')]:
            self.risks_tree.heading(key, text=title, command=lambda k=key:self.sort_risks(k))

        source_bottom = ttk.Frame(self.risk_pane)
        source_bottom.columnconfigure(0, weight=1)
        source_bottom.rowconfigure(1, weight=1, minsize=60)
        source_bar = ttk.Frame(source_bottom)
        source_bar.grid(row=0, column=0, sticky='ew', pady=(2, 5))
        self.risk_source_title = ttk.Label(source_bar, text='投注来源 · 请选择上方号码',
                                           font=('Microsoft YaHei UI', 10, 'bold'))
        self.risk_source_title.pack(side='left')
        self.risk_source_more = ttk.Button(source_bar, text='查看全部来源', command=self.show_all_sources)
        self.risk_source_more.pack(side='right')
        source_table = ttk.Frame(source_bottom)
        source_table.grid(row=1, column=0, sticky='nsew')
        source_table.rowconfigure(0, weight=1)
        source_table.columnconfigure(0, weight=1)
        self.source_rows = ttk.Treeview(source_table, show='headings', selectmode='browse', height=2,
                                         columns=('play', 'selection', 'multiple', 'payout', 'origin'))
        for key, title, width in [('play','玩法',100), ('selection','号码/条件',125),
                                  ('multiple','原始倍数',76), ('payout','该笔赔付',90),
                                  ('origin','来源',125)]:
            self.source_rows.heading(key, text=title)
            self.source_rows.column(key, width=width, minwidth=55, stretch=True,
                                    anchor='e' if key in ('multiple','payout') else 'w')
        self.source_rows.grid(row=0, column=0, sticky='nsew')
        source_scroll = ttk.Scrollbar(source_table, orient='vertical', command=self.source_rows.yview)
        source_scroll.grid(row=0, column=1, sticky='ns')
        self.source_rows.configure(yscrollcommand=source_scroll.set)
        self.source_rows.bind('<<TreeviewSelect>>', self.show_source_detail)
        self.sources = tk.Text(source_bottom, height=2, wrap='word', bg='#f7f9fd', fg=MUTED,
                               relief='flat', padx=8, pady=5)
        self.sources.grid(row=2, column=0, sticky='ew', pady=(6, 0))
        self.set_text(self.sources, '点选风险号码，再点下方投注行查看原始消息和赔付计算依据。')
        self.risk_pane.add(risk_top, minsize=135, stretch='always')
        self.risk_pane.add(source_bottom, minsize=165, stretch='always')
        self._risk_sash_initialized = False
        self.risk_pane.bind('<Configure>', self._position_initial_risk_details)

        plan_box = ttk.LabelFrame(risk_panel, text='当前处理方案', padding=10)
        plan_box.grid(row=0, column=1, sticky='nsew')
        plan_box.rowconfigure(0, weight=1)
        plan_box.columnconfigure(0, weight=1)
        self.plan = tk.Text(plan_box, height=10, wrap='word', bg='white',
                            relief='solid', bd=1, fg=INK,
                            font=('Microsoft YaHei UI', 11))
        self.plan.grid(row=0, column=0, sticky='nsew')
        plan_row = ttk.Frame(plan_box)
        plan_row.grid(row=1, column=0, sticky='ew', pady=(8, 0))
        self.plan_note = ttk.Label(plan_row, text='')
        self.plan_note.pack(side='left')
        self.copy_button = ttk.Button(plan_row, text='一键复制',
                                      style='Accent.TButton', command=self.copy_plan)
        self.copy_button.pack(side='right')

        # 第三页：独立纵向滚动；1366×768等小屏也能看到中奖明细和完整结算操作。
        draw_host = draws_page
        draw_canvas = tk.Canvas(draw_host, bg=BG, bd=0, highlightthickness=0)
        draw_scroll = ttk.Scrollbar(draw_host, orient='vertical', command=draw_canvas.yview)
        draw_canvas.configure(yscrollcommand=draw_scroll.set)
        draw_scroll.pack(side='right', fill='y')
        draw_canvas.pack(side='left', fill='both', expand=True)
        draws_page = ttk.Frame(draw_canvas)
        draw_window = draw_canvas.create_window((0, 0), anchor='nw', window=draws_page)
        self.draw_canvas = draw_canvas
        def update_draw_scroll(event=None):
            draw_canvas.configure(scrollregion=draw_canvas.bbox('all'))
        def fit_draw_width(event):
            draw_canvas.itemconfigure(draw_window, width=event.width)
        def scroll_draw(event):
            if event.delta:
                draw_canvas.yview_scroll(int(-event.delta / 120), 'units')
            return 'break'
        draws_page.bind('<Configure>', update_draw_scroll)
        draw_canvas.bind('<Configure>', fit_draw_width)
        draw_canvas.bind('<MouseWheel>', scroll_draw)
        draws_page.bind('<MouseWheel>', scroll_draw)
        # 左侧输入/核验操作，右侧金额结果；任何错误都可换行显示。
        draws_page.columnconfigure(0, weight=4, uniform='draw')
        draws_page.columnconfigure(1, weight=5, uniform='draw')
        draws_page.rowconfigure(1, weight=1)
        intro = ttk.Frame(draws_page)
        intro.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(15, 10))
        ttk.Label(intro, text='当前期开奖号码与赔付试算',
                  font=('Microsoft YaHei UI', 14, 'bold')).pack(anchor='w')
        ttk.Label(intro, text='跟随顶部选定彩种与日期。试算不改变投注和兑奖记录；正式结算需完成封盘。',
                  foreground=MUTED).pack(anchor='w', pady=(4, 0))

        draw_input = ttk.LabelFrame(draws_page, text='01  开奖号码', padding=15)
        draw_input.grid(row=1, column=0, sticky='nsew', padx=(0, 10), pady=(0, 12))
        ttk.Label(draw_input, text='按百位、十位、个位输入三位数字', foreground=MUTED).pack(anchor='w')
        entry_row = ttk.Frame(draw_input)
        entry_row.pack(fill='x', pady=(9, 10))
        self.draw_entry = ttk.Entry(entry_row, textvariable=self.draw_number, width=8,
                                    font=('Consolas', 24, 'bold'), justify='center')
        self.draw_entry.pack(side='left')
        self.draw_entry.bind('<KeyRelease>', self._draw_edited)
        self.draw_fetch_button = ttk.Button(entry_row, text='联网获取', command=self.lookup_draw)
        self.draw_fetch_button.pack(side='left', padx=(13, 0))
        ttk.Button(draw_input, text='计算应赔金额', style='Accent.TButton',
                   command=self.calculate_draw).pack(fill='x', pady=(0, 9))
        action_row = ttk.Frame(draw_input)
        action_row.pack(fill='x')
        ttk.Button(action_row, text='中奖明细', command=self.show_draw_details).pack(side='left', fill='x', expand=True, padx=(0, 5))
        ttk.Button(action_row, text='正式结算', command=self.save_draw_settlement).pack(side='left', fill='x', expand=True, padx=(5, 0))
        ttk.Separator(draw_input).pack(fill='x', pady=(15, 12))
        ttk.Label(draw_input, text='数据来源与联网核验',
                  font=('Microsoft YaHei UI', 10, 'bold')).pack(anchor='w', pady=(0, 6))
        self.draw_source_label = ttk.Label(draw_input, text='', foreground=MUTED,
                                            justify='left', wraplength=375)
        self.draw_source_label.pack(fill='x', anchor='w')
        self.draw_error_more = ttk.Button(draw_input, text='查看联网详情', command=self.show_draw_lookup_error)
        self.draw_error_more.pack(anchor='w', pady=(4, 0))
        self.draw_error_more.pack_forget()
        ttk.Label(draw_input, text='若官网尚未公布，可手动输入并先试算；联网失败不会清空已输入号码。',
                  foreground=MUTED, wraplength=375).pack(anchor='w', pady=(14, 0))

        draw_result = ttk.LabelFrame(draws_page, text='02  赔付结果', padding=15)
        draw_result.grid(row=1, column=1, sticky='nsew', pady=(0, 12))
        draw_result.columnconfigure(0, weight=1, uniform='draw-result')
        draw_result.columnconfigure(1, weight=1, uniform='draw-result')
        self.draw_cards = {}
        for i, (key, title) in enumerate([
            ('income', '计入收入'), ('payout', '应赔金额'),
            ('net', '当期净盈亏'), ('count', '中奖明细'),
        ]):
            row, col = divmod(i, 2)
            panel = tk.Frame(draw_result, bg='white', bd=1, relief='solid', padx=12, pady=14)
            panel.grid(row=row, column=col, sticky='nsew', padx=(0, 8 if col == 0 else 0), pady=(0, 8))
            tk.Label(panel, text=title, bg='white', fg=MUTED).pack(anchor='w')
            value = tk.Label(panel, text='—', bg='white', fg=RED if key == 'payout' else INK,
                             font=('Microsoft YaHei UI', 20, 'bold'))
            value.pack(anchor='w', pady=(8, 0))
            self.draw_cards[key] = value
        ttk.Label(draw_result, text='试算说明 / 待核对事项',
                  font=('Microsoft YaHei UI', 10, 'bold')).grid(
                      row=2, column=0, columnspan=2, sticky='w', pady=(12, 5))
        self.draw_summary_label = ttk.Label(draw_result,
            text='填写三位开奖号码，或点击联网获取；试算不会写入兑奖账本。',
            foreground=MUTED, wraplength=440, justify='left')
        self.draw_summary_label.grid(row=3, column=0, columnspan=2, sticky='ew')
        ttk.Label(draw_result, text='本期命中投注（完整明细仍可点击左侧「中奖明细」）',
                  font=('Microsoft YaHei UI', 10, 'bold')).grid(
                      row=4, column=0, columnspan=2, sticky='w', pady=(13, 5))
        winners_box = ttk.Frame(draw_result)
        winners_box.grid(row=5, column=0, columnspan=2, sticky='nsew')
        winners_box.rowconfigure(0, weight=1)
        winners_box.columnconfigure(0, weight=1)
        self.draw_winners = ttk.Treeview(winners_box, columns=('play', 'selection', 'multiplier', 'payout'),
                                          show='headings', selectmode='browse', height=6)
        for key, title, width in [('play','玩法',120),('selection','投注号码',115),
                                   ('multiplier','倍数',65),('payout','应赔金额',100)]:
            self.draw_winners.heading(key, text=title)
            self.draw_winners.column(key, width=width, minwidth=55, stretch=(key in ('play','selection')))
        self.draw_winners.grid(row=0, column=0, sticky='nsew')
        win_scroll = ttk.Scrollbar(winners_box, orient='vertical', command=self.draw_winners.yview)
        win_scroll.grid(row=0, column=1, sticky='ns')
        self.draw_winners.configure(yscrollcommand=win_scroll.set)
        ttk.Label(draw_result, text='只展示已经通过本地校验的命中记录；未识别投注须另外核对。',
                  foreground=MUTED, wraplength=460).grid(row=6, column=0, columnspan=2,
                                                           sticky='w', pady=(5, 0))
        draw_result.rowconfigure(5, weight=1)
    @staticmethod
    def tree(parent, columns):
        box = ttk.Frame(parent)
        box.pack(fill='both', expand=True)
        tree = ttk.Treeview(box, columns=[c[0] for c in columns], show='headings', selectmode='browse', height=6)
        vs = ttk.Scrollbar(box, orient='vertical', command=tree.yview)
        hs = ttk.Scrollbar(box, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        tree.grid(row=0, column=0, sticky='nsew')
        vs.grid(row=0, column=1, sticky='ns')
        hs.grid(row=1, column=0, sticky='ew')
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        for key, title, width in columns:
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=50, stretch=True, anchor='w' if key == 'raw' else 'center')
        return tree

    @staticmethod
    def set_text(widget, text):
        if widget.get('1.0', 'end-1c') == text:
            return
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.insert('1.0', text)
        widget.configure(state='disabled')

    def filter_changed(self, event=None):
        if self._threshold_timer:
            self.after_cancel(self._threshold_timer)
        self._threshold_timer = self.after(300, self.apply_filter)

    def apply_filter(self):
        self._threshold_timer = None
        try:
            value = cents(self.threshold.get())/100
            self.controller.settings['risk_threshold'] = value
            self.controller.settings['threshold_metric'] = 'payout' if self.metric.get() == '总赔付' else 'net_loss'
            self.store.save_settings(self.controller.settings)
            self.error = ''
            self.refresh()
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.warning.configure(text='阈值无效或设置未保存：'+str(exc))

    def switch_period(self, event=None):
        value = self.period.get().strip()
        if not value or len(value) > 40 or any(char in value for char in '/\\'):
            self.fail(ValueError('请输入有效日期或期号'))
            return
        self.controller.activate_period(value)
        self.refresh()

    def refresh(self):
        self._sync_draw_context()
        lottery = self.lottery.get()
        cache_key = (self.store.change_serial,self.controller.period,lottery)
        if self._records_cache[0] != cache_key:
            records = [r for r in self.controller.records() if r.lottery == lottery]
            self._records_cache = (cache_key,records)
        else:
            records = self._records_cache[1]
        rows = []
        for record in records:
            has_partial = bool(record.bets and record.pending_fragments)
            amount = (money(sum(cents(b.principal) for b in record.bets)) +
                      ('（部分）' if has_partial else '')) if record.status == '已识别' or has_partial else '—'
            rows.append((record.record_id,(record.created_at[11:19], record.source, record.raw_text, amount, record.status),
                         () if record.status == '已识别' else ('pending',)))
        self.records_tree.sync_rows(rows)
        recognized = sum(r.status == '已识别' for r in records)
        self.records_summary.configure(text=f'今日{len(records)}条 · 已识别{recognized} · 未识别{len(records)-recognized} · 已选{len(self.records_tree.selection())}')
        periods = list(dict.fromkeys([now().date().isoformat(), self.controller.period]+self.store.periods()))
        self.period_combo.configure(values=periods)
        inputs = self.controller.analysis_inputs(lottery,records)
        # Only financial inputs affect the 1000-outcome calculation; queue/status changes reuse it.
        signature = repr((self.controller.period,lottery,inputs))
        if signature == self._risk_key:
            self.update_status()
            return
        self._risk_key = signature
        self._risk_generation += 1
        generation = self._risk_generation
        if len(records) < 200:
            self._apply_risk_snapshot(self.controller.calculate_analysis(inputs))
        else:
            self.copy_button.configure(state='disabled')
            self.warning.configure(text='风险正在重新计算，当前显示为上次结果。',fg=RED)
            self._risk_next = (generation,inputs)
            self._start_risk_job()
        self.update_status()

    def _start_risk_job(self):
        if self._risk_running or not self._risk_next or self._closing:
            return
        generation,inputs = self._risk_next
        self._risk_next = None
        self._risk_running = True
        events = self.events
        def worker():
            try:
                events.put(('risk_ready',generation,Controller.calculate_analysis(inputs),''))
            except Exception as exc:
                events.put(('risk_ready',generation,None,str(exc)))
        self.risk_pool.submit(worker)

    def _apply_risk_snapshot(self, snapshot, error=''):
        lottery = self.lottery.get()
        previous_risk = self._risk_detail_number
        try:
            if error:
                raise ValueError(error)
            self.snapshot = snapshot
            self.error = ''
            self._render_risk_rows()
            if previous_risk and self.risks_tree.exists(previous_risk):
                self.risks_tree.selection_set(previous_risk)
                self.show_sources()
            else:
                self._risk_detail_number = None
                sync_tree(self.source_rows,[])
                self._risk_source_items = {}
                self.risk_source_title.configure(text='投注来源 · 请选择上方号码')
                self.set_text(self.sources, '点选风险号码，再点下方投注行查看原始消息和赔付计算依据。')
            self.risk_info.configure(text=f'{len(self.snapshot.risks)}个号码达到当前筛选条件  ·  点击号码查看原始投注与赔付依据')
            maximum = max((cents(p)-self.snapshot.revenue for p in self.snapshot.payouts.values()), default=0)
            for name, value in [('due', money(self.snapshot.principal)+'元'), ('income', money(self.snapshot.revenue)+'元'), ('risk', str(len(self.snapshot.risks))), ('loss', money(max(0, maximum))+'元')]:
                self.cards[name].configure(text=value)
            text = plan_text(self.snapshot.risks, self.controller.settings['direct_unit_cost'][lottery])
            if self.snapshot.pending:
                text = '当前统计不完整，以下仅供核对已验证部分，不能作为最终补单方案。\n'+text
            self.set_text(self.plan, text)
            note = '已计入全部补单成本' if self.controller.settings['include_hedge_cost'] else '倍数计算忽略补单成本'
            self.plan_note.configure(text=note)
            warnings = []
            if self.snapshot.pending:
                warnings.append(f'有{self.snapshot.pending}条待处理；已验证的部分投注已计入，未解决片段尚未计入，当前结果不完整。')
            batch = self.store.load_batch(self.controller.period, lottery)
            if batch and not self.controller.deadline_enabled():
                warnings.append('自动封盘已关闭；之前的截止快照仍留档，不代表当前实时统计。')
            if batch and batch['state'] == 'finalized' and self.controller.deadline_enabled():
                summary = batch['final']
                warnings.append('21:14截止快照已保存；已核实应收'
                                f"{summary['verified_principal_yuan']:g}元，"
                                f"未解决{summary['unresolved_messages']}条，"
                                + ('21:15前完成' if summary['completed_before_21_15'] else '超时完成/补建'))
            if self.snapshot.unconfirmed_receipts:
                warnings.append(f'{self.snapshot.unconfirmed_receipts}条未填实收，暂按应收计入收入。')
            if self.store.import_errors:
                warnings.append('部分旧数据迁入失败，请检查原文件。')
            self.warning.configure(text='  '.join(warnings) or '当前有效投注已完成计算。', fg=RED if warnings else MUTED)
            self.copy_button.configure(state='normal' if not self.snapshot.pending and any(r.plan_multiplier for r in self.snapshot.risks) else 'disabled')
        except (ValueError, KeyError, sqlite3.Error) as exc:
            self.snapshot = None
            self.error = str(exc)
            self.warning.configure(text='计算未完成：'+str(exc), fg=RED)
            self.set_text(self.plan, '方案暂不可用：'+str(exc))
            self.copy_button.configure(state='disabled')
            for card in self.cards.values():
                card.configure(text='—')
        self.update_status()

    def _sync_draw_context(self):
        """彩种或期次变化后，旧号码/旧试算不得沿用。"""
        context = (self.lottery.get(), self.controller.period)
        if self._draw_context == context:
            return
        self._draw_context = context
        self._draw_lookup_request += 1
        self._draw_busy = False
        self.draw_fetch_button.configure(state='normal', text='联网获取')
        self._draw_lookup_error_detail = ''
        self.draw_error_more.pack_forget()
        self._draw_online = None
        self._draw_preview = None
        self.draw_number.set('')
        self.draw_winners.delete(*self.draw_winners.get_children())
        for label in self.draw_cards.values():
            label.configure(text='—')
        self.draw_summary_label.configure(text='填写三位开奖号码，或点击联网获取；试算不会写入兑奖账本。', foreground=MUTED)
        saved = self.store.load_settlement(context[1], context[0])
        if saved:
            self.draw_source_label.configure(text=f'本期已有正式结算：{saved["draw"]}，应赔 {saved["total_payout_yuan"]:g} 元（不可覆盖）')
        else:
            self.draw_source_label.configure(text='')

    def _draw_edited(self, event=None):
        self._draw_lookup_request += 1
        self._draw_lookup_error_detail = ''
        self.draw_error_more.pack_forget()
        self._draw_online = None
        self._draw_preview = None
        self.draw_winners.delete(*self.draw_winners.get_children())
        for label in self.draw_cards.values():
            label.configure(text='—')
        self.draw_summary_label.configure(text='已修改开奖号码，点击“计算赔付”重新试算。', foreground=MUTED)
        self.draw_source_label.configure(text='当前号码来源：手动输入')

    def lookup_draw(self):
        lottery, period = self.lottery.get(), self.controller.period
        if not re.fullmatch(r'20\d{2}-\d{2}-\d{2}', period):
            messagebox.showwarning('无法联网获取', '请先在“日期／期号”选择 YYYY-MM-DD 日期。', parent=self)
            return
        if self._draw_busy:
            return
        self._draw_busy = True
        self._draw_lookup_request += 1
        request_id = self._draw_lookup_request
        self.draw_fetch_button.configure(state='disabled', text='查询中…')
        self.draw_source_label.configure(text=f'正在查询 {period} 的官方开奖结果…', foreground=MUTED)
        self.draw_error_more.pack_forget()
        self._draw_lookup_error_detail = ''
        event_queue = self.events   # 子线程不得捕获Tk窗口对象
        def worker():
            try:
                result = fetch_draw(lottery, period)
                event_queue.put(('draw_lookup', request_id, lottery, period, result, ''))
            except Exception as exc:
                event_queue.put(('draw_lookup', request_id, lottery, period, None, str(exc)))
        threading.Thread(target=worker, name='official-draw-fetch', daemon=True).start()

    def _apply_draw_lookup(self, request_id, lottery, period, result, error):
        if (request_id != self._draw_lookup_request or
                (lottery, period) != (self.lottery.get(), self.controller.period)):
            return
        self._draw_busy = False
        self.draw_fetch_button.configure(state='normal', text='联网获取')
        if error:
            self._draw_online = None
            self._draw_lookup_error_detail = str(error)
            # 不臆断尚未开奖：官网403/解析错误/列表滞后都可能造成查询失败。
            if '连接超时' in error or 'timed out' in error.lower():
                short = '联网连接超时，未取得有效开奖号码'
            elif '最新：' in error or '列表最新：' in error:
                match = re.search(r'(?:列表最新|最新)：(20\d{2}-\d{2}-\d{2})\s*第(\d+)期', error)
                short = ('所选日期无匹配公告；最新 ' + match.group(1) + ' 第' + match.group(2) + '期') if match else '未查到所选日期的开奖公告'
            elif 'HTTP 403' in error or 'HTTP 404' in error:
                short = '官网拒绝访问或页面暂不可用，请稍后重试'
            else:
                short = ('联网失败：' + error[:34]) if len(error) < 42 else '多来源查询未获得可核验的开奖号码'
            self.draw_source_label.configure(text=short, foreground=RED)
            self.draw_error_more.pack(anchor='w', pady=(4, 0))
            return
        self._draw_lookup_error_detail = ''
        self.draw_error_more.pack_forget()
        self.draw_number.set(result.number)
        self._draw_preview = None
        self._draw_online = result
        self.draw_source_label.configure(text=f'官网核对：{result.draw_date} · 第{result.issue}期 · {result.source_name}', foreground=MUTED)
        self.calculate_draw()

    def show_draw_lookup_error(self):
        if self._draw_lookup_error_detail:
            messagebox.showinfo('官网查询详情', self._draw_lookup_error_detail, parent=self)

    def calculate_draw(self):
        number = self.draw_number.get().strip()
        try:
            preview = self.controller.preview_draw(self.lottery.get(), number, self.controller.period)
        except (ValueError, KeyError) as exc:
            messagebox.showwarning('无法计算开奖赔付', str(exc), parent=self)
            return
        self._draw_preview = preview
        net = preview['net_income_yuan']
        self.draw_cards['income'].configure(text=f'{preview["income_yuan"]:g}元')
        self.draw_cards['payout'].configure(text=f'{preview["payout_yuan"]:g}元')
        self.draw_cards['net'].configure(text=('+' if net >= 0 else '-') + f'{abs(net):g}元',
                                          fg='#167647' if net >= 0 else RED)
        self.draw_cards['count'].configure(text=f'{preview["winning_count"]}笔')
        self.draw_winners.delete(*self.draw_winners.get_children())
        for item in preview['lines']:
            self.draw_winners.insert('', 'end', values=(
                item['play'], item['selection'], item['multiplier'], f'{item["payout_yuan"]:g}元'))
        self.draw_summary_label.configure(text=(
            f'计入收入 {preview["income_yuan"]:g}元 · 应赔 {preview["payout_yuan"]:g}元 · '
            f'净{("盈利" if net >= 0 else "亏损")} {abs(net):g}元 · '
            f'中奖明细 {preview["winning_count"]}笔' +
            (' · 结果不完整/需核对' if preview['warnings'] else ' · 已验证记录试算完毕')),
            foreground=RED if preview['warnings'] else INK)
        if self._draw_online and (self._draw_online.lottery, self._draw_online.draw_date, self._draw_online.number) == (self.lottery.get(),self.controller.period,number):
            self.draw_source_label.configure(text=f'官网核对：{self._draw_online.draw_date} · 第{self._draw_online.issue}期 · {self._draw_online.source_name}', foreground=MUTED)
        else:
            self._draw_online = None
            self.draw_source_label.configure(text='号码来源：手动输入（未经过官网核验）', foreground=MUTED)

    def show_draw_details(self):
        if not self._draw_preview or self._draw_preview.get('draw') != self.draw_number.get().strip() or self._draw_preview.get('period') != self.controller.period or self._draw_preview.get('lottery') != self.lottery.get():
            self.calculate_draw()
        result = self._draw_preview
        if not result:
            return
        win = tk.Toplevel(self)
        win.title(f'{result["lottery"]} · {result["period"]} · 开奖{result["draw"]} · 中奖明细')
        win.geometry('960x620')
        win.transient(self)
        headline = (f'开奖号码 {result["draw"]} | 已核对投注 {result["verified_bets"]}笔 | '
                    f'命中明细 {result["winning_count"]}笔 | 应赔 {result["payout_yuan"]:g}元')
        ttk.Label(win, text=headline, font=('Microsoft YaHei UI', 12, 'bold')).pack(anchor='w', padx=12, pady=(12,6))
        if result['warnings']:
            ttk.Label(win, text='注意：'+'；'.join(result['warnings']), foreground=RED, wraplength=920).pack(anchor='w', padx=12, pady=(0,6))
        columns = ('play','selection','times','money','sender','source')
        tree = ttk.Treeview(win, columns=columns, show='headings')
        for key,label,width in [('play','玩法',130),('selection','号码',110),('times','倍数',70),('money','应赔元',95),('sender','来源',120),('source','原始投注',420)]:
            tree.heading(key,text=label)
            tree.column(key,width=width,minwidth=55,stretch=key=='source')
        tree.pack(fill='both',expand=True,padx=12,pady=(0,8))
        for line in result['lines']:
            tree.insert('', 'end', values=(line['play'],line['selection'],line['multiplier'],
                                          f'{line["payout_yuan"]:g}',line['sender'],line['raw_text']))
        ttk.Button(win,text='关闭',command=win.destroy).pack(anchor='e',padx=12,pady=(0,10))

    def save_draw_settlement(self):
        """正式记账与试算分离：依旧要求已封盘、全笔无疑问。"""
        if not self._draw_preview or self._draw_preview.get('draw') != self.draw_number.get().strip():
            self.calculate_draw()
        preview = self._draw_preview
        if not preview:
            return
        if preview['blockers']:
            messagebox.showwarning('不能正式结算', '还有待核对事项，当前仅允许试算：\n'+'\n'.join(preview['blockers']), parent=self)
            return
        if not self.controller.deadline_enabled():
            messagebox.showwarning('不能正式结算', '21:14封盘开关当前关闭。仍然可以计算应赔并查看明细；正式结算需要启用封盘并完成当期截止汇总。', parent=self)
            return
        previous = self.store.load_settlement(self.controller.period, self.lottery.get())
        if previous:
            messagebox.showwarning('已结算', f'本期已结算开奖号码 {previous["draw"]}，不可覆盖。',parent=self)
            return
        online = self._draw_online
        verified = bool(online and (online.lottery,online.draw_date,online.number)==
                        (self.lottery.get(),self.controller.period,self.draw_number.get().strip()))
        source = (f'{online.source_name} · {online.issue}期' if verified else '手动输入（未联网核验）')
        prompt = (f'{self.lottery.get()} {self.controller.period} 开奖号码：{self.draw_number.get().strip()}\n'
                  f'来源：{source}\n预计算应赔：{preview["payout_yuan"]:g}元\n\n'
                  + (('核对提醒：'+'；'.join(preview['warnings'])+'\n') if preview['warnings'] else '') +
                  '正式结算后不能覆盖旧账本，确定保存吗？')
        if not messagebox.askyesno('确认正式开奖结算',prompt,parent=self):
            return
        draw_info = ({'source':online.source_name,'url':online.source_url,'issue':online.issue,
                      'draw_date':online.draw_date,'verified_online':True} if verified else
                     {'source':'手动输入','verified_online':False})
        try:
            saved = self.controller.record_settlement(self.lottery.get(),self.draw_number.get().strip(),
                                                       period=self.controller.period, draw_info=draw_info)
        except (ValueError, KeyError, sqlite3.Error) as exc:
            self.fail(exc)
            return
        self.draw_source_label.configure(text=f'已保存正式结算：{saved["draw"]} · 应赔{saved["total_payout_yuan"]:g}元（不可覆盖）')
        messagebox.showinfo('正式结算已保存',f'应赔 {saved["total_payout_yuan"]:g} 元，账本已单独保存。',parent=self)

    def update_status(self):
        ai_on = bool(self.controller.settings['ai'].get('enabled'))
        ai = f'转换中{len(self.ai_busy)}条' if self.ai_busy else ('已开启' if ai_on else '已关闭')
        if ai_on and (not self.ai_key or not self.controller.settings['ai'].get('model')):
            ai = '未配置'
        wechat_on = bool(self.wechat.listening)
        if hasattr(self, 'wechat_toggle_btn'):
            self.wechat_toggle_btn.configure(text='微信监听：开' if wechat_on else '微信监听：关', style='On.TButton' if wechat_on else 'Off.TButton')
        if hasattr(self, 'ai_toggle_btn'):
            self.ai_toggle_btn.configure(text='AI识别：开' if ai_on else 'AI识别：关', style='On.TButton' if ai_on else 'Off.TButton')
        if hasattr(self, 'history_button'):
            self.history_button.configure(state='disabled' if self._history_busy else 'normal', text='正在读取历史…' if self._history_busy else '读取历史消息')
        pending = self.snapshot.pending if self.snapshot else 0
        if hasattr(self, 'pending_label'):
            self.pending_label.configure(text=f'待处理：{pending}', fg=ORANGE if pending else '#167647')
        if hasattr(self, 'group_label'):
            wc = self.controller.settings.get('wechat', {})
            fc = self.wechat.group_display(wc.get('fc_group',''))
            tc = self.wechat.group_display(wc.get('tc_group',''))
            self.group_label.configure(text=f'福彩群：{fc}    体彩群：{tc}')
        cutoff_on = self.controller.deadline_enabled()
        is_frozen = self.store.load_batch(self.controller.period, self.lottery.get()) if cutoff_on else None
        cutoff_label = ('截止：关（可测试）' if not cutoff_on else
                        ('已封盘' if is_frozen else '21:14截止：开'))
        self.status.configure(text=f'微信：{self.wechat.status}    AI：{ai}    {cutoff_label}    更新：{now():%H:%M:%S}')

    def toggle_wechat(self):
        if self.wechat.listening:
            if not messagebox.askyesno('停止微信监听', '停止后将无法接收新的微信群投注数据。\n确定停止微信监听？', parent=self):
                return
            self.wechat.stop()
            self.update_status()
            return
        try:
            ok, result = self.wechat.start()
            if not ok:
                raise RuntimeError(result)
            self.update_status()
        except Exception as exc:
            self.fail(exc)

    def toggle_ai(self):
        enabled = bool(self.controller.settings['ai'].get('enabled'))
        if enabled:
            pending = self.snapshot.pending if self.snapshot else 0
            if (pending or self.ai_busy) and not messagebox.askyesno('关闭AI识别', f'当前还有{pending}条待确认、{len(self.ai_busy)}条正在转换。\n关闭后新的待确认数据不会自动交给AI，确定关闭？', parent=self):
                return
            self.controller.settings['ai']['enabled'] = False
            self.update_status()
            return
        if not self.ai_key or not self.controller.settings['ai'].get('model'):
            self.fail(ValueError('请先在设置中填写AI接口、模型和API Key'))
            return
        self.controller.settings['ai']['enabled'] = True
        self.resume_pending_ai()
        self.update_status()

    def history_dialog(self):
        wc = self.controller.settings.get('wechat', {})
        if not wc.get('fc_group') and not wc.get('tc_group'):
            self.fail(ValueError('请先在设置中绑定福彩群或体彩群'))
            return
        win = tk.Toplevel(self)
        win.title('读取历史消息')
        win.geometry('500x330')
        win.transient(self)
        target = tk.StringVar(value='当前彩种')
        scope = tk.StringVar(value='今天消息')
        ttk.Label(win, text='手动读取历史消息', font=('Microsoft YaHei UI',13,'bold')).pack(anchor='w', padx=18, pady=(18,8))
        ttk.Label(win, text='正常微信监听只接收开启后的新消息，不会自动回放历史。', foreground=MUTED).pack(anchor='w', padx=18, pady=(0,12))
        row = ttk.Frame(win); row.pack(fill='x', padx=18, pady=6)
        ttk.Label(row, text='读取群聊', width=12).pack(side='left')
        ttk.Combobox(row, textvariable=target, values=['当前彩种','福彩群','体彩群','两个群'], state='readonly').pack(side='left', fill='x', expand=True)
        row = ttk.Frame(win); row.pack(fill='x', padx=18, pady=6)
        ttk.Label(row, text='读取范围', width=12).pack(side='left')
        ttk.Combobox(row, textvariable=scope, values=['今天消息','最近50条','最近100条'], state='readonly').pack(side='left', fill='x', expand=True)
        ttk.Label(win, text='同一个微信消息ID不会重复导入；内容相同但消息ID不同仍按两笔处理。', foreground=MUTED, wraplength=450).pack(anchor='w', padx=18, pady=12)
        def run():
            ids=[]
            if target.get()=='当前彩种':
                ids=[wc.get('fc_group') if self.lottery.get()=='福彩' else wc.get('tc_group')]
            elif target.get()=='福彩群': ids=[wc.get('fc_group')]
            elif target.get()=='体彩群': ids=[wc.get('tc_group')]
            else: ids=[wc.get('fc_group'),wc.get('tc_group')]
            ids=[x for x in ids if x]
            if not ids:
                self.fail(ValueError('所选彩种还没有绑定微信群'))
                return
            if not messagebox.askyesno('确认读取历史', f'将读取：{target.get()}\n范围：{scope.get()}\n读取后会自动进入解析和风险计算。\n确定继续？', parent=win):
                return
            mode='today' if scope.get()=='今天消息' else 'recent'
            limit=50 if scope.get()=='最近50条' else 100
            self._history_busy=True
            self.update_status()
            win.destroy()
            def task():
                try:
                    result=self.wechat.read_history(ids, scope=mode, limit=limit)
                    self.events.put(('history_done', result))
                except Exception as exc:
                    self.events.put(('history_error', str(exc)))
            self.pool.submit(task)
        buttons=ttk.Frame(win); buttons.pack(fill='x', padx=18, pady=18)
        ttk.Button(buttons,text='取消',command=win.destroy).pack(side='right')
        ttk.Button(buttons,text='开始读取',style='Accent.TButton',command=run).pack(side='right',padx=(0,8))

    def add_event(self, event):
        if event.state & 1:
            return None
        self.add_manual()
        return 'break'

    def add_manual(self):
        try:
            text = self.input.get('1.0', 'end-1c').strip()
            if not text:
                self.fail(ValueError('请输入投注文字'))
                return
            # 实收字段校验失败不能连带丢掉原始投注。
            receipt = None
            receipt_warning = ''
            if self.receipt.get().strip():
                try:
                    receipt = cents(self.receipt.get()) / 100
                except (ValueError, TypeError):
                    receipt_warning = '实收填写格式无效，原文已保存但未登记实收，请在记录中修改'
            duplicate = self.controller.possible_duplicate(self.lottery.get(), text)
            if duplicate and not messagebox.askyesno('可能重复', '近期微信已有相同文字，仍要作为新投注添加吗？', parent=self):
                return
            record = self.controller.add(self.lottery.get(), text)
            added = list(self.controller.last_added_records)
            if not added:
                self.fail(ValueError('没有写入新记录，请核对是否重复导入同一微信消息'))
                return
            if receipt is not None:
                if len(added) != 1:
                    receipt_warning = '消息包含多个彩种，实收未自动分配，请分别修改'
                else:
                    record.received_amount = receipt
                    self.store.update(record, '登记实收')
            self.input.delete('1.0', 'end')
            if not receipt_warning:
                self.receipt.set('')
            # 显式“排/体”必须归体彩；自动切换，确保操作者能看见记录。
            lotteries = {r.lottery for r in added}
            if len(lotteries) == 1:
                self.lottery.set(added[0].lottery)
            elif self.lottery.get() not in lotteries:
                self.lottery.set(added[0].lottery)
            self.refresh()
            for item in added:
                self.queue_ai(item)
            counts = '、'.join(f'{lottery}{sum(r.lottery == lottery for r in added)}条' for lottery in ('福彩','体彩') if lottery in lotteries)
            pending = sum(self.store.get(r.record_id).status != '已识别' for r in added)
            if pending:
                if any(self.store.get(r.record_id).status == '规则违规' for r in added):
                    state = '规则违规待人工处理（不发送AI）'
                else:
                    state = '有待处理片段（只有真正疑难项才发送AI）'
                message = f'已保存：{counts}；{pending}条{state}'
            else:
                message = f'已保存并识别：{counts}'
            if receipt_warning:
                message += '；' + receipt_warning
            self.add_feedback.configure(text=message, foreground=ORANGE if pending or receipt_warning else '#167647')
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.fail(exc)

    def selected_record(self):
        selected = self.records_tree.selection()
        if not selected:
            return None
        return self.store.get(selected[0])

    def selected_records(self):
        return [record for record in (self.store.get(iid) for iid in self.records_tree.selection()) if record]

    def context_record(self):
        return self.store.get(self._context_record_id) if self._context_record_id else None

    def view_record(self, record_id=None):
        record = self.store.get(record_id) if record_id else self.selected_record()
        if not record:
            return
        win = tk.Toplevel(self)
        win.title('投注详情')
        win.geometry('740x540')
        win.minsize(610, 430)
        outer = ttk.Frame(win, padding=12)
        outer.pack(fill='both', expand=True)

        # 固定底部操作区；正文再长也不会把复制说明按钮挤出窗口。
        actions = ttk.Frame(outer)
        actions.pack(side='bottom', fill='x', pady=(8, 0))
        if record.note and record.status in ('待确认','规则违规','部分识别','AI待确认'):
            def copy_note():
                win.clipboard_clear()
                win.clipboard_append(record.note)
                win.update_idletasks()
            ttk.Button(actions, text='复制说明／失败原因', command=copy_note).pack(side='left')
        if record.status == 'AI待确认':
            def accept():
                try:
                    self.controller.accept_ai(record)
                    self.refresh()
                    win.destroy()
                except (ValueError, sqlite3.Error) as exc:
                    self.fail(exc)
            ttk.Button(actions, text='确认AI转换并计入', command=accept).pack(side='right')
        ttk.Button(actions, text='关闭', command=win.destroy).pack(side='right', padx=(0, 8))

        # 说明固定占据少量高度，独立滚动；不依赖用户把窗口拉大。
        note_area = ttk.LabelFrame(outer, text='识别说明 / 失败原因', padding=(8, 5))
        note_area.pack(side='bottom', fill='x', pady=(8, 0))
        note_frame = ttk.Frame(note_area)
        note_frame.pack(fill='both', expand=True)
        note_text = tk.Text(note_frame, height=4, wrap='word', padx=8, pady=5,
                            bg='#f7f9fc', fg=INK, bd=1, relief='solid')
        note_scroll = ttk.Scrollbar(note_frame, orient='vertical', command=note_text.yview)
        note_text.configure(yscrollcommand=note_scroll.set)
        note_text.pack(side='left', fill='both', expand=True)
        note_scroll.pack(side='right', fill='y')
        self.set_text(note_text, record.note or '无异常说明')
        win.failure_note_text = note_text  # UI测试定位，不存储任何数据。

        # 剩余高度全部交给可滚动的原文和明细。
        content_area = ttk.Frame(outer)
        content_area.pack(fill='both', expand=True)
        text = tk.Text(content_area, wrap='word', padx=12, pady=10)
        scroll = ttk.Scrollbar(content_area, orient='vertical', command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        lines = [f'{record.lottery} / {record.period} / {record.status}',
                 f'识别状态：{record.parse_status}  人工审核：{record.review_status}',
                 f'来源：{record.source}  发送人：{record.sender or "未登记"}',
                 f'收到时间：{record.created_at}',
                 '\n接收原文：'+(record.source_original_text or record.raw_text),
                 '\n当前解析文字：'+record.raw_text]
        if record.pending_fragments:
            lines.append('\n未解决片段（不计金额）：')
            lines.extend(f"[{item['start']}:{item['end']}] {item['text']}：{item['reason']}" for item in record.pending_fragments)
        if record.ai_candidate:
            lines.append('\nAI转换：'+record.ai_candidate)
        lines.append('\n逐笔明细：')
        lines.extend(f'{b.selection} {b.play} {b.multiplier}倍，应收{b.principal:g}元' for b in record.bets)
        lines.append('确认实收：'+('未填写，按应收计入收入' if record.received_amount is None else f'{record.received_amount:g}元'))
        self.set_text(text, '\n'.join(lines))

    def edit_record(self):
        record = self.selected_record()
        if not record:
            return
        win = tk.Toplevel(self)
        win.title('修改投注／登记实收')
        win.geometry('650x330')
        text = tk.Text(win, height=6, wrap='word')
        text.pack(fill='both', expand=True, padx=16, pady=16)
        text.insert('1.0', record.raw_text)
        row = ttk.Frame(win)
        row.pack(fill='x', padx=16)
        ttk.Label(row, text='已确认实收（留空按应收计入）').pack(side='left')
        receipt = tk.StringVar(value='' if record.received_amount is None else f'{record.received_amount:g}')
        ttk.Entry(row, textvariable=receipt).pack(side='right')
        def save():
            try:
                amount = cents(receipt.get())/100 if receipt.get().strip() else None
                if not messagebox.askyesno('确认修改', '保存后会立即重新计算本金、赔付和风险结果。\n确定保存这条修改？', parent=win):
                    return
                self.controller.edit(record, text.get('1.0', 'end-1c'), amount)
                self.refresh()
                self.queue_ai(record)
                win.destroy()
            except (ValueError, OSError, sqlite3.Error) as exc:
                self.fail(exc)
        ttk.Button(win, text='保存并重算', style='Accent.TButton', command=save).pack(pady=16)

    def copy_context_raw(self):
        record = self.context_record()
        if not record:
            return
        self.clipboard_clear()
        self.clipboard_append(record.raw_text)
        self.update_idletasks()

    def delete_current_record(self):
        record = self.context_record()
        if not record:
            return
        preview = record.raw_text.replace('\r', ' ').replace('\n', ' ')
        if len(preview) > 80:
            preview = preview[:77] + '…'
        if not messagebox.askyesno('删除当前数据', f'即将删除鼠标所在的这条投注记录：\n\n{preview}\n\n删除后会立即重新计算本金、赔付和风险结果。\n此操作不可撤销，确定删除？', parent=self):
            return
        try:
            removed = self.store.delete_many([record.record_id])
            if removed:
                self.ai_busy.discard(record.record_id)
                self._manual_ai.discard(record.record_id)
            self._context_record_id = None
            self.records_tree.clear_selection()
            self.refresh()
        except (sqlite3.Error, ValueError) as exc:
            self.fail(exc)

    def reparse_record(self):
        self.reparse_selected(single_ok=True)

    def reparse_selected(self, single_ok=False):
        records = self.selected_records()
        if not records:
            return
        if not messagebox.askyesno('重新识别', f'将重新识别 {len(records)} 条数据。\n识别结果可能变化，并会立即重新计算风险。\n确定继续？', parent=self):
            return
        try:
            for record in records:
                self.controller.reparse(record)
                self.queue_ai(record)
            self.refresh()
        except (ValueError, sqlite3.Error) as exc:
            self.fail(exc)

    def delete_record(self):
        self.delete_selected()

    def delete_selected(self):
        records = self.selected_records()
        if not records:
            return
        if not messagebox.askyesno('删除选中数据', f'即将删除 {len(records)} 条投注记录。\n删除后会立即重新计算本金、赔付和风险结果。\n此操作不可撤销，确定删除？', parent=self):
            return
        try:
            ids = [r.record_id for r in records]
            removed = self.store.delete_many(ids)
            # 已删除的后台AI回包可能还在队列中；主线程收件时再查数据库，
            # 找不到记录就直接丢弃，不允许重新入账。
            self.ai_busy.difference_update(ids)
            self._manual_ai.difference_update(ids)
            self._context_record_id = None
            self.records_tree.clear_selection()
            self.refresh()
            remaining = [rid for rid in ids if self.store.get(rid)]
            if remaining or removed != len(ids):
                messagebox.showwarning('删除结果需核对',
                    f'本次选中{len(ids)}条，实际删除{removed}条；仍有效{len(remaining)}条。请检查当前筛选条件。',
                    parent=self)
        except (sqlite3.Error, ValueError) as exc:
            self.fail(exc)

    def ai_record(self):
        self.ai_selected()

    def ai_selected(self):
        records = self.selected_records()
        if not records:
            return
        if len(records) > 1 and not messagebox.askyesno('AI转换选中数据', f'将把 {len(records)} 条待确认数据提交给AI转换。\n确定继续？', parent=self):
            return
        for record in records:
            self.queue_ai(record, force=True)

    def copy_selected_raw(self):
        records = self.selected_records()
        if not records:
            return
        self.clipboard_clear()
        self.clipboard_append('\n\n'.join(r.raw_text for r in records))
        self.update_idletasks()

    def select_record_status(self, recognized):
        wanted = '已识别' if recognized else None
        self.records_tree.select_where(lambda iid, values: (values[4] == wanted) if recognized else (values[4] != '已识别'))
        self._update_record_selection_count()

    def _update_record_selection_count(self):
        records=[r for r in self.controller.records() if r.lottery==self.lottery.get()]
        recognized=sum(r.status=='已识别' for r in records)
        if hasattr(self,'records_summary'):
            self.records_summary.configure(text=f'今日{len(records)}条 · 已识别{recognized} · 未识别{len(records)-recognized} · 已选{len(self.records_tree.selection())}')

    def record_context_menu(self, event):
        self._context_record_id = getattr(event, 'iid', None)
        self._update_record_selection_count()
        has_current = bool(self._context_record_id and self.records_tree.exists(self._context_record_id))
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label='查看当前原文', command=lambda:self.view_record(self._context_record_id), state='normal' if has_current else 'disabled')
        menu.add_command(label='复制当前原文', command=self.copy_context_raw, state='normal' if has_current else 'disabled')
        menu.add_separator()
        menu.add_command(label='全选', command=lambda:(self.records_tree.select_all(), self._update_record_selection_count()))
        menu.add_command(label='反选', command=lambda:(self.records_tree.invert_selection(), self._update_record_selection_count()))
        menu.add_command(label='取消选择', command=lambda:(self.records_tree.clear_selection(), self._update_record_selection_count()))
        menu.add_command(label='选中未识别', command=lambda:self.select_record_status(False))
        menu.add_command(label='选中已识别', command=lambda:self.select_record_status(True))
        menu.add_separator()
        menu.add_command(label='复制选中原始数据', command=self.copy_selected_raw)
        menu.add_command(label='重新识别选中数据', command=self.reparse_selected)
        menu.add_command(label='AI转换选中数据', command=self.ai_selected)
        menu.add_separator()
        menu.add_command(label='删除当前数据', command=self.delete_current_record, foreground=RED, state='normal' if has_current else 'disabled')
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def queue_ai(self, record, force=False, retry_feedback=''):
        if record:
            record = self.store.get(record.record_id)
        if not record or record.record_id in self.ai_busy:
            return
        if force and record.ai_request_state=='started' and record.status=='AI识别中':
            record.status='部分识别' if record.bets and record.pending_fragments else '待确认'
        # 用户主动处理旧待审单时先用当前本地核重试；升级后已能识别的不应付费发AI。
        if record.engine_version!=ENGINE_VERSION and record.status in ('待确认','部分识别'):
            self.controller.reparse(record)
            record=self.store.get(record.record_id)
            self.refresh()
        if record.status not in ('待确认', '部分识别'):
            if force and hasattr(self, 'add_feedback'):
                if record.status == '规则违规':
                    tip = '原文属于明确无效玩法，不能由AI擅自改成其它投注；请核对原始消息。'
                elif record.status == '已识别':
                    tip = '本条已由本地程序识别，不需要重复调用AI。'
                else:
                    tip = '这条记录当前不需要重新发给AI；如需重试请先重新识别。'
                self.add_feedback.configure(text=tip, foreground=ORANGE)
            return
        # 提交时带上同条原文与已确认投注语境，不能只发孤立的“4”或“定位”。
        ai_text = self.controller.ai_request_text(record, allow_existing_candidate=force)
        if retry_feedback and ai_text:
            try:
                context=json.loads(ai_text)
                if not isinstance(context,dict) or 'raw_text' not in context:raise ValueError()
            except ValueError:
                context={'raw_text':record.raw_text,'lottery':record.lottery}
            context['validation_feedback']=retry_feedback[:320]
            context['上次本地复核拒绝原因']=retry_feedback[:320]
            context['feedback_instruction']='反馈不是新原文；仅依据raw_text重提候选，不增加购买次数'
            ai_text=json.dumps(context,ensure_ascii=False)
        if not ai_text:
            if force:
                if self.controller.cutoff_applies(record.period):
                    # 真正开启封盘且已过截止时，才弹出封盘限制提醒。
                    self.fail(ValueError('当前已启用21:14自动封盘，今日投注已截止，不能再提交AI'))
                elif hasattr(self, 'add_feedback'):
                    # 不再将规则疑问/短标题等与“封盘”混为同一弹窗。
                    self.add_feedback.configure(
                        text='该条未找到有原文依据的待识别投注；可查看详情。关闭封盘时不会阻止其它有效投注的AI识别。',
                        foreground=ORANGE)
            return
        if not self.ai_key or not self.controller.settings['ai'].get('model'):
            if force:
                self.fail(ValueError('请先在设置中填写AI接口、模型和API Key'))
            return
        if not force and not self.controller.settings['ai'].get('enabled'):
            return
        # 用户主动重新提交先前失败的AI候选时，保留历史并清除旧候选，避免被旧状态阻止。
        if force and record.ai_candidate:
            record.ai_history.append({'action':'manual_retry', 'at':now().isoformat(),
                                      'previous_candidate':record.ai_candidate})
            record.ai_candidate = ''
        revision = record.revision
        record.status = 'AI识别中'
        record.ai_request_state = 'started'
        record.note = '正在转换，原文已保存；其他投注继续计算'
        self.store.update(record, '提交AI转换')
        self.ai_busy.add(record.record_id)
        if force:
            self._manual_ai.add(record.record_id)
        service = self.ai
        queued_at=perf_counter()
        def task():
            timings={'queue_s':perf_counter()-queued_at,'retry_count':1 if retry_feedback else 0}
            try:
                candidate, reason = service.parse_unknown(record.lottery, ai_text)
                metrics=getattr(service,'last_metrics',{})
                if isinstance(metrics,dict):timings.update(metrics)
                self.events.put(('ai_metrics',record.record_id,revision,timings))
                self.events.put(('ai', record.record_id, revision, candidate, reason))
            except AIError as exc:
                metrics=getattr(service,'last_metrics',{})
                if isinstance(metrics,dict):timings.update(metrics)
                self.events.put(('ai_metrics',record.record_id,revision,timings))
                self.events.put(('ai_error', record.record_id, revision, str(exc)))
            except Exception as exc:
                safe = _redact(str(exc), self.ai_key, [{'role':'user','content':ai_text}])
                self.events.put(('ai_error', record.record_id, revision, 'AI执行失败（'+type(exc).__name__+'）：'+safe+'；原始数据已保存'))
        self.pool.submit(task)
        self.refresh()

    def _retry_invalid_ai_once(self, record_id):
        """模型已返回但本地复核没过：给一次携带错误原因的自动修正机会。"""
        record = self.store.get(record_id)
        if not record or not self.controller.settings['ai'].get('enabled'):
            return False
        if record.status not in ('待确认', '部分识别') or not record.ai_candidate:
            return False
        if any(row.get('action') == 'auto_repair' for row in record.ai_history):
            return False
        if any(not item.get('ai_eligible', False) for item in record.pending_fragments):
            return False
        reason = record.note.split('；AI复核未通过：')[-1][:300]
        if reason == record.note:
            return False
        record.ai_candidate = ''
        record.ai_history.append({'action':'auto_repair', 'at':now().isoformat(),
                                  'rejected_reason':reason})
        record.note = 'AI第一次转换未通过本地校验，正在携带错误原因自动重试一次：' + reason
        self.store.update(record, 'AI复核未通过自动重试一次')
        self.queue_ai(record, retry_feedback=reason)
        return record.record_id in self.ai_busy

    def show_ai_error_detail(self, detail):
        """手动AI转换失败时显示可复制详情；自动监听失败只写在记录说明，避免刷屏。"""
        if self._closing:
            return
        win = tk.Toplevel(self)
        win.title('AI识别失败 · 详细原因')
        win.geometry('720x350')
        win.transient(self)
        ttk.Label(win, text='这笔投注的原始数据已保留，可在投注详情查看。以下是程序收到的具体错误：', wraplength=675).pack(fill='x', padx=16, pady=(14,8))
        panel = ttk.Frame(win)
        panel.pack(fill='both', expand=True, padx=16)
        area = tk.Text(panel, wrap='word', height=7)
        bar = ttk.Scrollbar(panel, orient='vertical', command=area.yview)
        area.configure(yscrollcommand=bar.set)
        area.pack(side='left', fill='both', expand=True)
        bar.pack(side='right', fill='y')
        area.insert('1.0', detail)
        area.configure(state='disabled')
        footer = ttk.Frame(win)
        footer.pack(fill='x', padx=16, pady=14)
        def copy_detail():
            win.clipboard_clear()
            win.clipboard_append(detail)
            win.update_idletasks()
        ttk.Button(footer, text='复制错误详情', command=copy_detail).pack(side='left')
        ttk.Button(footer, text='关闭', command=win.destroy).pack(side='right')

    def pump(self):
        if self._closing:
            return
        if self._timer:
            self.after_cancel(self._timer)
            self._timer=None
        dirty = False
        try:
            completed = self.controller.tick_deadline()
            dirty = bool(completed)
            for _ in range(30):
                if self.events.empty():
                    break
                event = self.events.get_nowait()
                if event[0] == 'draw_lookup':
                    _, rid, lottery, period, result, error = event
                    self._apply_draw_lookup(rid, lottery, period, result, error)
                    continue
                if event[0]=='ai_metrics':
                    if not hasattr(self,'_ai_timings'):self._ai_timings={}
                    self._ai_timings[(event[1],event[2])]=event[3]
                    continue
                if event[0] == 'ai':
                    _, record_id, revision, candidate, reason = event
                    self.ai_busy.discard(record_id)
                    was_manual = record_id in self._manual_ai
                    self._manual_ai.discard(record_id)
                    if not self.controller.settings['ai'].get('enabled') and not was_manual:
                        record = self.store.get(record_id)
                        if record and record.revision == revision and record.status == 'AI识别中':
                            record.status='部分识别' if record.pending_fragments and record.bets else '待确认'
                            record.note='AI识别已关闭，未解决片段保留待确认'
                            self.store.update(record,'关闭AI后保留原文')
                        dirty=True
                        continue
                    applied = self.controller.apply_ai_candidate(record_id, revision, candidate, reason)
                    if isinstance(self.ai, AIService):
                        metrics=getattr(self,'_ai_timings',{}).pop((record_id,revision),{})
                        metrics.update(getattr(self.controller,'last_ai_metrics',{}))
                        self.ai._log('投注本地校验完成', accepted=bool(applied), record_id=record_id,**metrics)
                    if not applied:
                        latest = self.store.get(record_id)
                        if latest and latest.status == 'AI识别中' and not self.controller.ai_text_for_record(latest):
                            latest.status = '部分识别' if latest.pending_fragments and latest.bets else '待确认'
                            if self.controller.cutoff_applies(latest.period):
                                latest.deadline_state = 'unresolved_at_deadline'
                            latest.note = ('截止拦截AI结果' if self.controller.cutoff_applies(latest.period)
                                           else 'AI候选未通过本地复核，原文仍保留')
                            self.store.update(latest, '截止拦截AI结果')
                        if latest and latest.revision != revision:
                            self.queue_ai(latest)
                        elif latest and not was_manual and self._retry_invalid_ai_once(record_id):
                            # 只自动再试一次；不让失败候选在程序重启后反复扣费。
                            dirty = True
                    dirty = True
                elif event[0] == 'ai_error':
                    _, record_id, revision, reason = event
                    self.ai_busy.discard(record_id)
                    was_manual = record_id in self._manual_ai
                    self._manual_ai.discard(record_id)
                    getattr(self,'_ai_timings',{}).pop((record_id,revision),None)
                    record = self.store.get(record_id)
                    if record and record.revision == revision and record.status in ('待确认', 'AI识别中'):
                        record.status = '部分识别' if record.pending_fragments and record.bets else '待确认'
                        record.ai_request_state = 'unknown_billing' if ('超时' in reason or 'timed out' in reason.lower()) else 'failed'
                        record.note = reason + ('；本地已识别部分仍保留' if record.bets else '')
                        if (self.controller.cutoff_applies(record.period)
                                and not self.controller.ai_text_for_record(record)):
                            record.deadline_state = 'unresolved_at_deadline'
                        self.store.update(record, 'AI失败保留原文')
                        if hasattr(self, 'add_feedback'):
                            self.add_feedback.configure(text='AI识别失败：'+reason[:210], foreground=RED)
                        if was_manual:
                            self.after(10, lambda detail=reason: self.show_ai_error_detail(detail))
                        dirty = True
                    elif record and record.revision != revision:
                        self.queue_ai(record)
                elif event[0] == 'risk_ready':
                    _,generation,snapshot,error = event
                    self._risk_running = False
                    if generation == self._risk_generation:
                        self._apply_risk_snapshot(snapshot,error)
                    self._start_risk_job()
                elif event[0] == 'test':
                    _, label, result = event
                    if label.winfo_exists():
                        label.configure(text=result)
                elif event[0] == 'ai_diag_line':
                    _, window, log_box, line = event
                    if window.winfo_exists():
                        log_box.configure(state='normal')
                        log_box.insert('end', line + '\n')
                        log_box.see('end')
                        log_box.configure(state='disabled')
                elif event[0] == 'ai_diag_done':
                    _, window, action_button, status_label, result, path = event
                    if window.winfo_exists():
                        action_button.configure(state='normal')
                        status_label.configure(text=('诊断已完成，日志：'+path) if path else '诊断未完成：'+result)
                elif event[0] == 'history_done':
                    _, result = event
                    self._history_busy = False
                    dirty = True
                    self.after(10, lambda r=result: messagebox.showinfo('历史消息读取完成', f"新增 {r.get('inserted',0)} 条\n已存在 {r.get('duplicate',0)} 条\n过滤/忽略 {r.get('ignored',0)} 条", parent=self))
                elif event[0] == 'history_error':
                    _, reason = event
                    self._history_busy = False
                    self.after(10, lambda m=reason: self.fail(RuntimeError(m)))
            added, errors = self.controller.drain_inbox()
            for record in added:
                self.queue_ai(record)
            dirty = dirty or bool(added)
            if errors:
                self.wechat.last_error = errors[0]
                self.wechat.status = '消息尚未处理：'+errors[0]
            today = now().date().isoformat()
            if today != self.last_day:
                if self.controller.period == self.last_day:
                    self.controller.activate_period(today)
                    self.period.set(today)
                    dirty = True
                self.last_day = today
            if dirty:
                self.refresh()
            else:
                self.update_status()
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.warning.configure(text='数据处理失败，原始消息仍保留：'+str(exc), fg=RED)
        self._timer = self.after(200, self.pump)

    def _position_initial_risk_details(self, event=None):
        """首次显示风险页时，给投注来源足够的高度；之后用户可自行拖动分隔线。"""
        try:
            if self._risk_sash_initialized:
                return
            height = event.height if event else self.risk_pane.winfo_height()
            if height >= 310:
                self._risk_sash_initialized = True
                bottom_height = min(215, max(165, int(height * 0.48)))
                self.risk_pane.sash_place(0, 1, height - bottom_height)
        except tk.TclError:
            pass

    def _render_risk_rows(self):
        if not self.snapshot:
            return
        rows = list(self.snapshot.risks)
        key = self._risk_sort_key
        keyfunc = {
            'number': lambda r: r.number,
            'payout': lambda r: r.payout,
            'loss': lambda r: r.net_loss,
            'plan': lambda r: r.plan_multiplier,
        }[key]
        rows.sort(key=lambda r: (keyfunc(r), r.number), reverse=self._risk_sort_reverse)
        sync_tree(self.risks_tree,[(r.number,(r.number,yuan_fmt(r.payout),yuan_fmt(r.net_loss),str(r.plan_multiplier)),
                                  ('odd',) if i % 2 else ()) for i,r in enumerate(rows)])

    def sort_risks(self, key):
        if key not in ('number', 'payout', 'loss', 'plan'):
            return
        if self._risk_sort_key == key:
            self._risk_sort_reverse = not self._risk_sort_reverse
        else:
            self._risk_sort_key, self._risk_sort_reverse = key, key != 'number'
        current = self.risks_tree.selection()
        self._render_risk_rows()
        if current and self.risks_tree.exists(current[0]):
            self.risks_tree.selection_set(current[0])

    def show_sources(self, event=None):
        selected = self.risks_tree.selection()
        if not selected:
            return
        number = selected[0]
        self._risk_detail_number = number
        matched = self.controller.explain(self.lottery.get(), number)
        old_selection = self.source_rows.selection()
        self._risk_source_items = {}
        rows = []
        total = sum(payout for _rec, _bet, payout in matched)
        self.risk_source_title.configure(text=f'{number} · 命中{len(matched)}笔 · 合计赔付{yuan_fmt(total)}元')
        for i,(record,bet,payout) in enumerate(matched):
            key=record.record_id+':'+str(sum(item[0].record_id==record.record_id for item in matched[:i]))
            self._risk_source_items[key]=(record,bet,payout)
            rows.append((key,(bet.play,bet.selection,f'{bet.multiplier:g}',yuan_fmt(payout),record.sender or record.source),()))
        sync_tree(self.source_rows,rows)
        if matched:
            if not any(self.source_rows.exists(iid) for iid in old_selection):
                self.source_rows.selection_set(rows[0][0])
            self.show_source_detail()
        else:
            self.set_text(self.sources, '本号码暂无命中投注。')

    def show_source_detail(self, event=None):
        selected = self.source_rows.selection()
        if not selected or selected[0] not in self._risk_source_items:
            return
        record, bet, payout = self._risk_source_items[selected[0]]
        details = (f'中奖依据：{bet.selection} {bet.play}，原始投注{bet.multiplier:g}倍，'
                   f'本笔应赔 {yuan_fmt(payout)} 元。\n'
                   f'来源：{record.source}  ·  发送人：{record.sender or "未登记"}\n'
                   f'原始消息：{record.source_original_text or record.raw_text}')
        self.set_text(self.sources, details)

    def show_all_sources(self):
        number = self._risk_detail_number
        if not number:
            messagebox.showinfo('投注来源', '请先选择一行风险开奖号码。', parent=self)
            return
        matched = self.controller.explain(self.lottery.get(), number)
        win = tk.Toplevel(self)
        win.title(f'{number} · 全部赔付来源（{len(matched)}笔）')
        win.geometry('870x560')
        win.transient(self)
        root = ttk.Frame(win, padding=12)
        root.pack(fill='both', expand=True)
        ttk.Label(root, text=f'开奖号码 {number} · 共 {len(matched)} 笔命中投注',
                  font=('Microsoft YaHei UI', 12, 'bold')).pack(anchor='w', pady=(0, 8))
        view = self.tree(root, [('payout', '该笔应赔', 100), ('play', '玩法', 110),
                                ('number', '投注号码', 125), ('times', '原始倍数', 90),
                                ('sender', '来源/发送人', 140), ('raw', '原始消息', 340)])
        for i,(record,bet,payout) in enumerate(matched):
            view.insert('', 'end', iid=str(i), values=(yuan_fmt(payout), bet.play, bet.selection,
                        f'{bet.multiplier:g}', record.sender or record.source,
                        record.source_original_text or record.raw_text))
        ttk.Button(root, text='关闭', command=win.destroy).pack(anchor='e', pady=(9, 0))

    def copy_plan(self):
        if not self.snapshot or self.snapshot.pending or self._risk_running or self._risk_next:
            return
        self.clipboard_clear()
        self.clipboard_append(plan_text(self.snapshot.risks, self.controller.settings['direct_unit_cost'][self.lottery.get()]))
        self.update_idletasks()
        self.copy_button.configure(text='已复制')
        if self._copy_timer:
            try:
                self.after_cancel(self._copy_timer)
            except tk.TclError:
                pass
        self._copy_timer = self.after(1500, self._reset_copy_button)

    def _reset_copy_button(self):
        self._copy_timer = None
        if not self._closing and self.copy_button.winfo_exists():
            self.copy_button.configure(text='一键复制')

    def export_csv(self):
        destination = filedialog.asksaveasfilename(parent=self, title='导出当前数据', defaultextension='.csv', initialfile=f'{self.lottery.get()}_{self.controller.period}.csv', filetypes=[('CSV表格', '*.csv')])
        if not destination:
            return
        def spreadsheet_safe(value):
            text = str(value)
            return "'"+text if text.startswith(('=', '+', '-', '@', '\t', '\r')) else text
        try:
            with open(destination, 'w', encoding='utf-8-sig', newline='') as handle:
                writer = csv.writer(handle)
                writer.writerow(['彩种', '期号', '来源', '发送人', '时间', '原文', '状态', '应收', '确认实收', '说明'])
                for record in self.controller.records():
                    if record.lottery == self.lottery.get():
                        writer.writerow([spreadsheet_safe(value) for value in [record.lottery, record.period, record.source, record.sender, record.created_at, record.raw_text, record.status, sum(b.principal for b in record.bets), '' if record.received_amount is None else record.received_amount, record.note]])
        except OSError as exc:
            self.fail(exc)

    def export_detail_csv(self):
        """每笔投注独立一行，未解决片段单独展示；不把待审金额当作0元投注。"""
        destination = filedialog.asksaveasfilename(
            parent=self, title='导出逐笔投注明细', defaultextension='.csv',
            initialfile=f'{self.lottery.get()}_{self.controller.period}_逐笔明细.csv',
            filetypes=[('CSV表格','*.csv')])
        if not destination:
            return
        def safe(value):
            value = '' if value is None else str(value)
            return "'"+value if value.startswith(('=', '+', '-', '@', '\t', '\r')) else value
        try:
            with open(destination, 'w', encoding='utf-8-sig', newline='') as handle:
                writer = csv.writer(handle)
                writer.writerow(['期号','彩种','消息ID','接收原文','识别状态','人工审核',
                                 '玩法','号码','倍数','单倍价格','本地计算金额',
                                 '来源文本','来源字符起点','来源字符终点','解析来源','待解决片段'])
                for record in self.controller.records():
                    if record.lottery != self.lottery.get():
                        continue
                    pending = ' | '.join(p.get('text','') for p in record.pending_fragments)
                    rows = record.bets or [None]
                    for bet in rows:
                        span = bet.source_span if bet else {}
                        data = [record.period,record.lottery,record.record_id,
                                record.source_original_text or record.raw_text,
                                record.parse_status,record.review_status,
                                bet.play if bet else '', bet.selection if bet else '',
                                bet.multiplier if bet else '',
                                bet.unit_price_yuan if bet else '',
                                bet.principal if bet else '',
                                span.get('text',''),span.get('start',''),span.get('end',''),
                                bet.parse_source if bet else '',pending]
                        writer.writerow([safe(v) for v in data])
        except OSError as exc:
            self.fail(exc)

    def export_batch_json(self):
        batch = self.store.load_batch(self.controller.period, self.lottery.get())
        if not batch or batch['state'] != 'finalized':
            self.fail(ValueError('本期还未生成截止汇总；只有开启21:14自动封盘后才会自动生成'))
            return
        if not self.controller.deadline_enabled():
            if not messagebox.askyesno('导出旧截止快照',
                    '自动封盘已关闭。此文件是以前保存的截止快照，不包含之后新增的测试投注。\n仍要导出存档吗？', parent=self):
                return
        destination = filedialog.asksaveasfilename(
            parent=self, title='导出截止汇总', defaultextension='.json',
            initialfile=f'{self.lottery.get()}_{self.controller.period}_截止汇总.json',
            filetypes=[('JSON','*.json')])
        if not destination:
            return
        try:
            with open(destination, 'w', encoding='utf-8') as handle:
                json.dump(batch, handle, ensure_ascii=False, indent=2)
        except OSError as exc:
            self.fail(exc)

    def export_contract_json(self):
        """输出包含原文、明细、问题及独立结算的统一JSON，供本地对账。"""
        destination = filedialog.asksaveasfilename(
            parent=self, title='导出结构化数据', defaultextension='.json',
            initialfile=f'{self.lottery.get()}_{self.controller.period}_结构化.json',
            filetypes=[('JSON','*.json')])
        if not destination:
            return
        lottery = self.lottery.get()
        result = {
            'schema_version':'1.0', 'lottery':lottery,
            'period':self.controller.period,
            'messages':[record_to_contract(r) for r in self.controller.records()
                        if r.lottery == lottery],
            'batch':self.store.load_batch(self.controller.period, lottery),
            'settlement':self.store.load_settlement(self.controller.period, lottery),
        }
        try:
            with open(destination, 'w', encoding='utf-8') as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2)
        except OSError as exc:
            self.fail(exc)

    def settings_dialog(self):
        win = tk.Toplevel(self)
        win.title('设置')
        screen_w = max(800, win.winfo_screenwidth())
        screen_h = max(600, win.winfo_screenheight())
        width = min(940, max(800, screen_w - 140))
        height = min(820, max(600, screen_h - 120))
        win.geometry(f'{width}x{height}')
        win.minsize(760, min(600, height))
        win.transient(self)

        notebook = ttk.Notebook(win)
        notebook.pack(fill='both', expand=True, padx=16, pady=(16, 8))
        pages = {}

        def make_scrollable_page(title):
            holder = ttk.Frame(notebook)
            notebook.add(holder, text=title)
            canvas = tk.Canvas(holder, highlightthickness=0, bd=0, bg=BG)
            scrollbar = ttk.Scrollbar(holder, orient='vertical', command=canvas.yview)
            canvas.configure(yscrollcommand=scrollbar.set)
            canvas.pack(side='left', fill='both', expand=True)
            scrollbar.pack(side='right', fill='y')
            page = ttk.Frame(canvas, padding=12)
            window_id = canvas.create_window((0, 0), window=page, anchor='nw')

            def sync_scrollregion(event=None):
                canvas.configure(scrollregion=canvas.bbox('all'))

            def fit_width(event):
                canvas.itemconfigure(window_id, width=event.width)

            def wheel(event):
                if event.delta:
                    canvas.yview_scroll(int(-event.delta / 120), 'units')
                return 'break'

            page.bind('<Configure>', sync_scrollregion)
            canvas.bind('<Configure>', fit_width)
            canvas.bind('<MouseWheel>', wheel)
            page.bind('<MouseWheel>', wheel)
            pages[title] = page
            return page

        for title in ['微信监听', 'AI识别', '计算规则', '赔率表', '自动封盘', '自定义写法', '数据管理']:
            if title in {'计算规则', 'AI识别', '微信监听'}:
                make_scrollable_page(title)
            else:
                page = ttk.Frame(notebook, padding=12)
                notebook.add(page, text=title)
                pages[title] = page
        settings = deepcopy(self.controller.settings)
        rules = deepcopy(self.controller.rules)
        fields = {}
        def entry(parent, label, value, key, show=None):
            row = ttk.Frame(parent)
            row.pack(fill='x', pady=5)
            ttk.Label(row, text=label, width=23).pack(side='left')
            variable = tk.StringVar(value=str(value))
            widget = ttk.Entry(row, textvariable=variable, show=show or '')
            widget.pack(side='left', fill='x', expand=True)
            fields[key] = variable
            return widget
        general = pages['计算规则']
        ttk.Label(general, text='复试按明细规则表：单倍本金固定10元；奖金按三码330、四码140、五码72、六码41、七码26、八码17、九码12。包含对子，豹子不算中奖。', wraplength=720).pack(anchor='w', pady=(0, 8))
        for lottery in LOTTERIES:
            entry(general, lottery+'外部直选单倍奖金', settings['external_direct_prize'][lottery], 'prize'+lottery)
            entry(general, lottery+'外部直选豹子奖金', settings['external_leopard_direct_prize'][lottery], 'leopard'+lottery)
            entry(general, lottery+'外部直选每倍成本', settings['direct_unit_cost'][lottery], 'cost'+lottery)
        cost_mode = tk.BooleanVar(value=settings['include_hedge_cost'])
        ttk.Checkbutton(general, text='计入全部补单成本后计算倍数', variable=cost_mode).pack(anchor='w', pady=8)
        deadline_enabled = tk.BooleanVar(value=bool(settings.get('deadline', {}).get('enabled', False)))
        deadline_page = pages['自动封盘']
        ttk.Label(deadline_page, text='每日封盘规则', font=('Microsoft YaHei UI', 13, 'bold')).pack(anchor='w', pady=(3, 12))
        ttk.Checkbutton(deadline_page, text='启用每日21:14自动封盘（北京时间）',
                        variable=deadline_enabled).pack(anchor='w', pady=(0, 12))
        ttk.Label(deadline_page,
                  text='默认关闭：不限制测试和正常识别。勾选保存后，当天北京时间21:14开始拒收新单并冻结快照，21:15为汇总完成目标。',
                  wraplength=690, foreground=MUTED, justify='left').pack(anchor='w', pady=(0, 10))
        ttk.Label(deadline_page,
                  text='关闭开关不会删除已经生成的旧快照；过去已经正式结算的记录也不会被覆盖。',
                  wraplength=690, foreground=RED, justify='left').pack(anchor='w')
        ttk.Label(general, text='定位收费已固定：一码定位每个展开数字1倍10元；两码定位每个展开组合1倍10元；三码定位每个展开组合1倍2元。',
                  wraplength=720).pack(anchor='w', pady=(10, 3))
        ttk.Label(general, text='豹子开出：独胆、定位、单独豹子和跨度0照常中奖；对应直选默认1600元/倍，单独豹子1600元/倍、跨度0为800元/倍。其余玩法全杀。', wraplength=720).pack(anchor='w', pady=14)
        rule_page = pages['赔率表']
        rule_lottery = tk.StringVar(value=self.lottery.get())
        combo = ttk.Combobox(rule_page, textvariable=rule_lottery, values=LOTTERIES, state='readonly', width=12)
        combo.pack(anchor='w', pady=(0, 8))
        rule_tree = self.tree(rule_page, [('play', '玩法', 180), ('principal', '单倍本金', 130), ('odds', '赔率', 100), ('prize', '单倍赔付', 130)])
        ttk.Label(rule_page, text='双击玩法修改。本金×赔率=单倍赔付。复试也严格按表格中的固定本金10元和对应奖金计算。').pack(anchor='w', pady=8)
        def refresh_rules(event=None):
            rule_tree.delete(*rule_tree.get_children())
            for play, rule in rules[rule_lottery.get()].items():
                principal = rule['principal']
                rule_tree.insert('', 'end', iid=play, values=(play, principal, f'{rule["prize"]/principal:g}', rule['prize']))
        combo.bind('<<ComboboxSelected>>', refresh_rules)
        def edit_rule(event=None):
            selected = rule_tree.selection()
            if not selected:
                return
            play, lottery = selected[0], rule_lottery.get()
            edit = tk.Toplevel(win)
            edit.title(lottery+' '+play)
            edit.geometry('480x300' if play == '直选' else '480x240')
            principal = tk.StringVar(value=str(rules[lottery][play]['principal']))
            prize = tk.StringVar(value=str(rules[lottery][play]['prize']))
            leopard_prize = tk.StringVar(value=str(rules[lottery][play].get('leopard_prize',1600)))
            rule_fields = [('单倍本金', principal), ('单倍赔付', prize)]
            if play == '直选':
                rule_fields.append(('豹子单倍赔付',leopard_prize))
            for label, var in rule_fields:
                row = ttk.Frame(edit, padding=10)
                row.pack(fill='x')
                ttk.Label(row, text=label).pack(side='left')
                widget = ttk.Entry(row, textvariable=var)
                widget.pack(side='right')
            def set_rule():
                try:
                    p, q = cents(principal.get()), cents(prize.get())
                    if p <= 0 or q <= 0:
                        raise ValueError('本金和赔付须为正数')
                    replacement = {'principal': p/100, 'prize': q/100}
                    if play == '直选':
                        leopard = cents(leopard_prize.get())
                        if leopard <= 0:
                            raise ValueError('豹子赔付须为正数')
                        replacement['leopard_prize'] = leopard/100
                    rules[lottery][play] = replacement
                    refresh_rules()
                    edit.destroy()
                except ValueError as exc:
                    self.fail(exc)
            ttk.Button(edit, text='确定', command=set_rule).pack(pady=10)
        rule_tree.bind('<Double-1>', edit_rule)
        refresh_rules()
        ai_page = pages['AI识别']
        ttk.Label(ai_page, text='只发送无法识别的原文。AI结果通过程序校验后自动计入，金额由程序计算。').pack(anchor='w', pady=(0, 10))
        api_format = tk.StringVar(value='GPT文档格式（prompt）' if settings['ai'].get('api_format')=='completions' else 'Chat格式（messages）')
        ttk.Combobox(ai_page, textvariable=api_format, values=['GPT文档格式（prompt）','Chat格式（messages）'], state='readonly', width=32).pack(anchor='w', pady=(0, 8))
        entry(ai_page, 'API地址（包含/v1等路径）', settings['ai']['base_url'], 'ai_url')
        entry(ai_page, '模型名称', settings['ai']['model'], 'ai_model')
        timeout_row = ttk.Frame(ai_page)
        timeout_row.pack(fill='x', pady=5)
        ttk.Label(timeout_row, text='AI请求超时时间', width=23).pack(side='left')
        ai_timeout = tk.StringVar(value=str(effective_timeout(settings['ai'])))
        ttk.Combobox(timeout_row, textvariable=ai_timeout, values=('15','30','60','90','120'),
                     state='readonly', width=12).pack(side='left')
        ttk.Label(timeout_row, text='秒（推荐60秒；只影响正常AI识别，诊断5/30秒独立）', foreground=MUTED).pack(side='left', padx=10)
        key_widget = entry(ai_page, 'API Key（加密保存）', self.ai_key, 'ai_key', show='●')
        show_key = tk.BooleanVar()
        ttk.Checkbutton(ai_page, text='显示Key', variable=show_key, command=lambda:key_widget.configure(show='' if show_key.get() else '●')).pack(anchor='w')
        ai_auto = tk.BooleanVar(value=settings['ai'].get('auto_start', settings['ai'].get('enabled', False)))
        ttk.Checkbutton(ai_page, text='程序启动后自动开启AI识别', variable=ai_auto).pack(anchor='w', pady=10)
        json_mode = tk.BooleanVar(value=settings['ai'].get('json_mode', False))
        ttk.Checkbutton(ai_page, text='Chat接口支持强制JSON格式（文档默认不勾选）', variable=json_mode).pack(anchor='w')

        prompt_title = ttk.Frame(ai_page)
        prompt_title.pack(fill='x', pady=(12, 4))
        ttk.Label(prompt_title, text='AI解析提示词（可修改）').pack(side='left')
        ttk.Label(prompt_title, text='只影响AI文字转换，不改变程序计算规则', foreground=MUTED).pack(side='left', padx=(10, 0))
        prompt_box = ttk.Frame(ai_page)
        prompt_box.pack(fill='x')
        ai_prompt = tk.Text(prompt_box, wrap='word', height=9)
        ai_prompt_scroll = ttk.Scrollbar(prompt_box, orient='vertical', command=ai_prompt.yview)
        ai_prompt.configure(yscrollcommand=ai_prompt_scroll.set)
        ai_prompt.pack(side='left', fill='both', expand=True)
        ai_prompt_scroll.pack(side='right', fill='y')
        ai_prompt.insert('1.0', settings['ai'].get('prompt') or DEFAULT_AI_PROMPT)
        prompt_actions = ttk.Frame(ai_page)
        prompt_actions.pack(fill='x', pady=(4, 8))
        def reset_ai_prompt():
            if messagebox.askyesno('恢复默认提示词', '将把AI解析提示词恢复为程序内置最新版。\n保存设置后生效，确定恢复？', parent=win):
                ai_prompt.delete('1.0', 'end')
                ai_prompt.insert('1.0', DEFAULT_AI_PROMPT)
        ttk.Button(prompt_actions, text='恢复默认提示词', command=reset_ai_prompt).pack(side='left')

        test_label = ttk.Label(ai_page, text='')
        test_label.pack(anchor='w', pady=8)
        def test_ai():
            service = AIService({'base_url':fields['ai_url'].get(), 'model':fields['ai_model'].get(), 'timeout':int(ai_timeout.get()), 'timeout_configured':True,
                                 'api_format':'completions' if api_format.get().startswith('GPT') else 'chat',
                                 'json_mode':json_mode.get()}, fields['ai_key'].get())
            test_label.configure(text='正在测试…')
            def task():
                try:
                    _, result = service.test_connection()
                except Exception as exc:
                    result = str(exc)
                self.events.put(('test', test_label, result))
            self.pool.submit(task)
        ttk.Button(ai_page, text='测试连接', command=test_ai).pack(anchor='w')

        def show_ai_diagnostics():
            # 所有值取自当前输入框，即使用户尚未保存设置也能排查。
            diagnostic_config = {
                'base_url': fields['ai_url'].get().strip(),
                'model': fields['ai_model'].get().strip(),
                'api_format': 'completions' if api_format.get().startswith('GPT') else 'chat',
                'json_mode': json_mode.get(),
                'timeout': int(ai_timeout.get()),
                'timeout_configured': True,
                'prompt': ai_prompt.get('1.0', 'end-1c').strip(),
            }
            diag_key = fields['ai_key'].get().strip()
            diag_rules = deepcopy(self.controller.rules)
            diag_policy = deepcopy(self.controller.settings.get('policy', {}))
            if not diagnostic_config['base_url'] or not diagnostic_config['model'] or not diag_key:
                messagebox.showwarning('诊断条件不足', '请先填写API地址、模型名称和API Key。', parent=win)
                return
            dialog = tk.Toplevel(win)
            dialog.title('AI连接与实际请求诊断')
            dialog.geometry('850x600')
            dialog.minsize(650, 420)
            dialog.transient(win)
            ttk.Label(dialog, text='将发送真实API请求：5秒短测试、失败时30秒对照、30秒完整识别请求。会产生少量模型费用。', wraplength=800).pack(fill='x', padx=15, pady=(14, 5))
            ttk.Label(dialog, text='使用当前输入的模型/接口/提示词；使用模拟下注数据，不上传实际客户记录。日志不保存API Key、提示词全文或模型回答全文。', wraplength=800, foreground=MUTED).pack(fill='x', padx=15, pady=(0, 8))
            log_panel = ttk.Frame(dialog)
            log_panel.pack(fill='both', expand=True, padx=15, pady=6)
            log_text = tk.Text(log_panel, wrap='word', state='disabled', font=('Consolas',10))
            log_scroll = ttk.Scrollbar(log_panel, orient='vertical', command=log_text.yview)
            log_text.configure(yscrollcommand=log_scroll.set)
            log_text.pack(side='left', fill='both', expand=True)
            log_scroll.pack(side='right', fill='y')
            path_label = ttk.Label(dialog, text='点击开始后实时显示诊断结果。', wraplength=800)
            path_label.pack(fill='x', padx=15, pady=(4, 8))
            actions = ttk.Frame(dialog)
            actions.pack(fill='x', padx=15, pady=(0, 14))

            def copy_report():
                raw = log_text.get('1.0','end-1c')
                if raw.strip():
                    dialog.clipboard_clear()
                    dialog.clipboard_append(raw)
                    dialog.update_idletasks()

            ttk.Button(actions, text='复制诊断日志', command=copy_report).pack(side='right', padx=(8,0))
            ttk.Button(actions, text='打开日志文件夹', command=lambda: os.startfile(str(self.store.data_dir / 'ai_diagnostics')) if os.name == 'nt' and (self.store.data_dir / 'ai_diagnostics').is_dir() else messagebox.showinfo('日志位置', str(self.store.data_dir / 'ai_diagnostics'), parent=dialog)).pack(side='right')

            def run_diagnostic():
                start_button.configure(state='disabled')
                path_label.configure(text='正在发送真实请求，请等待；这不会影响投注记录和风险计算。')
                # 独立线程，避免占用正常AI识别的2个工作线程。
                def worker():
                    try:
                        def validate_text(text):
                            parser = BetParser(diag_rules, diag_policy)
                            bets, _normalized, reason = parser.parse('福彩', text)
                            return bool(bets) and not reason, f'有效投注{len(bets)}笔' if bets and not reason else ('原因：'+str(reason)[:160])
                        diag = AIDiagnostics(diagnostic_config, diag_key, self.store.data_dir / 'ai_diagnostics',
                            callback=lambda line: self.events.put(('ai_diag_line',dialog,log_text,line)), validator=validate_text)
                        path, _report = diag.run(sample=AI_DIAG_SAMPLE)
                        self.events.put(('ai_diag_done',dialog,start_button,path_label,'完成',path))
                    except Exception as exc:
                        # 异常只记录类别，避免外部库报错包含请求凭据。
                        self.events.put(('ai_diag_done',dialog,start_button,path_label,type(exc).__name__,''))
                threading.Thread(target=worker, name='ai-diagnostics', daemon=True).start()

            start_button = ttk.Button(actions, text='开始完整诊断', command=run_diagnostic)
            start_button.pack(side='left')

        ttk.Button(ai_page, text='诊断连接与发送请求', command=show_ai_diagnostics).pack(anchor='w', pady=(6, 0))
        wechat_page = pages['微信监听']
        ttk.Label(wechat_page, text='推荐“微信4.x本地监听”：设置页只负责配置。请先在主页面开启微信监听，读取到群列表后再回来选择福彩/体彩群。正常监听只接新消息；历史消息请在主页面手动读取。', wraplength=720).pack(anchor='w', pady=(0, 8))
        backend_names = {'wechat4':'微信4.x本地监听（推荐）','bridge':'本机消息桥接','wcf':'WCF采集服务'}
        backend = tk.StringVar(value=backend_names.get(settings['wechat'].get('backend'),'微信4.x本地监听（推荐）'))
        ttk.Combobox(wechat_page, textvariable=backend, values=list(backend_names.values()), state='readonly', width=28).pack(anchor='w', pady=5)
        auto_start = tk.BooleanVar(value=settings['wechat'].get('auto_start', True))
        ttk.Checkbutton(wechat_page, text='程序启动后自动开启微信监听', variable=auto_start).pack(anchor='w', pady=(0, 6))
        filter_self = tk.BooleanVar(value=settings['wechat'].get('filter_self', True))
        ttk.Checkbutton(wechat_page, text='过滤自己发送的微信消息', variable=filter_self).pack(anchor='w', pady=(0, 8))
        entry(wechat_page, '微信账号（多账号才填）', settings['wechat'].get('wechat4_account',''), 'wechat4_account')
        entry(wechat_page, '监听间隔（秒）', settings['wechat'].get('poll_interval',0.5), 'poll_interval')
        entry(wechat_page, '本机接收端口', settings['wechat']['bridge_port'], 'bridge_port')
        entry(wechat_page, 'WCF采集地址', settings['wechat']['wcf_host'], 'wcf_host')
        entry(wechat_page, 'WCF采集端口', settings['wechat']['wcf_port'], 'wcf_port')
        group_ids = {key: tk.StringVar(value=settings['wechat'].get(key,'')) for key in ('fc_group','tc_group')}
        group_displays = {}
        for lottery, key in [('福彩','fc_group'), ('体彩','tc_group')]:
            row = ttk.Frame(wechat_page)
            row.pack(fill='x', pady=5)
            ttk.Label(row, text=lottery+'群', width=23).pack(side='left')
            display_var = tk.StringVar(value=self.wechat.group_display(group_ids[key].get()))
            group_displays[key] = display_var
            ttk.Entry(row, textvariable=display_var, state='readonly').pack(side='left', fill='x', expand=True)
            def select_group(var=group_ids[key], display=display_var):
                groups = self.wechat.group_list()
                if not groups:
                    self.fail(ValueError('尚未获取群列表。请先回主页面开启微信监听，等待状态显示已读取群列表，再回来选择聊天群。'))
                    return
                chooser = tk.Toplevel(win)
                chooser.title('选择聊天群')
                chooser.geometry('620x460')
                chooser.minsize(500, 360)
                chooser.transient(win)

                search_row = ttk.Frame(chooser, padding=(12, 12, 12, 6))
                search_row.pack(fill='x')
                ttk.Label(search_row, text='搜索群聊').pack(side='left')
                search_var = tk.StringVar()
                search_entry = ttk.Entry(search_row, textvariable=search_var)
                search_entry.pack(side='left', fill='x', expand=True, padx=(10, 0))

                result_label = ttk.Label(chooser, text='', foreground=MUTED)
                result_label.pack(fill='x', padx=12, pady=(0, 4))

                list_frame = ttk.Frame(chooser)
                list_frame.pack(fill='both', expand=True, padx=12, pady=(0, 8))
                listing = tk.Listbox(list_frame, activestyle='dotbox')
                scroll = ttk.Scrollbar(list_frame, orient='vertical', command=listing.yview)
                listing.configure(yscrollcommand=scroll.set)
                listing.pack(side='left', fill='both', expand=True)
                scroll.pack(side='right', fill='y')

                visible_groups = []

                def refresh_group_list(*args):
                    keyword = search_var.get().strip().lower()
                    visible_groups.clear()
                    listing.delete(0, 'end')
                    for group_id, name in groups:
                        haystack = f'{name} {group_id}'.lower()
                        if keyword and keyword not in haystack:
                            continue
                        visible_groups.append((group_id, name))
                        listing.insert('end', self.wechat.friendly_group_name(group_id, name))
                    result_label.configure(text=f'找到 {len(visible_groups)} 个群聊')
                    if visible_groups:
                        listing.selection_clear(0, 'end')
                        listing.selection_set(0)
                        listing.activate(0)

                def choose(event=None):
                    selected = listing.curselection()
                    if not selected or not visible_groups:
                        return
                    gid, gname = visible_groups[selected[0]]
                    var.set(gid)
                    display.set(self.wechat.friendly_group_name(gid, gname))
                    chooser.destroy()

                search_var.trace_add('write', refresh_group_list)
                search_entry.bind('<Return>', choose)
                listing.bind('<Return>', choose)
                listing.bind('<Double-1>', choose)
                chooser.bind('<Escape>', lambda event: chooser.destroy())

                buttons = ttk.Frame(chooser, padding=(12, 0, 12, 12))
                buttons.pack(fill='x')
                ttk.Button(buttons, text='取消', command=chooser.destroy).pack(side='right')
                ttk.Button(buttons, text='选定', style='Accent.TButton', command=choose).pack(side='right', padx=(0, 8))

                refresh_group_list()
                search_entry.focus_set()
            ttk.Button(row, text='选择', command=select_group).pack(side='left', padx=(8,0))
        receiver_label = ttk.Label(wechat_page, text=self.wechat.status, wraplength=720)
        receiver_label.pack(anchor='w', pady=8)
        def copy_bridge():
            self.clipboard_clear()
            self.clipboard_append(f'POST http://127.0.0.1:{fields["bridge_port"].get()}/message\nAuthorization: Bearer {self.wechat.token}')
            receiver_label.configure(text='接收地址和本次认证令牌已复制。更改端口请保存后重启接收。')
        ttk.Button(wechat_page, text='复制桥接接收地址与认证', command=copy_bridge).pack(anchor='w')
        data_page = pages['数据管理']
        ttk.Label(data_page, text='数据导出与备份', font=('Microsoft YaHei UI', 13, 'bold')).pack(anchor='w', pady=(4, 8))
        ttk.Label(data_page, text='导出操作与主页面顶部「数据导出」相同，不会清空或改写投注记录。',
                  foreground=MUTED, wraplength=700).pack(anchor='w', pady=(0, 12))
        for title, action in [('导出当前数据 CSV', self.export_csv),
                              ('导出逐笔明细 CSV', self.export_detail_csv),
                              ('导出结构化 JSON', self.export_contract_json),
                              ('导出截止汇总 JSON', self.export_batch_json)]:
            ttk.Button(data_page, text=title, command=action).pack(anchor='w', fill='x', pady=4)
        ttk.Separator(data_page).pack(fill='x', pady=(15, 11))
        ttk.Label(data_page, text='数据保护提醒：升级界面不需要替换或删除 data 文件夹；建议在测试环境中验收。',
                  foreground=MUTED, wraplength=690, justify='left').pack(anchor='w')
        alias_page = pages['自定义写法']
        ttk.Label(alias_page, text='每行：原写法 => 标准投注。按整条原文匹配，不会随意替换数字。').pack(anchor='w', pady=(0, 6))
        example = (
            '示例（仅演示，不会自动保存）：\n'
            '293一单一组50倍 => 293单组各50倍\n'
            '5781复十倍 => 5781复试10倍\n'
            '毒7十倍 => 7独胆10倍'
        )
        ttk.Label(alias_page, text=example, foreground=MUTED, justify='left', wraplength=760).pack(fill='x', anchor='w', pady=(0, 8))
        ttk.Label(alias_page, text='下面只填写你真正要保存的自定义写法：').pack(anchor='w', pady=(0, 4))
        alias_box = ttk.Frame(alias_page)
        alias_box.pack(fill='both', expand=True)
        aliases = tk.Text(alias_box, wrap='word', height=7)
        alias_scroll = ttk.Scrollbar(alias_box, orient='vertical', command=aliases.yview)
        aliases.configure(yscrollcommand=alias_scroll.set)
        aliases.pack(side='left', fill='both', expand=True)
        alias_scroll.pack(side='right', fill='y')
        aliases.insert('1.0', '\n'.join(f'{k} => {v}' for k,v in settings.get('aliases',{}).items()))
        footer = ttk.Frame(win)
        footer.pack(fill='x', padx=16, pady=(0, 16))
        def save(close=True):
            try:
                for lottery in LOTTERIES:
                    settings['external_direct_prize'][lottery] = cents(fields['prize'+lottery].get())/100
                    settings['external_leopard_direct_prize'][lottery] = cents(fields['leopard'+lottery].get())/100
                    settings['direct_unit_cost'][lottery] = cents(fields['cost'+lottery].get())/100
                settings['include_hedge_cost'] = cost_mode.get()
                settings['deadline'] = {'enabled': deadline_enabled.get()}
                settings['policy'] = {'position_cost': 'combination', 'span0_wins': True}
                if (deadline_enabled.get() and not self.controller.deadline_enabled()
                        and is_closed(self.controller.period, now())):
                    if not messagebox.askyesno('立即启用21:14封盘',
                            '北京时间今天已过21:14。现在启用会立即停止接收今天的新单，并尝试生成截止快照。\n'
                            '此前关闭开关期间加入的测试数据也可能被统计。已有截止快照不会覆盖。\n'
                            '确定现在开启吗？', parent=win):
                        return False
                prompt_value = ai_prompt.get('1.0','end-1c').strip()
                if not prompt_value:
                    raise ValueError('AI解析提示词不能为空')
                settings['ai'].update(base_url=fields['ai_url'].get().strip(), model=fields['ai_model'].get().strip(), auto_start=ai_auto.get(), enabled=self.controller.settings['ai'].get('enabled', False),
                                      api_format='completions' if api_format.get().startswith('GPT') else 'chat', json_mode=json_mode.get(), prompt=prompt_value,
                                      timeout=int(ai_timeout.get()), timeout_configured=True)
                selected_backend = {'微信4.x本地监听（推荐）':'wechat4','本机消息桥接':'bridge','WCF采集服务':'wcf'}[backend.get()]
                interval = float(fields['poll_interval'].get())
                if not 0.2 <= interval <= 5:
                    raise ValueError('微信监听间隔须为0.2～5秒')
                settings['wechat'].update(backend=selected_backend, auto_start=auto_start.get(), filter_self=filter_self.get(), poll_interval=interval,
                                          wechat4_account=fields['wechat4_account'].get().strip(),
                                          bridge_port=int(fields['bridge_port'].get()), wcf_host=fields['wcf_host'].get().strip(),
                                          wcf_port=int(fields['wcf_port'].get()), fc_group=group_ids['fc_group'].get().strip(),
                                          tc_group=group_ids['tc_group'].get().strip())
                mapping = {}
                for line in aliases.get('1.0','end-1c').splitlines():
                    if line.strip():
                        if '=>' not in line:
                            raise ValueError('自定义写法每行须包含 =>')
                        key, value = (item.strip() for item in line.split('=>',1))
                        if not key or not value or key in mapping:
                            raise ValueError('自定义写法不能为空或重复')
                        mapping[key] = value
                settings['aliases'] = mapping
                settings['ai']['api_key'] = fields['ai_key'].get().strip()
                self.controller.save_settings(settings, rules)
                self.ai_key = fields['ai_key'].get().strip()
                self.ai = AIService(self.controller.settings['ai'], self.ai_key, self.store.data_dir / 'ai_requests')
                was_listening = self.wechat.listening
                known_groups = dict(self.wechat.group_list())
                if was_listening:
                    self.wechat.stop()
                self.wechat = WeChatService(self.store.path, self.controller.settings['wechat'])
                self.wechat.groups.update(known_groups)
                if was_listening:
                    self.wechat.start()
                self.refresh()
                for record in self.controller.records():
                    self.queue_ai(record)
                if close:
                    win.destroy()
                return True
            except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
                self.fail(exc)
                return False
        ttk.Label(footer, text='设置只保存配置；AI和微信开关请在主页面操作。', foreground=MUTED).pack(side='left')
        ttk.Button(footer, text='保存并重算', style='Accent.TButton', command=save).pack(side='right')

    @staticmethod
    def template_text():
        return """统一写法：号码／条件 + 玩法 + 倍数。多笔用逗号或换行分开。

【默认规则】
293                    只有三位数字：默认直选1倍
293 5倍               没写玩法：默认直选5倍
293 154 826 5倍       多个三位号码：全部直选5倍
福／福彩／3D          = 福彩3D
体／体彩／排／排列三  = 体彩排列三
同一条消息可同时写福、体，程序按标记拆开；标记作用到下一个彩种标记。

【普通直选／组选】
293单50倍             三位固定位置直选
293组50倍             精确组六组选
505组1倍              精确组三组选
293单组各50倍         拆成单选与组选两笔
组选:003 588 669 245（一倍）   一批具体三位组选，共用1倍

【多码玩法】
0257组六10倍          四码至九码组六，按数字数量识别
095组三1倍            二码至九码组三，按数字数量识别
全包组六10倍／组六全包10倍
全包组三10倍／组三全包10倍
258复试10倍           三码至九码复试；按《明细规则2.xlsx》固定本金/奖金，包含对子、豹子不中奖

【大底批量下注】
大底本质仍是“批量直选/批量组选”，不是新赔率玩法。号码可空格或换行排列。

示例1：大底直选半倍
122 128 129 182 192
219 221 223 229 237
福10注直选半倍 合10

示例2：大底直选1倍
223 229 322 922
福4注直选1倍 合8

示例3：大底组选
003 588 669 245
福4注组选1倍 合8

规则：
- 半倍 = 0.5倍。
- “N注”和“合X”只做校验；实际号码数或金额对不上时整组进入待确认。
- 大底号码重复不会自动去重，会按重复投注保留并提示。
- 体彩同样写法，把“福”换成“体/排/排列三”即可。

【其它玩法】
7独胆10倍
27双飞5倍
55对子3倍
豹子5倍
百8定位5倍
百8十2定位5倍
百069十568个14定位8倍
跨度6 10倍            跨度0～9

可写“组六358421 10倍”“复试5781一倍”，或在数字间加空格；“复式”仍兼容为旧写法。
可加“合计100元”，程序会核对金额，不用自报金额代替计算。

关键：505组1倍 与 095组三1倍 是不同玩法。
本地无法识别时可交AI，AI结果通过程序校验后自动计入。
有歧义或未通过校验的消息整条保留待确认。"""

    def fail(self, error):
        messagebox.showerror('未完成', str(error), parent=self)

    def close(self):
        if self._closing:
            return
        self._closing = True
        if self._timer:
            self.after_cancel(self._timer)
        if self._threshold_timer:
            self.after_cancel(self._threshold_timer)
        if self._copy_timer:
            try:
                self.after_cancel(self._copy_timer)
            except tk.TclError:
                pass
            self._copy_timer = None
        if self._wechat_start_timer:
            try:
                self.after_cancel(self._wechat_start_timer)
            except tk.TclError:
                pass
            self._wechat_start_timer = None
        self.wechat.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.risk_pool.shutdown(wait=False, cancel_futures=True)
        # Cancel every pending timer in this window's Tcl interpreter before child
        # widgets delete their registered callback commands (including after_idle layout).
        for timer in self.tk.call('after','info'):
            self.tk.call('after','cancel',timer)
        self.store.close()
        self.destroy()
