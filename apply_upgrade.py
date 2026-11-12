#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""福彩3D V283 增量补丁：先验证、逐文件备份、再原子替换。

使用：python apply_upgrade.py --root "E:\\Python项目\\3D风险解析"
预检：python apply_upgrade.py --root "E:\\Python项目\\3D风险解析" --check

默认仅接受所依据的 GitHub 基线版本或已安装的相同补丁文件；
如检测到本地自行修改，会拒绝覆盖并显示文件名。
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile


PATCH_ROOT = Path(__file__).resolve().parent


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def prepare(root: Path, manifest: dict, allow_modified: bool) -> list[tuple[Path, Path, str]]:
    if not root.is_dir() or not all((root / item).is_file()
                                     for item in ('ui.py', 'parser.py', 'dashboard_v283.py')):
        raise ValueError('项目目录无效：应指定包含 ui.py、parser.py 的 3D风险解析项目根目录')
    actions = []
    for name, hashes in sorted(manifest['files'].items()):
        rel = Path(name)
        if rel.is_absolute() or '..' in rel.parts:
            raise ValueError('补丁内发现不安全路径：' + name)
        source = PATCH_ROOT / 'files' / rel
        target = root / rel
        if not source.is_file() or digest(source) != hashes['new_sha256']:
            raise ValueError('补丁文件校验失败：' + name)
        if target.exists() and not target.is_file():
            raise ValueError('目标不是文件：' + name)
        if target.is_file():
            current = digest(target)
            if current == hashes['new_sha256']:
                continue
            if current != hashes['base_sha256'] and not allow_modified:
                raise ValueError('本地文件与补丁基线不同，已停止以保护现有修改：' + name)
        elif hashes['base_sha256'] is not None and not allow_modified:
            raise ValueError('原有代码文件缺失，已停止：' + name)
        actions.append((source, target, name))
    return actions


def replace_atomically(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, dir=target.parent,
                                         prefix='.v283_patch_', suffix='.tmp') as handle:
            tmp = Path(handle.name)
        shutil.copyfile(source, tmp)
        os.replace(tmp, target)
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description='福彩3D V283 增量升级补丁（自动备份）')
    parser.add_argument('--root', required=True, help='现有 3D风险解析 源码项目的根目录')
    parser.add_argument('--check', action='store_true', help='仅校验，不替换任何文件')
    parser.add_argument('--allow-modified', action='store_true',
                        help='允许覆盖与基线不同的文件（仍自动备份；不建议常规使用）')
    args = parser.parse_args()
    root = Path(args.root).resolve()
    manifest = json.loads((PATCH_ROOT / 'manifest.json').read_text(encoding='utf-8'))
    try:
        actions = prepare(root, manifest, args.allow_modified)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print('CHECK_BLOCKED:', exc)
        return 2
    print('基线：', manifest.get('base_commit', 'unknown'))
    print(f'需要替换：{len(actions)}个文件；已是目标版本的文件会跳过。')
    if args.check or not actions:
        print('CHECK_OK：没有修改文件。' if args.check else 'ALREADY_APPLIED：无需重复升级。')
        return 0
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup = root / ('v283_upgrade_backup_' + stamp)
    while backup.exists():
        stamp += '_1'
        backup = root / ('v283_upgrade_backup_' + stamp)
    completed = []
    try:
        # All originals are saved before the first source file is replaced.
        for _source, target, name in actions:
            if target.is_file():
                old = backup / name
                old.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, old)
        for source, target, name in actions:
            replace_atomically(source, target)
            completed.append((target, name))
            print('更新：', name)
    except OSError as exc:
        print('APPLY_FAILED：', exc, '；正在恢复本次已经替换的文件。')
        for target, name in reversed(completed):
            old = backup / name
            try:
                if old.is_file():
                    replace_atomically(old, target)
                elif target.is_file():
                    target.unlink()  # Only a new file created by this run.
            except OSError as rollback_error:
                print('回滚未完成：', name, rollback_error)
        print('备份位置：', backup)
        return 3
    print('APPLY_OK：增量补丁已安装。')
    print('备份位置：', backup, '；原投注数据和数据库未修改。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
