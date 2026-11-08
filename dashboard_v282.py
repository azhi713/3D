# -*- coding: utf-8 -*-
"""V282 copy commands use the selected current candidate, never a saved purchase.

No change to the payout engine, threshold/adjustment, optimizer or purchase state.
"""
from __future__ import annotations
from tkinter import ttk, messagebox
import tkinter as tk
from dashboard_v281 import DashboardV281Mixin
from dashboard_v280 import LABELS, MODES, INK, MUTED
from dashboard_metrics_v280 import aggregate_rows, amount_cents
from risk_engine import plan_text
from risk_extension_v2734 import threshold_number_copy


def copy_payload(rows, unit_cost, lottery, numbers_only=False, pending=False):
    """One validated ledger supplies both copy modes and their count/cost."""
    rows = list(rows)
    numbered = aggregate_rows(rows)
    if not numbered:
        raise ValueError('当前方案没有有效补单号码')
    cost = sum(n for _, n in numbered) * amount_cents(unit_cost)
    if numbers_only:
        codes = [number for number, _ in numbered]
        text = '\n'.join(' '.join(codes[i:i+12]) for i in range(0, len(codes), 12))
        text += '\n\n合计：'+str(len(codes))+'注'
    else:
        # Preserve purchase multiples. Formatting only: no generation/optimization.
        text = plan_text(rows, unit_cost, lottery)
    if pending:
        text = '[仅供人工核对 / 不可作为正式补单依据]\n' + text
    return text, len(numbered), cost


class DashboardV282Mixin(DashboardV281Mixin):
    def _build(self):
        self._v282_copy_ready = False
        self._v282_copy_timer = None
        super()._build()
        # The risk table retains its original, explicitly named threshold copy.
        self.copy_risk_numbers_button.configure(text='复制阈值号码', command=self.copy_risk_numbers)
        parent = self.optimizer_compare_table.master
        line = ttk.Frame(parent)
        line.grid(row=6, column=0, sticky='ew', pady=(5, 0))
        self._v282_copy_line = line
        self._v282_copy_identity = ttk.Label(line, text='', foreground=MUTED,
                                             font=('Microsoft YaHei UI', 9))
        self._v282_copy_identity.pack(side='left')
        self.copy_button.destroy()
        self.copy_button = ttk.Button(line, text='复制完整方案', command=self.copy_plan,
                                      style='V280.Small.TButton')
        self.copy_button.pack(side='right')
        self.copy_current_numbers_button = ttk.Button(line, text='复制当前方案号码',
                command=self.copy_current_plan_numbers, style='V280.Small.TButton')
        self.copy_current_numbers_button.pack(side='right', padx=(0, 6))
        self._v282_copy_ready = True
        self._v282_refresh_copy_controls()

    def _v282_valid_copy_rows(self, flush=False):
        if self._closing or not self.snapshot:
            raise ValueError('等待本期风险数据完成')
        mode = self._plan_mode
        if mode not in MODES or self.plan_mode_choice.get() != LABELS[mode]:
            raise ValueError('方案选择尚未同步，请重新选择对应方案')
        if self._draw_context != (self.lottery.get(), self.controller.period):
            raise ValueError('日期或彩种已切换，等待本期方案更新')
        if getattr(self, '_risk_running', False) or getattr(self, '_risk_next', None) is not None:
            raise ValueError('投注风险正在更新，暂不复制旧方案')
        if flush and mode == 'manual':
            # A click can arrive before the <<Modified>> debounce commits.
            # Never copy the previous manual text in that small interval.
            widget = getattr(self, 'plan', None)
            raw = widget.get('1.0', 'end-1c') if widget is not None else None
            changed = (self._plan_manual_mode and raw is not None and raw != self._plan_manual_text)
            modified = bool(widget is not None and widget.edit_modified()
                            and not getattr(self, '_plan_editor_block', False))
            if modified and not self._plan_manual_mode:
                self._on_plan_text_modified()
            if self._plan_edit_timer is not None or changed or modified:
                if self._plan_edit_timer is not None:
                    self.after_cancel(self._plan_edit_timer)
                self._plan_edit_timer = None
                self._commit_manual_plan()
        if mode == 'manual' and self._plan_manual_mode and self._plan_edit_timer is not None:
            raise ValueError('手动方案正在核对')
        if mode != 'manual':
            # No old candidate, no fallback to manual/purchased/threshold rows.
            if (self._auto_optimizer_error or mode not in self._auto_optimizer_results
                    or self._v280_optimizer_key != self._v280_input_key()):
                raise ValueError('当前自动方案未完成或已失效，不能复制旧方案')
        rows = self._v280_candidate_rows(mode)
        if not aggregate_rows(rows):
            raise ValueError('当前方案没有有效补单号码')
        return list(rows)

    def _v282_refresh_copy_controls(self):
        if not getattr(self, '_v282_copy_ready', False):
            return
        try:
            self._v282_valid_copy_rows()
            enabled = True
        except (ValueError, TypeError, KeyError, ArithmeticError):
            enabled = False
        self.copy_button.configure(state='normal' if enabled else 'disabled', text='复制完整方案')
        self.copy_current_numbers_button.configure(state='normal' if enabled else 'disabled', text='复制当前方案号码')
        self._v282_copy_identity.configure(text='复制来源：'+LABELS.get(self._plan_mode, '等待选择'))
        self.copy_risk_numbers_button.configure(text='复制阈值号码')

    def _render_plan(self):
        super()._render_plan()
        self._v282_refresh_copy_controls()

    def _update_financial_cards(self):
        super()._update_financial_cards()
        self._v282_refresh_copy_controls()

    def _queue_optimizer(self):
        super()._queue_optimizer()
        self._v282_refresh_copy_controls()

    def copy_current_plan_numbers(self):
        return self._v282_copy_selected(True)

    def copy_plan(self):
        return self._v282_copy_selected(False)

    def _v282_copy_selected(self, numbers_only):
        try:
            rows = self._v282_valid_copy_rows(flush=True)
            text, count, cost = copy_payload(rows, self._v280_settings()[0], self.lottery.get(),
                                             numbers_only, bool(self.snapshot.pending))
            # This call stays on the Tk event thread. Candidate cannot switch
            # between snapshot selection and clipboard write.
            self.clipboard_clear()
            self.clipboard_append(text)
            # Readback catches an actual clipboard failure instead of claiming success.
            if self.clipboard_get() != text:
                raise tk.TclError('剪贴板回读与当前方案不一致')
        except (ValueError, TypeError, KeyError, ArithmeticError, tk.TclError) as exc:
            messagebox.showwarning('本次未复制', str(exc)+'\n未回退复制人工方案。', parent=self)
            return False
        label = LABELS[self._plan_mode]
        button = self.copy_current_numbers_button if numbers_only else self.copy_button
        button.configure(text='已复制'+label)
        self._v282_copy_identity.configure(text=label+' · '+str(count)+'组已复制')
        if self._v282_copy_timer is not None:
            try:
                self.after_cancel(self._v282_copy_timer)
            except tk.TclError:
                pass
        self._v282_copy_timer = self.after(1400, self._v282_restore_copy_labels)
        return True

    def _v282_restore_copy_labels(self):
        self._v282_copy_timer = None
        if not self._closing:
            self._v282_refresh_copy_controls()

    def _v280_restore_copy_label(self):
        # Retained for any pre-V282 pending compatibility timer.
        self._copy_timer = None
        self._v282_restore_copy_labels()

    def copy_risk_numbers(self):
        """An intentionally independent operation: current threshold rows only."""
        if (not self.snapshot or getattr(self, '_risk_running', False)
                or getattr(self, '_risk_next', None) is not None):
            return False
        text, count = threshold_number_copy(self.snapshot.risks, per_line=12)
        if not text or count <= 0:
            return False
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            if self.clipboard_get() != text:
                raise tk.TclError('阈值号码剪贴板回读失败')
        except tk.TclError as exc:
            messagebox.showwarning('本次未复制', str(exc), parent=self)
            return False
        self.copy_risk_numbers_button.configure(text='阈值号码已复制')
        timer = getattr(self, '_risk_number_copy_timer', None)
        if timer is not None:
            try:
                self.after_cancel(timer)
            except tk.TclError:
                pass
        self._risk_number_copy_timer = self.after(1400, self._reset_risk_copy_label)
        return True

    def _reset_risk_copy_label(self):
        self._risk_number_copy_timer = None
        if not self._closing:
            self.copy_risk_numbers_button.configure(text='复制阈值号码')
