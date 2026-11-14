#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Apply only four guarded UI text hunks; never overwrite complete source files.

Usage:
  python apply_layout_patch.py --root "E:\\Python项目\\3D风险解析" --check
  python apply_layout_patch.py --root "E:\\Python项目\\3D风险解析"
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile


HERE = Path(__file__).resolve().parent
ALLOWED = {'dashboard_v283.py', 'ui.py'}


def sha(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def read_preserving_newlines(path: Path) -> tuple[str, bytes, str, bool]:
    raw = path.read_bytes()
    if b'\r\n' in raw and b'\n' in raw.replace(b'\r\n', b''):
        raise ValueError(path.name + ' 使用混合换行符，为避免重排原文件已停止')
    bom = raw.startswith(b'\xef\xbb\xbf')
    try:
        text = raw.decode('utf-8-sig').replace('\r\n', '\n')
    except UnicodeError as exc:
        raise ValueError(path.name + ' 不是可解析的 UTF-8 源码') from exc
    return text, raw, '\r\n' if b'\r\n' in raw else '\n', bom


def prepare(root: Path, payload: dict) -> tuple[list[tuple[Path, bytes]], list[str]]:
    if not root.is_dir() or not (root / 'dashboard_v280.py').is_file():
        raise ValueError('项目根目录不正确，找不到 dashboard_v280.py')
    files = payload.get('files')
    if not isinstance(files, dict) or set(files) != ALLOWED:
        raise ValueError('补丁文件列表不符合预期')
    updates, already, conflicts = [], [], []
    for name in sorted(ALLOWED):
        target = root / name
        if target.is_symlink() or not target.is_file():
            conflicts.append(name + '（文件缺失或不是普通文件）')
            continue
        try:
            text, raw, newline, bom = read_preserving_newlines(target)
        except (OSError, ValueError) as exc:
            conflicts.append(str(exc))
            continue
        original = text
        hunks = files[name]
        if not isinstance(hunks, list) or not hunks:
            raise ValueError('补丁缺少修改片段：' + name)
        for i, hunk in enumerate(hunks, 1):
            old, new = hunk.get('old'), hunk.get('new')
            if not isinstance(old, str) or not isinstance(new, str) or old == new:
                raise ValueError('补丁修改片段无效：' + name)
            if sha(old) != hunk.get('old_sha256') or sha(new) != hunk.get('new_sha256'):
                raise ValueError('补丁片段校验失败：' + name)
            if text.count(new) == 1:
                continue
            if text.count(old) != 1:
                conflicts.append(f'{name} 的界面片段 {i} 与补丁不一致')
                continue
            text = text.replace(old, new, 1)
        if any(item.startswith(name + ' ') for item in conflicts):
            continue
        if text == original:
            already.append(name)
            continue
        try:
            ast.parse(text, filename=name)
        except SyntaxError as exc:
            conflicts.append(name + ' 修改后语法校验失败：' + str(exc))
            continue
        data = text.replace('\n', newline).encode('utf-8')
        if bom:
            data = b'\xef\xbb\xbf' + data
        if data == raw:
            already.append(name)
        else:
            updates.append((target, data))
    if conflicts:
        raise ValueError('检测到 ' + str(len(conflicts)) + ' 处差异，没有修改任何文件：\n  - '
                         + '\n  - '.join(conflicts))
    return updates, already


def replace_atomically(target: Path, data: bytes) -> None:
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.v283_layout_',
                                         suffix='.tmp', delete=False) as out:
            tmp = Path(out.name)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        if target.exists():
            shutil.copymode(target, tmp)
        os.replace(tmp, target)
        tmp = None
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description='V283 投注添加竖栏和盈亏达标表格局部安全补丁')
    parser.add_argument('--root', required=True, help='3D风险解析项目根目录')
    parser.add_argument('--check', action='store_true', help='只检查，不修改')
    args = parser.parse_args()
    root = Path(args.root).resolve()
    try:
        payload = json.loads((HERE / 'patch_hunks.json').read_text('utf-8'))
        updates, already = prepare(root, payload)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print('CHECK_BLOCKED:', exc)
        return 2
    print(f'需要更新 {len(updates)} 个源码文件；已完成 {len(already)} 个。')
    if args.check:
        print('CHECK_OK：没有修改任何文件。')
        return 0
    if not updates:
        print('ALREADY_APPLIED：界面已是目标版本。')
        return 0
    backup = root / ('v283_layout_backup_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
    if backup.exists():
        print('CHECK_BLOCKED：备份目录已存在，请稍后重试：' + str(backup))
        return 2
    completed = []
    try:
        for target, _data in updates:
            old = backup / target.name
            old.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, old)
        for target, data in updates:
            replace_atomically(target, data)
            completed.append(target)
            if target.read_bytes() != data:
                raise OSError('替换后校验失败：' + target.name)
            print('更新：' + target.name)
    except OSError as exc:
        print('APPLY_FAILED：', exc, '；正在恢复本次已替换文件')
        for target in reversed(completed):
            old = backup / target.name
            try:
                if old.is_file():
                    replace_atomically(target, old.read_bytes())
            except OSError as rollback_error:
                print('回滚失败：', target.name, rollback_error)
        print('备份目录：', backup)
        return 3
    print('APPLY_OK：布局补丁已安装；没有修改投注数据、计算规则或测试文件。')
    print('备份目录：', backup)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
