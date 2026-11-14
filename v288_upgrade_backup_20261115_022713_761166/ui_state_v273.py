# -*- coding: utf-8 -*-
"""V273 纯UI状态；独立JSON，不写入投注、风险或开奖结算数据库。"""
from datetime import date, timedelta
import json
import os
from pathlib import Path
import re
import tempfile
from risk_extension_v2734 import normalize_purchase_snapshot

WINDOW_RE = re.compile(r'^(\d{3,5})x(\d{3,5})([+-]\d{1,6})([+-]\d{1,6})$')
DATE_RE = re.compile(r'^20\d\d-\d\d-\d\d$')

class UIStateV273:
    def __init__(self, path):
        self.path = Path(path)
        self.data = {'schema': 1, 'scopes': {}, 'hidden_periods': [], 'actual_hedges': {},
                     'manual_hedge_plans': {}, 'risk_split': {}, 'hedge_modes': {},
                     'period_range': '7', 'window_geometry': '', 'window_zoomed': False}
        # V283-DISPLAY-SNAPSHOT-PROFIT-TARGET: include target in persisted-key allowlist.
        self.data.setdefault('target_profit_yuan', '')
        self.data.setdefault('target_loss_yuan', '')
        # None means "never migrated"; an explicit empty string means the user
        # cleared the unified target and must not resurrect a legacy value.
        self.data.setdefault('target_goal_yuan', None)
        self.data.setdefault('records_split', {})
        # V281-THRESHOLD-STEP-SETTING: include in persisted-key allowlist.
        self.data.setdefault('threshold_step_yuan', '1000')
        # V280-EIGHT-CARDS-FOUR-PLAN-DASHBOARD: loaded JSON may contain these keys.
        self.data.setdefault('purchased_hedge_modes', {})
        self.data.setdefault('normal_window_geometry', '')
        self.data.setdefault('window_geometry_schema', 0)
        try:
            loaded = json.loads(self.path.read_text(encoding='utf-8'))
            if isinstance(loaded, dict):
                self.data.update({key: loaded[key] for key in self.data if key in loaded})
        except (OSError, ValueError, TypeError):
            pass
        for key, default in (('scopes', {}), ('hidden_periods', []), ('actual_hedges', {}),
                             ('manual_hedge_plans', {}), ('risk_split', {}),
                             ('records_split', {}), ('hedge_modes', {})):
            if not isinstance(self.data[key], type(default)):
                self.data[key] = default
        if self.data['period_range'] not in ('7', '30', 'all'):
            self.data['period_range'] = '7'
        if not isinstance(self.data.get('purchased_hedge_modes'), dict):
            self.data['purchased_hedge_modes'] = {}
        if not isinstance(self.data.get('normal_window_geometry'), str):
            self.data['normal_window_geometry'] = ''

    # V2734: 实际已买补单与实时风险模拟分开保存。
    def get_actual_hedge(self, lottery, period):
        key = str(lottery) + '|' + str(period)
        stored = self.data.get('actual_hedges', {}).get(key)
        return normalize_purchase_snapshot(stored)

    def put_actual_hedge(self, lottery, period, snapshot):
        key = str(lottery) + '|' + str(period)
        normalized = normalize_purchase_snapshot(snapshot)
        if snapshot is not None and normalized is None:
            raise ValueError('实际补单快照校验失败，不允许保存')
        if normalized is None:
            self.data['actual_hedges'].pop(key, None)
        else:
            self.data['actual_hedges'][key] = normalized
        self.save()

    # V276-COMBINED-RECOGNITION-EDITABLE-HEDGE
    # V277-THREE-PROFILES-SORTED-HEDGE
    def get_hedge_mode(self, lottery, period):
        key = str(lottery) + '|' + str(period)
        mode = self.data.get('hedge_modes', {}).get(key, 'manual')
        return mode if mode in ('manual','cheap','balanced','profit') else 'manual'

    def put_hedge_mode(self, lottery, period, mode):
        if mode not in ('manual','cheap','balanced','profit'):
            raise ValueError('未知的自动补单模式')
        key = str(lottery) + '|' + str(period)
        self.data.setdefault('hedge_modes', {})[key] = mode
        self.save()

    def get_manual_plan(self, lottery, period):
        key = str(lottery) + '|' + str(period)
        saved = self.data.get('manual_hedge_plans', {}).get(key)
        return saved if isinstance(saved, str) and len(saved) <= 80000 else None

    def put_manual_plan(self, lottery, period, text):
        key = str(lottery) + '|' + str(period)
        if text is not None and (not isinstance(text, str) or len(text) > 80000):
            raise ValueError('手动抛单方案不是有效文本或过长')
        if text is None:
            self.data['manual_hedge_plans'].pop(key, None)
        else:
            self.data['manual_hedge_plans'][key] = text
        self.save()

    def get_scope(self, lottery, period):
        item = self.data['scopes'].get(str(lottery) + '|' + str(period), {})
        if not isinstance(item, dict):
            return {'draw': '', 'adjustment': 0}
        draw = item.get('draw', '')
        if not isinstance(draw, str) or not (draw == '' or re.fullmatch(r'[0-9]{3}', draw)):
            draw = ''
        try:
            delta = int(item.get('adjustment', 0))
            delta = max(-100000, min(100000, delta))
        except (TypeError, ValueError):
            delta = 0
        return {'draw': draw, 'adjustment': delta}

    def put_scope(self, lottery, period, draw, adjustment):
        # 即便正在编辑1-2位号码，也必须保住全局倍数调整量；
        # 部分号码不当作已确认的3位试算号码保存。
        if draw != '' and not re.fullmatch(r'[0-9]{3}', str(draw)):
            draw = self.get_scope(lottery, period)['draw']
        key = str(lottery) + '|' + str(period)
        self.data['scopes'][key] = {'draw': str(draw), 'adjustment': int(adjustment)}
        self.save()

    def get_window_geometry(self, screen_w, screen_h):
        from dashboard_metrics_v280 import restored_geometry
        return restored_geometry(self.data, screen_w, screen_h)

    def put_window_geometry(self, raw):
        if isinstance(raw, str) and WINDOW_RE.fullmatch(raw):
            self.data['window_geometry'] = raw
            self.save()

    def hidden_periods(self):
        return set(map(str, self.data.get('hidden_periods', [])))

    def set_hidden(self, period, hidden):
        current = self.hidden_periods()
        if hidden:
            current.add(str(period))
        else:
            current.discard(str(period))
        self.data['hidden_periods'] = sorted(current)
        self.save()

    def set_period_range(self, mode):
        if mode not in ('7','30','all'):
            raise ValueError('未知日期范围')
        self.data['period_range'] = mode
        self.save()

    def save(self):
        # 写到新临时文件后原子替换；失败不能损坏UI或数据库。
        temp_name = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', delete=False,
                                             dir=str(self.path.parent), suffix='.tmp') as handle:
                temp_name = handle.name
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
            os.replace(temp_name, self.path)
        except OSError:
            if temp_name:
                try: os.unlink(temp_name)
                except OSError: pass

    def get_purchased_mode(self, lottery, period):
        if self.get_actual_hedge(lottery, period) is None:
            return None
        key = str(lottery)+'|'+str(period)
        mode = self.data.get('purchased_hedge_modes', {}).get(key, 'manual')
        return mode if mode in ('manual','cheap','balanced','profit') else 'manual'

    def put_purchased_plan(self, lottery, period, snapshot, mode):
        if mode not in ('manual','cheap','balanced','profit'):
            raise ValueError('未知的已补单方案')
        normalized = normalize_purchase_snapshot(snapshot)
        if snapshot is not None and normalized is None:
            raise ValueError('已补单快照无效')
        key = str(lottery)+'|'+str(period)
        if normalized is None:
            self.data['actual_hedges'].pop(key, None)
            self.data['purchased_hedge_modes'].pop(key, None)
        else:
            self.data['actual_hedges'][key] = normalized
            self.data['purchased_hedge_modes'][key] = mode
        self.save()


def visible_periods(active_periods, today, current, hidden, mode='7'):
    result = {str(today), str(current)}
    hidden = set(hidden)
    for period in active_periods:
        period = str(period)
        if period in hidden and period != str(current):
            continue
        if mode == 'all':
            result.add(period)
            continue
        if not DATE_RE.fullmatch(period):
            continue
        try:
            age = (date.fromisoformat(today) - date.fromisoformat(period)).days
        except ValueError:
            continue
        if 0 <= age < (30 if mode == '30' else 7):
            result.add(period)
    return sorted(result, reverse=True)
