"""AI发送门禁与基于投注证据的复核入口，禁止模型改变已明确的下注。"""
import re
import unicodedata

_MANUAL_MARKERS=('撤单','取消','更正','改成','不买')
_LEOPARD_GROUP=re.compile(r'(?<!\d)(\d{3})\s*(?:组选|组[三六36]?)(?:\s|\d|倍|元|米|块|$)')
_LEOPARD_ROUND=re.compile(r'(?<!\d)(\d{3})\s*转圈打')
_ZERO_OR_NEGATIVE=re.compile(
    r'(?:直|直选|单|组选|组|双飞|飞|定位|跨度|跨|复试|复式|转圈打)\s*[-−]?0\s*倍'
    r'|(?:直|直选|单|组选|组|双飞|定位|跨度|复试|复式)\s*-\d+\s*倍')


def classify_fragment(text,reason=''):
    line=unicodedata.normalize('NFKC',text or '')
    if any(marker in line for marker in _MANUAL_MARKERS):
        return {'code':'manual_instruction','ai_eligible':False,'reason':'涉及撤单或更改指令，必须由用户确认'}
    if any(len(set(m[1]))==1 for m in _LEOPARD_ROUND.finditer(line)):
        return {'code':'rule_violation','ai_eligible':False,'reason':'豹子不能转圈打'}
    if any(len(set(m[1]))==1 for m in _LEOPARD_GROUP.finditer(line)):
        return {'code':'rule_violation','ai_eligible':False,'reason':'豹子不能购买组选'}
    if _ZERO_OR_NEGATIVE.search(line) or re.search('倍数须|倍数必须|不重复数字|号码数量或重复情况不正确',reason or ''):
        return {'code':'rule_violation','ai_eligible':False,'reason':reason or '非法号码或倍数'}
    if re.search(r'尚未确认|码数与|彩种.*冲突|同一位置写了多次|需确认下注口径',reason or ''):
        return {'code':'unconfirmed_rule','ai_eligible':False,'reason':reason}
    if re.search(r'胆拖|胆.*拖|拖.*胆',line):
        return {'code':'unconfirmed_rule','ai_eligible':False,'reason':'未找到已确认的胆拖收费、奖金与重复命中规则'}
    if re.search(r'怎么算|多少钱|多少倍|怎么下|是什么意思|中.*的话|中奖.*多少',line):
        return {'code':'non_bet','ai_eligible':False,'reason':'规则/金额询问，不自动作为新投注发送AI'}
    if re.fullmatch(r'\s*(?:好[的呀啊👌]*|收到|谢谢|好的谢谢|OK|ok|嗯[嗯好]*|[👍👌]+)\s*',line):
        return {'code':'non_bet','ai_eligible':False,'reason':'聊天确认语，不是投注，不调用AI'}
    if re.fullmatch(r'\s*(?:\d{1,2}|定位|定|[一二三]码定位)\s*',line):
        return {'code':'insufficient_context','ai_eligible':False,'reason':'缺少完整下注信息'}
    return {'code':'needs_review','ai_eligible':True,'reason':reason or '本地无法唯一确认语义'}


def ai_fragments(record):
    return [p for p in getattr(record,'pending_fragments',[]) if p.get('ai_eligible') is True and p.get('text','').strip()]


def ai_candidate_validation(source_text,candidate_text,bets,parser=None,lottery='福彩'):
    from bet_evidence import validate_evidence
    return validate_evidence(source_text,candidate_text,bets,parser,lottery)


def ai_candidate_safe(source_text,candidate_text,bets):
    return ai_candidate_validation(source_text,candidate_text,bets)[0]
