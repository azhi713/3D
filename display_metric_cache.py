"""Reuse only immutable presentation metrics, leaving publication side effects intact."""
import functools
import json
import os

def cached_metrics(function):
    latest=None
    @functools.wraps(function)
    def wrapped(revenue_cents,payouts_cents,purchase):
        nonlocal latest
        if os.environ.get('FC3D_DISPLAY_CACHE','0')!='1':
            return function(revenue_cents,payouts_cents,purchase)
        try:
            key=json.dumps([revenue_cents,payouts_cents,purchase],sort_keys=True,ensure_ascii=False,allow_nan=False)
        except (TypeError,ValueError):
            return function(revenue_cents,payouts_cents,purchase)
        cached=latest
        if cached is not None and cached[0]==key:
            return cached[1]
        result=function(revenue_cents,payouts_cents,purchase)
        # PlanMetrics is a frozen dataclass consisting of scalars and tuples.
        latest=(key,result)
        return result
    return wrapped
