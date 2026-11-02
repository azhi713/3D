# -*- coding: utf-8 -*-
"""Rollback only the exact V2.6.5 source bytes recorded in backup manifest."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
from patch_logic_v265 import TAG


def rollback(project,backup):
    manifest_path=backup/'MANIFEST_V265.json'
    if not manifest_path.is_file():raise RuntimeError('备份目录缺少MANIFEST_V265.json')
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('tag')!=TAG or manifest.get('data_modified') is not False:
        raise RuntimeError('备份标记不匹配，拒绝恢复')
    for name in manifest['updated']:
        target=project/name
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest()!=manifest['sha256'][name]:
            raise RuntimeError('文件已被其他版本或用户修改，禁止覆盖：'+name)
    for name in manifest['updated']:
        old=backup/name
        target=project/name
        if old.is_file():shutil.copy2(old,target)
        else:target.unlink()
    print('V2.6.5源码已恢复至升级前状态。数据库及用户设置均未修改。')

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('project',help='项目根目录')
    parser.add_argument('backup',help='V2.6.5备份目录')
    args=parser.parse_args()
    try:rollback(Path(args.project).resolve(),Path(args.backup).resolve())
    except Exception as exc:
        print('回滚未执行：'+str(exc),file=sys.stderr);raise SystemExit(1)
