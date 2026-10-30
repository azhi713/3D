# -*- coding: utf-8 -*-
"""官网开奖读取。先中国福彩网官方查询接口，备用省级发行机构。

只返回日期和期号都合法、号码严格是三位的记录；不从历史最近一期猜今天号码。
无需第三方 Python 依赖。中国福彩网出现反爬限制时，可选用系统 curl.exe 重试。
"""
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from urllib.parse import urljoin, urlsplit, urlencode
from urllib.error import HTTPError, URLError
import json
import re
import shutil
import subprocess
import tempfile
import urllib.request


class DrawLookupError(ValueError):
    """kind: network / unavailable / missing / invalid / conflict."""
    def __init__(self, message, kind='invalid'):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class DrawResult:
    lottery: str
    draw_date: str
    issue: str
    number: str
    source_name: str
    source_url: str


CWL_PAGE = 'https://www.cwl.gov.cn/ygkj/wqkjgg/fc3d/'
CWL_API = 'https://www.cwl.gov.cn/cwl_admin/front/cwlkj/search/kjxx/findDrawNotice'
SOURCE_URLS = {
    '福彩': ('四川省福利彩票发行中心', 'https://www.scflcp.com.cn/3dinfo.jhtml'),
    '体彩': ('江苏省体育彩票管理中心', 'https://api.js-lottery.com/wfzq/p3p5/p3data'),
}
_ALLOWED_HOSTS = ('www.cwl.gov.cn', 'www.scflcp.com.cn', 'api.js-lottery.com')
_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
MAX_HTML_BYTES = 800_000


class _OfficialTable(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.current = None
        self.cell = None
        self.links = []
        self.texts = []
        self._ignore = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style'):
            self._ignore += 1
        if self._ignore:
            return
        if tag == 'tr':
            self.current, self.links = [], []
        elif tag in ('td', 'th') and self.current is not None:
            self.cell = []
        elif tag == 'a' and self.current is not None and attrs.get('href'):
            self.links.append(attrs['href'])

    def handle_data(self, data):
        if self._ignore:
            return
        if data.strip():
            self.texts.append(data.strip())
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self._ignore = max(0, self._ignore-1)
            return
        if self._ignore:
            return
        if tag in ('td', 'th') and self.cell is not None:
            self.current.append(re.sub(r'\s+', ' ', ''.join(self.cell)).strip())
            self.cell = None
        elif tag == 'tr' and self.current is not None:
            if self.current:
                self.rows.append((tuple(self.current), tuple(self.links)))
            self.current, self.links = None, []


def _check_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in _ALLOWED_HOSTS or parsed.username or parsed.password:
        raise DrawLookupError('开奖地址不是允许的官方 HTTPS 网站', 'invalid')
    return parsed.hostname


class _OnlyOfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _check_url(newurl) != _check_url(req.full_url):
            raise DrawLookupError('官网跳转到其他域名，已停止查询', 'invalid')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _OfficialFetcher:
    def __init__(self):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()), _OnlyOfficialRedirect())

    def __call__(self, url):
        host = _check_url(url)
        is_api = host == 'www.cwl.gov.cn' and 'findDrawNotice' in url
        headers = {'User-Agent': _UA,
                   'Accept-Language': 'zh-CN,zh;q=0.9',
                   'Accept': 'application/json, text/javascript, */*;q=0.9' if is_api else 'text/html,application/xhtml+xml',
                   'Referer': CWL_PAGE if host == 'www.cwl.gov.cn' else f'https://{host}/'}
        req = urllib.request.Request(url, headers=headers)
        try:
            with self.opener.open(req, timeout=9) as response:
                if _check_url(response.geturl()) != host:
                    raise DrawLookupError('官网跳转到其他域名，已停止查询', 'invalid')
                raw = response.read(MAX_HTML_BYTES+1)
                if len(raw) > MAX_HTML_BYTES:
                    raise DrawLookupError('官网返回内容过大，已停止查询', 'invalid')
                enc = response.headers.get_content_charset() or 'utf-8'
                try:
                    return raw.decode(enc)
                except (LookupError, UnicodeDecodeError):
                    return raw.decode('utf-8', 'replace')
        except DrawLookupError:
            raise
        except HTTPError as exc:
            raise DrawLookupError(f'官网返回HTTP {exc.code}', 'network') from None
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise DrawLookupError('官网网络访问失败：'+str(exc)[:100], 'network') from None


def _curl_cwl():
    """仅当 Python 请求无法读取官网时，使用 Windows 自带 curl.exe。没有 curl 则正常转备用源。"""
    curl = shutil.which('curl.exe') if __import__('os').name == 'nt' else shutil.which('curl')
    if not curl:
        raise DrawLookupError('当前环境未安装 curl，转查省级官网', 'network')
    params = _cwl_api_url()
    with tempfile.TemporaryDirectory(prefix='cwl-session-') as folder:
        jar = __import__('os').path.join(folder, 'cookies.txt')
        common = [curl, '--silent', '--show-error', '--location', '--max-redirs', '4', '--max-time', '12',
                  '--proto', '=https', '--proto-redir', '=https', '-A', _UA,
                  '-H', 'Accept-Language: zh-CN,zh;q=0.9']
        try:
            init = subprocess.run(common + ['-c', jar, '-b', jar, '-o', __import__('os').devnull,
                                            '--write-out', '%{url_effective}', CWL_PAGE],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False)
            if init.returncode or _check_url(init.stdout.decode('utf-8', 'replace').strip()) != 'www.cwl.gov.cn':
                raise DrawLookupError('中国福彩网访问验证未通过', 'network')
            req = subprocess.run(common + ['-b', jar, '-H', 'Accept: application/json', '-H', 'Referer: '+CWL_PAGE,
                                           '--write-out', '\nCWL_URL:%{url_effective}', params],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False)
            if req.returncode or len(req.stdout) > MAX_HTML_BYTES:
                raise DrawLookupError('中国福彩网接口请求失败', 'network')
            raw, delimiter, effective = req.stdout.rpartition(b'\nCWL_URL:')
            if not delimiter or _check_url(effective.decode('utf-8', 'replace').strip()) != 'www.cwl.gov.cn':
                raise DrawLookupError('中国福彩网返回的地址未通过核验', 'invalid')
            return raw.decode('utf-8', 'replace')
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise DrawLookupError('中国福彩网访问失败：'+str(exc)[:100], 'network') from None


def _html(url):
    return _OfficialFetcher()(url)


def _table(content):
    parser = _OfficialTable()
    parser.feed(content)
    return parser


def _clean_digits(value):
    txt = re.sub(r'\s+', ' ', str(value)).strip()
    if re.fullmatch(r'[0-9]{3}', txt):
        return txt
    if re.fullmatch(r'[0-9]\s*[,，、/\- ]\s*[0-9]\s*[,，、/\- ]\s*[0-9]', txt):
        number = ''.join(re.findall(r'[0-9]', txt))
        return number if len(number) == 3 else ''
    return ''


def _issue(value):
    value = str(value).replace('第', '').replace('期', '').strip()
    return value if re.fullmatch(r'[0-9]{5,8}', value) else ''


def _cwl_api_url():
    params = {'name':'3d', 'issueCount':'', 'issueStart':'', 'issueEnd':'',
              'dayStart':'', 'dayEnd':'', 'pageNo':'1', 'pageSize':'100', 'systemType':'PC'}
    return CWL_API + '?' + urlencode(params)


def _from_cwl(wanted_date, api_url, fetch):
    try:
        obj = json.loads(fetch(api_url))
    except (ValueError, TypeError):
        raise DrawLookupError('中国福彩网接口未返回有效 JSON（可能需要访问验证）', 'network') from None
    if not isinstance(obj, dict):
        raise DrawLookupError('中国福彩网接口返回了不支持的数据格式', 'invalid')
    data = obj.get('result')
    if data is None and isinstance(obj.get('data'), dict):
        data = obj['data'].get('result')
    if not isinstance(data, list):
        raise DrawLookupError('中国福彩网尚未返回有效开奖列表（可能是网站访问限制）', 'network')
    if not data:
        raise DrawLookupError('中国福彩网开奖列表为空，无法确认是否已公布', 'network')
    latest = []
    matched = []
    for item in data:
        if not isinstance(item, dict):
            continue
        raw_date = str(item.get('date', '')).strip()
        m = re.match(r'^(20\d{2}-\d{2}-\d{2})(?:$|\D)', raw_date)
        if not m:
            continue
        d = m.group(1)
        issue = _issue(item.get('code', ''))
        if issue:
            latest.append((d, issue))
        if d != wanted_date:
            continue
        red = item.get('red', '')
        if isinstance(red, list):
            red = ','.join(str(x) for x in red)
        number = _clean_digits(red)
        if not (issue and number):
            raise DrawLookupError('中国福彩网本期记录的期号或开奖号码无效，已拒绝填入', 'invalid')
        matched.append((issue, number))
    if matched:
        if len(set(matched)) != 1:
            raise DrawLookupError('中国福彩网同一天出现互相矛盾的号码记录，请人工核对', 'conflict')
        issue, number = matched[0]
        return DrawResult('福彩', wanted_date, issue, number, '中国福彩网（官方）', CWL_PAGE)
    if latest:
        newest_date, newest_issue = max(latest)
        raise DrawLookupError(f'中国福彩网暂未找到{wanted_date}公告；列表最新：{newest_date} 第{newest_issue}期', 'missing')
    raise DrawLookupError('中国福彩网未返回可核对的开奖日期和期号', 'invalid')


def _url_from_index(index_url, href):
    candidate = urljoin(index_url, href)
    try:
        allowed = _check_url(candidate) == _check_url(index_url)
    except DrawLookupError:
        allowed = False
    if not allowed:
        raise DrawLookupError('官网返回了跨站开奖详情链接，未自动跟随', 'invalid')
    return candidate


def _fc_detail(html, wanted_date, wanted_issue):
    parsed = _table(html)
    body = ' '.join(parsed.texts)
    if wanted_issue not in body:
        raise DrawLookupError('福彩开奖详情期号与列表不一致', 'conflict')
    dates = re.findall(r'开奖日期\s*[:：]?\s*(20\d{2}-\d{2}-\d{2})', body)
    if wanted_date not in dates:
        raise DrawLookupError('福彩开奖详情日期与所选日期不一致', 'conflict')
    match = re.search(r'中奖号码\s*[:：]?\s*((?:[0-9]\s*[,，、 ]?\s*){3})', body)
    if not match:
        raise DrawLookupError('福彩详情未找到可验证的三位中奖号码', 'invalid')
    number = _clean_digits(match.group(1))
    if not number:
        raise DrawLookupError('福彩中奖号码格式无法可靠识别', 'invalid')
    return number


def _from_fc(wanted_date, index_url, fetch):
    parsed = _table(fetch(index_url))
    latest = []
    for cells, links in parsed.rows:
        if len(cells) < 2:
            continue
        d = cells[0]
        issue = _issue(cells[1])
        if re.fullmatch(r'20\d{2}-\d{2}-\d{2}', d) and issue:
            latest.append((d, issue))
        if d != wanted_date:
            continue
        if not issue or not links:
            raise DrawLookupError('福彩开奖列表缺少期号或详情链接', 'invalid')
        detail_url = _url_from_index(index_url, links[-1])
        number = _fc_detail(fetch(detail_url), wanted_date, issue)
        return DrawResult('福彩', wanted_date, issue, number, '四川省福利彩票发行中心（备用）', detail_url)
    if latest:
        newest_date, newest_issue = max(latest)
        raise DrawLookupError(f'省福彩公告中没有 {wanted_date} 的开奖信息；最新：{newest_date} 第{newest_issue}期', 'missing')
    raise DrawLookupError(f'省福彩公告中没有 {wanted_date} 的开奖信息；无法核对官网页面结构', 'invalid')


def _from_tc(wanted_date, index_url, fetch):
    parsed = _table(fetch(index_url))
    latest = []
    for cells, links in parsed.rows:
        if len(cells) < 3:
            continue
        issue = _issue(cells[1])
        number = _clean_digits(cells[2])
        if re.fullmatch(r'20\d{2}-\d{2}-\d{2}', cells[0]) and issue:
            latest.append((cells[0], issue))
        if cells[0] != wanted_date:
            continue
        if not issue or not number:
            raise DrawLookupError('体彩开奖行的期号或三位号码不完整，已拒绝自动填入', 'invalid')
        return DrawResult('体彩', wanted_date, issue, number, '江苏省体育彩票管理中心', index_url)
    if latest:
        newest_date, newest_issue = max(latest)
        raise DrawLookupError(f'省体彩公告中没有 {wanted_date} 的开奖信息；最新：{newest_date} 第{newest_issue}期', 'missing')
    raise DrawLookupError(f'省体彩公告中没有 {wanted_date} 的开奖信息', 'missing')


def fetch_draw(lottery, draw_date, fetch=None):
    if lottery not in SOURCE_URLS:
        raise DrawLookupError('未知彩种')
    if not isinstance(draw_date, str) or not re.fullmatch(r'20\d{2}-\d{2}-\d{2}', draw_date):
        raise DrawLookupError('联网获取要求选择 YYYY-MM-DD 格式的开奖日期')
    try:
        date.fromisoformat(draw_date)
    except ValueError:
        raise DrawLookupError('开奖日期无效') from None

    # 测试可注入 fetch，不触发任何外部请求。每次网络调用共用内存 CookieJar。
    getter = fetch or _OfficialFetcher()
    if lottery == '体彩':
        return _from_tc(draw_date, SOURCE_URLS[lottery][1], getter)

    errors = []
    api_url = _cwl_api_url()
    try:
        # 中国福彩网先访问列表页，获取会话 Cookie，再调用官方查询接口。
        getter(CWL_PAGE)
        return _from_cwl(draw_date, api_url, getter)
    except DrawLookupError as exc:
        if exc.kind == 'conflict':
            raise  # 同一官方来源出现矛盾号码，禁止用其他网站的结果掩盖冲突。
        errors.append(('中国福彩网', exc))
        if fetch is None and exc.kind == 'network':
            try:
                return _from_cwl(draw_date, api_url, lambda _: _curl_cwl())
            except DrawLookupError as retry:
                errors.append(('中国福彩网备用访问', retry))

    try:
        return _from_fc(draw_date, SOURCE_URLS['福彩'][1], getter)
    except DrawLookupError as exc:
        errors.append(('四川福彩备用', exc))

    detail = '；'.join(f'{name}：{str(exc)}' for name, exc in errors)
    if any(exc.kind == 'missing' for _, exc in errors):
        kind = 'missing'
    elif any(exc.kind == 'conflict' for _, exc in errors):
        kind = 'conflict'
    elif all(exc.kind == 'network' for _, exc in errors):
        kind = 'network'
    else:
        kind = 'invalid'
    raise DrawLookupError(detail[:600], kind) from None
