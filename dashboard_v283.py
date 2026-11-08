# -*- coding: utf-8 -*-
"""V283: one displayed snapshot for text/metrics/copy, plus read-only profit target.

Inherits the shipped V280/V281/V282 features. Does not alter payout calculation,
threshold formula, optimizer profiles, recorded receipts or purchased snapshots.
"""
from __future__ import annotations
from dataclasses import replace
import json
import tkinter as tk
from tkinter import ttk, messagebox
from dashboard_v282 import DashboardV282Mixin
from dashboard_v280 import PlanComparisonTable, LABELS, MODES, INK, MUTED, RED, GREEN
from dashboard_metrics_v280 import (aggregate_rows, format_yuan, purchased_metrics)
from plan_snapshot_v283 import (DisplayedPlan, build_displayed_plan, parse_profit_target,
                                target_outcomes, assert_view_matches)


class PlanComparisonV283(PlanComparisonTable):
    headers = PlanComparisonTable.headers + ('达标组数',)
    minimum_widths = (96, 52, 73, 65, 100, 104, 106, 115)

    def __init__(self, parent, choose):
        super().__init__(parent, choose)
        for i, mode in enumerate(MODES, 1):
            label = tk.Label(self.body, text='—', font=self.cell_font, anchor='e', padx=5,
                             bg='white', fg=INK)
            label.grid(row=i, column=7, sticky='nsew', padx=(0, 1), pady=(0, 1))
            label.bind('<Button-1>', lambda _e, key=mode: self.choose(key))
            self.cells[mode, 7] = label
        self.target_heading = self.body.grid_slaves(row=0, column=7)[0]
        self.body.grid_slaves(row=0, column=5)[0].configure(text='补中最低净盈亏/元')
        self._resize()

    def set_target(self, target, invalid=False):
        self.target_heading.configure(text=('目标待核对' if invalid else '达标组数' if target is None
                                            else '达标≥' + format_yuan(target) + '元'))


def _colour(value):
    return MUTED if value is None else RED if value < 0 else GREEN if value > 0 else INK


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
        super()._build()
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
        raw = self._ui_state.data.get('target_profit_yuan', '')
        if not isinstance(raw, str):
            raw = ''
        try:
            self._v283_target = parse_profit_target(raw)
        except ValueError:
            raw = ''; self._v283_target = None
        self._v283_saved_target = raw
        self.target_profit = tk.StringVar(self, value=raw)
        # Physically after the history button, not in the plan's copy/action bar.
        box = ttk.Frame(self.history_button.master)
        box.pack(side='left', after=self.history_button, padx=(12, 0))
        self._v283_target_box = box
        ttk.Label(box, text='目标盈利', font=('Microsoft YaHei UI', 9)).pack(side='left')
        self.target_profit_entry = ttk.Entry(box, textvariable=self.target_profit,
                                             width=9, justify='right')
        self.target_profit_entry.pack(side='left', padx=(5, 3))
        ttk.Label(box, text='元', font=('Microsoft YaHei UI', 9)).pack(side='left')
        self.target_profit_count = ttk.Label(box, text='实际达标：—', foreground=MUTED,
                                             font=('Microsoft YaHei UI', 9))
        self.target_profit_count.pack(side='left', padx=(8, 0))
        self.target_profit_source = ttk.Label(box, text='', foreground=MUTED,
                                              font=('Microsoft YaHei UI', 8))
        self.target_profit_source.pack(side='left', padx=(3, 0))
        self.target_profit.trace_add('write', self._v283_target_changed)
        self.target_profit_entry.bind('<Return>', self._v283_commit_target)
        self.target_profit_entry.bind('<FocusOut>', self._v283_commit_target)

    def _v283_target_changed(self, *_):
        if self._closing:
            return
        if self._v283_goal_timer is not None:
            self.after_cancel(self._v283_goal_timer)
        # Do not leave old target counts labelled as the newly typed amount.
        self.target_profit_count.configure(text='实际达标：更新中', foreground=MUTED)
        self.target_profit_source.configure(text='')
        self._v283_goal_timer = self.after(180, self._v283_commit_target)

    def _v283_commit_target(self, _event=None):
        if self._v283_goal_timer is not None:
            self.after_cancel(self._v283_goal_timer)
            self._v283_goal_timer = None
        raw = self.target_profit.get().strip()
        self._v283_target_error = ''
        try:
            self._v283_target = parse_profit_target(raw)
        except ValueError as exc:
            self._v283_target = None
            self._v283_target_error = str(exc)
        if not self._v283_target_error and raw != self._v283_saved_target:
            before = self._ui_state.data.get('target_profit_yuan', '')
            try:
                self._ui_state.data['target_profit_yuan'] = raw
                self._ui_state.save()
                path = getattr(self._ui_state, 'path', None)
                if path is not None and json.loads(path.read_text('utf-8')).get('target_profit_yuan') != raw:
                    raise OSError('目标盈利没有保存成功')
                self._v283_saved_target = raw
            except (OSError, ValueError, TypeError) as exc:
                self._ui_state.data['target_profit_yuan'] = before
                self._v283_target_error = '目标未保存：' + str(exc)
        if self._v283_ready:
            self.optimizer_compare_table.set_target(self._v283_target, bool(self._v283_target_error))
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
                values = (str(view.count), format_yuan(view.cost_cents), str(item.loss_count),
                          format_yuan(item.worst_profit_cents, True),
                          format_yuan(minimum, True) if minimum is not None else '—',
                          format_yuan(at, True) if at is not None else '—',
                          str(len(target)) if target is not None else '—')
                colours = (INK, INK, RED if item.loss_count else INK, _colour(item.worst_profit_cents),
                           _colour(minimum), _colour(at), INK)
            except (ValueError, TypeError, KeyError, ArithmeticError):
                status = ('等待数据' if not self.snapshot else '更新中' if self._v283_risk_busy()
                          else '待核对' if mode == 'manual' else '计算失败' if self._auto_optimizer_error else '计算中')
                values = ('—', status, '—', '—', '—', '—', '—')
                colours = (MUTED,) * 7
            data[mode] = (values, colours)
        self.optimizer_compare_table.set_target(self._v283_target, bool(self._v283_target_error))
        self.optimizer_compare_table.show(data, self._plan_mode, self._purchased_mode())
        self._show_hedge_checkbox()
        self._v282_refresh_copy_controls()
        self._v283_update_actual_target()

    def _show_hedge_checkbox(self):
        super()._show_hedge_checkbox()
        if not getattr(self, '_v283_ready', False):
            return
        purchase = self._v280_purchase()
        owner = self._purchased_mode()
        source = ('手动编辑' if self._plan_manual_mode else '阈值计算') if self._plan_mode == 'manual' else '自动计算'
        text = '查看：' + LABELS[self._plan_mode] + '〔' + source + '〕 ｜ 计入：' + (LABELS.get(owner, '人工方案') if purchase else '未抛单')
        if purchase and owner == self._plan_mode and self._v283_current:
            from risk_extension_v2734 import freeze_purchase_snapshot
            if freeze_purchase_snapshot(self._v283_current.as_rows(), *self._v280_settings()) != purchase:
                text += '（已购与当前推荐不同）'
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

    def _v283_update_actual_target(self):
        if not getattr(self, '_v283_ready', False):
            return
        self.target_profit_source.configure(text='')
        try:
            if self._v283_target_error:
                raise ValueError(self._v283_target_error)
            if self._v283_target is None:
                self.target_profit_count.configure(text='实际达标：—', foreground=MUTED)
                return
            if self._v283_goal_timer is not None or self._v283_risk_busy():
                self.target_profit_count.configure(text='实际达标：更新中', foreground=MUTED)
                return
            income, payouts = self._v280_inputs()
            purchase = self._v280_purchase()
            actual = purchased_metrics(income, payouts, purchase)
            self.target_profit_source.configure(text=('〔计入：'+LABELS.get(self._purchased_mode(), '人工方案')+'〕')
                                                if purchase else '〔未抛单〕')
            count = len(target_outcomes(actual, self._v283_target))
            self.target_profit_count.configure(text=f'实际达标：{count}组' + (' *' if self.snapshot.pending else ''),
                                                foreground=INK)
        except (ValueError, TypeError, KeyError, ArithmeticError):
            self.target_profit_count.configure(text='目标待核对' if self._v283_target_error else '实际达标：—',
                                                foreground=RED if self._v283_target_error else MUTED)

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
        super()._on_plan_text_modified(event)
        if getattr(self, '_v283_ready', False) and self._v283_pending_manual():
            self._v283_current = None
            self._v283_views.pop('manual', None)
            self.plan_count_label.configure(text='号码：核对中')
            self._v283_show_comparison()

    def _switch_plan_mode(self, event=None):
        if getattr(self, '_v283_ready', False):
            self._v283_flush_manual()
        super()._switch_plan_mode(event)

    def _toggle_actual_hedge(self):
        if not getattr(self, '_v283_ready', False):
            return super()._toggle_actual_hedge()
        desired = bool(self.hedge_confirmed.get())
        if desired:
            try:
                self._v283_checked_view(flush=True)
            except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
                self._show_hedge_checkbox()
                messagebox.showwarning('不能计入旧方案', str(exc), parent=self)
                return
            self.hedge_confirmed.set(True)
        return super()._toggle_actual_hedge()

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
                '用当前阈值和已保存的全局倍数调整重新生成。\n仅替换人工编辑文本，不改已补单记录，也不切换到三档自动方案。\n确认重算？', parent=self):
            return
        if self._plan_edit_timer is not None:
            self.after_cancel(self._plan_edit_timer); self._plan_edit_timer = None
        self._ui_state.put_manual_plan(self.lottery.get(), self.controller.period, None)
        self._plan_manual_mode = False; self._plan_manual_text = None; self._plan_edit_error = ''
        self._v283_views.pop('manual', None); self._v283_current = None
        self._v280_scroll_request = True
        self._render_plan(); self._render_risk_rows(); self._update_financial_cards()

    def _sync_draw_context(self):
        context = (self.lottery.get(), self.controller.period)
        if getattr(self, '_v283_ready', False) and self._draw_context != context:
            self._v283_views.clear(); self._v283_current = None
        return super()._sync_draw_context()

    def _start_risk_job(self):
        result = super()._start_risk_job()
        if getattr(self, '_v283_ready', False) and self._v283_risk_busy():
            self._v283_current = None
            self._render_plan()
            self._v283_update_actual_target()
        return result

    def close(self):
        if getattr(self, '_v283_ready', False):
            self._v283_commit_target()
        return super().close()
