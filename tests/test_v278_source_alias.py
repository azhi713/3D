# -*- coding: utf-8 -*-
"""Run inside the user's source tree with the actual BetParser and rule table.

These tests are intentionally stricter than the standalone fake-parser tests:
when a syntax is not supported by the real parser, it must be fixed or left
pending, never silently accepted with a different play or multiplier.
"""
from decimal import Decimal
import pytest
from parser import BetParser


@pytest.mark.parametrize('lottery,source,play,selection,multiple,principal', [
    ('福彩', '福1479四码组六100🈴100', '四码组六','1479',10,100),
    ('福彩', '福1479四码组六10合100', '四码组六','1479',10,100),
    ('福彩', '福1479四码组六1/4倍', '四码组六','1479',.25,2.5),
    ('福彩', '福1479四码组六四分之一倍', '四码组六','1479',.25,2.5),
    ('福彩', '福1479四码组六0.5米', '四码组六','1479',.05,.5),
    ('福彩', '福1479四码组六半米', '四码组六','1479',.05,.5),
    ('福彩', '福1479四码组六5毛', '四码组六','1479',.05,.5),
    ('福彩', '福123直选0.5米', '直选','123',.25,.5),
    ('福彩', '福123 0.5米', '直选','123',.25,.5),
    ('福彩', '福123直选1/4倍', '直选','123',.25,.5),
    ('福彩', '福112组三1元', '组三（组选）','112',.5,1),
    ('福彩', '福124组六0.5米', '组六（组选）','124',.25,.5),
    ('福彩', '福1234四码组三1元', '四码组三','1234',.1,1),
    ('福彩', '福12345五码复试0.5米', '五码复试','12345',.05,.5),
    ('福彩', '福12双飞0.5米', '双飞','12',.05,.5),
    ('福彩', '福7独胆0.5米', '独胆','7',.05,.5),
    ('福彩', '福11对子0.5米', '对子','11',.05,.5),
    ('福彩', '福豹子0.5米', '豹子','',.25,.5),
    ('体彩', '体1479四码组六100合100', '四码组六','1479',10,100),
])
def test_local_alias(lottery,source,play,selection,multiple,principal):
    p=BetParser()
    bets, _, error=p.parse(lottery,source)
    assert not error,(source,error)
    assert len(bets)==1,(source,len(bets))
    b=bets[0]
    assert (b.play,b.selection)==(play,selection)
    assert Decimal(str(b.multiplier))==Decimal(str(multiple))
    assert Decimal(str(b.principal))==Decimal(str(principal))
    assert b.raw==source


@pytest.mark.parametrize('source,substring', [
    ('福1479四码组六4/1倍', '4/1倍'),
    ('福1479四码组六100合90', None),
])
def test_ambiguous_must_not_silently_post(source,substring):
    p=BetParser()
    bets,_,reason=p.parse('福彩',source)
    assert not bets, (source,[(b.play,b.selection,b.multiplier) for b in bets])
    assert reason


def test_reverse_fraction_with_unique_total():
    p=BetParser()
    bets,_,error=p.parse('福彩','福1479四码组六4/1倍合2.5')
    assert not error and len(bets)==1
    assert Decimal(str(bets[0].multiplier))==Decimal('0.25')
    assert any('歧义' in x for x in p.last_warnings)


def test_bad_claimed_amount_is_warning_for_explicit_unit():
    p=BetParser()
    bets,_,error=p.parse('福彩','福1479四码组六1米合2')
    assert not error and len(bets)==1
    assert Decimal(str(bets[0].principal))==Decimal('1')
    assert any('不一致' in x for x in p.last_warnings)


def test_subcent_multiplier_kept_pending():
    p=BetParser()
    bets,_,error=p.parse('福彩','福1479四码组六0.25米')
    assert not bets
    assert error
