"""事务存储原文；旧JSON只读迁入，损坏时明确报错，绝不当作空数据。"""
import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from config import DEFAULT_SETTINGS, LOTTERIES
from models import MessageRecord, now
from credentials import protect_key, unprotect_key, CredentialError


class DataStore:
    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self.data_dir = self.base_dir / 'data'
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / 'risk_v2.sqlite3'
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS records (
                id TEXT PRIMARY KEY, period TEXT NOT NULL, lottery TEXT NOT NULL,
                source_id TEXT, source TEXT NOT NULL, payload TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0);
            CREATE UNIQUE INDEX IF NOT EXISTS source_message_unique
                ON records(lottery,source,source_id) WHERE source_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS period_index ON records(period,lottery);
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, timestamp TEXT, action TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS imports (name TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS inbox (
                id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, group_id TEXT NOT NULL,
                payload TEXT NOT NULL, processed INTEGER NOT NULL DEFAULT 0,
                UNIQUE(source_id,group_id));
            CREATE TABLE IF NOT EXISTS period_rules (period TEXT PRIMARY KEY, payload TEXT NOT NULL);
        ''')
        self.import_errors = []
        self.credential_error = ''

    def _audit(self, action, obj):
        self.db.execute('INSERT INTO audit(timestamp,action,payload) VALUES(?,?,?)',
                        (now().isoformat(), action, json.dumps(obj, ensure_ascii=False)))

    def insert(self, record):
        with self.db:
            try:
                self.db.execute('INSERT INTO records(id,period,lottery,source_id,source,payload) VALUES(?,?,?,?,?,?)',
                                (record.record_id, record.period, record.lottery, record.source_message_id or None,
                                 record.source, json.dumps(record.to_dict(), ensure_ascii=False)))
            except sqlite3.IntegrityError:
                if record.source_message_id and self.find_source(record.lottery, record.source, record.source_message_id):
                    return False
                raise
            self._audit('添加原文', record.to_dict())
        return True

    def update(self, record, action='更新'):
        with self.db:
            changed = self.db.execute('UPDATE records SET payload=? WHERE id=? AND deleted=0',
                                     (json.dumps(record.to_dict(), ensure_ascii=False), record.record_id)).rowcount
            if changed != 1:
                raise ValueError('记录不存在或已删除')
            self._audit(action, record.to_dict())

    def delete(self, record):
        with self.db:
            self.db.execute('UPDATE records SET deleted=1 WHERE id=?', (record.record_id,))
            self._audit('删除', record.to_dict())

    def get(self, record_id):
        row = self.db.execute('SELECT payload FROM records WHERE id=? AND deleted=0', (record_id,)).fetchone()
        return MessageRecord.from_dict(json.loads(row[0])) if row else None

    def find_source(self, lottery, source, source_id):
        return self.db.execute('SELECT id FROM records WHERE lottery=? AND source=? AND source_id=?',
                               (lottery, source, source_id)).fetchone()

    def load_records(self, period=None):
        period = period or now().date().isoformat()
        return [MessageRecord.from_dict(json.loads(row[0])) for row in self.db.execute(
            'SELECT payload FROM records WHERE period=? AND deleted=0 ORDER BY rowid', (period,))]

    def periods(self):
        return [row[0] for row in self.db.execute('SELECT DISTINCT period FROM records ORDER BY period DESC')]

    def import_legacy(self):
        count = 0
        for path in self.data_dir.glob('????-??-??.json'):
            if self.db.execute('SELECT 1 FROM imports WHERE name=?', (path.name,)).fetchone():
                continue
            try:
                obj = json.loads(path.read_text(encoding='utf-8-sig'))
                if not isinstance(obj.get('records'), list):
                    raise ValueError('缺少records数组')
                with self.db:
                    for old in obj['records']:
                        record = MessageRecord.from_dict(old)
                        if record.lottery not in LOTTERIES:
                            raise ValueError('旧记录彩种无效')
                        record.period = obj.get('date', path.stem)
                        # 旧解析结果不可信，必须用原文重新识别。
                        record.bets = []
                        record.status = '待确认'
                        record.note = '旧版数据迁入，等待新规则重新识别'
                        self.db.execute('INSERT OR IGNORE INTO records(id,period,lottery,source_id,source,payload) VALUES(?,?,?,?,?,?)',
                                        (record.record_id, record.period, record.lottery, record.source_message_id or None,
                                         record.source, json.dumps(record.to_dict(), ensure_ascii=False)))
                        count += 1
                    self.db.execute('INSERT INTO imports(name) VALUES(?)', (path.name,))
                    self._audit('旧数据迁入', {'file': path.name})
            except (ValueError, KeyError, TypeError) as exc:
                self.import_errors.append(f'{path.name}：{exc}')
        return count

    def load_settings(self):
        settings = deepcopy(DEFAULT_SETTINGS)
        row = self.db.execute('SELECT payload FROM settings WHERE id=1').fetchone()
        if row:
            merge(settings, json.loads(row[0]))
        else:
            old = self.data_dir / 'settings.json'
            if old.exists():
                saved = json.loads(old.read_text(encoding='utf-8-sig'))
                merge(settings, saved)
                settings['threshold_metric'] = 'payout'
                settings['plan_total_mode'] = 'money'
        protected = settings.get('ai', {}).pop('api_key_encrypted', '')
        self.credential_error = ''
        if protected:
            try:
                settings['ai']['api_key'] = unprotect_key(protected)
            except CredentialError as exc:
                settings['ai']['api_key'] = ''
                self.credential_error = str(exc)
        return settings

    def save_settings(self, settings):
        clean = deepcopy(settings)
        ai = clean.setdefault('ai', {})
        key = ai.pop('api_key', '').strip()
        ai.pop('api_key_encrypted', None)
        if key:
            ai['api_key_encrypted'] = protect_key(key)
        clean.get('wechat', {}).pop('bridge_token', None)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO settings(id,payload) VALUES(1,?)',
                            (json.dumps(clean, ensure_ascii=False),))

    def close(self):
        self.db.close()

    def save_period_rules(self, period, rules, policy):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO period_rules(period,payload) VALUES(?,?)',
                            (period, json.dumps({'rules': rules, 'policy': policy}, ensure_ascii=False)))

    def load_period_rules(self, period):
        row = self.db.execute('SELECT payload FROM period_rules WHERE period=?', (period,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def enqueue(path, payload):
        # 接收线程独立连接，HTTP成功返回前已持久保存。
        with closing(sqlite3.connect(path, timeout=10)) as db:
            with db:
                changed = db.execute('INSERT OR IGNORE INTO inbox(source_id,group_id,payload) VALUES(?,?,?)',
                                     (payload['message_id'], payload['group_id'], json.dumps(payload, ensure_ascii=False))).rowcount
        return bool(changed)

    def inbox(self, group_ids=None):
        query = 'SELECT id,payload FROM inbox WHERE processed=0'
        args = []
        if group_ids is not None:
            args = [group for group in group_ids if group]
            if not args:
                return []
            query += ' AND group_id IN ('+','.join('?' for _ in args)+')'
        query += ' ORDER BY id LIMIT 100'
        return [(row[0], json.loads(row[1])) for row in self.db.execute(query, args)]

    def known_groups(self):
        return [(row[0], row[0]) for row in self.db.execute('SELECT DISTINCT group_id FROM inbox')]

    def mark_inbox_processed(self, inbox_id):
        with self.db:
            self.db.execute('UPDATE inbox SET processed=1 WHERE id=?', (inbox_id,))


def merge(target, source):
    for key, val in source.items():
        if isinstance(val, dict) and isinstance(target.get(key), dict):
            merge(target[key], val)
        else:
            target[key] = val
