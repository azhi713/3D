# -*- coding: utf-8 -*-
"""V2.6.8 source-only local upgrade, with guarded preflight/backup/rollback."""
from __future__ import annotations
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from patch_logic_v268 import TARGETS

BASELINE='168ea5d7510d8a8789e1d64ab8c06955df9943b1'
UNMODIFIED_BLOBS={
  'risk_engine.py':'947e1f4792b0da47d295a17c5ff64bc1f97fc6ef',
  'amount_audit_v264.py':'4e295f9a3cdf873864a34965c29dd1cb6df0aa89',
  'recognition_amount_v264.py':'c2080ff02a6e37a0a4cbf49d1ea193bf230ce7e2',
}
V267_MARKERS={
  'ai_pipeline_v266.py':'# V267-AI-LEDGER-20261103',
  'controller.py':'# V267-CONTROLLER-20261103',
  'ui.py':'# V267-UI-20261103',
  'config.py':'V2.6.7-ai-evidence-manual-review',
}


def sha(blob:bytes)->str:
    return hashlib.sha256(blob).hexdigest()


def git_blob(blob:bytes)->str:
    return hashlib.sha1(b'blob '+str(len(blob)).encode()+b'\0'+blob).hexdigest()


def atomic(path:Path,bytes_data:bytes):
    fd,tmp=tempfile.mkstemp(prefix='.v268_',suffix='.tmp',dir=str(path.parent))
    try:
        with os.fdopen(fd,'wb') as handle:
            handle.write(bytes_data)
            handle.flush();os.fsync(handle.fileno())
        if path.exists():shutil.copymode(path,tmp)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def prepare(root:Path):
    if not root.is_dir():raise RuntimeError('项目目录不存在：'+str(root))
    config=root/'config.py'
    if not config.is_file() or 'V2.6.7-' not in config.read_text(encoding='utf-8-sig'):
        raise RuntimeError('需要已安装且生效的 V2.6.7，版本不一致；拒绝覆盖')
    before={}; after={}
    for name,transform in TARGETS.items():
        path=root/name
        if not path.is_file():raise RuntimeError('缺少源码文件：'+name)
        raw=path.read_bytes()
        if raw.startswith(b'\xef\xbb\xbf'):
            raise RuntimeError(name+' UTF-8 BOM与校验基线不符，拒绝覆盖')
        normalized=raw.replace(b'\r\n',b'\n')
        if name in UNMODIFIED_BLOBS:
            expected=UNMODIFIED_BLOBS[name]
            if expected not in (git_blob(raw),git_blob(normalized)):
                raise RuntimeError(name+' 非GitHub基线 '+BASELINE[:8]+'；拒绝覆盖')
        else:
            marker=V267_MARKERS[name]
            if marker.encode() not in normalized:
                raise RuntimeError(name+' 未找到V2.6.7完整安装标记，拒绝覆盖')
        try:
            text=normalized.decode('utf-8')
            changed=transform(text)
            if text==changed:raise RuntimeError(name+' 补丁没有产生改变')
            compile(changed,name,'exec')
        except (UnicodeError,RuntimeError,ValueError,SyntaxError) as e:
            raise RuntimeError(name+' 预检失败：'+str(e)) from e
        line_end='\r\n' if b'\r\n' in raw else '\n'
        before[name]=raw
        after[name]=changed.replace('\n',line_end).encode('utf-8')
    return before,after


def stage_checks(project:Path,modified:dict):
    # No project data, settings, secrets or DB is copied into stage.
    with tempfile.TemporaryDirectory(prefix='v268_src_only_') as directory:
        stage=Path(directory)
        for file in project.glob('*.py'):
            shutil.copy2(file,stage/file.name)
        for name,blob in modified.items():
            (stage/name).write_bytes(blob)
        for file in stage.glob('*.py'):
            compile(file.read_text(encoding='utf-8'),str(file),'exec')
        env=os.environ.copy()
        env.update(PYTHONDONTWRITEBYTECODE='1',PYTHONNOUSERSITE='1',
                   V268_STAGE_ROOT=str(stage),
                   PYTHONPATH=os.pathsep.join([str(stage),str(HERE/'tests'),str(HERE)]))
        tasks=[
          ([sys.executable,'-c',
             'import risk_engine,amount_audit_v264,ai_pipeline_v266,controller,parser,ui,config;'
             'assert "V2.6.8" in config.APP_VERSION'],stage),
          ([sys.executable,'-m','unittest','discover','-s',str(HERE/'tests'),
              '-p','test_*.py','-v'],HERE/'tests'),
        ]
        for command,cwd in tasks:
            result=subprocess.run(command,cwd=str(cwd),env=env,
                stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                encoding='utf-8',errors='replace',timeout=150)
            print(result.stdout[-10000:])
            if result.returncode:
                raise RuntimeError('隔离源码导入/回归测试失败，源文件尚未修改')


def install(root:Path,check_only=False):
    print('版本：V2.6.8；只读GitHub基线：'+BASELINE)
    print('目标目录：'+str(root))
    print('范围：7个Python源码；不访问或修改data、用户配置、赔率和历史记录。')
    before,after=prepare(root)
    stage_checks(root,after)
    print('[OK] 源码兼容与离线测试通过。')
    if check_only:
        print('[OK] 预检模式：没有修改任何文件。')
        return
    stamp=datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    folder=root/'_patch_backups'/('v268_'+stamp)
    folder.mkdir(parents=True,exist_ok=False)
    meta={'version':'V2.6.8','source_git_commit':BASELINE,'data_modified':False,
          'files':{}}
    for name,blob in before.items():
        (folder/name).write_bytes(blob)
        meta['files'][name]={'before_sha256':sha(blob),'after_sha256':sha(after[name])}
    (folder/'MANIFEST_V268.json').write_text(json.dumps(meta,indent=2,ensure_ascii=False),encoding='utf8')
    written=[]
    try:
        for name,blob in after.items():
            atomic(root/name,blob)
            written.append(name)
        stage_checks(root,{})
    except BaseException:
        for name in reversed(written):
            current=root/name
            if current.is_file() and sha(current.read_bytes())==sha(after[name]):
                atomic(current,before[name])
        print('[ERROR] 安装校验失败，已尝试自动恢复；备份文件：'+str(folder))
        raise
    print('[OK] V2.6.8本地升级成功。备份目录：'+str(folder))
    print('[OK] GitHub与data/未修改。')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',default=str(HERE.parent),help='项目目录')
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    try:install(Path(args.root).resolve(),args.check)
    except Exception as e:
        print('[ERROR] '+str(e),file=sys.stderr)
        raise SystemExit(1)

if __name__=='__main__':main()
