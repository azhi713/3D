"""RiskApp UI mixin: in-window keyboard shortcuts, capture UI, and scope checks."""
import tkinter as tk
from tkinter import ttk, messagebox
from shortcut_v261 import DEFAULT_SHORTCUTS, SHORTCUT_GROUPS, SHORTCUT_LABELS, validate, from_event

class ShortcutMixin:
    def _install_shortcuts(self):
        self._shortcut_map=validate(self.controller.settings.get('shortcuts',{}))
        # One handler rather than dozens of overlapping bind_all callbacks.
        self.bind_all('<KeyPress>',self._dispatch_shortcut,add='+')
        # Intercept Ctrl+Enter on the input widget before native Text editing.
        self.input.bind('<Control-Return>',self._dispatch_shortcut,add='+')

    def _reload_shortcuts(self):
        self._shortcut_map=validate(self.controller.settings.get('shortcuts',{}))

    def shortcut_label(self,action):
        binding=getattr(self,'_shortcut_map',{}).get(action,DEFAULT_SHORTCUTS.get(action,''))
        return f'  ({binding.replace("+"," + ")})' if binding else ''

    def _is_descendant(self,widget,ancestor):
        if widget is None or ancestor is None:return False
        cursor=widget
        while cursor is not None:
            if cursor is ancestor:return True
            try:
                name=cursor.winfo_parent()
                cursor=cursor.nametowidget(name) if name else None
            except tk.TclError:return False
        return False

    def _dispatch_shortcut(self,event):
        if getattr(self,'_closing',False):return None
        try:
            focus=self.focus_get()
            # Prevent shortcuts from bypassing the modal detail window,
            # the settings dialog, or independent popup windows.
            if focus is None or focus.winfo_toplevel() is not self:
                return None
            key=from_event(event)
            if not key:return None
            action=next((name for name,combo in self._shortcut_map.items() if combo==key),None)
            if not action:return None
            editing=isinstance(focus,(tk.Text,tk.Entry,ttk.Entry,ttk.Combobox,tk.Spinbox))
            tab=self.workspace_tabs.index(self.workspace_tabs.select())
            in_table=self._is_descendant(focus,self.records_tree)
            if action in ('lottery_fc','lottery_tc'):
                self.lottery.set('福彩' if action=='lottery_fc' else '体彩');self.refresh()
            elif action in ('tab_records','tab_risk','tab_draw'):
                self.workspace_tabs.select({'tab_records':0,'tab_risk':1,'tab_draw':2}[action])
            elif action=='toggle_wechat':self.toggle_wechat()
            elif action=='toggle_ai':self.toggle_ai()
            elif action=='search_records':
                if tab!=0:self.workspace_tabs.select(0)
                self.record_search_entry.focus_set()
                self.record_search_entry.selection_range(0,'end')
            elif action=='add_bet':
                if focus is not self.input:return None
                self.add_manual()
            elif action in ('select_all','invert','clear_selection','select_pending',
                            'reparse','ai_convert','copy_records','open_details','delete_records'):
                # Native Ctrl+A/C editing must always work inside user fields.
                # All table hotkeys require the table to have keyboard focus.
                if editing or tab!=0 or not in_table:return None
                if action=='select_all':self.records_tree.select_all()
                elif action=='invert':self.records_tree.invert_selection()
                elif action=='clear_selection':self.records_tree.clear_selection()
                elif action=='select_pending':self.select_record_status(False)
                elif action=='reparse':self.reparse_selected()
                elif action=='ai_convert':self.ai_selected()
                elif action=='copy_records':self.copy_selected_raw()
                elif action=='open_details':self.view_record()
                elif action=='delete_records':self.delete_selected()
                self._update_record_selection_count()
            else:return None
            return 'break'
        except (tk.TclError,ValueError,KeyError) as exc:
            try:self.add_feedback.configure(text='快捷键执行失败：'+str(exc),foreground='#b45309')
            except Exception:pass
            return 'break'

    def _make_shortcut_settings(self,page,settings):
        """Build shortcut editing controls and return a validated save callback."""
        draft=validate(settings.get('shortcuts',{}))
        ttk.Label(page,text='点击任意一行的快捷键输入框，直接按下组合键。'
                       '删除按钮可清空绑定；不使用Windows全局快捷键。',wraplength=700).pack(anchor='w',pady=(2,10))
        all_vars={}
        for group,keys in SHORTCUT_GROUPS.items():
            frame=ttk.LabelFrame(page,text=group,padding=(10,8));frame.pack(fill='x',pady=(0,8))
            for action in keys:
                row=ttk.Frame(frame);row.pack(fill='x',pady=2)
                ttk.Label(row,text=SHORTCUT_LABELS[action],width=24).pack(side='left')
                var=tk.StringVar(value=draft.get(action,''));all_vars[action]=var
                editor=ttk.Entry(row,textvariable=var,width=25,state='readonly',takefocus=1)
                editor.pack(side='left',padx=(3,8))
                def capture(event,key=action):
                    captured=from_event(event)
                    if captured is None:return 'break'
                    proposed={k:v.get() for k,v in all_vars.items()}
                    proposed[key]=captured
                    try:validate(proposed)
                    except ValueError as error:
                        messagebox.showwarning('快捷键冲突或无效',str(error),parent=page.winfo_toplevel())
                    else:all_vars[key].set(captured)
                    return 'break'
                # Capture by key, without the usual readonly-entry editing.
                editor.bind('<KeyPress>',capture)
                ttk.Button(row,text='清除',command=lambda k=action:all_vars[k].set('')).pack(side='left')
        def reset():
            if not messagebox.askyesno('恢复快捷键','确定将所有快捷键恢复为默认设置？',parent=page.winfo_toplevel()):return
            for action,combo in DEFAULT_SHORTCUTS.items():all_vars[action].set(combo)
        ttk.Button(page,text='恢复全部默认快捷键',command=reset).pack(anchor='w',pady=(3,7))
        def get_values():return validate({key:var.get() for key,var in all_vars.items()})
        return get_values
