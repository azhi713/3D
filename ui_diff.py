"""稳定ID的控件差量同步，不销毁未变化行。仅由Tk主线程调用。"""
def sync_tree(tree, rows):
    current=list(tree.get_children())
    desired=[str(row[0]) for row in rows]
    wanted=set(desired)
    selected=tree.selection();focus=tree.focus()
    view=tree.yview()
    counts={'insert':0,'update':0,'delete':0,'move':0}
    for iid in current:
        if iid not in wanted:tree.delete(iid);counts['delete']+=1
    existing=set(tree.get_children())
    for index,(iid,values,tags) in enumerate(rows):
        iid=str(iid)
        if iid not in existing:
            tree.insert('','end',iid=iid,values=values,tags=tags);counts['insert']+=1
        else:
            item=tree.item(iid)
            if tuple(map(str,item.get('values',())))!=tuple(map(str,values)) or tuple(item.get('tags',()))!=tuple(tags):
                tree.item(iid,values=values,tags=tags);counts['update']+=1
        if tree.index(iid)!=index:tree.move(iid,'',index);counts['move']+=1
    retained=[i for i in selected if i in wanted]
    if tuple(retained)!=tree.selection():tree.selection_set(retained)
    if focus in wanted:tree.focus(focus)
    if view and current and counts['move']==0 and counts['insert']==counts['delete']==0:tree.yview_moveto(view[0])
    tree.last_diff=counts
    return counts
