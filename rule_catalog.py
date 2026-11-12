"""用户提供的2026-11-09赔率图片；以图片金额为准，不采用官方赔率。"""
SUM_PRIZES=(7500,3000,1500,900,600,430,320,250,200,160,140,130,120,120,
            120,120,130,140,160,200,250,320,430,600,900,1500,3000,7500)
DANTUO_PRIZES={6:(1500,500,250,150,100,70,50,40),3:(750,500,370,300,250,200,185,160)}

# Only confirmed differences for plays already supported by the risk engine.
# (principal, old prize, photo prize). All periods with saved snapshots retain
# their own historical odds; this migration applies to newly created periods.
PHOTO_CHANGES = {
    '四码组六': (10, 370, 360),
    '七码复试': (10, 26, 25),
    '跨度1': (10, 157, 165),
    '跨度2': (10, 88, 92),
    '跨度3': (10, 67, 72),
    '跨度4': (10, 58, 62),
    '跨度5': (10, 56, 60),
    '跨度6': (10, 58, 63),
    '跨度7': (10, 67, 72),
    '跨度8': (10, 88, 93),
    '跨度9': (10, 157, 170),
    '和值0': (10, 8000, 7500),
    '和值27': (10, 8000, 7500),
}


def migrate_photo_defaults_for_new_period(rules):
    """Advance only untouched legacy defaults; preserve custom and old-period odds.

    Called only while constructing a *new* period, never when reading saved
    period_rules or recalculating recorded bets.
    """
    for lottery_rules in rules.values():
        for play, (principal, old, new) in PHOTO_CHANGES.items():
            rule = lottery_rules.get(play)
            if rule and rule.get('principal') == principal and rule.get('prize') == old:
                rule['prize'] = new

def extra_rules():
    result={'和值'+str(i):{'principal':10,'prize':p} for i,p in enumerate(SUM_PRIZES)}
    for group,values in DANTUO_PRIZES.items():
        for count,prize in enumerate(values,2):
            result[f'1胆{count}拖组{group}']={'principal':10,'prize':prize}
    return result
