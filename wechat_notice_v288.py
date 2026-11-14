"""Pure schedule, crash-safe dispatch ledger and explicit WeChat send adapters.

All ledger methods are called from Tk's main thread.  The actual send runs on
one background worker; it must never access Tk or the main SQLite connection.
"""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from wechat_groups_v288 import bound_groups

MESSAGES = {'福彩': '福停止收单', '体彩': '体停止收单'}


def notice_times(clock):
    """Saturday changes FC; Thursday changes TC; weekday() Monday=0."""
    return {
        '福彩': (21, 13 if clock.weekday() == 5 else 14),
        '体彩': (21, 23 if clock.weekday() == 3 else 24),
    }


def due_send(clock, config):
    if not config.get('send_stop_notice', False):
        return []
    return [(lottery, gid, MESSAGES[lottery])
            for lottery, (hour, minute) in notice_times(clock).items()
            if (clock.hour, clock.minute) == (hour, minute)
            for gid in bound_groups(config, lottery)]


def due_reminder(clock, config):
    if not config.get('special_day_reminder', False):
        return []
    return ([('福彩', '周六福彩提前收单：21:13停单，剩余3分钟')]
            if clock.weekday() == 5 and (clock.hour, clock.minute) == (21, 10) else
            [('体彩', '周四体彩提前收单：21:23停单，剩余3分钟')]
            if clock.weekday() == 3 and (clock.hour, clock.minute) == (21, 20) else [])


class NoticeLedger:
    """Claim before sending: a crash/uncertain reply must not cause a resend."""

    def __init__(self, path):
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(self.data, dict):
                raise ValueError('invalid ledger')
        except FileNotFoundError:
            self.data = {}
        # A damaged ledger is a hard stop, not a reason to resend all messages.

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.wechat_notice_', suffix='.tmp', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(self.data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @staticmethod
    def _key(day, lottery, group_id):
        return '|'.join((day, lottery, group_id))

    def reserve(self, day, lottery, group_id, clock):
        key = self._key(day, lottery, group_id)
        if key in self.data:
            return False
        self.data[key] = {'status': 'sending_unknown', 'at': clock.isoformat(),
                          'lottery': lottery, 'group_id': group_id}
        try:
            self._save()
        except Exception:
            self.data.pop(key, None)
            raise
        return True

    def finish(self, day, lottery, group_id, status, reason, clock):
        key = self._key(day, lottery, group_id)
        if key not in self.data:
            return
        if status not in ('confirmed', 'failed', 'unknown'):
            status = 'unknown'
        self.data[key].update(status=status, result_at=clock.isoformat(),
                              detail=str(reason)[:180])
        self._save()


def send_notice(backend, group_id, group_name, text, *, gui_send=None, wcf_sender=None):
    """Return a status; never turn an unverified send into confirmed success."""
    if backend == 'wcf':
        if wcf_sender is None:
            raise RuntimeError('WCF发送端不可用')
        result = wcf_sender(text, group_id)
        # WCF send_text returns 0 when its API accepted the send.
        return ('confirmed', 'WCF接口返回成功') if type(result) is int and result == 0 else ('unknown', f'WCF返回：{result!s}')
    if backend != 'wechat4':
        raise RuntimeError('本机桥接仅支持接收；暂不支持群消息发送')
    if not group_name or group_name.startswith('未命名群 ') or group_name == group_id:
        raise RuntimeError('没有可靠的群名称，停止发送以避免发错群')
    if gui_send is None:
        from wechatauto.guia import quick_send
        gui_send = quick_send
    result = gui_send(text, group_name, verify=True)
    if result is True:
        return 'confirmed', '发送端已验证'
    return 'unknown', '发送端未返回可验证的成功标识，禁止自动重发'
