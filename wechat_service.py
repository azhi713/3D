"""微信消息接收层。

支持三种方式：
1. 微信4.x本地监听（推荐）：wechatauto-replica 直接读取本机微信4.x数据库增量消息；
2. 本机HTTP桥接：兼容任意外部采集器；
3. 外部WCF服务：保留旧版兼容。

本模块只接收文本消息，不发送消息；所有原始消息先入 inbox，再由主线程解析计算。
"""
import json
import secrets
import threading
import time
import hashlib
import re
from datetime import datetime
from queue import Empty
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from models import now
from data_store import DataStore


class WeChatService:
    def __init__(self, db_path, config=None, factory=None, db_factory=None, listener_factory=None):
        self.db_path = db_path
        self.config = config or {}
        self.factory = factory                 # 旧WCF测试/注入
        self.db_factory = db_factory           # 微信4.x测试/注入
        self.listener_factory = listener_factory
        self.connected = False
        self.listening = False
        self.status = '未连接'
        self.token = secrets.token_urlsafe(32)
        self.groups = {}
        self.server = None
        self.client = None
        self.db_client = None
        self.listener = None
        self.thread = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.last_received = None
        self.last_error = ''
        self.self_wxid = ''
        self.self_name = ''

    def _persist(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('消息须为JSON对象')
        for key in ('message_id', 'group_id', 'text'):
            if not isinstance(payload.get(key), str) or not payload[key].strip():
                raise ValueError('消息缺少有效的'+key)
        if len(payload['text']) > 10000 or len(payload['message_id']) > 256 or len(payload['group_id']) > 256:
            raise ValueError('消息过长')
        timestamp = payload.get('received_at', now().isoformat())
        try:
            parsed = datetime.fromisoformat(timestamp)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=now().tzinfo)
            parsed = parsed.astimezone(now().tzinfo)
        except (ValueError, TypeError):
            raise ValueError('received_at须为有效的ISO日期时间') from None
        clean = {key: payload[key].strip() for key in ('message_id', 'group_id', 'text')}
        clean.update(
            sender=str(payload.get('sender', ''))[:256],
            group_name=self.friendly_group_name(payload['group_id'], payload.get('group_name', self.groups.get(payload['group_id'], payload['group_id'])))[:256],
            received_at=parsed.isoformat(),
            period=parsed.date().isoformat(),
            history=bool(payload.get('history', False)),
            is_self=bool(payload.get('is_self', False)),
        )
        if clean['is_self'] and self.config.get('filter_self', True):
            return False
        inserted = DataStore.enqueue(self.db_path, clean)
        with self.lock:
            self.groups[clean['group_id']] = clean['group_name']
        self.last_received = now()
        self.status = '微信正常 · 最后消息 '+self.last_received.strftime('%H:%M:%S')
        return inserted

    @staticmethod
    def friendly_group_name(group_id, name=''):
        group_id = str(group_id or '').strip()
        name = str(name or '').strip()
        if name and name != group_id and '@chatroom' not in name:
            return name
        core = group_id.split('@', 1)[0]
        return f'未命名群 {core or "未知"}'

    def group_display(self, group_id):
        group_id = str(group_id or '').strip()
        if not group_id:
            return '未绑定'
        with self.lock:
            name = self.groups.get(group_id, group_id)
        return self.friendly_group_name(group_id, name)

    def _refresh_self_info(self):
        if self.db_client is None or not hasattr(self.db_client, 'get_self_info'):
            return
        try:
            info = self.db_client.get_self_info() or {}
        except Exception:
            return
        if isinstance(info, dict):
            self.self_wxid = str(info.get('username') or info.get('wxid') or info.get('account') or '').strip()
            self.self_name = str(info.get('nick_name') or info.get('nickname') or info.get('remark') or '').strip()

    def _is_self_message(self, msg, sender=''):
        try:
            if int(msg.get('real_sender_id', -1)) == 2:
                return True
        except (TypeError, ValueError):
            pass
        raw = msg.get('is_send')
        if raw is not None:
            try:
                if int(raw) == 1:
                    return True
            except (TypeError, ValueError):
                if bool(raw):
                    return True
        sender = str(sender or '').strip()
        return bool(sender and self.self_wxid and sender == self.self_wxid)

    def start(self):
        if self.listening:
            return True, self.status
        self.stop_event.clear()
        backend = self.config.get('backend', 'bridge')
        if backend == 'wechat4':
            self.listening = True
            self.status = '正在读取微信4.x群列表…'
            self.thread = threading.Thread(target=self._wechat4_loop, daemon=True, name='wechat4-listener')
            self.thread.start()
            return True, self.status
        if backend == 'wcf':
            self.listening = True
            self.status = '正在连接WCF采集服务'
            self.thread = threading.Thread(target=self._wcf_loop, daemon=True, name='wcf-listener')
            self.thread.start()
            return True, self.status
        return self._start_bridge()

    # -------------------- 微信4.x 本地数据库监听 --------------------
    def _wechat4_imports(self):
        if self.db_factory is not None and self.listener_factory is not None:
            return self.db_factory, self.listener_factory
        try:
            from wechatauto import WeChatDB
            from wechatauto.db import Listener
        except ImportError as exc:
            raise RuntimeError('未安装微信4.x监听组件，请运行“安装微信4监听依赖.cmd”') from exc
        return WeChatDB, Listener

    def _build_wechat4_db(self, db_factory):
        # 将解密缓存/监听水位放到程序data目录，重启后可继续增量，不把临时文件散落到源码目录。
        workdir = Path(self.db_path).parent / 'wechat4_cache'
        workdir.mkdir(parents=True, exist_ok=True)
        account = str(self.config.get('wechat4_account', '')).strip()
        kwargs = {'workdir': str(workdir)}
        if account:
            kwargs['account'] = account
        try:
            return db_factory(**kwargs)
        except TypeError:
            # 测试桩或旧兼容实现可能不接收这些关键字。
            return db_factory()

    def _refresh_wechat4_groups(self):
        sessions = self.db_client.get_sessions(limit=int(self.config.get('session_scan_limit', 1000))) or []
        discovered = {}
        for item in sessions:
            group_id = str(item.get('username', '')).strip()
            if not group_id.endswith('@chatroom'):
                continue
            try:
                name = self.db_client.get_nickname(group_id) or item.get('nickname') or item.get('remark') or group_id
            except Exception:
                name = item.get('nickname') or item.get('remark') or group_id
            discovered[group_id] = self.friendly_group_name(group_id, name)
        # 某些账号session表没有完整列出群，补读含消息会话。
        if hasattr(self.db_client, 'list_message_chats'):
            try:
                for item in self.db_client.list_message_chats() or []:
                    group_id = str(item.get('username') or item.get('user') or item.get('wxid') or '').strip()
                    if not group_id.endswith('@chatroom') or group_id in discovered:
                        continue
                    name = item.get('nickname') or item.get('name') or group_id
                    discovered[group_id] = self.friendly_group_name(group_id, name)
            except Exception:
                pass
        with self.lock:
            self.groups.update(discovered)
        return discovered

    @staticmethod
    def _message_time(value):
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, (int, float)):
            stamp = float(value)
            if stamp > 10_000_000_000:  # 毫秒时间戳
                stamp /= 1000
            dt = datetime.fromtimestamp(stamp, now().tzinfo)
        elif isinstance(value, str) and value.strip():
            try:
                dt = datetime.fromisoformat(value)
            except ValueError:
                try:
                    dt = datetime.fromtimestamp(float(value), now().tzinfo)
                except (ValueError, OSError):
                    dt = now()
        else:
            dt = now()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=now().tzinfo)
        return dt.astimezone(now().tzinfo).isoformat()

    @staticmethod
    def _is_text_message(msg):
        local_type = msg.get('local_type')
        if local_type is not None:
            try:
                # 微信4.x local_type可能是复合整数，低32位1为文本。
                return (int(local_type) & 0xffffffff) == 1
            except (ValueError, TypeError):
                pass
        kind = str(msg.get('type', '')).strip().lower()
        if kind in ('text', '文本', '1'):
            return True
        # 无类型字段时，非空正文仍按文本接收；图片/语音等占位符不进入投注解析。
        content = str(msg.get('content', '')).strip()
        return bool(content) and not (content.startswith('[') and content.endswith(']'))

    def _wechat4_callback(self, group_id, msg, history=False):
        if not isinstance(msg, dict) or not self._is_text_message(msg):
            return False
        text = str(msg.get('content', '')).strip()
        if not text:
            return False
        sender = str(msg.get('sender_username') or msg.get('sender_wxid') or msg.get('sender_id') or '')
        is_self = self._is_self_message(msg, sender)
        if is_self and self.config.get('filter_self', True):
            return False
        # 部分微信4.x数据库记录会把群内发送者前缀放进正文。
        text = re.sub(r'^wxid_[^:\r\n]{1,128}:\s*(?:\r?\n)?', '', text, count=1).strip()
        if not text:
            return False
        stable = msg.get('server_id') or msg.get('msg_svr_id') or msg.get('msg_id') or msg.get('local_id') or msg.get('sort_seq')
        if stable is None:
            digest = hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]
            stable = f"{msg.get('create_time','')}:{sender}:{digest}"
        payload = {
            'message_id': f'wx4:{group_id}:{stable}',
            'group_id': group_id,
            'group_name': self.groups.get(group_id, group_id),
            'sender': sender,
            'text': text,
            'received_at': self._message_time(msg.get('create_time') or msg.get('timestamp')),
            'history': bool(history),
            'is_self': is_self,
        }
        return self._persist(payload)

    def _wechat4_loop(self):
        listener_cls = None
        while not self.stop_event.is_set():
            try:
                db_factory, listener_cls = self._wechat4_imports()
                self.db_client = self._build_wechat4_db(db_factory)
                self._refresh_self_info()
                discovered = self._refresh_wechat4_groups()
                bound = []
                for key in ('fc_group', 'tc_group'):
                    group_id = str(self.config.get(key, '')).strip()
                    if group_id and group_id not in bound:
                        bound.append(group_id)

                if not bound:
                    self.connected = True
                    self.status = f'微信4.x已连接 · 已读取{len(discovered)}个群，请选择福彩/体彩群'
                    # 无绑定群时保持数据库连接，周期刷新群列表，方便用户选择后重启监听。
                    while not self.stop_event.wait(3):
                        try:
                            self._refresh_wechat4_groups()
                            self.status = f'微信4.x已连接 · 已读取{len(self.groups)}个群，请选择福彩/体彩群'
                        except Exception:
                            pass
                    break

                interval = float(self.config.get('poll_interval', 0.5))
                interval = min(5.0, max(0.2, interval))
                try:
                    # 实时监听只从“现在”开始：禁用持久水位回放。历史消息由主页面按钮手动读取。
                    self.listener = listener_cls(self.db_client, interval=interval, watermark_file='')
                except TypeError:
                    try:
                        self.listener = listener_cls(self.db_client, interval=interval)
                    except TypeError:
                        self.listener = listener_cls(self.db_client)
                for group_id in bound:
                    self.listener.add_listener(group_id, lambda msg, listener, gid=group_id: self._wechat4_callback(gid, msg))
                self.listener.start()
                self.connected = True
                missing = [gid for gid in bound if gid not in self.groups]
                if missing:
                    self.status = f'微信4.x正在监听{len(bound)}个群 · {len(missing)}个群名暂未读取'
                else:
                    self.status = f'微信4.x正常 · 正在监听{len(bound)}个群'

                # Listener自身负责增量水位/重试；这里仅维持生命周期并周期刷新群名。
                while not self.stop_event.wait(5):
                    self._refresh_wechat4_groups()
            except RuntimeError as exc:
                self.connected = False
                self.last_error = str(exc)
                self.status = self.last_error
                self.listening = False
                return
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc) or exc.__class__.__name__
                self.status = '微信4.x监听中断，5秒后自动重连：'+self.last_error[:120]
            finally:
                if self.listener is not None:
                    try:
                        self.listener.stop()
                    except Exception:
                        pass
                    self.listener = None
                self.db_client = None
            if not self.stop_event.is_set():
                self.stop_event.wait(5)
        self.listening = False
        self.connected = False

    # -------------------- 本机HTTP桥接 --------------------
    def _start_bridge(self):
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.connection.settimeout(5)
                if self.path != '/message':
                    self.reply(404, {'error': '接口不存在'})
                    return
                if not secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer '+service.token):
                    self.reply(401, {'error': '认证失败'})
                    return
                try:
                    length = int(self.headers.get('Content-Length', 0))
                    if not 0 < length <= 65536:
                        self.reply(413, {'error': '消息长度无效'})
                        return
                    obj = json.loads(self.rfile.read(length))
                    inserted = service._persist(obj)
                    self.reply(200, {'saved': True, 'duplicate': not inserted})
                except (ValueError, TypeError):
                    self.reply(400, {'error': '消息格式无效，请检查message_id、group_id、text、received_at'})
                except Exception:
                    service.last_error = '消息保存失败，采集端须重试'
                    self.reply(503, {'error': service.last_error})

            def reply(self, code, obj):
                data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
                self.send_response(code)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        try:
            self.server = ThreadingHTTPServer(('127.0.0.1', int(self.config.get('bridge_port', 18765))), Handler)
        except OSError:
            return False, '接收端口被占用，或无法启动本机接收服务'
        self.server.daemon_threads = True
        self.listening = True
        self.status = '本机接收端已启动，等待采集端群消息'
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.2}, daemon=True, name='wechat-bridge')
        self.thread.start()
        return True, self.status

    # -------------------- 旧WCF兼容 --------------------
    def _wcf_loop(self):
        while not self.stop_event.is_set():
            try:
                if self.factory is None:
                    try:
                        from wcferry import Wcf
                    except ImportError:
                        self.status = '未安装WCF客户端；可改用微信4.x本地监听'
                        self.listening = False
                        return
                    factory = Wcf
                else:
                    factory = self.factory
                self.client = factory(host=self.config.get('wcf_host', '127.0.0.1'), port=int(self.config.get('wcf_port', 10086)))
                if not self.client.is_login():
                    raise ConnectionError('采集端微信未登录')
                for item in self.client.get_contacts():
                    if str(item.get('wxid', '')).endswith('@chatroom'):
                        with self.lock:
                            self.groups[item['wxid']] = item.get('name') or item['wxid']
                if not self.client.enable_receiving_msg():
                    raise ConnectionError('采集服务未能启动消息接收')
                self.connected, self.status = True, 'WCF已连接，正在接收'
                checked = time.monotonic()
                while not self.stop_event.is_set():
                    if time.monotonic()-checked > 5:
                        checked = time.monotonic()
                        if not self.client.is_login():
                            raise ConnectionError('微信已掉线')
                    try:
                        msg = self.client.get_msg()
                    except Empty:
                        continue
                    if not msg.from_group() or not msg.is_text():
                        continue
                    is_self = False
                    try:
                        is_self = bool(msg.from_self())
                    except Exception:
                        is_self = bool(getattr(msg, 'is_self', False))
                    if is_self and self.config.get('filter_self', True):
                        continue
                    self._persist({'message_id': str(msg.id), 'group_id': msg.roomid, 'sender': msg.sender,
                                   'text': msg.content, 'received_at': datetime.fromtimestamp(msg.ts, now().tzinfo).isoformat(),
                                   'is_self': is_self})
            except Exception:
                self.connected = False
                self.status = '采集服务连接中断，正在重连；原始消息队列已保留'
            finally:
                if self.client is not None:
                    try:
                        self.client.disable_recv_msg()
                    except Exception:
                        pass
                    for attr in ('cmd_socket', 'msg_socket'):
                        socket = getattr(self.client, attr, None)
                        if socket:
                            try:
                                socket.close()
                            except Exception:
                                pass
                    if hasattr(self.client, '_is_running'):
                        self.client._is_running = False
                    self.client = None
            self.stop_event.wait(5)
        self.listening = False

    def read_history(self, group_ids, scope='today', limit=100):
        """手动读取绑定群历史消息。实时监听永远不会隐式调用本方法。"""
        if self.config.get('backend', 'wechat4') != 'wechat4':
            raise RuntimeError('读取历史消息目前仅支持微信4.x本地监听')
        group_ids = [str(g).strip() for g in group_ids if str(g).strip()]
        if not group_ids:
            raise ValueError('请先绑定要读取的微信群')
        db = self.db_client
        temporary = db is None
        if temporary:
            db_factory, _ = self._wechat4_imports()
            db = self._build_wechat4_db(db_factory)
        old = self.db_client
        self.db_client = db
        self._refresh_self_info()
        try:
            try:
                self._refresh_wechat4_groups()
            except Exception:
                pass
            today = now().date()
            inserted = duplicate = ignored = 0
            messages = []
            for gid in group_ids:
                if scope == 'today':
                    offset = 0
                    while offset < 20000:
                        rows = db.get_messages(gid, limit=500, offset=offset) or []
                        if not rows:
                            break
                        saw_older = False
                        for msg in rows:
                            if not isinstance(msg, dict):
                                continue
                            received = datetime.fromisoformat(self._message_time(msg.get('create_time') or msg.get('timestamp')))
                            if received.date() == today:
                                messages.append((received, gid, msg))
                            elif received.date() < today:
                                saw_older = True
                        if len(rows) < 500 or saw_older:
                            break
                        offset += len(rows)
                else:
                    rows = db.get_messages(gid, limit=max(1, int(limit)), offset=0) or []
                    for msg in rows:
                        if not isinstance(msg, dict):
                            continue
                        received = datetime.fromisoformat(self._message_time(msg.get('create_time') or msg.get('timestamp')))
                        messages.append((received, gid, msg))
            messages.sort(key=lambda row: (row[0], row[2].get('sort_seq', 0)))
            for _, gid, msg in messages:
                if not self._is_text_message(msg):
                    ignored += 1
                    continue
                before_self = self._is_self_message(msg, str(msg.get('sender_username') or msg.get('sender_wxid') or msg.get('sender_id') or ''))
                if before_self and self.config.get('filter_self', True):
                    ignored += 1
                    continue
                if self._wechat4_callback(gid, msg, history=True):
                    inserted += 1
                else:
                    duplicate += 1
            return {'inserted': inserted, 'duplicate': duplicate, 'ignored': ignored}
        finally:
            self.db_client = old

    def group_list(self):
        with self.lock:
            rows = [(gid, self.friendly_group_name(gid, name)) for gid, name in self.groups.items()]
            return sorted(rows, key=lambda item: (item[1], item[0]))

    def stop(self):
        self.stop_event.set()
        if self.listener is not None:
            try:
                self.listener.stop()
            except Exception:
                pass
            self.listener = None
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)
        self.connected = self.listening = False
        self.status = '未连接'
