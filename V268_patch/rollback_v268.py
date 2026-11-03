# -*- coding: utf-8 -*-
"""Checksum-guarded V2.6.8 rollback. Does not touch data/ or other files."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from install_v268 import atomic


def sha(content):return hashlib.sha256(content).hexdigest()


def restore(root,backup):
    doc=json.loads((backup/'MANIFEST_V268.json').read_text(encoding='utf8'))
    if doc.get('version')!='V2.6.8':raise ValueError('备份不是V2.6.8')
    changes={}
    for name,entry in doc['files'].items():
        source=backup/name
        target=root/name
        if not source.is_file() or not target.is_file():
            raise ValueError('缺少备份或待回滚文件：'+name)
        old=source.read_bytes();now=target.read_bytes()
        if sha(old)!=entry['before_sha256']:
            raise ValueError('备份文件校验失败：'+name)
        if sha(now)!=entry['after_sha256']:
            raise ValueError('已发生新修改，禁止覆盖：'+name)
        changes[name]=old
    for name,old in changes.items():atomic(root/name,old)
    print('[OK] V2.6.8源码已恢复安装前状态。data及历史未修改。')


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--root',default=str(Path(__file__).resolve().parent.parent))
    p.add_argument('--backup')
    args=p.parse_args()
    root=Path(args.root).resolve()
    if args.backup:
        backup=Path(args.backup).resolve()
    else:
        all_backups=sorted((root/'_patch_backups').glob('v268_*'),reverse=True)
        if not all_backups:raise ValueError('找不到V2.6.8的回滚备份')
        backup=all_backups[0]
    restore(root,backup)

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('[ERROR] '+str(exc),file=sys.stderr)
        raise SystemExit(1)
