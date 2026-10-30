"""2026-10-26私有赔率源与2026-10-31确认覆盖；不采用互联网官方赔率。"""
SUM_PRIZES=(8000,3000,1500,900,600,430,320,250,200,160,140,130,120,120,
            120,120,130,140,160,200,250,320,430,600,900,1500,3000,8000)
DANTUO_PRIZES={6:(1500,500,250,150,100,70,50,40),3:(750,500,370,300,250,200,185,160)}

def extra_rules():
    result={'和值'+str(i):{'principal':10,'prize':p} for i,p in enumerate(SUM_PRIZES)}
    for group,values in DANTUO_PRIZES.items():
        for count,prize in enumerate(values,2):
            result[f'1胆{count}拖组{group}']={'principal':10,'prize':prize}
    return result
