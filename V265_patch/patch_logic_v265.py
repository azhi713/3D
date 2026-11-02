# -*- coding: utf-8 -*-
"""Exact-anchor, idempotent V2.6.5 source patch. No DB/schema mutation."""
TAG='V265-20261102-AUTO-RECOGNIZE-SAFE-RETRY'

def once(s,before,after,section):
    found=s.count(before)
    if found!=1:raise RuntimeError('%s：预期唯一锚点，实际%d个；安装未修改源码'%(section,found))
    return s.replace(before,after,1)

def update_parser(old):
    if '# '+TAG in old:return old
    if '# V264-20261102-AMOUNT-AUDIT' not in old:
        raise RuntimeError('parser.py未检测到V2.6.4，请先安装并确认V2.6.4')
    s=once(old,
        '    def parse(self, lottery, text):\n        from recognition_amount_v264 import parse_amount_v264\n',
        '    def parse(self, lottery, text):\n'
        '        from recognition_v265 import parse_v265\n'
        '        complete_v265 = parse_v265(self, lottery, text)\n'
        '        if complete_v265 is not None:\n'
        '            return complete_v265\n'
        '        from recognition_amount_v264 import parse_amount_v264\n',
        '本地识别优先级')
    return s.rstrip()+'\n\n# '+TAG+'\n'

def update_ui(old):
    if '# '+TAG in old:return old
    if '# V264-20261102-AMOUNT-AUDIT' not in old:
        raise RuntimeError('ui.py未检测到V2.6.4，请先安装并确认V2.6.4')
    s=once(old,
       '                candidate, reason = service.parse_unknown(record.lottery, ai_text)\n',
       '                from ai_retry_v265 import parse_with_safe_retry\n'
       '                candidate, reason = parse_with_safe_retry(service, record.lottery, ai_text, timings)\n',
       '后台AI安全重试入口')
    # Two request results: successful and caught AIError. No other source text changes.
    target='                if isinstance(metrics,dict):timings.update(metrics)\n'
    if s.count(target)!=2:
        raise RuntimeError('ui.py AI耗时更新位置不是2处；拒绝盲目替换')
    s=s.replace(target,
       '                if isinstance(metrics,dict):timings.update(metrics)\n'
       "                if 'api_retry_total_s' in timings:\n"
       "                    timings['api_total_s']=timings['api_retry_total_s']\n"
       ,2)
    s=once(s,
       "            for key,label in (('queue_s','排队'),('api_total_s','接口'),('verification_s','本地校验'),('total_s','总耗时')):\n",
       "            if timed['timings'].get('retry_attempts',1)>1:\n"
       "                lines.append(f\"接口尝试：{timed['timings']['retry_attempts']}次（仅安全失败才自动重试）\")\n"
       "            for key,label in (('queue_s','排队'),('api_total_s','接口累计'),('verification_s','本地校验'),('total_s','总耗时')):\n",
       'AI详情展示重试次数')
    s=once(s,
        "                    metrics['total_s']=sum(float(metrics.get(k) or 0) for k in ('queue_s','api_total_s','verification_s','posting_s'))\n",
        "                    metrics['total_s']=sum(float(metrics.get(k) or 0) for k in ('queue_s','api_total_s','retry_wait_s','verification_s','posting_s'))\n",
        '成功请求总耗时包含重试间隔')
    s=once(s,
        "                    metrics['total_s']=float(metrics.get('queue_s') or 0)+float(metrics.get('api_total_s') or 0)\n",
        "                    metrics['total_s']=sum(float(metrics.get(k) or 0) for k in ('queue_s','api_total_s','retry_wait_s'))\n",
        '失败请求总耗时包含重试间隔')
    return s.rstrip()+'\n\n# '+TAG+'\n'

def update_controller(old):
    if '# '+TAG in old:return old
    if '# V264-20261102-AMOUNT-AUDIT' not in old:
        raise RuntimeError('controller.py未检测到V2.6.4，请先安装并确认V2.6.4')
    s=once(old,
        "'一单一组的明确金额与单倍本金不符')))",
        "'一单一组的明确金额与单倍本金不符',\n"
        "                                                    '声明注数与实际三位选号数量不一致')))",
        '严格注数校验禁止部分误入账')
    return s.rstrip()+'\n\n# '+TAG+'\n'

def update_config(old):
    if '# '+TAG in old:return old
    if '# V264-20261102-AMOUNT-AUDIT' not in old:
        raise RuntimeError('config.py未检测到V2.6.4，请先安装并确认V2.6.4')
    s=once(old,'APP_VERSION = "V2.6.4-金额核算与状态安全"',
           'APP_VERSION = "V2.6.5-自动识别与安全重试"','程序版本')
    s=once(s,'ENGINE_VERSION = "V2.6.4-amount-audit-local"',
           'ENGINE_VERSION = "V2.6.5-auto-recognize-safe-retry"','引擎版本')
    return s.rstrip()+'\n\n# '+TAG+'\n'
