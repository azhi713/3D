import csv
import os
import queue
import sqlite3
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from config import APP_NAME, APP_VERSION, LOTTERIES, DEFAULT_AI_PROMPT
from models import cents, money, now
from data_store import DataStore
from controller import Controller
from ai_service import AIService, AIError, effective_timeout, _redact
from ai_diagnostics import AIDiagnostics, SAMPLE as AI_DIAG_SAMPLE
from parser import BetParser
from wechat_service import WeChatService
from risk_engine import plan_text

BG = '#f3f5fa'
INK = '#17243b'
MUTED = '#62718b'
BLUE = '#2563eb'
RED = '#be3345'
ORANGE = '#c46a14'
GRID = '#d7deea'


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
        self.window_id = self.canvas.create_window((0,0), window=self.inner, anchor='nw')
        self.inner.bind('<Configure>', lambda e:self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', lambda e:self.canvas.itemconfigure(self.window_id, width=e.width))
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
            self.delete(iid)
        frame = tk.Frame(self.inner, bg='white')
        frame.pack(fill='x', expand=False)
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
        self._rows[iid]={'frame':frame,'widgets':widgets,'values':tuple(values),'tags':tuple(tags)}
        self._vars[iid]=var
        self._paint(iid,False)
        return iid

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
                row['frame'].destroy()
            self._vars.pop(iid,None)
            if iid in self._selected:
                self._selected.remove(iid)
        self._sync_checks()


class RiskGridTable(tk.Frame):
    """带真实单元格分隔线的风险表。"""
    def __init__(self, parent, columns):
        super().__init__(parent, bg='white', bd=1, relief='solid')
        self.columns = columns
        self._rows = {}
        self._selected = []
        self._callbacks = {}
        header = tk.Frame(self, bg='#eaf0fa')
        self.header = header
        header.pack(fill='x')
        for i,(key,title,_width) in enumerate(columns):
            lbl=tk.Label(header,text=title,bg='#eaf0fa',fg=INK,font=('Microsoft YaHei UI',10,'bold'),bd=1,relief='solid',padx=6,pady=7)
            lbl.grid(row=0,column=i,sticky='nsew')
            header.columnconfigure(i,weight=1,minsize=max(65, int(_width)))
        body=tk.Frame(self,bg='white'); body.pack(fill='both',expand=True)
        self.canvas=tk.Canvas(body,bg='white',bd=0,highlightthickness=0,height=140)
        vs=ttk.Scrollbar(body,orient='vertical',command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vs.set)
        self.canvas.pack(side='left',fill='both',expand=True); vs.pack(side='right',fill='y')
        self.inner=tk.Frame(self.canvas,bg='white')
        self.window_id=self.canvas.create_window((0,0),window=self.inner,anchor='nw')
        self.inner.bind('<Configure>',lambda e:self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>',lambda e:self.canvas.itemconfigure(self.window_id,width=e.width))
        self.canvas.bind('<MouseWheel>',self._wheel)
    def _wheel(self,event):
        if event.delta: self.canvas.yview_scroll(int(-event.delta/120),'units')
        return 'break'
    def bind(self,sequence=None,func=None,add=None):
        if sequence=='<<TreeviewSelect>>': self._callbacks[sequence]=func; return None
        return super().bind(sequence,func,add)
    def tag_configure(self,*args,**kwargs): return None
    def get_children(self): return tuple(self._rows.keys())
    def exists(self,iid): return str(iid) in self._rows
    def selection(self): return tuple(self._selected)
    def selection_set(self,items):
        if isinstance(items,str): items=(items,)
        self._selected=[str(x) for x in items if str(x) in self._rows]
        self._paint_all()
    def _paint_all(self):
        sel=set(self._selected)
        for iid,row in self._rows.items():
            bg='#dce8ff' if iid in sel else row['base_bg']
            for w in row['widgets']: w.configure(bg=bg)
    def insert(self,parent,index,iid=None,values=(),tags=()):
        iid=str(iid)
        frame=tk.Frame(self.inner,bg='white'); frame.pack(fill='x',expand=True)
        widgets=[]
        base_bg='#f8fafc' if 'odd' in tags else 'white'
        for i,(key,_title,_width) in enumerate(self.columns):
            value=values[i] if i<len(values) else ''
            lbl=tk.Label(frame,text=str(value),bg=base_bg,fg=INK,bd=1,relief='solid',padx=6,pady=6,anchor='center')
            lbl.grid(row=0,column=i,sticky='nsew')
            frame.columnconfigure(i,weight=1,minsize=max(65, int(_width)))
            lbl.bind('<Button-1>',lambda e,x=iid:self._select(x,e))
            lbl.bind('<MouseWheel>',self._wheel)
            widgets.append(lbl)
        self._rows[iid]={'frame':frame,'widgets':widgets,'base_bg':base_bg,'values':tuple(values)}
        return iid
    def _select(self,iid,event):
        self._selected=[iid]; self._paint_all()
        cb=self._callbacks.get('<<TreeviewSelect>>')
        if cb: cb(event)
    def delete(self,*items):
        for iid in items:
            iid=str(iid); row=self._rows.pop(iid,None)
            if row: row['frame'].destroy()
            if iid in self._selected: self._selected.remove(iid)



class RiskApp(tk.Tk):
    def __init__(self, base_dir):
        super().__init__()
        self.base_dir = Path(base_dir)
        self.title(APP_NAME+' · '+APP_VERSION)
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        width = min(1440, max(1120, screen_w-100))
        height = min(980, max(800, screen_h-70))
        self.geometry(f'{width}x{height}')
        self.minsize(1120, 780)
        self.configure(bg=BG)
        self.store = DataStore(base_dir)
        self.controller = Controller(self.store)
        self.lottery = tk.StringVar(value='福彩')
        self.period = tk.StringVar(value=self.controller.period)
        self.threshold = tk.StringVar(value=str(self.controller.settings['risk_threshold']))
        self.metric = tk.StringVar(value='总赔付' if self.controller.settings['threshold_metric'] == 'payout' else '净亏损')
        self.receipt = tk.StringVar()
        self.events = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='bet-ai')
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
        style.configure('On.TButton', background='#e8f7ee', foreground='#167647')
        style.configure('Off.TButton', background='#fff0f1', foreground=RED)
        style.configure('Warn.TButton', background='#fff5e8', foreground=ORANGE)

    def _build(self):
        header = tk.Frame(self, bg=INK)
        header.pack(fill='x')
        tk.Label(header, text=APP_NAME, bg=INK, fg='white', font=('Microsoft YaHei UI', 19, 'bold')).pack(side='left', padx=24, pady=14)
        tk.Label(header, text='实时赔付分析', bg=INK, fg='#a8bddb').pack(side='left')
        ttk.Button(header, text='设置', command=self.settings_dialog).pack(side='right', padx=24)

        ops = tk.Frame(self, bg='white', bd=1, relief='solid')
        ops.pack(fill='x', padx=24, pady=(12, 8))
        left = tk.Frame(ops, bg='white')
        left.pack(side='left', fill='x', expand=True, padx=12, pady=10)
        self.wechat_toggle_btn = ttk.Button(left, text='微信监听：关', command=self.toggle_wechat)
        self.wechat_toggle_btn.pack(side='left', padx=(0,8))
        self.ai_toggle_btn = ttk.Button(left, text='AI识别：关', command=self.toggle_ai)
        self.ai_toggle_btn.pack(side='left', padx=(0,8))
        self.history_button = ttk.Button(left, text='读取历史消息', command=self.history_dialog)
        self.history_button.pack(side='left', padx=(0,12))
        self.pending_label = tk.Label(left, text='待处理：0', bg='white', fg=INK, font=('Microsoft YaHei UI',10,'bold'))
        self.pending_label.pack(side='left', padx=(4,12))
        self.group_label = tk.Label(left, text='', bg='white', fg=MUTED, anchor='w')
        self.group_label.pack(side='left', fill='x', expand=True)
        self.status = tk.Label(ops, bg='white', fg=MUTED, anchor='e')
        self.status.pack(side='right', padx=12)

        bar = ttk.Frame(self)
        bar.pack(fill='x', padx=24, pady=(2,8))
        for lottery, title in [('福彩', '福彩3D'), ('体彩', '体彩排列三')]:
            ttk.Radiobutton(bar, text=title, value=lottery, variable=self.lottery, command=self.refresh).pack(side='left', padx=(0, 20))
        ttk.Label(bar, text='日期／期号').pack(side='left', padx=(10, 6))
        self.period_combo = ttk.Combobox(bar, textvariable=self.period, width=13)
        self.period_combo.pack(side='left')
        self.period_combo.bind('<<ComboboxSelected>>', self.switch_period)
        self.period_combo.bind('<Return>', self.switch_period)
        ttk.Label(bar, text='显示').pack(side='left', padx=(24, 6))
        metric_box = ttk.Combobox(bar, textvariable=self.metric, values=['总赔付', '净亏损'], width=7, state='readonly')
        metric_box.pack(side='left')
        metric_box.bind('<<ComboboxSelected>>', self.filter_changed)
        ttk.Label(bar, text='≥').pack(side='left', padx=5)
        threshold = ttk.Entry(bar, textvariable=self.threshold, width=10)
        threshold.pack(side='left')
        threshold.bind('<KeyRelease>', self.filter_changed)
        ttk.Label(bar, text='元').pack(side='left', padx=5)
        ttk.Button(bar, text='导出当前数据', command=self.export_csv).pack(side='right')

        metrics = ttk.Frame(self)
        metrics.pack(fill='x', padx=24, pady=(0, 8))
        self.cards = {}
        for i, (name, label) in enumerate([('due', '有效投注应收'), ('income', '计入收入'), ('risk', '达到阈值的号码'), ('loss', '当前最大净亏损')]):
            metrics.columnconfigure(i, weight=1)
            card = tk.Frame(metrics, bg='white', padx=14, pady=9, bd=1, relief='solid')
            card.grid(row=0, column=i, sticky='ew', padx=(0, 8 if i < 3 else 0))
            tk.Label(card, text=label, fg=MUTED, bg='white').pack(anchor='w')
            value = tk.Label(card, text='—', bg='white', fg=RED if name == 'loss' else INK, font=('Microsoft YaHei UI', 20, 'bold'))
            value.pack(anchor='w', pady=(2, 0))
            self.cards[name] = value
        self.warning = tk.Label(self, bg=BG, fg=RED, anchor='w', justify='left')
        self.warning.pack(fill='x', padx=24, pady=(0,6))

        # 主工作区固定为“投注记录在上、风险/方案在下”。
        # 不再使用可拖动 PanedWindow，避免小屏或内容较多时风险表被挤到看不见。
        body = ttk.Frame(self)
        body.pack(fill='both', expand=True, padx=24, pady=(0,12))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=5, minsize=300)
        body.rowconfigure(1, weight=4, minsize=220)
        records_panel = ttk.Frame(body)
        records_panel.grid(row=0, column=0, sticky='nsew', pady=(0,8))
        risk_panel = ttk.Frame(body)
        risk_panel.grid(row=1, column=0, sticky='nsew')
        risk_panel.rowconfigure(0, weight=1)
        risk_panel.columnconfigure(0, weight=3, minsize=620)
        risk_panel.columnconfigure(1, weight=2, minsize=360)

        input_box = ttk.LabelFrame(records_panel, text='今日数据 / 手动补录', padding=8)
        input_box.pack(fill='x', pady=(0,8))
        input_row = ttk.Frame(input_box)
        input_row.pack(fill='x')
        self.input = tk.Text(input_row, height=2, wrap='word', relief='solid', bd=1, bg='white', fg=INK)
        self.input.pack(side='left', fill='x', expand=True)
        self.input.bind('<Return>', self.add_event)
        self.input.bind('<Shift-Return>', lambda event: None)
        side = ttk.Frame(input_row)
        side.pack(side='right', padx=(8,0))
        ttk.Button(side, text='添加', style='Accent.TButton', command=self.add_manual).pack(fill='x')
        ttk.Label(side, text='Shift+Enter换行', foreground=MUTED).pack(pady=(4,0))
        receipt_row = ttk.Frame(input_box)
        receipt_row.pack(fill='x', pady=(6,0))
        ttk.Label(receipt_row, text='已确认实收').pack(side='left')
        ttk.Entry(receipt_row, textvariable=self.receipt, width=10).pack(side='left', padx=6)
        ttk.Label(receipt_row, text='元（留空按应收）').pack(side='left')
        ttk.Label(receipt_row, text='示例：358421组六10倍，293单组各50倍', foreground=MUTED).pack(side='right')
        self.add_feedback = ttk.Label(input_box, text='', foreground='#167647')
        self.add_feedback.pack(fill='x', anchor='w', pady=(4,0))

        notebook = ttk.Notebook(records_panel)
        notebook.pack(fill='both', expand=True)
        records_tab, guide_tab = ttk.Frame(notebook), ttk.Frame(notebook)
        notebook.add(records_tab, text='投注记录')
        notebook.add(guide_tab, text='下单模板')
        records_tab.rowconfigure(1, weight=1)
        records_tab.columnconfigure(0, weight=1)
        self.records_summary = ttk.Label(records_tab, text='今日0条 · 已识别0 · 未识别0 · 已选0')
        self.records_summary.grid(row=0,column=0,sticky='ew',pady=(6,4))
        self.records_tree = RecordTable(records_tab, [('time','时间',75),('source','来源',70),('raw','投注原文',420),('amount','应收',75),('status','状态',95)])
        self.records_tree.grid(row=1,column=0,sticky='nsew')
        self.records_tree.tag_configure('pending', foreground=RED)
        self.records_tree.bind('<Button-3>', self.record_context_menu)
        self.records_tree.on_selection_change = self._update_record_selection_count
        actions = ttk.Frame(records_tab)
        actions.grid(row=2,column=0,sticky='ew',pady=6)
        for title, command in [('查看', self.view_record), ('修改／实收', self.edit_record), ('重新识别', self.reparse_selected), ('AI转换', self.ai_selected), ('删除', self.delete_selected)]:
            ttk.Button(actions, text=title, command=command).pack(side='left', padx=(0,4))
        ttk.Label(actions, text='单击复选框或双击数据行选择；右键可全选、反选、删除当前数据', foreground=MUTED).pack(side='right')
        guide = tk.Text(guide_tab, wrap='word', bg='white', relief='solid', bd=1)
        guide.pack(fill='both', expand=True, padx=2, pady=6)
        guide.insert('1.0', self.template_text())
        guide.configure(state='disabled')

        # 风险表和处理方案并排显示。这样即使窗口高度较低，风险表也不会被方案区挤没。
        risk_box = ttk.LabelFrame(risk_panel, text='实时风险 · 点击号码查看来源', padding=8)
        risk_box.grid(row=0, column=0, sticky='nsew', padx=(0,8))
        risk_box.rowconfigure(0, weight=1, minsize=135)
        risk_box.columnconfigure(0, weight=1)
        self.risks_tree = RiskGridTable(risk_box, [('number','号码',70),('payout','总赔付',110),('income','计入收入',100),('loss','净亏损',110),('plan','处理倍数',95)])
        self.risks_tree.grid(row=0,column=0,sticky='nsew')
        self.risks_tree.bind('<<TreeviewSelect>>', self.show_sources)
        self.sources = tk.Text(risk_box, height=2, wrap='word', bg='#f7f9fd', relief='solid', bd=1, fg=MUTED)
        self.sources.grid(row=1,column=0,sticky='ew',pady=(6,0))
        self.set_text(self.sources, '选择号码，可查看产生赔付的投注、发送人和原文。')

        plan_box = ttk.LabelFrame(risk_panel, text='当前处理方案', padding=8)
        plan_box.grid(row=0, column=1, sticky='nsew')
        plan_box.rowconfigure(0, weight=1)
        plan_box.columnconfigure(0, weight=1)
        self.plan = tk.Text(plan_box, height=6, wrap='word', bg='white', relief='solid', bd=1, fg=INK, font=('Microsoft YaHei UI', 11))
        self.plan.grid(row=0, column=0, sticky='nsew')
        row = ttk.Frame(plan_box)
        row.grid(row=1, column=0, sticky='ew', pady=(6,0))
        self.plan_note = ttk.Label(row, text='')
        self.plan_note.pack(side='left')
        self.copy_button = ttk.Button(row, text='一键复制', style='Accent.TButton', command=self.copy_plan)
        self.copy_button.pack(side='right')

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
        lottery = self.lottery.get()
        records = [r for r in self.controller.records() if r.lottery == lottery]
        selection = self.records_tree.selection()
        self.records_tree.delete(*self.records_tree.get_children())
        for record in records:
            has_partial = bool(record.bets and record.pending_fragments)
            amount = (money(sum(cents(b.principal) for b in record.bets)) +
                      ('（部分）' if has_partial else '')) if record.status == '已识别' or has_partial else '—'
            self.records_tree.insert('', 'end', iid=record.record_id, values=(record.created_at[11:19], record.source, record.raw_text, amount, record.status), tags=() if record.status == '已识别' else ('pending',))
        keep = [iid for iid in selection if self.records_tree.exists(iid)]
        if keep:
            self.records_tree.selection_set(keep)
        recognized = sum(r.status == '已识别' for r in records)
        self.records_summary.configure(text=f'今日{len(records)}条 · 已识别{recognized} · 未识别{len(records)-recognized} · 已选{len(self.records_tree.selection())}')
        periods = list(dict.fromkeys([now().date().isoformat(), self.controller.period]+self.store.periods()))
        self.period_combo.configure(values=periods)
        self.risks_tree.delete(*self.risks_tree.get_children())
        try:
            self.snapshot = self.controller.analysis(lottery)
            self.error = ''
            for idx, result in enumerate(self.snapshot.risks):
                self.risks_tree.insert('', 'end', iid=result.number, values=(result.number, f'{result.payout:g}', money(self.snapshot.revenue), f'{result.net_loss:g}', result.plan_multiplier), tags=('odd',) if idx % 2 else ())
            maximum = max((cents(p)-self.snapshot.revenue for p in self.snapshot.payouts.values()), default=0)
            for name, value in [('due', money(self.snapshot.principal)+'元'), ('income', money(self.snapshot.revenue)+'元'), ('risk', str(len(self.snapshot.risks))), ('loss', money(max(0, maximum))+'元')]:
                self.cards[name].configure(text=value)
            text = plan_text(self.snapshot.risks, self.controller.settings['direct_unit_cost'][lottery])
            self.set_text(self.plan, text)
            note = '已计入全部补单成本' if self.controller.settings['include_hedge_cost'] else '倍数计算忽略补单成本'
            self.plan_note.configure(text=note)
            warnings = []
            if self.snapshot.pending:
                warnings.append(f'有{self.snapshot.pending}条待处理；已验证的部分投注已计入，未解决片段尚未计入，当前结果不完整。')
            if self.snapshot.unconfirmed_receipts:
                warnings.append(f'{self.snapshot.unconfirmed_receipts}条未填实收，暂按应收计入收入。')
            if self.store.import_errors:
                warnings.append('部分旧数据迁入失败，请检查原文件。')
            self.warning.configure(text='  '.join(warnings) or '当前有效投注已完成计算。', fg=RED if warnings else MUTED)
            self.copy_button.configure(state='normal' if any(r.plan_multiplier for r in self.snapshot.risks) else 'disabled')
        except (ValueError, KeyError, sqlite3.Error) as exc:
            self.snapshot = None
            self.error = str(exc)
            self.warning.configure(text='计算未完成：'+str(exc), fg=RED)
            self.set_text(self.plan, '方案暂不可用：'+str(exc))
            self.copy_button.configure(state='disabled')
            for card in self.cards.values():
                card.configure(text='—')
        self.update_status()

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
        self.status.configure(text=f'微信：{self.wechat.status}    AI：{ai}    更新：{now():%H:%M:%S}')

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
                state = '待AI识别' if self.controller.settings['ai'].get('enabled') and self.ai_key and self.controller.settings['ai'].get('model') else '待确认（AI未开启或未配置）'
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
        win.geometry('680x480')
        text = tk.Text(win, wrap='word', padx=16, pady=16)
        text.pack(fill='both', expand=True)
        lines = [f'{record.lottery} / {record.period} / {record.status}', f'来源：{record.source}  发送人：{record.sender or "未登记"}', f'收到时间：{record.created_at}', '\n原文：'+record.raw_text, '\n说明：'+record.note]
        if record.pending_fragments:
            lines.append('\n未解决片段（不计金额）：')
            lines.extend(f"[{item['start']}:{item['end']}] {item['text']}：{item['reason']}" for item in record.pending_fragments)
        if record.ai_candidate:
            lines.append('\nAI转换：'+record.ai_candidate)
        lines.append('\n逐笔明细：')
        lines.extend(f'{b.selection} {b.play} {b.multiplier}倍，应收{b.principal:g}元' for b in record.bets)
        lines.append('确认实收：'+('未填写，按应收计入收入' if record.received_amount is None else f'{record.received_amount:g}元'))
        self.set_text(text, '\n'.join(lines))
        if record.note and record.status in ('待确认','AI待确认'):
            def copy_note():
                win.clipboard_clear()
                win.clipboard_append(record.note)
                win.update_idletasks()
            ttk.Button(win, text='复制说明／失败原因', command=copy_note).pack(pady=5)
        if record.status == 'AI待确认':
            def accept():
                try:
                    self.controller.accept_ai(record)
                    self.refresh()
                    win.destroy()
                except (ValueError, sqlite3.Error) as exc:
                    self.fail(exc)
            ttk.Button(win, text='确认AI转换并计入', command=accept).pack(pady=10)

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
            self.store.delete(record)
            self.refresh()
        except sqlite3.Error as exc:
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
            for record in records:
                self.store.delete(record)
            self.refresh()
        except sqlite3.Error as exc:
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

    def queue_ai(self, record, force=False):
        if record:
            record = self.store.get(record.record_id)
        if not record or record.status not in ('待确认', '部分识别') or record.record_id in self.ai_busy:
            return
        if not self.ai_key or not self.controller.settings['ai'].get('model'):
            if force:
                self.fail(ValueError('请先在设置中填写AI接口、模型和API Key'))
            return
        if not force and not self.controller.settings['ai'].get('enabled'):
            return
        revision = record.revision
        ai_text = ('\n'.join(item['text'] for item in record.pending_fragments)
                   if record.pending_fragments and record.bets else record.raw_text)
        record.status = 'AI识别中'
        record.note = '正在转换，原文已保存；其他投注继续计算'
        self.store.update(record, '提交AI转换')
        self.ai_busy.add(record.record_id)
        if force:
            self._manual_ai.add(record.record_id)
        service = self.ai
        def task():
            try:
                candidate, reason = service.parse_unknown(record.lottery, ai_text)
                self.events.put(('ai', record.record_id, revision, candidate, reason))
            except AIError as exc:
                self.events.put(('ai_error', record.record_id, revision, str(exc)))
            except Exception as exc:
                safe = _redact(str(exc), self.ai_key, [{'role':'user','content':ai_text}])
                self.events.put(('ai_error', record.record_id, revision, 'AI执行失败（'+type(exc).__name__+'）：'+safe+'；原始数据已保存'))
        self.pool.submit(task)
        self.refresh()

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
        dirty = False
        try:
            for _ in range(30):
                if self.events.empty():
                    break
                event = self.events.get_nowait()
                if event[0] == 'ai':
                    _, record_id, revision, candidate, reason = event
                    self.ai_busy.discard(record_id)
                    self._manual_ai.discard(record_id)
                    if not self.controller.settings['ai'].get('enabled'):
                        record = self.store.get(record_id)
                        if record and record.revision == revision and record.status == 'AI识别中':
                            record.status='部分识别' if record.pending_fragments and record.bets else '待确认'
                            record.note='AI识别已关闭，未解决片段保留待确认'
                            self.store.update(record,'关闭AI后保留原文')
                        dirty=True
                        continue
                    applied = self.controller.apply_ai_candidate(record_id, revision, candidate, reason)
                    if isinstance(self.ai, AIService):
                        self.ai._log('投注本地校验完成', accepted=bool(applied), record_id=record_id)
                    if not applied:
                        latest = self.store.get(record_id)
                        if latest and latest.revision != revision:
                            self.queue_ai(latest)
                    dirty = True
                elif event[0] == 'ai_error':
                    _, record_id, revision, reason = event
                    self.ai_busy.discard(record_id)
                    was_manual = record_id in self._manual_ai
                    self._manual_ai.discard(record_id)
                    record = self.store.get(record_id)
                    if record and record.revision == revision and record.status in ('待确认', 'AI识别中'):
                        record.status = '部分识别' if record.pending_fragments and record.bets else '待确认'
                        record.note = reason + ('；本地已识别部分仍保留' if record.bets else '')
                        self.store.update(record, 'AI失败保留原文')
                        if hasattr(self, 'add_feedback'):
                            self.add_feedback.configure(text='AI识别失败：'+reason[:210], foreground=RED)
                        if was_manual:
                            self.after(10, lambda detail=reason: self.show_ai_error_detail(detail))
                        dirty = True
                    elif record and record.revision != revision:
                        self.queue_ai(record)
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

    def show_sources(self, event=None):
        selected = self.risks_tree.selection()
        if not selected:
            return
        number = selected[0]
        rows = self.controller.explain(self.lottery.get(), number)
        self.set_text(self.sources, '\n'.join(f'赔付{payout:g}元 · {bet.selection} {bet.play}{bet.multiplier}倍 · {record.sender or record.source}\n原文：{record.raw_text}' for record, bet, payout in rows) or '此号码没有产生赔付的投注。')

    def copy_plan(self):
        if not self.snapshot:
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

        for title in ['计算规则', '赔率表', 'AI', '微信群', '自定义写法']:
            if title in {'计算规则', 'AI', '微信群'}:
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
        ai_page = pages['AI']
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
        wechat_page = pages['微信群']
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
                settings['policy'] = {'position_cost': 'combination', 'span0_wins': True}
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
        self.store.close()
        self.destroy()
