"""Opt-in reuse of detached risk inputs; exact SQLite payload comparison on writes."""
import json
import os
from preview_cache import database_token

def enabled():
    return os.environ.get('FC3D_INPUT_CACHE','0')=='1'

def records_version(store, period):
    token=database_token(store)
    if token is None:return None
    old=getattr(store,'_risk_records_version',None)
    if old and old[0]==(token,period):
        return old[2]
    # Compare the actual stored rows, not change_serial or a lossy aggregate.
    rows=tuple(tuple(r) for r in store.db.execute(
        'SELECT id,lottery,source_id,source,payload,deleted FROM records WHERE period=? ORDER BY id',
        (period,)))
    if database_token(store)!=token:return None
    serial=(old[2][1] if old and old[0][1]==period and old[1]==rows
            else (old[2][1]+1 if old else 1))
    version=(id(store),serial,period)
    store._risk_records_version=((token,period),rows,version)
    return version

def prepare(app, lottery, records, version):
    controller=app.controller
    cfg=controller.settings
    if version is None or records_version(controller.store,controller.period)!=version:
        inputs=controller.analysis_inputs(lottery)
        return inputs,repr((controller.period,lottery,inputs))
    config={k:v for k,v in cfg.items() if k not in ('risk_filters','risk_threshold','threshold_metric')}
    key=(version,lottery,json.dumps([controller.rules,config],sort_keys=True,ensure_ascii=False))
    cached=getattr(app,'_detached_risk_inputs',None)
    if cached is None or cached[0]!=key:
        inputs=controller.analysis_inputs(lottery,records)
        app._detached_risk_inputs=(key,inputs)
    else:
        inputs=cached[1]
    filters=cfg.get('risk_filters',{}).get(lottery,{})
    threshold=filters.get('threshold',cfg['risk_threshold'])
    metric=filters.get('metric',cfg['threshold_metric'])
    args=list(inputs[2]);args[2]=threshold;args[5]=metric
    # RiskEngine reads these detached bets/rules; no controller-owned objects
    # are shared with a worker, and filtering never mutates the snapshot.
    result=(inputs[0],inputs[1],tuple(args),*inputs[3:])
    return result,(key,threshold,metric)
