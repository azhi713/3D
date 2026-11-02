# -*- coding: utf-8 -*-
"""Strict generic local recognizers for receipt-backed order families.

Never assumes an amount is a bet multiplier.  All prices come from the user's
existing parser._make / _from_kind and current lottery rules.
"""
from __future__ import annotations
import re
import unicodedata
from decimal import Decimal

LOT = r'(?:福彩3[Dd]?|福彩|3[Dd]|体彩排列三|体彩|排列三|福|体|排)'
DELIM = r'[\s,，、。;；/\-]+'
BATCH = re.compile(r'^\s*(?P<lot>'+LOT+r')?\s*[:：,，。]?\s*'
    r'(?P<codes>\d{3}(?:'+DELIM+r'\d{3})+)\s*[,，、。;；/\-]*\s*'
    r'(?:直选|直|单)\s*(?P<count>\d+)\s*注\s*'
    r'(?P<amount>\d+(?:\.\d{1,2})?)\s*[元米块]?\s*$',re.I)
PAIRED = re.compile(r'^\s*(?P<lot>'+LOT+r')?\s*[:：,，。]?\s*'
    r'(?P<codes>\d{3}(?:'+DELIM+r'\d{3})+)\s*[,，、。;；/\-]*\s*'
    r'(?:直选|直|单)\s*(?P<dm>\d+(?:\.\d{1,2})?)\s*倍?\s*'
    r'(?:组选|组)\s*(?P<gm>\d+(?:\.\d{1,2})?)\s*倍?\s*'
    r'(?:(?:[，,。;；\s]+)\s*(?P<label>合计|总计|金额|共)?\s*(?P<tail>\d+(?:\.\d{1,2})?)\s*(?P<unit>[元米块注])?)?\s*$',re.I)


def _lottery_matches(token,lottery):
    if not token:return True
    from parser import lottery_from_token
    return lottery_from_token(token)==lottery


def _due(bets):
    return sum((Decimal(str(b.principal)) for b in bets),Decimal('0'))


def parse_amount_v264(parser,lottery,text):
    if not isinstance(text,str) or not text.strip():return None
    raw=unicodedata.normalize('NFKC',text).replace('㊗️','注').replace('㊗','注').replace('祝️','注')
    normalized=parser.normalize_text(text)
    batch=BATCH.fullmatch(raw)
    if batch:
        if not _lottery_matches(batch['lot'],lottery):return None
        codes=re.findall(r'(?<!\d)\d{3}(?!\d)',batch['codes'])
        declared_count=int(batch['count'])
        if len(codes)!=declared_count:
            parser.last_claimed=float(batch['amount'])
            parser.last_receipts=[]
            parser.last_warnings=[]
            return [],normalized,f'原文声明{declared_count}注，实际只有{len(codes)}个三位号码；未自动入账'
        bets=[]
        try:
            for code in codes:bets.extend(parser._from_kind(lottery,text,code,'直选',1))
        except (ValueError,KeyError,ArithmeticError):return None
        parser.last_claimed=float(batch['amount'])
        parser.last_receipts=[]
        parser.last_warnings=[]
        return bets,normalized,''
    paired=PAIRED.fullmatch(raw)
    if paired:
        if not _lottery_matches(paired['lot'],lottery):return None
        codes=re.findall(r'(?<!\d)\d{3}(?!\d)',paired['codes'])
        bets=[]
        try:
            dm=parser._multiplier(paired['dm'])
            gm=parser._multiplier(paired['gm'])
            for code in codes:
                bets.extend(parser._from_kind(lottery,text,code,'直选',dm))
                bets.extend(parser._from_kind(lottery,text,code,'组选',gm))
        except (ValueError,KeyError,ArithmeticError):return None
        tail=paired['tail']
        if tail is not None and paired['unit']=='注':
            if Decimal(tail)!=Decimal(len(bets)):
                return [],normalized,'原文声明注数与实际展开笔数不一致；未自动入账'
            parser.last_claimed=None
        else:
            parser.last_claimed=float(tail) if tail is not None else None
        parser.last_receipts=[]
        parser.last_warnings=[]
        return bets,normalized,''
    return None
