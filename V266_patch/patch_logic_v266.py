# -*- coding: utf-8 -*-
"""Strict source transformations for V2.6.6; no persistence/data changes."""


def only_one(source, old, new, name):
    count=source.count(old)
    if count!=1:
        raise ValueError(f'{name} 模块锚点数量异常：{count}（应为1），为防覆盖已停止')
    return source.replace(old,new,1)


def between(source, begin, end, replacement, name):
    if source.count(begin)!=1 or source.count(end)!=1:
        raise ValueError(f'{name} 缺少准确锚点，拒绝修改')
    i=source.index(begin);j=source.index(end,i+len(begin))
    return source[:i]+replacement+source[j:]


def controller(source):
    source=only_one(source,
        '        for p in ai_fragments(record):\n',
        '        for p in record.pending_fragments:\n',
        'controller.ai_request_text:保留所有未解释片段')
    source=only_one(source,
        "                           'instruction':'只解释未确定投注；使用程序提供的segment_id。'\n"
        "                                         '不用计算字符偏移；勿重复已确认投注；金额/倍数必须区分。'},\n",
        "                           'use_full_ai_interpretation':True,\n"
        "                           'original_message':record.source_original_text or record.raw_text,\n"
        "                           'instruction':'独立理解完整raw_text，local_confirmed仅作参考、不可作为语法限制；'\n"
        "                                         '必须解释全部有依据的投注并保留重复购买；使用真实segment_id，'\n"
        "                                         '不计算字符偏移，不得虚构号码/玩法/倍数，金额与倍数分开。'},\n",
        'controller.ai_request_text:完整原文模式')
    source=between(source,
        "        if any(not frag.get('ai_eligible', False) for frag in record.pending_fragments):\n",
        "\n    @staticmethod\n    def _annotate_bets(record, parser):\n",
        "        from recognition_policy import classify_fragment\n"
        "        policy=classify_fragment(record.raw_text)\n"
        "        if policy['code'] in ('manual_instruction','rule_violation','unconfirmed_rule','non_bet'):\n"
        "            return ''  # 撤单、明确无效玩法等不允许AI擅自改为有效投注。\n"
        "        if record.status=='规则违规':\n"
        "            return ''\n"
        "        if not record.pending_fragments and record.parse_status!='amount_mismatch':\n"
        "            return ''\n"
        "        return record.raw_text  # 本地错误/金额歧义不再阻止AI理解整条消息。\n",
        'controller.ai_text_for_record:解除整单误拦截')
    ai_block='''        if record.ai_candidate and (pending or not bets):
            from deadline_v260 import allow_candidate
            from recognition_policy import classify_fragment
            policy=classify_fragment(record.raw_text)
            if (not allow_candidate(self,record) or
                policy['code'] in ('manual_instruction','rule_violation','unconfirmed_rule','non_bet')):
                review_note='；存在撤单、明确违规或已截止等禁止自动入账的情况'
            else:
                from ai_pipeline_v266 import reconcile
                from ai_pipeline_v266 import message_total
                result=reconcile(record.raw_text,record.ai_candidate,parser,record.lottery,
                                 bets,pending,claimed)
                if result.accepted or result.complete:
                    bets=result.bets
                    pending=self._pending_with_policy(result.pending,text)
                    used_ai=True
                    if result.complete:
                        # Full AI evidence independently verified. Prior local
                        # provisional line receipts must not override it.
                        provisional_due=None
                        receipt_rows=[]
                        grand=message_total(record.raw_text)
                        if grand is not None:claimed=float(grand)
                        reason='AI独立证据及原文覆盖校验通过'
                    review_note=('；AI独立证据校验成功，剩余疑问保留' if pending
                                 else '；AI独立证据校验通过')
                    if result.detail:review_note+='；'+result.detail
                else:
                    review_note='；AI候选未通过独立证据校验：'+(result.detail or '原文证据不足')
'''
    source=between(source,
        '        if record.ai_candidate and (pending or not bets):\n',
        '        # Re-audit after verified AI candidates have replaced tentative local bets.\n',
        ai_block,
        'controller.parse_record:独立验证候选')
    source=only_one(source,
        "        if used_ai:\n"
        "            record.note += '；AI转换已通过程序校验，自动计入' if not allow_conflict else '；AI候选经人工确认并通过本地校验'\n",
        "        if used_ai:\n"
        "            if pending:record.note += '；仅将已验证的AI投注保留，剩余片段待核对'\n"
        "            else:record.note += '；AI独立证据校验通过'\n",
        'controller.parse_record:部分结果不可宣称整单自动计入')
    source=only_one(source,
        '        started=perf_counter()\n        self.last_ai_metrics={\'verification_s\':0.0,\'posting_s\':0.0}\n        record = self.store.get(record_id)\n',
        '        started=perf_counter()\n        self.last_ai_metrics={\'verification_s\':0.0,\'posting_s\':0.0}\n        record = self.store.get(record_id)\n',
        'controller.apply_ai_candidate:起点核对')
    source=only_one(source,
        "        record.ai_candidate = candidate\n        record.ai_request_state = 'responded'\n",
        "        previous_bet_count=len(record.bets)\n"
        "        record.ai_candidate = candidate\n        record.ai_request_state = 'responded'\n",
        'controller.apply_ai_candidate:记录原已确认数量')
    source=only_one(source,
        "        return record.status == '已识别'\n\n    def accept_ai(self, record):\n",
        "        return (record.status == '已识别' or\n"
        "                (record.status == '部分识别' and len(record.bets)>previous_bet_count))\n\n"
        "    def accept_ai(self, record):\n",
        'controller.apply_ai_candidate:局部成功返回')
    return source


def ai_service(source):
    source=between(source,
        "        instructions += '\\n' + 'V2.5.7双字段协议：",
        "        # V257-RECEIPT-FOLLOWUP-20261101: deterministic receipt-backed candidates first.\n",
        "        instructions += ('\\nV2.6.6独立识别协议：完整阅读输入JSON中的raw_text，'\n"
        "                         '以下新协议覆盖上述旧版只解释unresolved_segments的要求；local_confirmed仅供参考，不能限制AI理解；'\n"
        "                         '按真实source_segments分组返回全部可证实的candidate_bets；'\n"
        "                         '每笔含segment_id、bet_type、ticket_codes/digit_set/position_sets、'\n"
        "                         'bet_multiples、declared_amount_yuan、amount_kind、multiple_type；'\n"
        "                         '明确倍数保留原值，明确金额保留金额，未知写null；'\n"
        "                         '金额字段是该candidate_bets项全部号码的总金额，而不是每注金额；'\n"
        "                         '同一号码重复购买必须逐次表达；不准编造号码、玩法、金额、倍数；'\n"
        "                         '无法确定的子项列入uncertainties，仍返回可确定的候选。')\n",
        'ai_service:统一V266协议')
    source=only_one(source,
        "        inferred = infer_receipt_candidates(request_data)\n",
        "        inferred = (None if request_data.get('use_full_ai_interpretation')\n"
        "                    else infer_receipt_candidates(request_data))\n",
        'ai_service:完整AI模式不能先走本地金额猜测')
    source=only_one(source,
        "        if inferred is not None:\n",
        "        if inferred is not None and not request_data.get('use_full_ai_interpretation'):\n",
        'ai_service:取消本地捷径对真正AI的拦截')
    return source


def ui(source):
    source=only_one(source,
        "        if record.status == 'AI待确认':\n",
        "        if record.status in ('待确认','部分识别','AI待确认'):\n"
        "            def audit():\n"
        "                win.destroy()\n"
        "                self.edit_record(record.record_id)\n"
        "            ttk.Button(actions,text='人工核对／修改后确认',command=audit).pack(side='right',padx=(0,8))\n"
        "        if record.status == 'AI待确认':\n",
        'ui.view_record:添加可见核对入口')
    source=only_one(source,
        '    def edit_record(self):\n        record = self.selected_record()\n',
        '    def edit_record(self, record_id=None):\n'
        '        record = self.store.get(record_id) if record_id else self.selected_record()\n',
        'ui.edit_record:支持从详情窗口直接编辑本条')
    return source


def config(source):
    source=only_one(source,
        'APP_VERSION = "V2.6.5-自动识别与安全重试"',
        'APP_VERSION = "V2.6.6-AI独立识别与证据校验"',
        'config:版本')
    source=only_one(source,
        'ENGINE_VERSION = "V2.6.5-auto-recognize-safe-retry"',
        'ENGINE_VERSION = "V2.6.6-independent-ai-evidence"',
        'config:引擎版本')
    return source

TRANSFORMS={'controller.py':controller,'ai_service.py':ai_service,
            'ui.py':ui,'config.py':config}
