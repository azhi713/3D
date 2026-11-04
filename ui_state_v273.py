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
                     'period_range': '7', 'window_geometry': '', 'window_zoomed': False}
        try:
            loaded = json.loads(self.path.read_text(encoding='utf-8'))
            if isinstance(loaded, dict):
                self.data.update({key: loaded[key] for key in self.data if key in loaded})
        except (OSError, ValueError, TypeError):
            pass
        for key, default in (('scopes', {}), ('hidden_periods', []), ('actual_hedges', {})):
            if not isinstance(self.data[key], type(default)):
                self.data[key] = default
        if self.data['period_range'] not in ('7', '30', 'all'):
            self.data['period_range'] = '7'

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
        raw = self.data.get('window_geometry', '')
        if not isinstance(raw, str):
            return ''
        match = WINDOW_RE.fullmatch(raw)
        if not match:
            return ''
        w, h, x, y = map(int, match.groups())
        # 避免分辨率变化后窗口落到屏幕以外。
        if not 900 <= w <= max(900, screen_w) or not 590 <= h <= max(590, screen_h):
            return ''
        if x < -w + 150 or x > screen_w - 100 or y < -50 or y > screen_h - 100:
            return ''
        return raw

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
