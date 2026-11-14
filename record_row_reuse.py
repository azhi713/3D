"""Bounded reuse of hidden record widgets during search; Tk main thread only."""
def retire(table,ids):
    pool=getattr(table,'_recycled_record_rows',None)
    if pool is None:
        pool=table._recycled_record_rows={}
    for iid in ids:
        row=table._rows.pop(iid)
        var=table._vars.pop(iid)
        var.set(False)
        table.canvas.itemconfigure(row['window'],state='hidden')
        pool[iid]=(row,var)
        if iid in table._selected:table._selected.remove(iid)
        if iid==table._focused_iid:table._focused_iid=None
    while len(pool)>3000:
        row,var=pool.pop(next(iter(pool)))
        table.canvas.delete(row['window'])
        row['frame'].destroy()
    if not table._rows:
        table.canvas.yview_moveto(0)
        table.canvas.configure(scrollregion=(0,0,0,0))
    table._sync_checks()

def restore(table,iid,values,tags):
    pool=getattr(table,'_recycled_record_rows',{})
    cached=pool.pop(iid,None)
    if cached is None:return False
    row,var=cached
    table._rows[iid]=row
    table._vars[iid]=var
    table.update_row(iid,values,tags)
    table._paint(iid,False)
    table.canvas.itemconfigure(row['window'],state='normal')
    return True
