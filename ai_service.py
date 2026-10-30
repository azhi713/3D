"""AI识别接口：统一请求结构、可配置超时、脱敏日志及清晰的失败说明。"""
import json
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from config import DEFAULT_AI_PROMPT

SYSTEM = DEFAULT_AI_PROMPT
_TIMEOUT_OPTIONS = (15, 30, 60, 90, 120)
_MAX_REPLY = 1024 * 1024
_MAX_ERROR = 4096
_LOG_LOCK = threading.Lock()


class AIError(Exception):
    pass


def effective_timeout(config):
    """旧版默认15秒没有用户主动设置标记，升级后迁移成60秒。"""
    config = config or {}
    value = config.get('timeout', 60)
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 60
    if number == 15 and not config.get('timeout_configured', False):
        return 60
    return number if number in _TIMEOUT_OPTIONS else 60


def _redact(value, api_key='', messages=()):
    """不记录密钥及完整用户请求；平台错误若回显原文，也会尝试遮盖。"""
    value = str(value or '')
    secrets = [api_key]
    for msg in messages:
        original = str(msg.get('content', ''))
        if not original:
            continue
        secrets.append(original)
        # 原始投注可能出现在服务端错误描述中，尽量遮盖JSON里的raw_text。
        if msg.get('role') == 'user':
            try:
                parsed = json.loads(original)
                if isinstance(parsed, dict):
                    secrets.append(str(parsed.get('raw_text', '')))
            except (TypeError, ValueError):
                pass
    for secret in secrets:
        if secret and len(secret) >= 3:
            value = value.replace(secret, '[敏感内容已隐藏]')
            value = value.replace(json.dumps(secret, ensure_ascii=False)[1:-1], '[敏感内容已隐藏]')
    value = re.sub(r'(?i)(bearer\s+)[^\s\"\',;]+', r'\1[已隐藏]', value)
    value = re.sub(r'(?i)((?:api[_ -]?key|authorization|password|token)\s*[=:]\s*)[^\s,;]+', r'\1[已隐藏]', value)
    value = value.replace('\r', ' ').replace('\n', ' ')
    return value[:480]


def _server_error(raw, api_key, messages):
    if not raw:
        return '服务器未提供错误说明'
    try:
        obj = json.loads(raw.decode('utf-8', 'replace'))
        if isinstance(obj, dict):
            err = obj.get('error', obj)
            if isinstance(err, dict):
                summary = '；'.join('%s=%s' % (key, err[key]) for key in ('code', 'type', 'message') if err.get(key) is not None)
                if summary:
                    return _redact(summary, api_key, messages)
            elif isinstance(err, str):
                return _redact(err, api_key, messages)
    except (TypeError, ValueError, UnicodeError):
        pass
    # 非JSON响应通常是代理/网关的HTML，避免将整页写入日志。
    return '服务器返回非JSON错误信息（开头：%s）' % _redact(raw[:160].decode('utf-8', 'replace'), api_key, messages)


class AIService:
    def __init__(self, config=None, api_key='', log_dir=None):
        self.config = config or {}
        self.api_key = api_key
        self.connected = False
        self.log_dir = Path(log_dir) if log_dir else None
        self._timing_local=threading.local()

    @property
    def last_metrics(self):
        return dict(getattr(self._timing_local,'metrics',{}))

    def _log(self, stage, **details):
        if self.log_dir is None:
            return
        # 这里仅允许传入已经脱敏的固定字段，禁止请求体、完整提示词及模型原文。
        payload = {'time': datetime.now().isoformat(timespec='milliseconds'), 'stage': stage, **details}
        try:
            with _LOG_LOCK:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                target = self.log_dir / ('ai_requests_' + datetime.now().strftime('%Y%m%d') + '.jsonl')
                with target.open('a', encoding='utf-8') as file:
                    file.write(json.dumps(payload, ensure_ascii=False) + '\n')
        except OSError:
            pass  # 日志写入失败不能影响投注或AI请求

    def _endpoint(self):
        base = self.config.get('base_url', '').strip().rstrip('/')
        parts = urlsplit(base)
        if parts.username or parts.password or parts.query or parts.fragment:
            raise AIError('API地址不能带账号、密码或查询参数')
        if parts.scheme != 'https' and not (parts.scheme == 'http' and parts.hostname in ('127.0.0.1', 'localhost', '::1')):
            raise AIError('API地址须使用HTTPS；本机服务可以使用HTTP')
        if not parts.hostname or not self.config.get('model', '').strip():
            raise AIError('请填写API地址和模型名称')
        if not self.api_key.strip():
            raise AIError('请填写API Key')
        if base.endswith('/completions'):
            return base
        if not parts.path:
            base += '/v1'
        return base+('/completions' if self.config.get('api_format') == 'completions' else '/chat/completions')

    def _build_request(self, messages):
        url = self._endpoint()
        legacy = url.endswith('/completions') and not url.endswith('/chat/completions')
        if legacy:
            prompt = '\n\n'.join(('规则说明：' if item['role']=='system' else '待转换数据：')+item['content'] for item in messages)
            payload = {'model': self.config['model'].strip(), 'prompt': prompt}
        else:
            payload = {'model': self.config['model'].strip(), 'messages': messages, 'stream': False}
            if self.config.get('json_mode', True):
                payload['response_format'] = {'type':'json_object'}
        request = urllib.request.Request(url, json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                                         {'Authorization': 'Bearer '+self.api_key.strip(), 'Content-Type': 'application/json'})
        return request

    def _request(self, messages):
        started = time.perf_counter()
        timeout = effective_timeout(self.config)
        rid = datetime.now().strftime('%H%M%S%f') + '-' + str(threading.get_ident())
        self.connected = False
        metrics={'response_s':0.0,'json_s':0.0,'request_build_s':0.0,'headers_s':None,'first_byte_s':None,'body_read_s':None,
                 'transport_stage':'build','billing_state':'not_sent'}
        self._timing_local.metrics=metrics
        try:
            request = self._build_request(messages)
            metrics['request_build_s']=time.perf_counter()-started
            legacy = request.full_url.endswith('/completions') and not request.full_url.endswith('/chat/completions')
            parsed_url = urlsplit(request.full_url)
            # URL仅保留安全的scheme/host/path；不含凭据及查询字符串。
            endpoint = f'{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}'
            self._log('开始请求', request_id=rid, model=self.config.get('model', ''),
                      endpoint=_redact(endpoint, self.api_key, messages), mode='completions' if legacy else 'chat',
                      timeout_s=timeout, request_bytes=len(request.data or b''))
            response_started=time.perf_counter()
            metrics['transport_stage']='connect_or_wait_headers'
            metrics['billing_state']='unknown_after_send'
            with urllib.request.urlopen(request, timeout=timeout) as response:
                metrics['headers_s']=time.perf_counter()-response_started
                metrics['transport_stage']='read_body'
                self._log('收到响应头', request_id=rid, http_status=response.status,
                          elapsed_s=round(time.perf_counter()-started,3))
                body_started=time.perf_counter()
                first=response.read(1)
                metrics['first_byte_s']=time.perf_counter()-response_started
                raw=first+response.read(_MAX_REPLY+1-len(first))
                metrics['body_read_s']=time.perf_counter()-body_started
            metrics['response_s']=time.perf_counter()-response_started
            self._log('收到完整响应', request_id=rid, elapsed_s=round(time.perf_counter()-started,3), response_bytes=len(raw))
            if len(raw)>_MAX_REPLY:
                raise AIError('AI返回内容超过1MB，原始数据已保存')
            json_started=time.perf_counter()
            metrics['transport_stage']='json'
            try:
                obj = json.loads(raw)
                content = obj['choices'][0]['text'] if legacy else obj['choices'][0]['message']['content']
                if not isinstance(content,str):
                    raise TypeError('choices不是文本')
                content = content.strip()
                fenced = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', content, flags=re.S|re.I)
                answer = json.loads(fenced[1] if fenced else content)
                if not isinstance(answer,dict):
                    raise TypeError('JSON不是对象')
            except (ValueError, KeyError, TypeError, IndexError) as err:
                raise AIError('AI返回格式错误：'+type(err).__name__+'；未能解析为程序需要的JSON对象，原始数据已保存') from None
            self.connected = True
            metrics['transport_stage']='complete'
            metrics['billing_state']='response_received'
            metrics['json_s']=time.perf_counter()-json_started
            self._log('JSON解析成功', request_id=rid, elapsed_s=round(time.perf_counter()-started,3), result_field_count=len(answer))
            return answer
        except urllib.error.HTTPError as exc:
            detail = _server_error(exc.read(_MAX_ERROR), self.api_key, messages)
            tag = ('认证失败，请检查API Key' if exc.code == 401 else '权限不足' if exc.code == 403 else
                   '请求过于频繁或额度不足' if exc.code == 429 else '接口路径可能不支持' if exc.code == 404 else
                   '服务端异常' if exc.code >= 500 else '请求参数或模型可能不兼容' if exc.code == 400 else '接口请求失败')
            summary = f'AI请求失败：HTTP {exc.code}（{tag}）；平台说明：{detail}；原始数据已保存'
            self._log('HTTP错误', request_id=rid, http_status=exc.code, elapsed_s=round(time.perf_counter()-started,3), error=detail)
            raise AIError(summary) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError, OSError) as exc:
            cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            is_timeout = isinstance(cause, (TimeoutError, socket.timeout)) or ('timed out' in str(cause).lower())
            kind = '响应超时' if is_timeout else '网络连接失败'
            metrics['timeout_stage']=metrics['transport_stage'] if is_timeout else None
            detail = _redact(str(cause), self.api_key, messages)[:200]
            self._log('网络失败', request_id=rid, error_type=type(cause).__name__,
                      elapsed_s=round(time.perf_counter()-started,3), configured_timeout_s=timeout,
                      transport_stage=metrics['transport_stage'],billing_state=metrics['billing_state'],error=detail)
            raise AIError(f'AI{kind}（请求超时设置{timeout}秒）；{type(cause).__name__}: {detail}；原始数据已保存' +
                          ('；可能已经产生模型费用，请勿立即重复提交' if is_timeout else '')) from None
        except AIError as exc:
            self._log('识别失败', request_id=rid, elapsed_s=round(time.perf_counter()-started,3), error=_redact(str(exc),self.api_key,messages))
            raise
        finally:
            metrics['api_total_s']=time.perf_counter()-started
            self._timing_local.metrics=dict(metrics)

    def test_connection(self):
        self._request([{'role': 'system', 'content': '只返回JSON对象 {"ok":true}'},
                       {'role': 'user', 'content': '测试连接'}])
        return True, '连接正常'

    def parse_unknown(self, lottery, text):
        if len(text) > 10000:
            raise AIError('文字过长，请分批添加')
        request_data={'lottery':lottery,'raw_text':text}
        if text.lstrip().startswith('{'):
            try:
                context=json.loads(text)
                if isinstance(context,dict) and isinstance(context.get('raw_text'),str):
                    request_data=context
            except ValueError:
                pass
        obj = self._request([{'role': 'system', 'content': (self.config.get('prompt') or DEFAULT_AI_PROMPT).strip()},
                             {'role': 'user', 'content': json.dumps(request_data, ensure_ascii=False)}])
        if isinstance(obj.get('candidate_bets'),list) and obj['candidate_bets']:
            return json.dumps(obj,ensure_ascii=False),str(obj.get('brief_reason',''))[:200]
        if obj.get('uncertain') is not False or not isinstance(obj.get('standard_text'), str) or not obj['standard_text'].strip():
            reason = _redact(str(obj.get('reason', '需要人工确认')), self.api_key,
                             [{'role':'user','content':json.dumps({'raw_text':text},ensure_ascii=False)}])[:200]
            self._log('AI字段校验未通过', reason=reason)
            raise AIError('AI已返回但无法确认投注：'+reason+'；原始数据已保存')
        self._log('AI转换文本已生成', output_char_count=len(obj['standard_text'].strip()))
        return obj['standard_text'].strip(), str(obj.get('reason', ''))[:200]
