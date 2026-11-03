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



def _v268_exact_amount_groups(parser, lottery, text, original):
    from decimal import Decimal
    from recognition_v265 import _num
    def by_cash(code, play, amount):
        base=parser._from_kind(lottery,original,code,play,1)
        if len(base)!=1:raise ValueError('组合展开异常')
        unit=Decimal(str(base[0].principal))
        if unit<=0:raise ValueError('单倍本金无效')
        multiplier=Decimal(str(amount))/unit
        if multiplier<=0:raise ValueError('金额不能为0')
        mult=parser._multiplier(format(multiplier,'f'))
        bets=parser._from_kind(lottery,original,code,play,mult)
        if sum(Decimal(str(b.principal)) for b in bets)!=Decimal(str(amount)):
            raise ValueError('金额不能整倍核算')
        return bets

    # Example: 640-660-600-606福1直1组打1米 8.
    one=re.fullmatch(r'\s*(?P<lot_before>福彩|福|体彩|体)?\s*'
        r'(?P<codes>\d{3}(?:[\s,，、/\-]+\d{3})+)\s*'
        r'(?P<lot_after>福彩|福|体彩|体)?\s*1\s*直\s*1\s*组\s*打\s*1\s*[元米块]\s*'
        r'(?P<total>\d+(?:\.\d{1,2})?)\s*[元米块]?\s*',text)
    if one:
        label=one['lot_before'] or one['lot_after']
        if label and not _lottery_matches(label,lottery):return None
        nums=re.findall(r'(?<!\d)\d{3}(?!\d)',one['codes'])
        if Decimal(one['total']) != Decimal(2*len(nums)):
            return None  # Never reconcile an inconsistent receipt by guessing.
        try:
            bets=[]
            for code in nums:
                bets.extend(by_cash(code,'直选',1))
                bets.extend(by_cash(code,'组选',1))
            total=sum(Decimal(str(b.principal)) for b in bets)
            if total != Decimal(one['total']):return None
        except (ValueError, KeyError, ArithmeticError):return None
        parser.last_claimed=float(total)
        parser.last_receipts=[]
        parser.last_warnings=[]
        return bets,parser.normalize_text(original),''

    # Entire order + total uniquely disambiguate numeric group/fly amounts.
    fly=re.fullmatch(r'\s*(?P<lot>福彩|福)?\s*(?P<main>\d{3})\s*'
         r'(?P<direct>[一二两三四五六七八九十\d]+)\s*单\s*'
         r'(?P<group>[一二两三四五六七八九十\d]+)\s*组\s*'
         r'(?P<pool>\d{4,9})\s*组六\s*(?P<poolcash>\d+(?:\.\d{1,2})?)\s*'
         r'[。.,，;；\s]+(?P<flycodes>\d{2}(?:[\s,、，\-]+\d{2})+)\s*'
         r'双飞\s*(?P<flycash>\d+(?:\.\d{1,2})?)\s*'
         r'(?:合计|总计)\s*(?P<total>\d+(?:\.\d{1,2})?)\s*[元米块]?\s*',text)
    if fly:
        if fly['lot'] and not _lottery_matches(fly['lot'],lottery):return None
        try:
            nums=re.findall(r'(?<!\d)\d{2}(?!\d)',fly['flycodes'])
            bets=[]
            bets.extend(parser._from_kind(lottery,original,fly['main'],'直选',
                                          parser._multiplier(str(_num(fly['direct'])))))
            bets.extend(parser._from_kind(lottery,original,fly['main'],'组选',
                                          parser._multiplier(str(_num(fly['group'])))))
            bets.extend(by_cash(fly['pool'],'组六',Decimal(fly['poolcash'])))
            for code in nums:
                bets.extend(by_cash(code,'双飞',Decimal(fly['flycash'])))
            total=sum(Decimal(str(b.principal)) for b in bets)
            if total != Decimal(fly['total']):return None
        except (ValueError,KeyError,ArithmeticError):return None
        parser.last_claimed=float(total)
        parser.last_receipts=[]
        parser.last_warnings=[]
        return bets,parser.normalize_text(original),''
    return None


def parse_amount_v264(parser,lottery,text):
    if not isinstance(text,str) or not text.strip():return None
    raw=unicodedata.normalize('NFKC',text).replace('㊗️','注').replace('㊗','注').replace('祝️','注')
    normalized=parser.normalize_text(text)
    exact=_v268_exact_amount_groups(parser,lottery,raw,text)
    if exact is not None:
        return exact
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
