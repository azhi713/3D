# -*- coding: utf-8 -*-
"""V2.6.5 - bounded retries for demonstrably rejected or unsent AI requests.

Never resend when delivery, billing, or server processing is uncertain.
"""
import re
import time

MAX_ATTEMPTS=3


def _safe_to_retry(error,metrics):
    message=str(error)
    if re.search(r'AI请求失败：HTTP 429\b',message):
        return True
    if metrics.get('billing_state')=='not_sent':
        # TransportSetupError, invalid key, invalid JSON, etc. aren't transient.
        attempts=metrics.get('transport_attempts') or []
        return any(':pre_send_failed' in str(s) for s in attempts) and not any(':delivery_unknown' in str(s) for s in attempts)
    return False


def parse_with_safe_retry(service,lottery,text,timings=None,sleeper=time.sleep):
    timings=timings if timings is not None else {}
    total=0.0;history=[];waited=0.0
    for attempt in range(1,MAX_ATTEMPTS+1):
        began=time.perf_counter()
        try:
            answer=service.parse_unknown(lottery,text)
            total+=time.perf_counter()-began
            timings['retry_attempts']=attempt
            timings['retry_history']=history+['attempt'+str(attempt)+':success']
            timings['api_retry_total_s']=round(total,3)
            timings['retry_wait_s']=waited
            return answer
        except Exception as err:
            total+=time.perf_counter()-began
            metrics=getattr(service,'last_metrics',{}) or {}
            safe=_safe_to_retry(err,metrics)
            history.append('attempt'+str(attempt)+(':safe_failed' if safe else ':no_retry'))
            timings['retry_attempts']=attempt
            timings['retry_history']=history[:]
            timings['api_retry_total_s']=round(total,3)
            timings['retry_wait_s']=waited
            if not safe or attempt>=MAX_ATTEMPTS:raise
            # Keep retries short; do not block other worker slots for long.
            delay=0.6 if attempt==1 else 1.2
            sleeper(delay)
            waited+=delay
    raise RuntimeError('unreachable')
