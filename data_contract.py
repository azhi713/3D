"""本地识别与AI候选共用的只读输出契约；原文和账本保持独立。"""
from models import cents

SCHEMA_VERSION = '1.0'
LOTTERY_CODES = {'福彩':'FC3D', '体彩':'PL3'}
PLAY_CODES = {
    '直选':'DIRECT',
    '组三（组选）':'GROUP3_SINGLE', '组六（组选）':'GROUP6_SINGLE',
    '独胆':'SINGLE_DAN', '双飞':'DOUBLE_FLY', '对子':'PAIR',
    '豹子':'LEOPARD',
    '一码定位':'POSITION_1', '两码定位':'POSITION_2','三码定位':'POSITION_3',
    '全包组三':'GROUP3_ALL', '全包组六':'GROUP6_ALL',
    '组三复式':'GROUP3_PERMUTATION', '组六复式':'GROUP6_PERMUTATION',
}


def play_code(play):
    if play in PLAY_CODES:
        return PLAY_CODES[play]
    if play.endswith('码组三'):
        return 'GROUP3_NCODE'
    if play.endswith('码组六'):
        return 'GROUP6_NCODE'
    if play.endswith('码复试'):
        return 'RETEST_N'
    if play.startswith('跨度'):
        return 'SPAN'
    if play.startswith('和值'):
        return 'SUM'
    if play.startswith('1胆'):
        return 'DANTUO_GROUP3' if play.endswith('组3') else 'DANTUO_GROUP6'
    return 'UNKNOWN'


def record_to_contract(record):
    bets = []
    for bet in record.bets:
        positions = {}
        if bet.play.endswith('码定位'):
            positions = {location: list(digits) for location, digits in
                         (part.split(':') for part in bet.selection.split('|'))}
        is_pool = bet.play.endswith(('码组三','码组六','码复试')) or bet.play in ('组三复式','组六复式')
        ticket = (bet.play in ('直选','组三（组选）','组六（组选）'))
        bets.append({
            'bet_type':play_code(bet.play), 'play_label':bet.play,
            'ticket_codes':[bet.selection] if ticket else [],
            'digit_sets':list(bet.selection) if is_pool else [],
            'position_sets':positions,
            'dan':bet.selection.split(':')[0] if bet.play.startswith('1胆') else None,
            'tuo':bet.selection.split(':')[1] if bet.play.startswith('1胆') else None,
            'original_selection':bet.selection,
            'bet_multiples':str(bet.multiplier),
            'unit_price_yuan':None if bet.unit_price_yuan is None else str(bet.unit_price_yuan),
            'entered_amount_yuan':bet.entered_amount_yuan,
            'computed_amount_yuan':f'{cents(bet.principal)/100:.2f}',
            'source_span':bet.source_span,
            'parse_source':bet.parse_source,
            'rules_version':bet.rules_version,
            'settlement_rule_pending':list(bet.settlement_rule_pending),
        })
    return {
        'schema_version':SCHEMA_VERSION,
        'message_id':record.record_id,
        'source_message_id':record.source_message_id,
        'period':record.period,
        'received_at':record.created_at,
        'source':record.source,
        'sender':record.sender,
        'raw_text':record.source_original_text or record.raw_text,
        'current_text':record.raw_text,
        'normalized_text':record.normalized_text,
        'engine_version':record.engine_version,
        'rules_version':record.rules_version,
        'blocks':[{
            'lottery':LOTTERY_CODES.get(record.lottery,record.lottery),
            'bets':bets,
            'calculated_total_yuan':f'{sum(cents(b.principal) for b in record.bets)/100:.2f}',
            'declared_total_yuan':record.declared_total_yuan,
            'parse_status':record.parse_status,
            'review_status':record.review_status,
        'ai_request_state':record.ai_request_state,
            'issues':record.issues,
            'unresolved_fragments':record.pending_fragments,
        }],
    }
