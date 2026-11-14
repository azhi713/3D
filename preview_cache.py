"""Conservative, opt-in cache for repeated automatic draw previews."""
from copy import deepcopy
import json
import os
import sqlite3

def database_token(store):
    db=store.db
    if db.in_transaction:
        return None
    try:
        return (id(db),db.total_changes,db.execute('PRAGMA data_version').fetchone()[0],
                db.execute('PRAGMA schema_version').fetchone()[0])
    except (sqlite3.Error, AttributeError, TypeError):
        return None

def preview(controller, lottery, draw, period, compute):
    if os.environ.get('FC3D_PREVIEW_CACHE','1')!='1':
        return compute()
    def key():
        token=database_token(controller.store)
        if token is None:return None
        return (token,lottery,draw,period,json.dumps(
            [controller.rules,controller.settings],sort_keys=True,ensure_ascii=False))
    before=key()
    cached=getattr(controller,'_automatic_preview_cache',None)
    if before is not None and cached is not None and cached[0]==before:
        return deepcopy(cached[1])
    value=compute()
    after=key()
    controller._automatic_preview_cache=((before,deepcopy(value))
                                        if before is not None and before==after else None)
    return value
