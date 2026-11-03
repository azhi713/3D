# -*- coding: utf-8 -*-
"""Restore an exact V2.6.6 backup; refuse to overwrite later edits."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from install_v266 import atomic

def sha(data):return hashlib.sha256(data).hexdigest()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument('--backup',default=None)
    args=parser.parse_args()
    root=Path(args.root).resolve()
    parent=root/'_patch_backups'
    candidates=sorted(parent.glob('v266_*'),reverse=True)
    path=Path(args.backup).resolve() if args.backup else (candidates[0] if candidates else None)
    if not path or not (path/'manifest.json').is_file():
        raise RuntimeError('没有找到V2.6.6备份清单')
    manifest=json.loads((path/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('version')!='V2.6.6':raise RuntimeError('备份版本不匹配')
    # Check ALL files before changing any of them.
    for name,item in manifest['files'].items():
        target=root/name
        if not target.is_file() or sha(target.read_bytes())!=item['after_sha256']:
            raise RuntimeError(name+' 自升级后已改变，拒绝自动回滚以免覆盖新修改')
        before=item['before_sha256']
        if before and sha((path/name).read_bytes())!=before:
            raise RuntimeError('备份文件校验失败：'+name)
    for name,item in manifest['files'].items():
        if item['before_sha256']:
            atomic(root/name,(path/name).read_bytes())
        else:
            (root/name).unlink()
    print('✓ 已恢复到升级前源码；不修改data及历史记录。')

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('✗ 回滚停止：'+str(exc));sys.exit(1)
