# -*- coding: utf-8 -*-
"""V2.6.2 unified AI request transports.

Safe automatic fallback: retries occur only when connection establishment failed
before any HTTP request bytes could have reached the endpoint.  A response,
read timeout, broken pipe, remote reset or HTTP status never triggers retry.
No API key or prompt is placed on a subprocess command line or written to disk.
"""
from __future__ import annotations

import errno
import io
import os
import shutil
import socket
import ssl
import subprocess
import uuid
import urllib.error
import urllib.request
from pathlib import Path

MAX_REPLY = 1024 * 1024
MAX_ERROR = 4096
MODES = ('auto', 'urllib', 'httpx', 'curl')


class TransportSetupError(Exception):
    """A requested backend is missing or invalid (no HTTP call occurred)."""


class _HttpxStream:
    """File-like, bounded streaming HTTPX response compatible with urllib."""

    def __init__(self, client, response):
        self.client = client
        self.response = response
        self.status = response.status_code
        self._iter = response.iter_bytes(chunk_size=8192)
        self._buffer = bytearray()
        self._ended = False

    def read(self, count=-1):
        if count is None or count < 0:
            count = MAX_REPLY + 1
        while len(self._buffer) < count and not self._ended:
            try:
                part = next(self._iter)
            except StopIteration:
                self._ended = True
                break
            except Exception as exc:
                # A response has already started; delivery is ambiguous.
                raise urllib.error.URLError('HTTPX读取响应失败（'+type(exc).__name__+'），请求状态未知，可能已发送') from None
            self._buffer.extend(part)
        result = bytes(self._buffer[:count])
        del self._buffer[:count]
        return result

    def close(self):
        try:
            self.response.close()
        finally:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class _BufferedResponse:
    def __init__(self, payload, code):
        self._body = io.BytesIO(payload)
        self.status = code

    def read(self, size=-1):
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self._body.close()


def _code_error(request, status, body):
    return urllib.error.HTTPError(request.full_url, status, 'HTTP response', {}, io.BytesIO(body[:MAX_ERROR]))


def _httpx_open(request, timeout):
    try:
        import httpx
    except ImportError as exc:
        raise TransportSetupError('HTTPX未安装，打包时需包含httpx依赖') from exc
    client = httpx.Client(timeout=httpx.Timeout(timeout, connect=min(10.0, float(timeout))),
                          trust_env=True, follow_redirects=False, verify=True)
    try:
        prepared = client.build_request('POST', request.full_url,
                                        headers=dict(request.header_items()),
                                        content=request.data or b'')
        response = client.send(prepared, stream=True)
    except BaseException:
        client.close()
        raise
    if not 200 <= response.status_code < 300:
        try:
            raw = response.read()[:MAX_ERROR]
        finally:
            response.close()
            client.close()
        raise _code_error(request, response.status_code, raw)
    return _HttpxStream(client, response)


def _curl_exe():
    if os.name == 'nt':
        system = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'curl.exe'
        if system.is_file():
            return str(system)
        return shutil.which('curl.exe')
    return shutil.which('curl')


def _cfg_quote(value):
    """Escape curl --config double-quoted values (UTF-8, no shell involved)."""
    value = str(value)
    if '\x00' in value or '\r' in value or '\n' in value:
        raise TransportSetupError('cURL配置包含禁止的控制字符')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _curl_open(request, timeout):
    exe = _curl_exe()
    if not exe:
        raise TransportSetupError('找不到系统cURL，请安装Windows自带curl.exe或使用其它方式')
    marker = '__V262_STATUS_' + uuid.uuid4().hex + '__:'
    headers = dict(request.header_items())
    cfg = [
        'url = ' + _cfg_quote(request.full_url),
        'request = "POST"',
        'header = "Expect:"',
        'header = ' + _cfg_quote('Content-Type: application/json'),
    ]
    for key, value in headers.items():
        if key.casefold() == 'content-type':
            continue
        cfg.append('header = ' + _cfg_quote(key + ': ' + value))
    body = (request.data or b'').decode('utf-8')
    cfg += [
        'data-binary = ' + _cfg_quote(body),
        # Curl config parses \\n as newline in --write-out; unique marker avoids
        # accidental collision with an AI JSON response body.
        'write-out = ' + _cfg_quote('\\n' + marker + '%{http_code}'),
    ]
    argv = [exe, '-q', '--silent', '--show-error', '--max-time', str(float(timeout)),
            '--connect-timeout', str(min(10.0, float(timeout))),
            '--max-filesize', str(MAX_REPLY + 1), '--proto', '=http,https',
            '--config', '-']
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        completed = subprocess.run(argv, input=('\n'.join(cfg) + '\n').encode('utf-8'),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=float(timeout) + 3, creationflags=flags)
    except subprocess.TimeoutExpired as exc:
        # Even if curl reports no status, a timeout may have occurred after POST.
        raise urllib.error.URLError('cURL执行超时，请求状态未知，可能已发送，请勿立即重试') from exc
    except OSError as exc:
        raise TransportSetupError('无法启动cURL：' + type(exc).__name__) from exc
    output = completed.stdout
    prefix, separator, status = output.rpartition(marker.encode('ascii'))
    try:
        code = int(status.strip()) if separator else 0
    except ValueError:
        code = 0
    if completed.returncode:
        err_code = completed.returncode
        # cURL exit 5/6/7 = proxy DNS/DNS/failed TCP connect, no HTTP body.
        if err_code in (5, 6, 7):
            raise _SafeNotSent('cURL建立连接失败，错误码' + str(err_code))
        # Certificate/TLS handshake failure occurs before sending HTTP body.
        if err_code in (35, 60):
            raise _SafeNotSent('cURL TLS校验/握手失败，错误码' + str(err_code))
        raise urllib.error.URLError('cURL网络错误' + str(err_code) + '，请求状态未知，可能已发送，请勿立即重试')
    if not separator or not 100 <= code <= 599:
        raise urllib.error.URLError('cURL未返回有效HTTP状态，请求状态未知，禁止自动重试')
    if not 200 <= code < 300:
        raise _code_error(request, code, prefix)
    return _BufferedResponse(prefix, code)


class _SafeNotSent(Exception):
    pass


def _safe_connection_exception(exc, backend):
    """Conservative classification: only definitely pre-HTTP connection failures."""
    if isinstance(exc, _SafeNotSent):
        return True
    if backend == 'httpx':
        try:
            import httpx
        except ImportError:
            return False
        # Never treat HTTPX ReadTimeout/ReadError/WriteTimeout/WriteError as safe.
        return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout,
                                httpx.ProxyError, httpx.UnsupportedProtocol))
    if backend == 'urllib':
        err = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(err, (socket.gaierror, ConnectionRefusedError, ssl.SSLCertVerificationError)):
            return True
        return isinstance(err, OSError) and getattr(err, 'errno', None) in (
            errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ECONNREFUSED)
    return False


def _reason(exc, backend):
    if isinstance(exc, _SafeNotSent):
        return str(exc)
    if isinstance(exc, TransportSetupError):
        return str(exc)
    # No raw exception text: proxy addresses and credentials may be embedded.
    return backend + '：' + type(exc).__name__


def available_backends():
    result = {'urllib': True, 'httpx': False, 'curl': bool(_curl_exe())}
    try:
        import httpx  # noqa: F401
        result['httpx'] = True
    except ImportError:
        pass
    return result


def open_transport_request(request, *, timeout, config=None, metrics=None):
    """Open a POST using configured backend; retry ONLY known pre-send failures.

    metrics is the AIService thread-local dictionary, not shared state.
    Legacy urllib HTTPError/URLError contracts are preserved.
    """
    config = config or {}
    metrics = metrics if metrics is not None else {}
    mode = str(config.get('transport', 'auto') or 'auto').strip().lower()
    if mode not in MODES:
        raise TransportSetupError('未知AI发送方式：' + mode)
    backends = ('urllib', 'httpx', 'curl') if mode == 'auto' else (mode,)
    available = available_backends()
    attempts = []
    metrics['transport_mode'] = mode
    for backend in backends:
        if not available.get(backend):
            attempts.append(backend + ':unavailable')
            if mode != 'auto':
                raise TransportSetupError(backend + '不可用，需安装依赖或选择自动模式')
            continue
        try:
            if backend == 'urllib':
                response = urllib.request.urlopen(request, timeout=timeout)
            elif backend == 'httpx':
                response = _httpx_open(request, timeout)
            else:
                response = _curl_open(request, timeout)
        except urllib.error.HTTPError:
            metrics['transport_used'] = backend
            metrics['transport_attempts'] = attempts + [backend + ':http_error']
            raise
        except Exception as exc:
            safe = _safe_connection_exception(exc, backend) or isinstance(exc, TransportSetupError)
            attempts.append(backend + (':pre_send_failed' if safe else ':delivery_unknown'))
            metrics['transport_attempts'] = attempts[:]
            if not safe:
                metrics['billing_state'] = 'unknown_after_send'
                raise urllib.error.URLError(_reason(exc, backend) + '；请求状态未知，可能已发送，请勿立即重试') from None
            if mode != 'auto':
                metrics['billing_state'] = 'not_sent'
                raise urllib.error.URLError(_reason(exc, backend) + '；确认未发送请求') from None
            continue
        metrics['transport_used'] = backend
        metrics['transport_attempts'] = attempts + [backend + ':success']
        return response
    metrics['billing_state'] = 'not_sent'
    metrics['transport_attempts'] = attempts
    raise urllib.error.URLError('所有可用发送方式均未建立连接，确认未发送请求；尝试记录：' + ', '.join(attempts))
