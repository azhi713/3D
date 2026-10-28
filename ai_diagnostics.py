"""内置AI接口诊断：使用与正式识别相同的请求结构，诊断信息不含密钥/原文。"""
import json
import os
import platform
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from ai_service import AIService
from config import DEFAULT_AI_PROMPT

SAMPLE = '福293一单一组5倍，095组505组三一倍'
MAX_REPLY = 1024 * 1024


def _safe(value, key=''):
    value = str(value or '').replace(str(key or '') if key else '\0UNLIKELY\0', '[已隐藏]')
    value = re.sub(r'(?i)(bearer\s+)[^\s"\',;]+', r'\1[已隐藏]', value)
    value = re.sub(r'(?i)((?:api[_ -]?key|authorization|password|token)\s*[=:]\s*)[^\s,;]+', r'\1[已隐藏]', value)
    return value[:350]


def _extract_error(raw, key):
    try:
        obj = json.loads(raw.decode('utf-8', 'replace'))
        err = obj.get('error') if isinstance(obj, dict) else None
        if isinstance(err, dict):
            return ' | '.join(f'{k}={_safe(err.get(k),key)}' for k in ('type','code','message') if err.get(k) is not None)
        if isinstance(err, str):
            return _safe(err,key)
    except (TypeError, ValueError, UnicodeError):
        pass
    return _safe(raw[:280].decode('utf-8', 'replace'), key)


class AIDiagnostics:
    def __init__(self, config, key, output_dir, callback=None, validator=None):
        self.config = dict(config)
        self.key = str(key or '')
        self.output_dir = Path(output_dir)
        self.lines = []
        self.callback = callback
        self.validator = validator
        self.service = AIService(self.config, self.key)

    def note(self, text):
        value = _safe(text, self.key) if text.startswith(('错误明细:', '请求异常:', '服务器说明:')) else str(text)
        self.lines.append(value)
        if self.callback:
            self.callback(value)

    def request(self, name, messages, timeout, expect_standard=False):
        self.note(f'--- {name}（超时设置 {timeout:g} 秒）---')
        started = time.perf_counter()
        try:
            # _build_request 与正常AI转换共用，保证路径、字段、Key、JSON开关一致。
            req = self.service._build_request(messages)
            endpoint = urlsplit(req.full_url)
            self.note(f'请求端点: {endpoint.scheme}://{endpoint.netloc}{_safe(endpoint.path, self.key)}')
            self.note(f'请求体大小: {len(req.data or b"")} 字节')
            self.note('请求方式: POST | 非流式 | 与程序AI服务共用请求结构')
            with urllib.request.urlopen(req, timeout=timeout) as response:
                t_headers = time.perf_counter() - started
                self.note(f'HTTP状态: {response.status} | 收到响应头: {t_headers:.3f}秒')
                first = response.read(1)
                t_first = time.perf_counter() - started
                self.note(f'收到首字节: {t_first:.3f}秒')
                raw = first + response.read(MAX_REPLY + 1)
            elapsed = time.perf_counter() - started
            self.note(f'接收完成: {elapsed:.3f}秒 | 响应体: {len(raw)}字节')
            if len(raw) > MAX_REPLY:
                self.note('结果: 失败 | 返回内容超过1MB限制')
                return False
            obj = json.loads(raw)
            legacy = req.full_url.endswith('/completions') and not req.full_url.endswith('/chat/completions')
            content = obj['choices'][0]['text'] if legacy else obj['choices'][0]['message']['content']
            if not isinstance(content, str):
                raise ValueError('choices内容不是文本')
            m = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', content.strip(), flags=re.S | re.I)
            answer = json.loads(m.group(1) if m else content.strip())
            if not isinstance(answer, dict):
                raise ValueError('内容不是JSON对象')
            if expect_standard:
                if answer.get('uncertain') is not False or not isinstance(answer.get('standard_text'), str) or not answer['standard_text'].strip():
                    self.note('结果: 请求成功，但AI转换结构或确定性不满足程序要求')
                    self.note('返回字段: '+', '.join(sorted(str(k) for k in answer.keys())))
                    self.note('uncertain: '+repr(answer.get('uncertain')))
                    return False
                self.note('结果: 请求成功 | 模型返回可用standard_text，长度='+str(len(answer['standard_text'])))
                if self.validator:
                    try:
                        valid, summary = self.validator(answer['standard_text'])
                        self.note('程序本地复核: '+ ('通过 | ' if valid else '未通过 | ') + _safe(summary, self.key))
                        if not valid:
                            return False
                    except Exception as exc:
                        self.note('程序本地复核: 异常 | '+type(exc).__name__+' '+_safe(exc, self.key))
                        return False
            else:
                self.note('结果: 成功 | 收到有效JSON对象')
            self.note('返回字段: '+', '.join(sorted(str(k) for k in answer.keys())))
            self.note(f'本地JSON解析完成: {time.perf_counter()-started:.3f}秒')
            return True
        except urllib.error.HTTPError as err:
            elapsed = time.perf_counter() - started
            self.note(f'结果: HTTP {err.code} | 用时: {elapsed:.3f}秒')
            body = err.read(4096)
            self.note('服务器说明: '+_extract_error(body, self.key))
            return False
        except urllib.error.URLError as err:
            self.note(f'结果: 网络异常 | 用时: {time.perf_counter()-started:.3f}秒')
            self.note('请求异常: '+type(err.reason).__name__+' '+_safe(err.reason, self.key))
            return False
        except (TimeoutError, socket.timeout) as err:
            self.note(f'结果: 超时 | 用时: {time.perf_counter()-started:.3f}秒')
            self.note('请求异常: '+type(err).__name__+' '+_safe(err, self.key))
            return False
        except (OSError, ValueError, KeyError, TypeError, UnicodeError, ssl.SSLError) as err:
            self.note(f'结果: {type(err).__name__} | 用时: {time.perf_counter()-started:.3f}秒')
            self.note('错误明细: '+_safe(err, self.key))
            return False

    def run(self, sample=SAMPLE, prompt=None):
        self.note('=== 彩票风险分析 AI内置诊断 ===')
        self.note('开始时间: '+datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.note('系统: '+platform.platform())
        self.note('打包状态: '+('EXE' if getattr(sys,'frozen',False) else '源码运行'))
        self.note('Python: '+platform.python_version())
        self.note('AI模型: '+_safe(self.config.get('model',''), self.key))
        self.note('接口模式: '+str(self.config.get('api_format', 'chat')))
        self.note('强制JSON: '+str(bool(self.config.get('json_mode', True))))
        self.note('已填写Key: '+str(bool(self.key)))
        for name in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY'):
            self.note(name+'存在: '+str(bool(os.environ.get(name) or os.environ.get(name.lower()))))
        try:
            proxy = urllib.request.getproxies()
            self.note('Python检测到代理类别: '+(','.join(k for k in ('http','https','all') if k in proxy) or '无'))
            url = self.service._endpoint()
            host = urlsplit(url).hostname
            tick = time.perf_counter()
            addr = socket.getaddrinfo(host, 443 if url.startswith('https://') else 80, type=socket.SOCK_STREAM)
            self.note(f'DNS: 成功，{len(addr)}条地址，耗时{time.perf_counter()-tick:.3f}秒')
        except Exception as err:
            self.note('DNS/配置检查: '+type(err).__name__+' '+_safe(err,self.key))
        self.note('注意：API Key、完整提示词、原始客户消息及完整模型回答均不会写入日志')
        brief = [{'role':'system','content':'只返回JSON对象 {"ok":true}'}, {'role':'user','content':'测试连接'}]
        first = self.request('1. 简短请求（用于复现5秒故障）', brief, 5)
        second = self.request('2. 简短请求（30秒对照）', brief, 30) if not first else None
        active_prompt = (prompt or self.config.get('prompt') or DEFAULT_AI_PROMPT).strip()
        actual = [{'role':'system','content':active_prompt}, {'role':'user','content':json.dumps({'lottery':'福彩','raw_text':sample},ensure_ascii=False)}]
        self.note(f'真实识别模拟：提示词{len(active_prompt)}字、模拟投注{len(sample)}字；使用程序当前设置')
        third = self.request('3. 实际发送识别请求（30秒）', actual, 30, expect_standard=True)
        self.note(f'结论：5秒短请求={"成功" if first else "失败"}，30秒短请求={"未运行" if second is None else ("成功" if second else "失败")}，实际识别={"成功" if third else "失败"}')
        self.note('结束时间: '+datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.note('=== 诊断结束 ===')
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / ('ai_diag_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.txt')
        path.write_text('\n'.join(self.lines)+'\n', encoding='utf-8-sig')
        return str(path), '\n'.join(self.lines)
