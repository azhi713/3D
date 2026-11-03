# -*- coding: utf-8 -*-
"""V2.6.8 local guarded transformations. No data or settings mutations."""


def once(source, old, new, label):
    n=source.count(old)
    if n!=1:
        raise RuntimeError('%s: expected one source anchor, got %d; no files changed' % (label,n))
    return source.replace(old,new,1)


PLAN = '''def plan_text(rows, unit_cost=2, lottery='福彩'):
    """Display only. Group identical multiples; leave risk arithmetic unchanged."""
    from collections import defaultdict
    from decimal import Decimal
    groups = defaultdict(list)
    for row in rows:
        if Decimal(str(row.plan_multiplier)) > 0:
            groups[Decimal(str(row.plan_multiplier))].append(str(row.number))
    if not groups:
        return '暂无需要补单的号码'
    prefix = '体' if lottery in ('体彩', '体彩排列三', '排') else '福'
    paragraphs = []
    for multiplier in sorted(groups, reverse=True):
        amount = format(multiplier, 'f').rstrip('0').rstrip('.') if multiplier % 1 else str(int(multiplier))
        numbers = groups[multiplier]
        lines = [prefix+'直选'+amount+'倍']
        lines += ['  '.join(numbers[i:i+12]) for i in range(0,len(numbers),12)]
        paragraphs.append('\\n'.join(lines))
    cost = sum((Decimal(str(row.plan_multiplier)) for row in rows if row.plan_multiplier>0),Decimal(0))
    total_cents = cost * Decimal(cents(unit_cost))
    return '\\n\\n'.join(paragraphs) + '\\n\\n合计：' + money(total_cents) + '元'
'''


def risk_engine(source):
    anchor="def plan_text(rows, unit_cost=2):\n"
    if source.count(anchor)!=1 or not source.endswith("    return '\\n'.join(lines)\n"):
        raise RuntimeError('risk_engine.plan_text boundary mismatch')
    return source[:source.index(anchor)]+PLAN


SCOPE_HELPER='''

def reconcile_terminal_shared_total(source, bets, claimed, receipts):
    """Correct terminal 共N assigned to just the last group by a split parser.

    Strict positive proof: total equals ALL computed bets; the erroneous
    receipt points at the last subgroup only; source contains independent bet
    spans before it. Any ambiguous or mismatching case keeps its review flag.
    Return (claimed, receipts, changed). Never creates/reprices bets.
    """
    if not isinstance(source,str) or not bets or not receipts:
        return claimed,list(receipts or ()),False
    match=re.search(r'(?:[，,。;；\\s])共\\s*[:：]?\\s*(\\d+(?:\\.\\d{1,2})?)\\s*[元米块]\\s*$',source)
    if not match:return claimed,list(receipts),False
    stated=_dec(match.group(1))
    due=sum((_dec(getattr(b,'principal',None)) or Decimal(0) for b in bets),Decimal(0))
    if stated is None or due != stated or stated<=0:
        return claimed,list(receipts),False
    spans=set()
    for bet in bets:
        span=getattr(bet,'source_span',None) or {}
        if type(span.get('start')) is int and type(span.get('end')) is int:
            spans.add((span['start'],span['end']))
    if len(spans)<2:
        return claimed,list(receipts),False
    removed=[];kept=[]
    for receipt in receipts:
        content=str(receipt.get('text') or '')
        bad_scope=(content and re.search(r'(?:[，,。;；\\s])共\\s*[:：]?\\s*'+
                              re.escape(match.group(1))+r'\\s*[元米块]\\s*$',content)
                   and _dec(receipt.get('declared'))==stated
                   and _dec(receipt.get('computed'))!=stated)
        if bad_scope:removed.append(receipt)
        else:kept.append(receipt)
    if len(removed)!=1:
        return claimed,list(receipts),False
    if claimed is not None and _dec(claimed)!=stated:
        return claimed,list(receipts),False
    return float(stated),kept,True
'''


def amount_audit(source):
    if 'def reconcile_terminal_shared_total(' in source:
        raise RuntimeError('scope helper already present; refusing double install')
    anchor="def audit_untyped_tail(raw, bets, claimed, receipts):\n"
    return once(source,anchor,SCOPE_HELPER+'\n'+anchor,'terminal shared total scope')


COUNT_GUARD='''    # Declared ticket count must not turn 3 named selections into 4 bets.
    # Retain raw AI candidate for manual review, but reject automatic ledger entry.
    matched_count=re.search(r'(?:直选|直|单)\\s*(\\d+)\\s*注',region)
    if (matched_count and all(len(code)==3 and code.isdigit() for code in codes)
            and len(codes)<int(matched_count.group(1))):
        raise EvidenceError('声明'+matched_count.group(1)+'注，实际仅'+str(len(codes))+
                            '个号码；AI候选保留待人工确认，不自动入账')
'''


def pipeline(source):
    anchor="    play=_kind(item.get('bet_type') or item.get('play'),codes,region)\n"
    return once(source,anchor,anchor+COUNT_GUARD,'candidate count/amount mismatch safeguard')


CONTROLLER_EDIT_BEFORE='''        record.source_original_text = record.source_original_text or record.raw_text
        record.raw_text = text
        record.received_amount = received
'''

CONTROLLER_EDIT_AFTER='''        record.source_original_text = record.source_original_text or record.raw_text
        # An unchanged text is a review action, not a new bet or new AI request.
        # Do not wipe the model's earlier structured explanation on save.
        if record.raw_text == text and record.received_amount == received:
            record.revision += 1  # Invalidate old in-flight AI result.
            if record.status != '已识别':
                record.review_status = 'manual_review_pending'
            record.ai_request_state = 'manual_saved'
            record.ai_history.append({'action':'manual_review_saved_unchanged',
                                      'at':now().isoformat()})
            self.store.update(record, '人工核对原文未更改：保留已有候选与明细')
            return
        record.raw_text = text
        record.received_amount = received
'''

CONTROLLER_RECEIPT_BEFORE='''        receipt_rows = list(getattr(parser, 'last_receipts', []) or [])
        audit_note = None
'''
CONTROLLER_RECEIPT_AFTER='''        receipt_rows = list(getattr(parser, 'last_receipts', []) or [])
        from amount_audit_v264 import reconcile_terminal_shared_total
        claimed, receipt_rows, terminal_shared_total = reconcile_terminal_shared_total(
            text, bets, claimed, receipt_rows)
        if terminal_shared_total:
            # Parser's prior line warning was caused by the same false scope.
            parser.last_warnings = [w for w in getattr(parser,'last_warnings',[]) if
                 not str(w).startswith('行金额不一致：')]
        audit_note = None
'''


def controller(source):
    source=once(source,CONTROLLER_EDIT_BEFORE,CONTROLLER_EDIT_AFTER,
                'manual review unchanged text retains AI candidate')
    source=once(source,CONTROLLER_RECEIPT_BEFORE,CONTROLLER_RECEIPT_AFTER,
                'scope correction before line mismatch audit')
    return source


UI_MENU_BEFORE="            ('结构化 JSON', self.export_contract_json),\n"
UI_MENU_AFTER="            ('结构化 JSON', self.export_contract_json),\n            ('识别诊断 JSON（含AI历史）', self.export_diagnostics_json),\n"

UI_REFRESH_BEFORE='''            text = plan_text(self.snapshot.risks, self.controller.settings['direct_unit_cost'][lottery])
            if self.snapshot.pending:
                text = '当前统计不完整，以下仅供核对已验证部分，不能作为最终补单方案。\\n'+text
'''
UI_REFRESH_AFTER='''            text = plan_text(self.snapshot.risks,
                             self.controller.settings['direct_unit_cost'][lottery], lottery)
            if self.snapshot.pending:
                text = ('[仅供人工核对 / 不可作为正式补单依据]\\n'
                        '当前统计不完整，存在未确认投注；不得直接按此方案下注。\\n\\n'+text)
'''

UI_COPY_BEFORE="            self.copy_button.configure(state='normal' if not self.snapshot.pending and any(r.plan_multiplier for r in self.snapshot.risks) else 'disabled')\n"
UI_COPY_AFTER="            self.copy_button.configure(text=('复制核对草稿' if self.snapshot.pending else '一键复制'),\n                                       state='normal' if any(r.plan_multiplier for r in self.snapshot.risks) else 'disabled')\n"

UI_COPY_HANDLER='''    def copy_plan(self):
        if not self.snapshot or self._risk_running or self._risk_next:
            return
        if not any(r.plan_multiplier for r in self.snapshot.risks):
            return
        # Copy the displayed text, never a hidden unmarked official plan.
        # On unresolved orders this contains the mandatory draft warning.
        text = self.plan.get('1.0', 'end-1c')
        if self.snapshot.pending and '[仅供人工核对 / 不可作为正式补单依据]' not in text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update_idletasks()
        self.copy_button.configure(text='已复制')
        if self._copy_timer:
            try:
                self.after_cancel(self._copy_timer)
            except tk.TclError:
                pass
        self._copy_timer = self.after(1500, self._reset_copy_button)

'''

UI_COPY_HANDLER_OLD='''    def copy_plan(self):
        if not self.snapshot or self.snapshot.pending or self._risk_running or self._risk_next:
            return
        self.clipboard_clear()
        self.clipboard_append(plan_text(self.snapshot.risks, self.controller.settings['direct_unit_cost'][self.lottery.get()]))
        self.update_idletasks()
        self.copy_button.configure(text='已复制')
        if self._copy_timer:
            try:
                self.after_cancel(self._copy_timer)
            except tk.TclError:
                pass
        self._copy_timer = self.after(1500, self._reset_copy_button)

'''

DIAGNOSTIC_METHOD='''    def export_diagnostics_json(self):
        """Read-only diagnosis of current period & lottery, no API keys/settings."""
        destination = filedialog.asksaveasfilename(
            parent=self, title='导出识别诊断（AI候选、人工修订记录）',
            defaultextension='.json',
            initialfile=f'{self.lottery.get()}_{self.controller.period}_识别诊断.json',
            filetypes=[('JSON', '*.json')])
        if not destination:
            return
        try:
            records=[r for r in self.controller.records()
                     if r.lottery==self.lottery.get()]
            messages=[]
            for record in records:
                history=[]
                for row in self.store.record_history(record.record_id):
                    before=row.get('before') or {}
                    after=row.get('after') or {}
                    history.append({'timestamp':row.get('timestamp'),
                                    'action':row.get('action'),
                                    'before_revision':before.get('revision'),
                                    'after_revision':after.get('revision'),
                                    'before_status':before.get('status'),
                                    'after_status':after.get('status'),
                                    'before_bets':before.get('bets',[]),
                                    'after_bets':after.get('bets',[]),
                                    'before_ai_candidate':before.get('ai_candidate',''),
                                    'after_ai_candidate':after.get('ai_candidate','')})
                messages.append({'record_id':record.record_id,
                    'received_at':record.created_at,
                    'raw_text':record.source_original_text or record.raw_text,
                    'current_text':record.raw_text,
                    'parse_status':record.parse_status,
                    'review_status':record.review_status,
                    'ai_request_state':record.ai_request_state,
                    'status':record.status,
                    'claimed_amount':record.claimed_amount,
                    'computed_amount_yuan':record.computed_amount_yuan,
                    'current_bets':[b.to_dict() for b in record.bets],
                    'pending_fragments':record.pending_fragments,
                    'issues':record.issues,
                    'note':record.note,
                    'ai_candidate':record.ai_candidate,
                    'ai_history':record.ai_history,
                    'record_revisions':history})
            result={'schema_version':'v268-diagnostics-1',
                    'lottery':self.lottery.get(),
                    'period':self.controller.period,
                    'messages':messages,
                    'notice':'诊断包含原文及发送内容，请确认没有不希望分享的隐私信息。'}
            with open(destination,'w',encoding='utf-8') as handle:
                json.dump(result,handle,ensure_ascii=False,indent=2)
            messagebox.showinfo('导出完成',
                f'识别诊断已导出 {len(messages)} 条。发送前请检查原文中的隐私信息。', parent=self)
        except (OSError,ValueError,KeyError,sqlite3.Error) as exc:
            self.fail(exc)

'''



LOCAL_RECHECK_METHOD = '''    def verify_selected_locally(self):
        """Explicit local revalidation; preserve candidate and never enqueue AI."""
        records=self.selected_records()
        if not records:
            return
        if any(r.record_id in self.ai_busy for r in records):
            messagebox.showinfo('任务正在处理','有AI请求正在执行，不能同时重核。',parent=self)
            return
        if not messagebox.askyesno('仅本地重核',
                f'用当前本地规则重算 {len(records)} 条；不重新发送AI，保留历史与实收。确定继续？',
                parent=self):
            return
        try:
            for old in records:
                current=self.store.get(old.record_id)
                if not current:continue
                current.revision+=1
                self.controller.parse_record(current)
                current.ai_request_state='manual_saved'
                if current.status!='已识别' and current.review_status=='manually_confirmed':
                    current.review_status='manual_review_pending'
                current.ai_history.append({'action':'explicit_local_recheck_no_ai',
                                           'at':now().isoformat()})
                self.store.update(current,'仅本地重新核算（不发送AI）')
            self.refresh()
        except (ValueError,KeyError,ArithmeticError,sqlite3.Error) as exc:
            self.fail(exc)

''' 

def ui(source):
    source=once(source,UI_MENU_BEFORE,UI_MENU_AFTER,'add diagnostics export menu')
    source=once(source,UI_REFRESH_BEFORE,UI_REFRESH_AFTER,'draft plan safety text')
    source=once(source,UI_COPY_BEFORE,UI_COPY_AFTER,'enable explicit draft copy')
    source=once(source,UI_COPY_HANDLER_OLD,UI_COPY_HANDLER,'copy only visible marked preview')
    source=once(source,"        self.copy_button.configure(text='一键复制')\n",
                "        self.copy_button.configure(text=('复制核对草稿' if self.snapshot and self.snapshot.pending else '一键复制'))\n",
                'restore draft/official copy label')
    source=once(source,'    def export_contract_json(self):\n',
                DIAGNOSTIC_METHOD+'    def export_contract_json(self):\n',
                'new diagnosis export action')
    source=once(source,"('导出结构化 JSON', self.export_contract_json),\n",
                "('导出结构化 JSON', self.export_contract_json),\n                              ('导出识别诊断 JSON', self.export_diagnostics_json),\n",
                'diagnostics in settings')
    source=once(source,"'unknown_billing':'网络超时，计费不明'}.get(record.ai_request_state,'未运行')",
                "'unknown_billing':'网络超时，计费不明','manual_saved':'人工保存，未重新调用AI'}.get(record.ai_request_state,'未运行')",
                'display manual_saved status')
    source=once(source,
        "        menu.add_command(label='重新识别选中数据'+self.shortcut_label('reparse'), command=self.reparse_selected)\n",
        "        menu.add_command(label='重新识别选中数据'+self.shortcut_label('reparse'), command=self.reparse_selected)\n"
        "        menu.add_command(label='仅本地重核所选（不调用AI）', command=self.verify_selected_locally)\n",
        'add explicit local-only revalidation')
    source=once(source,'    def reparse_record(self):\n',
                LOCAL_RECHECK_METHOD+'    def reparse_record(self):\n',
                'local-only revalidation handler')
    return source


def config(source):
    source=once(source,'APP_VERSION = "V2.6.7-识别证据与人工审核修复"',
        'APP_VERSION = "V2.6.8-补单分组_核对草稿_诊断导出"','version')
    source=once(source,'ENGINE_VERSION = "V2.6.7-ai-evidence-manual-review"',
        'ENGINE_VERSION = "V2.6.8-safe-plan-and-review"','engine version')
    return source


TARGETS={'risk_engine.py':risk_engine,'amount_audit_v264.py':amount_audit,
         'ai_pipeline_v266.py':pipeline,'controller.py':controller,
         'ui.py':ui,'config.py':config}

# Strict, receipt-backed shorthand families: opt in only when the ENTIRE
# message matches and configured parser prices sum to the named total.
SHORT_ORDER_HELPERS = r'''
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

'''


def recognition_amount(source):
    # Match one exact, source-backed input. All prices come from parser._make.
    anchor='def parse_amount_v264(parser,lottery,text):\n'
    source=once(source,anchor,SHORT_ORDER_HELPERS+'\n'+anchor,
                'receipt-backed shorthand recognition helper')
    anchor='''    normalized=parser.normalize_text(text)
    batch=BATCH.fullmatch(raw)
'''
    return once(source,anchor,
        '''    normalized=parser.normalize_text(text)
    exact=_v268_exact_amount_groups(parser,lottery,raw,text)
    if exact is not None:
        return exact
    batch=BATCH.fullmatch(raw)
''','exact shorthand before generic batch interpretation')

TARGETS['recognition_amount_v264.py']=recognition_amount
