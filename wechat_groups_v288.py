"""Backward-compatible, bounded WeChat group bindings.

The older settings format contains one fc_group/tc_group string.  New settings
contain lists.  An explicitly saved empty list must not resurrect a legacy ID.
"""

MAX_GROUPS_PER_LOTTERY = 20
KEYS = {'福彩': ('fc_groups', 'fc_group'), '体彩': ('tc_groups', 'tc_group')}


def _normalized(value):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    ids = []
    for item in value:
        if not isinstance(item, str):
            continue
        group = item.strip()
        if group and len(group) <= 256 and group not in ids:
            ids.append(group)
    return ids


def bound_groups(config, lottery):
    plural, singular = KEYS[lottery]
    if plural in config:
        return _normalized(config[plural])
    return _normalized(config.get(singular, ''))


def group_lotteries(config):
    return {gid: lottery for lottery in KEYS
            for gid in bound_groups(config, lottery)}


def normalize_bindings(config):
    """Validate and persist without silently discarding an existing binding."""
    result = dict(config)
    for lottery, (plural, singular) in KEYS.items():
        groups = bound_groups(result, lottery)
        if len(groups) > MAX_GROUPS_PER_LOTTERY:
            raise ValueError(f'{lottery}最多绑定{MAX_GROUPS_PER_LOTTERY}个微信群')
        result[plural] = groups
        result[singular] = groups[0] if groups else ''
    fc = set(result['fc_groups'])
    tc = set(result['tc_groups'])
    if fc & tc:
        raise ValueError('福彩、体彩不能绑定同一个微信群：' + ', '.join(sorted(fc & tc)[:3]))
    return result
