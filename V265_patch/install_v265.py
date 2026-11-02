# -*- coding: utf-8 -*-
"""V2.6.5 local-source installer. Source-only, guarded preflight and rollback."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from patch_logic_v265 import update_parser,update_ui,update_config,update_controller,TAG

ROOT=Path(__file__).resolve().parent
TARGETS={'controller.py':update_controller,'parser.py':update_parser,'ui.py':update_ui,'config.py':update_config}
MODULES={'recognition_v265.py','ai_retry_v265.py'}


def prepare(project):
    changes={}
    for name,fn in TARGETS.items():
        path=project/name
        if not path.is_file():raise RuntimeError('项目缺少源码：'+str(path))
        original=path.read_text(encoding='utf-8')
        changed=fn(original)
        compile(changed,str(path),'exec')
        if changed!=original:changes[name]=changed
    for name in MODULES:
        content=(ROOT/'modules'/name).read_text(encoding='utf-8')
        compile(content,name,'exec')
        dest=project/name
        if dest.is_file():
            if dest.read_text(encoding='utf-8')!=content:
                raise RuntimeError('发现同名但内容不同的已有模块，拒绝覆盖：'+name)
        else:changes[name]=content
    status=[bool(name in changes) for name in TARGETS]
    if any(status) and not all(status):
        raise RuntimeError('检测到V2.6.5部分安装或源码版本不一致，拒绝覆盖')
    return changes


def preflight(project,changes):
    with tempfile.TemporaryDirectory(prefix='v265_preflight_') as temp:
        work=Path(temp)
        for file in project.glob('*.py'):shutil.copy2(file,work/file.name)
        for name,content in changes.items():(work/name).write_text(content,encoding='utf-8')
        for file in work.glob('*.py'):
            compile(file.read_text(encoding='utf-8'),str(file),'exec')
        env=dict(os.environ, PYTHONPATH=str(work),PYTHONNOUSERSITE='1',PYTHONDONTWRITEBYTECODE='1')
        cmds=[
            [sys.executable,'-c','import config,parser,controller,ui,recognition_v265,ai_retry_v265; assert "V2.6.5" in config.APP_VERSION'],
            [sys.executable,'-m','unittest','discover','-s',str(ROOT/'tests'),'-p','test_v265_*.py','-v'],
        ]
        for cmd in cmds:
            res=subprocess.run(cmd,cwd=work,env=env,stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE,text=True,timeout=120)
            if res.returncode:
                raise RuntimeError('安装前检查失败：\n'+(res.stdout+'\n'+res.stderr)[-8000:])


def install(project,check=False):
    changes=prepare(project)
    preflight(project,changes)
    print('V2.6.5源码编译、模块导入、自动识别与安全重试测试通过。')
    print('计划修改：'+(', '.join(changes) if changes else '无（已安装）'))
    print('不会修改数据库、历史记录、赔率、API Key或用户提示词。')
    if check:return
    if not changes:
        print('V2.6.5已安装，无需重复操作');return
    backup=project/'_patch_backups'/('v265_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    backup.mkdir(parents=True,exist_ok=False)
    touched=[]
    try:
        for name in changes:
            if (project/name).exists():shutil.copy2(project/name,backup/name)
        for name,content in changes.items():
            fd,tmp=tempfile.mkstemp(dir=project,prefix='.v265_',suffix='.tmp')
            try:
                with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as fp:fp.write(content)
                os.replace(tmp,project/name)
            finally:
                if os.path.exists(tmp):os.unlink(tmp)
            touched.append(name)
        preflight(project,{})
        manifest={'tag':TAG,'updated':touched,
                  'created':[name for name in touched if not (backup/name).exists()],
                  'sha256':{name:hashlib.sha256((project/name).read_bytes()).hexdigest() for name in touched},
                  'data_modified':False}
        (backup/'MANIFEST_V265.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        print('V2.6.5升级成功，备份目录：'+str(backup))
        print('旧记录不自动重算。请先用新数据测试，旧记录按需手动重新识别。')
    except BaseException:
        for name in reversed(touched):
            old=backup/name
            if old.is_file():shutil.copy2(old,project/name)
            else:(project/name).unlink(missing_ok=True)
        print('升级失败：已恢复源码；数据库和用户配置未修改。')
        raise

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('project',nargs='?',default='.')
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    try:install(Path(args.project).expanduser().resolve(),args.check)
    except Exception as exc:
        print('V2.6.5安装未完成：'+str(exc),file=sys.stderr)
        raise SystemExit(1)
