# -*- coding: utf-8 -*-
"""Separate a verified WeChat transport header from the customer message.

A first-line verified identifier plus newline is separated from the body.
Native/metadata-verified wxid_ prefixes also preserve the old same-line format.
No arbitrary prose, lottery/position header, or later line is deleted.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import re

HEADER=re.compile(r'\A(?:\ufeff)?(?P<id>[^\r\n:：]{1,128})[:：][ \t]*(?:\r\n|\n|\r)(?P<body>[\s\S]*)\Z')
CUSTOM=re.compile(r'[A-Za-z][A-Za-z0-9_-]{5,63}\Z')
WXID=re.compile(r'wxid_[A-Za-z0-9_-]{1,120}\Z')
RESERVED={'福','福彩','福彩3D','3D','3d','体','体彩','排','排列三','百','十','个',
          '百位','十位','个位','定位','直选','组选','组三','组六','合计','总计'}

@dataclass(frozen=True)
class CleanMessage:
    text: str
    sender: str
    original: str
    prefix: str=''
    proof: str=''


def split_sender(text,sender='',group_id='',trusted_database=False):
    original=str(text or '')
    current=str(sender or '').strip()
    m=HEADER.fullmatch(original)
    if m is None:
        # Legacy native wxid_ envelopes may put text immediately after ':'.
        # Do not extend this exception to custom names or ordinary prose.
        m=re.fullmatch(r'(?:\ufeff)?(?P<id>wxid_[A-Za-z0-9_-]{1,120})[:：][ \t]*(?P<body>[^\r\n].*)', original, re.S)
    if not m or not str(group_id).endswith('@chatroom'):
        return CleanMessage(original,current,original)
    prefix=m['id'].strip();body=m['body']
    if prefix in RESERVED or not body.strip():return CleanMessage(original,current,original)
    proof=''
    if current and prefix==current:proof='matches_sender_metadata'
    elif (not current or current.isdigit()) and trusted_database and (WXID.fullmatch(prefix) or CUSTOM.fullmatch(prefix)):
        # Only the native database callback can opt into this transport grammar.
        # A valid betting body corroborates a non-wxid legacy account name.
        if re.search(r'(?<![0-9])[0-9]{1,9}(?![0-9])',body) and re.search(r'注|倍|直选|组六|组三|组选|双飞|定位|复试|复式|跨度|和值|独|对子|元|米|块',body):
            proof='native_group_header_and_betting_body'
    if not proof:return CleanMessage(original,current,original)
    return CleanMessage(body.lstrip('\r\n'),prefix,original,prefix,proof)


def normalize_payload(payload):
    if not isinstance(payload,dict):return payload
    p=dict(payload)
    result=split_sender(p.get('text',''),p.get('sender',''),p.get('group_id',''),
                        p.get('transport_source')=='wechat4')
    p['text']=result.text;p['sender']=result.sender
    if result.prefix:
        p.setdefault('transport_original_text',result.original)
        p['sender_prefix']=result.prefix;p['sender_prefix_proof']=result.proof
    return p


def repair_record(record):
    """For an explicit reparse of an existing WeChat record; never at install."""
    if getattr(record,'source','') not in ('微信','微信历史'):return False
    raw=getattr(record,'raw_text','')
    result=split_sender(raw,getattr(record,'sender',''),getattr(record,'group_id',''),True)
    if not result.prefix:return False
    original=getattr(record,'source_original_text','') or raw
    record.source_original_text=original
    record.raw_text=result.text
    record.sender=result.sender
    entry={'action':'wechat_sender_prefix_v281','at':datetime.now(timezone.utc).isoformat(),
           'transport_original_text':raw,'sender_prefix':result.prefix,'proof':result.proof}
    if hasattr(record,'ai_history'):record.ai_history.append(entry)
    return True


def annotate_received(record,payload):
    original=payload.get('transport_original_text')
    if original is None:return False
    entry={'action':'wechat_sender_prefix_v281',
           'transport_original_text':original,
           'sender_prefix':payload.get('sender_prefix',''),
           'proof':payload.get('sender_prefix_proof','')}
    if entry not in record.ai_history:record.ai_history.append(entry)
    # source_original_text is the original customer's body (not the database
    # envelope); the complete envelope above remains in the audit trail.
    return True
