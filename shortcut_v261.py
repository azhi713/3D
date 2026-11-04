"""Configurable in-window hotkeys for the 3D risk desktop UI.

No global OS hooks, no disk I/O. Bindings are validated and persisted via the
existing settings save path. Native text editing shortcuts take precedence.
"""
from __future__ import annotations
import re

DEFAULT_SHORTCUTS={
    'lottery_fc':'Ctrl+1', 'lottery_tc':'Ctrl+2',
    'tab_records':'Ctrl+F1', 'tab_risk':'Ctrl+F2', 'tab_draw':'Ctrl+F3',
    'toggle_wechat':'Ctrl+F4', 'toggle_ai':'Ctrl+F5',
    'search_records':'Ctrl+F', 'add_bet':'Ctrl+Enter',
    'select_all':'Ctrl+A', 'invert':'Ctrl+I', 'clear_selection':'Ctrl+Shift+D',
    'select_pending':'Ctrl+Shift+U', 'reparse':'Ctrl+R',
    'ai_convert':'Ctrl+Shift+I', 'copy_records':'Ctrl+C',
    'open_details':'Enter', 'delete_records':'Delete',
}
SHORTCUT_GROUPS={
    '页面切换':('lottery_fc','lottery_tc','tab_records','tab_risk','tab_draw'),
    '运行控制':('toggle_wechat','toggle_ai'),
    '录入与搜索':('search_records','add_bet'),
    '投注表格':('select_all','invert','clear_selection','select_pending',
              'reparse','ai_convert','copy_records','open_details','delete_records'),
}
SHORTCUT_LABELS={
 'lottery_fc':'切换福彩3D','lottery_tc':'切换体彩排列三',
 'tab_records':'投注管理','tab_risk':'实时风险','tab_draw':'开奖试算（实时风险）',
 'toggle_wechat':'开启/关闭微信监听','toggle_ai':'开启/关闭AI识别',
 'search_records':'搜索投注记录','add_bet':'添加投注',
 'select_all':'全选记录','invert':'反选记录','clear_selection':'取消选择',
 'select_pending':'选中待处理','reparse':'重新识别',
 'ai_convert':'AI转换','copy_records':'复制原文',
 'open_details':'查看详情','delete_records':'删除选中记录',
}


def canonical(raw):
    """Whitelist Ctrl/Shift and keyboard numbers; KP_1 is not Ctrl+1."""
    if not isinstance(raw,str):raise ValueError('快捷键须为文字')
    tokens=[x.strip().lower() for x in raw.split('+')]
    if len(tokens)>3 or not tokens or any(not x for x in tokens):
        raise ValueError('快捷键格式无效')
    if len(set(tokens))!=len(tokens):raise ValueError('快捷键修饰键重复')
    modifiers=[x for x in tokens if x in ('ctrl','control','shift')]
    keys=[x for x in tokens if x not in ('ctrl','control','shift')]
    if len(keys)!=1:raise ValueError('快捷键只允许一个主按键')
    k=keys[0]
    if k.startswith('kp_') or k.startswith('num'):
        raise ValueError('请使用字母上方数字键，而不是小键盘数字')
    if k in ('return','enter'):k='Enter'
    elif k in ('delete','del'):k='Delete'
    elif re.fullmatch('f([1-9]|1[0-2])',k):k=k.upper()
    elif re.fullmatch('[a-z0-9]',k):k=k.upper()
    else:raise ValueError('仅支持字母、字母上方数字、F1-F12、Enter、Delete')
    # Unmodified letters/digits interfere with ordinary text entry.
    if not modifiers and k not in ('Enter','Delete'):
        raise ValueError('字母和数字快捷键必须同时使用Ctrl')
    if 'shift' in modifiers and not any(x in modifiers for x in ('ctrl','control')):
        raise ValueError('Shift不能单独用于此类快捷键')
    return ('Ctrl+' if any(x in modifiers for x in ('ctrl','control')) else '') + ('Shift+' if 'shift' in modifiers else '')+k


def validate(mapping):
    if not isinstance(mapping,dict):raise ValueError('快捷键配置格式无效')
    checked={};assigned={}
    for action in DEFAULT_SHORTCUTS:
        raw=mapping.get(action,DEFAULT_SHORTCUTS[action])
        if raw in ('',None):
            checked[action]=''
            continue
        combo=canonical(raw)
        if combo in assigned:
            raise ValueError(f'快捷键冲突：{SHORTCUT_LABELS[assigned[combo]]} 与 {SHORTCUT_LABELS[action]} 都使用 {combo}')
        checked[action]=combo;assigned[combo]=action
    return checked


def from_event(event):
    """Return a canonical binding for an actual Tk KeyPress, or None."""
    key=str(getattr(event,'keysym',''))
    if key in ('Control_L','Control_R','Shift_L','Shift_R','Alt_L','Alt_R'):
        return None
    state=int(getattr(event,'state',0))
    ctrl=bool(state&0x4)
    shift=bool(state&0x1)
    if key.startswith('KP_'):return None
    # Tk reports either uppercase A or lowercase a depending on Shift.
    spec=('Ctrl+' if ctrl else '')+('Shift+' if shift else '')+key
    try:return canonical(spec)
    except ValueError:return None
