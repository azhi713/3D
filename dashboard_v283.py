# -*- coding: utf-8 -*-
"""V283: one verified displayed plan for preview and live purchased accounting.

The payout formula and original customer receipts are unchanged. If a hedge is
already marked purchased, a complete newly displayed plan replaces that period's
actual hedge, including its cost and outcome-specific returns.
"""
from __future__ import annotations
from dataclasses import replace
import json
import tkinter as tk
from tkinter import ttk, messagebox, font as tkfont
from dashboard_v282 import DashboardV282Mixin
from dashboard_v280 import PlanComparisonTable, LABELS, MODES, INK, MUTED, RED, GREEN
from dashboard_v281 import save_purchase_checked
from dashboard_metrics_v280 import (aggregate_rows, format_yuan, purchased_metrics)
from plan_snapshot_v283 import (DisplayedPlan, build_displayed_plan, parse_profit_target,
                                target_outcomes, assert_view_matches)
from risk_extension_v2734 import freeze_purchase_snapshot


# Use a short delay after ordinary typing, and a slightly longer one after
# deleting characters (e.g. 15000 -> 5000 -> 25000). Each edit resets it.
INPUT_IDLE_MS = 600
INPUT_DELETE_IDLE_MS = 900


class PlanComparisonV283(PlanComparisonTable):
    headers = PlanComparisonTable.headers + ('盈利达标', '亏损达标')
    minimum_widths = (96, 52, 73, 65, 100, 104, 106, 135, 135)

    def __init__(self, parent, choose):
        super().__init__(parent, choose)
        for i, mode in enumerate(MODES, 1):
            for column in (7, 8):
                label = tk.Label(self.body, text='—', font=self.cell_font, anchor='e', padx=5,
                                 bg='white', fg=INK)
                label.grid(row=i, column=column, sticky='nsew', padx=(0, 1), pady=(0, 1))
                label.bind('<Button-1>', lambda _e, key=mode: self.choose(key))
                self.cells[mode, column] = label
        self.target_heading = self.body.grid_slaves(row=0, column=7)[0]
        self.loss_heading = self.body.grid_slaves(row=0, column=8)[0]
        self.body.grid_slaves(row=0, column=5)[0].configure(text='补中最低净盈亏/元')
        self._resize()

    def set_target(self, target, invalid=False, loss=None, loss_invalid=False):
        if invalid:
            heading = '目标待核对'
        elif target is None:
            heading = '盈利达标'
        else:
            heading = '盈利≥' + format_yuan(target) + '元'
        self.target_heading.configure(text=heading)
        self.loss_heading.configure(text=('目标待核对' if loss_invalid else '亏损达标' if loss is None
                                          else '亏损≥' + format_yuan(loss) + '元'))


def _colour(value):
    return MUTED if value is None else RED if value < 0 else GREEN if value > 0 else INK


def _v283_format_target_numbers(numbers):
    """Readable number-only copy, keeping 000 and every matching outcome."""
    return '\n'.join(' '.join(numbers[i:i+12]) for i in range(0, len(numbers), 12))


def _v283_parse_unsigned_target(raw, label):
    amount = parse_profit_target(raw)
    if amount is not None and amount < 0:
        raise ValueError(label + '请输入非负金额，不需要负号')
    return amount


def _v283_loss_outcomes(metrics, loss_cents):
    """Loss threshold uses a positive amount, including the zero boundary."""
    if loss_cents is None:
        return None
    if type(loss_cents) is not int or loss_cents < 0:
        raise ValueError('目标亏损须为非负整数分')
    if len(metrics.profits_cents) != 1000:
        raise ValueError('目标亏损需要完整1000个开奖结果')
    return tuple(f'{i:03d}' for i, profit in enumerate(metrics.profits_cents)
                 if profit <= -loss_cents)


def _v283_migrate_signed_target(profit_raw, loss_raw):
    """Move an old negative profit target to the new positive loss field."""
    if profit_raw.strip().startswith('-'):
        if not loss_raw.strip():
            loss_raw = profit_raw.strip()[1:]
        profit_raw = ''
    return profit_raw, loss_raw


class DashboardV283Mixin(DashboardV282Mixin):
    def _build(self):
        self._v283_ready = False
        self._v283_rendering = False
        self._v283_current = None
        self._v283_views = {}
        self._v283_goal_timer = None
        self._v283_refresh_timer = None
        self._v283_target = None
        self._v283_target_error = ''
        self._v283_saved_target = ''
        self._v283_loss_target = None
        self._v283_loss_target_error = ''
        self._v283_saved_loss = ''
        self._v283_live_timer = None
        self._v283_live_syncing = False
        self._v283_live_failed_key = None
        self._v283_live_armed_context = None
        self._v283_target_numbers = None
        self._v283_target_detail = None
        self._v283_loss_numbers = None
        self._v283_loss_detail = None
        self._v283_target_tip = None
        self._v283_target_tip_kind = None
        self._v283_target_tip_after = None
        self._v283_target_dialog = None
        self._v283_target_dialog_kind = 'profit'
        super()._build()
        # The legacy threshold entry used a 300 ms KeyRelease timer. Keep the
        # immediate +/- and combobox actions, but debounce handwritten values.
        self.threshold_entry.bind('<KeyRelease>', self._v283_threshold_input)
        self.threshold_entry.bind('<FocusOut>', self._v283_threshold_focus_out)
        old = self.optimizer_compare_table
        parent = old.master
        old.destroy()
        self.optimizer_compare_table = PlanComparisonV283(parent, self._v280_choose_mode)
        self.optimizer_compare_table.grid(row=2, column=0, sticky='ew', pady=(0, 7))
        self.restore_system_plan_button.configure(text='按当前阈值重算', command=self._restore_system_plan)
        self._v283_build_target_control()
        self._v283_ready = True
        self._render_plan()
        self._update_financial_cards()

    def _v283_build_target_control(self):
        stored_profit = self._ui_state.data.get('target_profit_yuan', '')
        stored_loss = self._ui_state.data.get('target_loss_yuan', '')
        stored_profit = stored_profit if isinstance(stored_profit, str) else ''
        stored_loss = stored_loss if isinstance(stored_loss, str) else ''
        profit_raw, loss_raw = _v283_migrate_signed_target(stored_profit, stored_loss)
        try:
            self._v283_target = parse_profit_target(profit_raw)
            if self._v283_target is not None and self._v283_target < 0:
                raise ValueError('目标盈利请填写非负金额')
        except ValueError:
            profit_raw = ''; self._v283_target = None
        try:
            loss_cents = parse_profit_target(loss_raw)
            if loss_cents is not None and loss_cents < 0:
                raise ValueError('目标亏损请填写非负金额')
            self._v283_loss_target = loss_cents
        except ValueError:
            loss_raw = ''; self._v283_loss_target = None
        self._v283_saved_target = stored_profit
        self._v283_saved_loss = stored_loss
        self.target_profit = tk.StringVar(self, value=profit_raw)
        self.target_loss = tk.StringVar(self, value=loss_raw)
        # Physically after the history button, not in the plan's copy/action bar.
        box = ttk.Frame(self.history_button.master)
        box.pack(side='left', after=self.history_button, padx=(12, 0))
        self._v283_target_box = box
        count_font = ('Microsoft YaHei UI', 9)
        measured_font = tkfont.Font(root=self, family=count_font[0], size=count_font[1])
        # The maximum possible result is 1000 groups. Reserve the pixels for
        # that full string (plus the existing incomplete-data marker) so text
        # updates never push the buttons and loss controls left or right.
        count_width = max(measured_font.measure('盈利达标：1000组 *'),
                          measured_font.measure('亏损达标：1000组 *')) + 6
        count_height = measured_font.metrics('linespace') + 6
        ttk.Label(box, text='目标盈利', font=('Microsoft YaHei UI', 9)).pack(side='left')
        self.target_profit_entry = ttk.Entry(box, textvariable=self.target_profit,
                                             width=9, justify='right')
        self.target_profit_entry.pack(side='left', padx=(5, 3))
        ttk.Label(box, text='元', font=('Microsoft YaHei UI', 9)).pack(side='left')
        profit_slot = ttk.Frame(box, width=count_width, height=count_height)
        profit_slot.pack(side='left', padx=(6, 0))
        profit_slot.pack_propagate(False)
        self.target_profit_count = ttk.Label(profit_slot, text='盈利达标：—', foreground=MUTED,
                                             font=count_font, cursor='hand2', anchor='w')
        self.target_profit_count.pack(fill='x')
        self.target_profit_count.bind('<Enter>', self._v283_schedule_target_tip)
        self.target_profit_count.bind('<Leave>', self._v283_schedule_target_tip_hide)
        self.target_profit_view_button = ttk.Button(box, text='查看盈利号码',
                                                    command=self._v283_open_target_dialog,
                                                    state='disabled')
        self.target_profit_view_button.pack(side='left', padx=(6, 0))
        ttk.Label(box, text='目标亏损', font=('Microsoft YaHei UI', 9)).pack(side='left', padx=(12, 0))
        self.target_loss_entry = ttk.Entry(box, textvariable=self.target_loss,
                                           width=9, justify='right')
        self.target_loss_entry.pack(side='left', padx=(5, 3))
        ttk.Label(box, text='元', font=('Microsoft YaHei UI', 9)).pack(side='left')
        loss_slot = ttk.Frame(box, width=count_width, height=count_height)
        loss_slot.pack(side='left', padx=(6, 0))
        loss_slot.pack_propagate(False)
        self.target_loss_count = ttk.Label(loss_slot, text='亏损达标：—', foreground=MUTED,
                                           font=count_font, cursor='hand2', anchor='w')
        self.target_loss_count.pack(fill='x')
        self.target_loss_count.bind('<Enter>', lambda e: self._v283_schedule_target_tip(e, 'loss'))
        self.target_loss_count.bind('<Leave>', self._v283_schedule_target_tip_hide)
        self.target_loss_view_button = ttk.Button(box, text='查看亏损号码',
                                                  command=lambda: self._v283_open_target_dialog('loss'),
                                                  state='disabled')
        self.target_loss_view_button.pack(side='left', padx=(6, 0))
        self.target_profit_source = ttk.Label(box, text='', foreground=MUTED,
                                              font=('Microsoft YaHei UI', 8))
        self.target_profit_source.pack(side='left', padx=(8, 0))
        self._v283_previous_goal_text = (profit_raw, loss_raw)
        self.target_profit.trace_add('write', self._v283_target_changed)
        self.target_loss.trace_add('write', self._v283_target_changed)
        self.target_profit_entry.bind('<Return>', self._v283_commit_target)
        self.target_profit_entry.bind('<FocusOut>', self._v283_commit_target)
        self.target_loss_entry.bind('<Return>', self._v283_commit_target)
        self.target_loss_entry.bind('<FocusOut>', self._v283_commit_target)

    def _v283_set_target_numbers(self, numbers, kind='profit'):
        """Only publish numbers from the same completed calculation as the count."""
        numbers = tuple(numbers) if numbers is not None else None
        target = self._v283_loss_target if kind == 'loss' else self._v283_target
        detail = ((self.lottery.get(), self.controller.period, target,
                   self.target_profit_source.cget('text')) if numbers is not None else None)
        old_numbers = self._v283_loss_numbers if kind == 'loss' else self._v283_target_numbers
        old_detail = self._v283_loss_detail if kind == 'loss' else self._v283_target_detail
        if numbers == old_numbers and detail == old_detail:
            return
        if kind == 'loss':
            self._v283_loss_numbers = numbers
            self._v283_loss_detail = detail
            button = self.target_loss_view_button
        else:
            self._v283_target_numbers = numbers
            self._v283_target_detail = detail
            button = self.target_profit_view_button
        button.configure(state='normal' if numbers is not None else 'disabled')
        if self._v283_target_tip_kind == kind:
            self._v283_hide_target_tip()
        if self._v283_target_dialog_kind == kind:
            self._v283_refresh_target_dialog()

    def _v283_target_header(self, kind='profit'):
        detail = self._v283_loss_detail if kind == 'loss' else self._v283_target_detail
        numbers = self._v283_loss_numbers if kind == 'loss' else self._v283_target_numbers
        if detail is None:
            return '达标号码更新中'
        lottery, period, target, _source = detail
        condition = ('亏损≥' if kind == 'loss' else '盈利≥') + format_yuan(target) + '元'
        return f'{lottery} / {period} / {condition} / 共{len(numbers)}组'

    def _v283_cancel_target_tip_timer(self, _event=None):
        if self._v283_target_tip_after is not None:
            try:
                self.after_cancel(self._v283_target_tip_after)
            except tk.TclError:
                pass
            self._v283_target_tip_after = None

    def _v283_hide_target_tip(self):
        self._v283_cancel_target_tip_timer()
        tip = self._v283_target_tip
        self._v283_target_tip = None
        self._v283_target_tip_kind = None
        if tip is not None:
            try:
                if tip.winfo_exists():
                    tip.destroy()
            except tk.TclError:
                pass

    def _v283_schedule_target_tip(self, _event=None, kind='profit'):
        numbers = self._v283_loss_numbers if kind == 'loss' else self._v283_target_numbers
        if self._closing or numbers is None:
            return
        self._v283_cancel_target_tip_timer()
        if self._v283_target_tip is not None and self._v283_target_tip_kind != kind:
            self._v283_hide_target_tip()
        if self._v283_target_tip is None:
            self._v283_target_tip_after = self.after(300, lambda: self._v283_show_target_tip(kind))

    def _v283_schedule_target_tip_hide(self, _event=None):
        self._v283_cancel_target_tip_timer()
        if self._v283_target_tip is not None:
            self._v283_target_tip_after = self.after(180, self._v283_maybe_hide_target_tip)

    def _v283_maybe_hide_target_tip(self):
        self._v283_target_tip_after = None
        tip = self._v283_target_tip
        if tip is None:
            return
        try:
            hovered = self.winfo_containing(self.winfo_pointerx(), self.winfo_pointery())
            if hovered in (self.target_profit_count, self.target_loss_count) or (hovered is not None and hovered.winfo_toplevel() is tip):
                return
        except tk.TclError:
            pass
        self._v283_hide_target_tip()

    def _v283_show_target_tip(self, kind='profit'):
        self._v283_target_tip_after = None
        numbers = self._v283_loss_numbers if kind == 'loss' else self._v283_target_numbers
        if self._closing or numbers is None or self._v283_target_tip is not None:
            return
        anchor = self.target_loss_count if kind == 'loss' else self.target_profit_count
        width = min(540, max(260, self.winfo_screenwidth() - 24))
        height = min(310, max(180, self.winfo_screenheight() - 40))
        x = max(8, min(anchor.winfo_rootx(), self.winfo_screenwidth() - width - 8))
        y = anchor.winfo_rooty() + anchor.winfo_height() + 5
        if y + height > self.winfo_screenheight() - 12:
            y = max(8, anchor.winfo_rooty() - height - 5)
        tip = tk.Toplevel(self)
        tip.overrideredirect(True)
        tip.geometry(f'{width}x{height}+{x}+{y}')
        self._v283_target_tip = tip
        self._v283_target_tip_kind = kind
        panel = ttk.Frame(tip, padding=9, borderwidth=1, relief='solid')
        panel.pack(fill='both', expand=True)
        ttk.Label(panel, text=self._v283_target_header(kind) + '（滚动查看全部）',
                  wraplength=width-30,
                  font=('Microsoft YaHei UI', 9, 'bold')).pack(anchor='w', pady=(0, 6))
        content = ttk.Frame(panel)
        content.pack(fill='both', expand=True)
        area = tk.Text(content, wrap='word', font=('Consolas', 10), relief='flat',
                       background='#FFFFFF', cursor='arrow', height=12)
        bar = ttk.Scrollbar(content, orient='vertical', command=area.yview)
        area.configure(yscrollcommand=bar.set)
        area.pack(side='left', fill='both', expand=True)
        bar.pack(side='right', fill='y')
        area.insert('1.0', _v283_format_target_numbers(numbers)
                    or '当前没有符合条件的号码')
        area.configure(state='disabled')
        tip.bind('<Enter>', self._v283_cancel_target_tip_timer)
        tip.bind('<Leave>', self._v283_schedule_target_tip_hide)

    def _v283_refresh_target_dialog(self):
        win = self._v283_target_dialog
        if win is None or not win.winfo_exists():
            return
        kind = self._v283_target_dialog_kind
        numbers = self._v283_loss_numbers if kind == 'loss' else self._v283_target_numbers
        detail = self._v283_loss_detail if kind == 'loss' else self._v283_target_detail
        self._v283_target_dialog_header.configure(
            text=(self._v283_target_header(kind) + '\n' + detail[3]
                  if numbers is not None else '目标或补单数据更新中，请稍候。'))
        area = self._v283_target_dialog_text
        area.configure(state='normal')
        area.delete('1.0', 'end')
        area.insert('1.0', _v283_format_target_numbers(numbers) if numbers
                    else '当前没有符合条件的号码。' if numbers is not None else '等待最新达标统计…')
        area.configure(state='disabled')
        self._v283_target_dialog_copy.configure(state='normal' if numbers else 'disabled',
                                                 text='一键复制全部号码')

    def _v283_open_target_dialog(self, kind='profit'):
        numbers = self._v283_loss_numbers if kind == 'loss' else self._v283_target_numbers
        if self._closing or numbers is None:
            return
        self._v283_hide_target_tip()
        self._v283_target_dialog_kind = kind
        win = self._v283_target_dialog
        if win is not None and win.winfo_exists():
            self._v283_refresh_target_dialog()
            win.lift()
            win.focus_set()
            return
        win = tk.Toplevel(self)
        self._v283_target_dialog = win
        win.title('查看达标号码')
        win.geometry('680x490')
        win.minsize(460, 320)
        win.transient(self)
        self._v283_target_dialog_header = ttk.Label(win, wraplength=640,
                                                    font=('Microsoft YaHei UI', 10))
        self._v283_target_dialog_header.pack(fill='x', padx=16, pady=(14, 10))
        panel = ttk.Frame(win)
        panel.pack(fill='both', expand=True, padx=16)
        area = tk.Text(panel, wrap='word', font=('Consolas', 11), height=16)
        bar = ttk.Scrollbar(panel, orient='vertical', command=area.yview)
        area.configure(yscrollcommand=bar.set)
        area.pack(side='left', fill='both', expand=True)
        bar.pack(side='right', fill='y')
        self._v283_target_dialog_text = area
        footer = ttk.Frame(win)
        footer.pack(fill='x', padx=16, pady=14)
        self._v283_target_dialog_copy = ttk.Button(footer, text='一键复制全部号码',
                                                  command=self._v283_copy_target_numbers)
        self._v283_target_dialog_copy.pack(side='left')
        ttk.Button(footer, text='关闭', command=win.destroy).pack(side='right')
        win.bind('<Escape>', lambda _e: win.destroy())
        self._v283_refresh_target_dialog()

    def _v283_copy_target_numbers(self):
        kind = self._v283_target_dialog_kind
        numbers = self._v283_loss_numbers if kind == 'loss' else self._v283_target_numbers
        if not numbers or self._closing:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(_v283_format_target_numbers(numbers))
            self.update_idletasks()
        except tk.TclError as exc:
            messagebox.showerror('复制失败', str(exc), parent=self._v283_target_dialog or self)
        else:
            self._v283_target_dialog_copy.configure(text=f'已复制 {len(numbers)} 组')

    def _v283_target_changed(self, *_):
        if self._closing:
            return
        new_text = (self.target_profit.get(), self.target_loss.get())
        before = getattr(self, '_v283_previous_goal_text', new_text)
        deleting = any(len(current) < len(previous) for current, previous in zip(new_text, before))
        self._v283_previous_goal_text = new_text
        self._v283_set_target_numbers(None)
        self._v283_set_target_numbers(None, 'loss')
        if self._v283_goal_timer is not None:
            self.after_cancel(self._v283_goal_timer)
        # Do not leave old target counts labelled as the newly typed amount.
        self.target_profit_count.configure(text='盈利达标：' + ('输入中' if self.target_profit.get().strip() else '—'),
                                           foreground=MUTED)
        self.target_loss_count.configure(text='亏损达标：' + ('输入中' if self.target_loss.get().strip() else '—'),
                                         foreground=MUTED)
        self.target_profit_source.configure(text='')
        self._v283_goal_timer = self.after(INPUT_DELETE_IDLE_MS if deleting else INPUT_IDLE_MS,
                                          self._v283_commit_target)

    def _v283_threshold_input(self, event=None):
        """Apply typed risk thresholds after the same quiet period as goals."""
        if self._closing or (event is not None and event.keysym in (
                'Return', 'KP_Enter', 'Tab', 'ISO_Left_Tab', 'Escape',
                'Shift_L', 'Shift_R', 'Control_L', 'Control_R', 'Alt_L', 'Alt_R',
                'Left', 'Right', 'Up', 'Down', 'Home', 'End')):
            return
        timer = getattr(self, '_threshold_timer', None)
        if timer is not None:
            self.after_cancel(timer)
        deleting = event is not None and event.keysym in ('BackSpace', 'Delete')
        self._threshold_timer = self.after(INPUT_DELETE_IDLE_MS if deleting else INPUT_IDLE_MS,
                                           self.apply_filter)

    def _v283_threshold_focus_out(self, _event=None):
        """Leaving a half-edited field explicitly completes that edit."""
        if not self._closing and getattr(self, '_threshold_timer', None) is not None:
            self._v281_apply_threshold_now()

    def _v283_commit_target(self, _event=None):
        if self._v283_goal_timer is not None:
            self.after_cancel(self._v283_goal_timer)
            self._v283_goal_timer = None
        profit_raw = self.target_profit.get().strip()
        loss_raw = self.target_loss.get().strip()
        self._v283_target_error = ''
        self._v283_loss_target_error = ''
        try:
            self._v283_target = _v283_parse_unsigned_target(profit_raw, '目标盈利')
        except ValueError as exc:
            self._v283_target = None
            self._v283_target_error = str(exc)
        try:
            loss_cents = _v283_parse_unsigned_target(loss_raw, '目标亏损')
            self._v283_loss_target = loss_cents
        except ValueError as exc:
            self._v283_loss_target = None
            self._v283_loss_target_error = str(exc)
        updates = {}
        if not self._v283_target_error and profit_raw != self._v283_saved_target:
            updates['target_profit_yuan'] = profit_raw
        if not self._v283_loss_target_error and loss_raw != self._v283_saved_loss:
            updates['target_loss_yuan'] = loss_raw
        if updates:
            before = {key: self._ui_state.data.get(key, '') for key in updates}
            try:
                self._ui_state.data.update(updates)
                self._ui_state.save()
                path = getattr(self._ui_state, 'path', None)
                if path is not None:
                    persisted = json.loads(path.read_text('utf-8'))
                    if any(persisted.get(k) != value for k, value in updates.items()):
                        raise OSError('目标盈利或亏损没有保存成功')
                self._v283_saved_target = self._ui_state.data.get('target_profit_yuan', '')
                self._v283_saved_loss = self._ui_state.data.get('target_loss_yuan', '')
            except (OSError, ValueError, TypeError) as exc:
                self._ui_state.data.update(before)
                if 'target_profit_yuan' in updates:
                    self._v283_target_error = '目标盈利未保存：' + str(exc)
                if 'target_loss_yuan' in updates:
                    self._v283_loss_target_error = '目标亏损未保存：' + str(exc)
        if self._v283_ready:
            self.optimizer_compare_table.set_target(self._v283_target, bool(self._v283_target_error),
                                                     self._v283_loss_target, bool(self._v283_loss_target_error))
            self._update_optimizer_comparison()
            self._v283_update_actual_target()
        return 'break' if _event is not None and getattr(_event, 'keysym', '') == 'Return' else None

    def _v283_risk_busy(self):
        return bool(getattr(self, '_risk_running', False) or getattr(self, '_risk_next', None) is not None
                    or getattr(self, '_threshold_timer', None) is not None)

    def _v283_pending_manual(self):
        return self._plan_mode == 'manual' and self._plan_edit_timer is not None

    def _v283_source_key(self, mode):
        if self._closing or not self.snapshot:
            raise ValueError('等待本期风险数据')
        context = (self.lottery.get(), self.controller.period)
        if self._draw_context != context:
            raise ValueError('日期或彩种切换中')
        base = (context, mode, self._v280_input_key(), getattr(self, '_risk_generation', 0),
                bool(self.snapshot.pending))
        if mode == 'manual':
            if self._plan_manual_mode:
                return base + ('edited', self._plan_manual_text or '')
            risk_rows = tuple((str(r.number), str(r.plan_multiplier)) for r in self.snapshot.risks)
            return base + ('threshold', int(self._plan_adjustment), risk_rows,
                           self.threshold.get(), self.metric.get(),
                           bool(self.controller.settings.get('include_hedge_cost')))
        if mode not in MODES or self._auto_optimizer_error:
            raise ValueError('当前自动方案不可用')
        result = self._auto_optimizer_results.get(mode)
        if result is None or self._v280_optimizer_key != self._v280_input_key():
            raise ValueError('当前自动方案正在更新')
        return base + ('auto', self._auto_optimizer_generation,
                       tuple(sorted((str(c), str(v)) for c, v in result['bets'].items())))

    def _v283_capture(self, mode):
        key = self._v283_source_key(mode)
        if self._v283_risk_busy():
            raise ValueError('风险数据更新中')
        if mode == 'manual' and self._v283_pending_manual():
            raise ValueError('手动方案核对中')
        cached = self._v283_views.get(mode)
        if cached is not None and cached.source_key == key:
            return cached
        rows = super()._v280_candidate_rows(mode)
        income, payouts = self._v280_inputs()
        source = ('手动编辑' if self._plan_manual_mode else '阈值计算') if mode == 'manual' else LABELS[mode] + '自动计算'
        view = build_displayed_plan((self.lottery.get(), self.controller.period), mode, source, key,
                income, payouts, rows, self._v280_settings(),
                manual_text=(self._plan_manual_text or '') if mode == 'manual' and self._plan_manual_mode else None,
                pending=bool(self.snapshot.pending))
        self._v283_views[mode] = view
        return view

    def _render_plan(self):
        if not getattr(self, '_v283_ready', False):
            return super()._render_plan()
        if self._v283_rendering:
            return
        self._v283_rendering = True
        try:
            mode = self._plan_mode
            view = None
            try:
                view = self._v283_capture(mode)
                if mode == 'manual' and self._plan_manual_mode and view.display_text != (self._plan_manual_text or ''):
                    self._plan_manual_text = view.display_text
                    if self._draw_context and not self._v280_context_syncing:
                        self._ui_state.put_manual_plan(*self._draw_context, view.display_text)
                    view = replace(view, source_key=self._v283_source_key(mode))
                    self._v283_views[mode] = view
                self._plan_edit_error = ''
                text = view.display_text
            except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
                self._plan_edit_error = str(exc)
                self._v283_views.pop(mode, None)
                text = ((self._plan_manual_text or '') if mode == 'manual' and self._plan_manual_mode
                        else '方案暂不可用：' + str(exc))
            self._v283_current = view
            self._plan_rows = view.as_rows() if view is not None else []
            self._set_editable_plan_text(text)
            self.plan_count_label.configure(text=view.count_caption if view else '号码：—')
            self.plan_cost_label.configure(text=view.cost_caption if view else '成本：待更新',
                                            foreground=INK if view else MUTED)
            manual = mode == 'manual'
            valid = view is not None and bool(view.bets)
            self.plan.configure(state='normal' if manual else 'disabled')
            self.sort_plan_button.configure(state='normal' if manual and valid else 'disabled')
            self.restore_system_plan_button.configure(text='按当前阈值重算',
                state='normal' if manual and self.snapshot and not self._v283_risk_busy() else 'disabled')
            self.plan_plus_button.configure(state='normal' if manual and valid else 'disabled')
            self.plan_minus_button.configure(state='normal' if manual and valid and any(n > 1 for _, n in view.bets) else 'disabled')
            source = ('手动编辑' if self._plan_manual_mode else f'阈值计算 · 全局{self._plan_adjustment:+d}倍') if manual else LABELS[mode] + '自动计算'
            self.plan_adjust_label.configure(text=source)
            threshold_count = len(self.snapshot.risks) if self.snapshot else 0
            if view:
                note = f'阈值{threshold_count}组；当前方案{view.count}组。'
                note += ('手动文本不随阈值改写。' if manual and self._plan_manual_mode else
                         '已排除0倍号码。' if manual else '自动方案不受人工阈值影响。')
                if self.snapshot.pending:
                    note += ' 数据未全，仅供核对。'
            else:
                note = '方案未就绪：' + self._plan_edit_error[:75]
            self.plan_note.configure(text=note, foreground=MUTED if view else RED)
        finally:
            self._v283_rendering = False
        self._v283_show_comparison()

    def _update_optimizer_comparison(self):
        if not getattr(self, '_v283_ready', False):
            return super()._update_optimizer_comparison()
        if self._v283_rendering:
            return
        current = self._v283_current
        try:
            key = self._v283_source_key(self._plan_mode)
            fresh = current is not None and current.source_key == key and not self._v283_risk_busy()
        except (ValueError, TypeError, KeyError, ArithmeticError):
            fresh = False
        if not fresh and not self._v283_pending_manual():
            self._render_plan()
            return
        self._v283_show_comparison()

    def _v283_show_comparison(self):
        if not self._v283_ready:
            return
        draw, _ = self._v280_draw()
        data = {}
        for mode in MODES:
            try:
                if self._v283_risk_busy():
                    raise ValueError('更新中')
                view = self._v283_current if mode == self._plan_mode else self._v283_capture(mode)
                if view is None or (mode == 'manual' and self._v283_pending_manual()):
                    raise ValueError('等待当前方案')
                item = view.metrics
                minimum = item.covered_min_profit_cents
                at = item.at(draw)['total_profit_cents'] if draw else None
                target = None if self._v283_target_error else target_outcomes(item, self._v283_target)
                loss_target = None if self._v283_loss_target_error else _v283_loss_outcomes(item, self._v283_loss_target)
                values = (str(view.count), format_yuan(view.cost_cents), str(item.loss_count),
                          format_yuan(item.worst_profit_cents, True),
                          format_yuan(minimum, True) if minimum is not None else '—',
                          format_yuan(at, True) if at is not None else '—',
                          str(len(target)) if target is not None else '—',
                          str(len(loss_target)) if loss_target is not None else '—')
                colours = (INK, INK, RED if item.loss_count else INK, _colour(item.worst_profit_cents),
                           _colour(minimum), _colour(at), INK, INK)
            except (ValueError, TypeError, KeyError, ArithmeticError):
                status = ('等待数据' if not self.snapshot else '更新中' if self._v283_risk_busy()
                          else '待核对' if mode == 'manual' else '计算失败' if self._auto_optimizer_error else '计算中')
                values = ('—', status, '—', '—', '—', '—', '—', '—')
                colours = (MUTED,) * 8
            data[mode] = (values, colours)
        self.optimizer_compare_table.set_target(self._v283_target, bool(self._v283_target_error),
                                                self._v283_loss_target, bool(self._v283_loss_target_error))
        self.optimizer_compare_table.show(data, self._plan_mode, self._purchased_mode())
        self._show_hedge_checkbox()
        self._v282_refresh_copy_controls()
        self._v283_queue_purchase_sync()
        self._v283_update_actual_target()

    def _v283_arm_purchase_sync(self):
        """Only edits/active data changes arm replacement, not opening a saved period."""
        if self._v280_purchase() is not None:
            self._v283_live_armed_context = (self.lottery.get(), self.controller.period)

    def _v283_queue_purchase_sync(self):
        """Coalesce redraws; only a verified, complete plan can replace a purchase."""
        if (not self._v283_ready or self._closing or self._v283_live_syncing
                or self._v283_live_timer is not None or self._v280_purchase() is None
                or self._v283_live_armed_context != (self.lottery.get(), self.controller.period)
                or not self.snapshot or self.snapshot.pending or self._v283_risk_busy()
                or self._v283_pending_manual() or self._v283_current is None):
            return
        try:
            view = self._v283_current
            if view.mode != self._plan_mode:
                return
            candidate = freeze_purchase_snapshot(view.as_rows(), *self._v280_settings())
            if candidate is None or (candidate == self._v280_purchase()
                                     and view.mode == self._purchased_mode()):
                return
            attempt = (view.context, view.mode, view.source_key, view.bets,
                       tuple(map(str, self._v280_settings())),
                       repr(self._v280_purchase()), self._purchased_mode())
            if attempt == self._v283_live_failed_key:
                return
        except (ValueError, TypeError, KeyError, ArithmeticError):
            return
        self._v283_live_timer = self.after_idle(self._v283_sync_live_purchase)

    def _v283_sync_live_purchase(self):
        """Replace, never append; a cancelled purchase must not be resurrected."""
        self._v283_live_timer = None
        if (self._closing or self._v283_live_syncing or self._v280_purchase() is None
                or not self.snapshot or self.snapshot.pending
                or self._v283_live_armed_context != (self.lottery.get(), self.controller.period)):
            return
        try:
            view = self._v283_checked_view()
            candidate = freeze_purchase_snapshot(view.as_rows(), *self._v280_settings())
            if candidate is None:
                return  # An empty/invalid scheme must not silently delete an actual purchase.
            attempt = (view.context, view.mode, view.source_key, view.bets,
                       tuple(map(str, self._v280_settings())),
                       repr(self._v280_purchase()), self._purchased_mode())
            if candidate == self._v280_purchase() and view.mode == self._purchased_mode():
                self._v283_live_failed_key = None
                return
            if attempt == self._v283_live_failed_key:
                return
        except (ValueError, TypeError, KeyError, ArithmeticError):
            # The editor, worker and comparison table can be briefly out of sync.
            # Their next valid render schedules another attempt.
            return
        self._v283_live_syncing = True
        try:
            save_purchase_checked(self._ui_state, *view.context, candidate, view.mode)
            self._v283_live_failed_key = None
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self._v283_live_failed_key = attempt
            messagebox.showerror('补单自动同步失败',
                '当前方案没有覆盖原已补单记录，顶部仍按原记录计算。\n' + str(exc), parent=self)
        finally:
            self._v283_live_syncing = False
        self._v280_eval_cache.clear()
        self._show_hedge_checkbox()
        self._update_financial_cards()

    def _show_hedge_checkbox(self):
        super()._show_hedge_checkbox()
        if not getattr(self, '_v283_ready', False):
            return
        purchase = self._v280_purchase()
        owner = self._purchased_mode()
        # "已补单" means the current period has an actual purchase. It must not
        # flicker off just because the selected plan is awaiting auto-replacement.
        self.hedge_confirmed.set(purchase is not None)
        source = ('手动编辑' if self._plan_manual_mode else '阈值计算') if self._plan_mode == 'manual' else '自动计算'
        text = '查看：' + LABELS[self._plan_mode] + '〔' + source + '〕 ｜ 计入：' + (LABELS.get(owner, '人工方案') if purchase else '未抛单')
        if purchase and owner == self._plan_mode and self._v283_current:
            if freeze_purchase_snapshot(self._v283_current.as_rows(), *self._v280_settings()) != purchase:
                text += ('（已购与当前预览不同）' if self._v283_live_armed_context is None else
                         '（自动同步中）' if not self._v283_live_failed_key else '（自动同步失败）')
        elif purchase and owner != self._plan_mode:
            text += ('（已购与当前预览不同）' if self._v283_live_armed_context is None else
                     '（自动切换中）' if not self._v283_live_failed_key else '（自动切换失败）')
        draw, hypothetical = self._v280_draw()
        if draw:
            text += ' ｜ ' + ('模拟' if hypothetical else '开奖号') + draw
        self.plan_identity_label.configure(text=text)
        if self._v283_current is None or self._v283_risk_busy():
            # An existing purchase can always be cancelled with the independent cancel button.
            self.actual_hedge_checkbox.configure(state='disabled')

    def _update_financial_cards(self):
        super()._update_financial_cards()
        if getattr(self, '_v283_ready', False):
            self._v283_update_actual_target()

    def _v283_target_status(self, status):
        status = {'更新中': '计算中', '方案核对中': '核对中',
                  '补单同步中': '同步中', '方案编辑中': '编辑中'}.get(status, status)
        for kind, target, error, caption, label in (
            ('profit', self._v283_target, self._v283_target_error, '盈利达标', self.target_profit_count),
            ('loss', self._v283_loss_target, self._v283_loss_target_error, '亏损达标', self.target_loss_count),
        ):
            self._v283_set_target_numbers(None, kind)
            label.configure(text='目标待核对' if error else caption + '：' + (status if target is not None else '—'),
                            foreground=RED if error else MUTED)

    def _v283_update_actual_target(self):
        if not getattr(self, '_v283_ready', False):
            return
        self.target_profit_source.configure(text='')
        try:
            if not ((self._v283_target is not None and not self._v283_target_error)
                    or (self._v283_loss_target is not None and not self._v283_loss_target_error)):
                self._v283_target_status('—')
                return
            if self._v283_goal_timer is not None or self._v283_risk_busy():
                self._v283_target_status('更新中')
                return
            if (self._v280_purchase() is not None
                    and (self._v283_pending_manual() or self._v283_current is None)):
                self._v283_target_status('方案核对中')
                self.target_profit_source.configure(text='〔旧补单保留，待同步〕')
                return
            if self._v283_live_timer is not None or self._v283_live_syncing:
                self._v283_target_status('补单同步中')
                return
            income, payouts = self._v280_inputs()
            purchase = self._v280_purchase()
            actual = purchased_metrics(income, payouts, purchase)
            self.target_profit_source.configure(
                text=('〔计入：' + LABELS.get(self._purchased_mode(), '人工方案')
                      + '·含成本及返奖' + ('·同步失败' if self._v283_live_failed_key else '') + '〕')
                     if purchase else '〔未补单·不计成本及返奖〕')
            for kind, target, error, caption, label in (
                ('profit', self._v283_target, self._v283_target_error, '盈利达标', self.target_profit_count),
                ('loss', self._v283_loss_target, self._v283_loss_target_error, '亏损达标', self.target_loss_count),
            ):
                if error or target is None:
                    self._v283_set_target_numbers(None, kind)
                    label.configure(text='目标待核对' if error else caption + '：—',
                                    foreground=RED if error else MUTED)
                    continue
                numbers = _v283_loss_outcomes(actual, target) if kind == 'loss' else target_outcomes(actual, target)
                label.configure(text=f'{caption}：{len(numbers)}组' + (' *' if self.snapshot.pending else ''),
                                foreground=INK)
                self._v283_set_target_numbers(numbers, kind)
        except (ValueError, TypeError, KeyError, ArithmeticError):
            self._v283_target_status('—')

    def _v283_flush_manual(self):
        if self._plan_mode != 'manual':
            return
        modified = bool(self.plan.edit_modified() and not self._plan_editor_block)
        if modified:
            self._on_plan_text_modified()
        changed = self._plan_manual_mode and self.plan.get('1.0', 'end-1c') != self._plan_manual_text
        if self._plan_edit_timer is not None or changed:
            if self._plan_edit_timer is not None:
                self.after_cancel(self._plan_edit_timer)
                self._plan_edit_timer = None
            self._commit_manual_plan()

    def _v283_checked_view(self, flush=False):
        if flush:
            self._v283_flush_manual()
        if self._closing or self._v283_risk_busy() or self._v283_pending_manual():
            raise ValueError('方案正在更新，暂不复制旧结果')
        if self.plan_mode_choice.get() != LABELS.get(self._plan_mode):
            raise ValueError('方案选择与显示未同步')
        values = self.optimizer_compare_table.values.get(self._plan_mode)
        if not values:
            raise ValueError('方案对比结果未就绪')
        if (str(self.optimizer_compare_table.cells[self._plan_mode, 1].cget('text')) != str(values[0][0])
                or str(self.optimizer_compare_table.cells[self._plan_mode, 2].cget('text')) != str(values[0][1])):
            raise ValueError('对比表显示与方案结果尚未同步，未复制')
        return assert_view_matches(self._v283_current, mode=self._plan_mode,
            context=(self.lottery.get(), self.controller.period), source_key=self._v283_source_key(self._plan_mode),
            rows=self._plan_rows, editor_text=self.plan.get('1.0', 'end-1c'),
            count_caption=str(self.plan_count_label.cget('text')), cost_caption=str(self.plan_cost_label.cget('text')),
            comparison_count=values[0][0], comparison_cost=values[0][1])

    def _v282_valid_copy_rows(self, flush=False):
        if not getattr(self, '_v283_ready', False):
            return super()._v282_valid_copy_rows(flush)
        return self._v283_checked_view(flush).as_rows()

    def _v282_refresh_copy_controls(self):
        if not getattr(self, '_v283_ready', False):
            return super()._v282_refresh_copy_controls()
        try:
            view = self._v283_checked_view()
            enabled = True
        except (ValueError, TypeError, KeyError, ArithmeticError):
            view = None; enabled = False
        self.copy_button.configure(text='复制完整方案', state='normal' if enabled else 'disabled')
        self.copy_current_numbers_button.configure(text='复制当前方案号码', state='normal' if enabled else 'disabled')
        identity = '复制来源：' + LABELS.get(self._plan_mode, '等待选择')
        if view:
            identity += f' · {view.count}组 / {format_yuan(view.cost_cents)}元'
        else:
            identity += ' · 等待同步'
        self._v282_copy_identity.configure(text=identity)
        self.copy_risk_numbers_button.configure(text='复制阈值号码',
            state='normal' if self.snapshot and self.snapshot.risks and not self._v283_risk_busy() else 'disabled')

    def _v282_copy_selected(self, numbers_only):
        if not getattr(self, '_v283_ready', False):
            return super()._v282_copy_selected(numbers_only)
        try:
            view = self._v283_checked_view(flush=True)
            text = view.numbers_text if numbers_only else view.full_text
            self.clipboard_clear(); self.clipboard_append(text)
            if self.clipboard_get() != text:
                raise tk.TclError('剪贴板回读校验失败')
        except (ValueError, TypeError, KeyError, ArithmeticError, tk.TclError) as exc:
            self._v282_refresh_copy_controls()
            if not self._closing and self._v283_refresh_timer is None:
                self._v283_refresh_timer = self.after_idle(self._v283_refresh_view)
            messagebox.showwarning('本次未复制', str(exc) + '\n未回退为人工方案或阈值号码。', parent=self)
            return False
        self._v282_copy_identity.configure(text=f'已复制：{LABELS[view.mode]} · {view.count}组 / {format_yuan(view.cost_cents)}元')
        button = self.copy_current_numbers_button if numbers_only else self.copy_button
        button.configure(text='已复制' + LABELS[view.mode])
        if self._v282_copy_timer is not None:
            self.after_cancel(self._v282_copy_timer)
        self._v282_copy_timer = self.after(1400, self._v282_restore_copy_labels)
        return True

    def _v283_refresh_view(self):
        self._v283_refresh_timer = None
        if not self._closing:
            self._render_plan()
            self._render_risk_rows()
            self._update_financial_cards()

    def _on_plan_text_modified(self, event=None):
        before_text = getattr(self, '_plan_manual_text', '') or ''
        first_pending_edit = getattr(self, '_plan_edit_timer', None) is None
        super()._on_plan_text_modified(event)
        if getattr(self, '_v283_ready', False) and self._v283_pending_manual():
            # Postpone parsing and purchased-plan replacement until typing ends.
            self.after_cancel(self._plan_edit_timer)
            deleting = len(self._plan_manual_text or '') < len(before_text)
            self._plan_edit_timer = self.after(INPUT_DELETE_IDLE_MS if deleting else INPUT_IDLE_MS,
                                               self._commit_manual_plan)
            self._v283_current = None
            self._v283_views.pop('manual', None)
            self.plan_count_label.configure(text='号码：核对中')
            if first_pending_edit:
                # Disable stale comparison/copy controls once. Recomputing all
                # scenarios on every editor keystroke defeats debouncing.
                for column in range(1, 9):
                    self.optimizer_compare_table.cells['manual', column].configure(
                        text='编辑中' if column == 1 else '—', foreground=MUTED)
                self._v283_target_status('方案编辑中')
                self._v282_refresh_copy_controls()
                self._show_hedge_checkbox()

    def _switch_plan_mode(self, event=None):
        if getattr(self, '_v283_ready', False):
            self._v283_flush_manual()
            selected = {v: k for k, v in LABELS.items()}.get(self.plan_mode_choice.get(), 'manual')
            if selected != self._plan_mode:
                self._v283_arm_purchase_sync()
        super()._switch_plan_mode(event)

    def _update_actual_hedge_from_plan(self):
        if not getattr(self, '_v283_ready', False):
            return super()._update_actual_hedge_from_plan()
        self._v283_arm_purchase_sync()
        self._v283_queue_purchase_sync()

    def _toggle_actual_hedge(self):
        if not getattr(self, '_v283_ready', False):
            return super()._toggle_actual_hedge()
        desired = bool(self.hedge_confirmed.get())
        if not desired:
            # Even during a mode switch the checkbox represents the one actual
            # purchase, not the ownership of the currently displayed plan.
            if self._v283_live_timer is not None:
                self.after_cancel(self._v283_live_timer)
                self._v283_live_timer = None
            self._v281_cancel_current_purchase()
            if self._v280_purchase() is None:
                self._v283_live_armed_context = None
            self._v283_queue_purchase_sync()  # If cancellation was declined, resume live sync.
            return
        if desired:
            try:
                self._v283_checked_view(flush=True)
            except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
                self._show_hedge_checkbox()
                messagebox.showwarning('不能计入旧方案', str(exc), parent=self)
                return
            self.hedge_confirmed.set(True)
        result = super()._toggle_actual_hedge()
        self._v283_arm_purchase_sync()
        return result

    def adjust_plan(self, delta):
        if getattr(self, '_v283_ready', False) and self._plan_mode == 'manual':
            self._v283_flush_manual()
            try:
                self._v283_checked_view()
            except (ValueError, TypeError, KeyError, ArithmeticError):
                return
        return super().adjust_plan(delta)

    def _restore_system_plan(self):
        if not getattr(self, '_v283_ready', False):
            return super()._restore_system_plan()
        if self._plan_mode != 'manual' or not self.snapshot or self._v283_risk_busy():
            return
        if self._plan_manual_mode and not messagebox.askyesno('按当前阈值重算',
                '用当前阈值和已保存的全局倍数调整重新生成。\n'
                + ('已勾选补单，将自动覆盖本期实际补单记录。\n' if self._v280_purchase() else '')
                + '不会切换到三档自动方案。确认重算？', parent=self):
            return
        if self._plan_edit_timer is not None:
            self.after_cancel(self._plan_edit_timer); self._plan_edit_timer = None
        self._v283_arm_purchase_sync()
        self._ui_state.put_manual_plan(self.lottery.get(), self.controller.period, None)
        self._plan_manual_mode = False; self._plan_manual_text = None; self._plan_edit_error = ''
        self._v283_views.pop('manual', None); self._v283_current = None
        self._v280_scroll_request = True
        self._render_plan(); self._render_risk_rows(); self._update_financial_cards()

    def _sync_draw_context(self):
        context = (self.lottery.get(), self.controller.period)
        if getattr(self, '_v283_ready', False) and self._draw_context != context:
            self._v283_set_target_numbers(None)
            if self._v283_live_timer is not None:
                self.after_cancel(self._v283_live_timer)
                self._v283_live_timer = None
            self._v283_live_failed_key = None
            self._v283_live_armed_context = None
            self._v283_views.clear(); self._v283_current = None
        return super()._sync_draw_context()

    def _start_risk_job(self):
        if (getattr(self, '_v283_ready', False) and self.snapshot is not None
                and self._draw_context == (self.lottery.get(), self.controller.period)):
            self._v283_arm_purchase_sync()
        result = super()._start_risk_job()
        if getattr(self, '_v283_ready', False) and self._v283_risk_busy():
            self._v283_current = None
            self._render_plan()
            self._v283_update_actual_target()
        return result

    def close(self):
        if getattr(self, '_v283_ready', False):
            if self._v283_live_timer is not None:
                self.after_cancel(self._v283_live_timer)
                self._v283_live_timer = None
            self._v283_commit_target()
            self._v283_set_target_numbers(None)
        return super().close()
