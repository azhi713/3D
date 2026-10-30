"""事务存储原文；旧JSON只读迁入，损坏时明确报错，绝不当作空数据。"""
import json
import hashlib
import re
import sqlite3
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from config import DEFAULT_SETTINGS, DEFAULT_AI_PROMPT, LOTTERIES
from models import MessageRecord, now
from credentials import protect_key, unprotect_key, CredentialError


class StaleRecordError(ValueError):
    pass


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
            CREATE TABLE IF NOT EXISTS record_revisions (
                id INTEGER PRIMARY KEY, record_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                action TEXT NOT NULL, before_payload TEXT NOT NULL, after_payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS record_revision_index ON record_revisions(record_id,id);
            CREATE TABLE IF NOT EXISTS batch_snapshots (
                period TEXT NOT NULL, lottery TEXT NOT NULL, state TEXT NOT NULL,
                frozen_at TEXT NOT NULL, frozen_payload TEXT NOT NULL,
                finalized_at TEXT, final_payload TEXT,
                PRIMARY KEY(period,lottery));
            CREATE TABLE IF NOT EXISTS settlements (
                period TEXT NOT NULL, lottery TEXT NOT NULL, draw TEXT NOT NULL,
                created_at TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(period,lottery));
        ''')
        self.import_errors = []
        self.credential_error = ''

    def _audit(self, action, obj):
        self.db.execute('INSERT INTO audit(timestamp,action,payload) VALUES(?,?,?)',
                        (now().isoformat(), action, json.dumps(obj, ensure_ascii=False)))

    def insert(self, record):
        if not record.source_original_text:
            record.source_original_text = record.raw_text
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

    def update(self, record, action='更新', expected_revision=None):
        with self.db:
            old = self.db.execute('SELECT payload FROM records WHERE id=? AND deleted=0',
                                  (record.record_id,)).fetchone()
            if not old:
                raise StaleRecordError('记录不存在或已删除')
            if expected_revision is not None and json.loads(old[0]).get('revision',0)!=expected_revision:
                raise StaleRecordError('记录已修改，迟到结果未写入')
            new_payload = json.dumps(record.to_dict(), ensure_ascii=False)
            condition='UPDATE records SET payload=? WHERE id=? AND deleted=0'
            values=[new_payload,record.record_id]
            if expected_revision is not None:
                condition+=' AND payload=?'
                values.append(old[0])
            changed = self.db.execute(condition,values).rowcount
            if changed != 1:
                raise StaleRecordError('记录已删除或修改，迟到结果未写入')
            if old[0] != new_payload:
                self.db.execute(
                    'INSERT INTO record_revisions(record_id,timestamp,action,before_payload,after_payload)'
                    ' VALUES(?,?,?,?,?)',
                    (record.record_id, now().isoformat(), action, old[0], new_payload))
            self._audit(action, record.to_dict())

    def delete(self, record):
        return self.delete_many([record.record_id])

    def delete_many(self, record_ids):
        """批量软删除：整批成功或整批回滚，审计保留，后台AI不能写回。"""
        ids = list(dict.fromkeys(str(item) for item in record_ids if item))
        if not ids:
            return 0
        deleted = 0
        with self.db:
            for record_id in ids:
                row = self.db.execute(
                    'SELECT payload FROM records WHERE id=? AND deleted=0', (record_id,)
                ).fetchone()
                if row is None:
                    continue
                changed = self.db.execute(
                    'UPDATE records SET deleted=1 WHERE id=? AND deleted=0', (record_id,)
                ).rowcount
                if changed == 1:
                    self._audit('删除', json.loads(row[0]))
                    deleted += 1
        return deleted

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
        # 仅更新程序自带的旧版默认提示词；用户自行编辑的提示词绝不覆盖。
        saved_prompt = settings.get('ai', {}).get('prompt') or ''
        legacy_default_hashes = {
            '4c417912d05a502218648d78283337bd4f8e4ffab18b468eb3788a075e80df7f',
            'fd4cfae8a62172c5bf163994b9ac93d8a3d408363700aacd10a91afdadb4c7ad',
        }
        if saved_prompt and hashlib.sha256(saved_prompt.encode('utf-8')).hexdigest() in legacy_default_hashes:
            settings['ai']['prompt'] = DEFAULT_AI_PROMPT
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

    def load_batch(self, period, lottery):
        row = self.db.execute(
            'SELECT state,frozen_at,frozen_payload,finalized_at,final_payload FROM batch_snapshots'
            ' WHERE period=? AND lottery=?', (period,lottery)).fetchone()
        if not row:
            return None
        return {'state':row[0], 'frozen_at':row[1], 'frozen':json.loads(row[2]),
                'finalized_at':row[3], 'final':json.loads(row[4]) if row[4] else None}

    def freeze_batch(self, period, lottery, snapshot, timestamp):
        """冻结快照只写一次，避免之后的AI/人工修改覆盖截止时的数据。"""
        with self.db:
            changed = self.db.execute(
                'INSERT OR IGNORE INTO batch_snapshots'
                '(period,lottery,state,frozen_at,frozen_payload) VALUES(?,?,?,?,?)',
                (period, lottery, 'frozen', timestamp,
                 json.dumps(snapshot, ensure_ascii=False))).rowcount
            if changed:
                self._audit('21:14冻结批次', {'period':period,'lottery':lottery,'at':timestamp})
        return bool(changed)

    def finalize_batch(self, period, lottery, summary, timestamp):
        """截止快照独立保存；最终统计也只允许第一次成功写入。"""
        with self.db:
            changed = self.db.execute(
                'UPDATE batch_snapshots SET state=?,finalized_at=?,final_payload=?'
                ' WHERE period=? AND lottery=? AND state=?',
                ('finalized', timestamp, json.dumps(summary, ensure_ascii=False),
                 period, lottery, 'frozen')).rowcount
            if changed:
                self._audit('批次汇总完成', {'period':period,'lottery':lottery,'at':timestamp})
        return bool(changed)

    def record_history(self, record_id):
        return [
            {'timestamp':row[0], 'action':row[1],
             'before':json.loads(row[2]), 'after':json.loads(row[3])}
            for row in self.db.execute(
                'SELECT timestamp,action,before_payload,after_payload'
                ' FROM record_revisions WHERE record_id=? ORDER BY id', (record_id,))
        ]

    def save_settlement(self, period, lottery, draw, payload):
        """独立于下注记录、首次写入后不可覆盖的兑奖账本。"""
        if not re.fullmatch(r'[0-9]{3}', draw):
            raise ValueError('开奖号码必须为三位数字，000也有效')
        with self.db:
            existing = self.db.execute(
                'SELECT draw FROM settlements WHERE period=? AND lottery=?',
                (period,lottery)).fetchone()
            if existing:
                raise ValueError('本期已经结算，不能覆盖旧开奖结果或派奖账本')
            self.db.execute(
                'INSERT INTO settlements(period,lottery,draw,created_at,payload)'
                ' VALUES(?,?,?,?,?)',
                (period,lottery,draw,now().isoformat(),json.dumps(payload,ensure_ascii=False)))
            self._audit('独立开奖结果结算', {'period':period,'lottery':lottery,'draw':draw})

    def load_settlement(self, period, lottery):
        row = self.db.execute(
            'SELECT payload FROM settlements WHERE period=? AND lottery=?',
            (period,lottery)).fetchone()
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

    def mark_inbox_rejected(self, inbox_id, reason):
        """逾期收件仍保留原payload和审计证据，不默默删掉原文。"""
        with self.db:
            self.db.execute('UPDATE inbox SET processed=2 WHERE id=? AND processed=0', (inbox_id,))
            self._audit('截止后拒收收件箱消息', {'inbox_id':inbox_id,'reason':reason})


def merge(target, source):
    for key, val in source.items():
        if isinstance(val, dict) and isinstance(target.get(key), dict):
            merge(target[key], val)
        else:
            target[key] = val
