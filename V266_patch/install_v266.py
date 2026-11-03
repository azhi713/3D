# -*- coding: utf-8 -*-
"""Source-only V2.6.6 installer. No database/user-data access."""
from __future__ import annotations
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

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from patch_logic_v266 import TRANSFORMS

COMMIT='6bc113ee234a9d155dec0d9ccc78c89ceda20212'
EXPECTED={
 'controller.py':'cd7f5f7450e420fb1407bd6d01fe69f80c30ce0c',
 'ai_service.py':'2105d6d34590f2c4da9d804d0e4deac5c71b570e',
 'ui.py':'1eeacc5776df57711aaf396a1d35ecb42867f8ec',
 'config.py':'23a3d93fe3508ec72d8c19ccedbcd9c49087f80a',
}
PREREQUISITES=('candidate_v261.py','candidate_v260.py','evidence_v257.py',
              'ai_retry_v265.py','recognition_v265.py','amount_audit_v264.py',
              'recognition_amount_v264.py','deadline_v260.py','parser_v257.py')
NEW='ai_pipeline_v266.py'


def git_blob(data):
    return hashlib.sha1(b'blob '+str(len(data)).encode('ascii')+b'\0'+data).hexdigest()


def b64_sha(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.v266_',suffix='.tmp',dir=str(path.parent))
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(data);f.flush();os.fsync(f.fileno())
        if path.exists():shutil.copymode(path,tmp)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def prepare(root):
    if not root.exists():raise RuntimeError('找不到程序目录：'+str(root))
    for name in PREREQUISITES:
        if not (root/name).is_file():
            raise RuntimeError('缺少V2.6.5依赖模块：'+name+'；安装已停止')
    if (root/NEW).exists():
        raise RuntimeError('发现已存在的 '+NEW+'，为避免覆盖不明文件，安装已停止')
    old={};after={}
    for name,fn in TRANSFORMS.items():
        path=root/name
        if not path.is_file():raise RuntimeError('缺少核心源码：'+name)
        data=path.read_bytes()
        # GitHub blob is LF-based; Windows text checkouts may be CRLF.
        hashes={git_blob(data),git_blob(data.replace(b'\r\n',b'\n'))}
        if EXPECTED[name] not in hashes:
            raise RuntimeError(name+' 与GitHub基线 '+COMMIT[:8]+' 不一致，拒绝覆盖。\n'
                               '请先把本机源码同步到该提交；现有data和配置不会受影响。')
        encoding='utf-8-sig' if data.startswith(b'\xef\xbb\xbf') else 'utf-8'
        try:
            # GitHub baseline is LF; Windows checkouts may use CRLF even
            # when their normalized Git blob SHA is identical.  Transform
            # against normalized text, then restore the original line endings.
            txt=data.decode(encoding).replace('\r\n','\n')
            replaced=fn(txt)
            compile(replaced,name,'exec')
        except (UnicodeError,ValueError,SyntaxError) as exc:
            raise RuntimeError(name+' 补丁预检失败：'+str(exc)) from exc
        old[name]=data
        line_ending='\r\n' if b'\r\n' in data else '\n'
        new_data=replaced.replace('\n',line_ending).encode(encoding)
        after[name]=new_data
    new_bytes=(ROOT/'modules'/NEW).read_bytes()
    compile(new_bytes.decode('utf-8'),NEW,'exec')
    after[NEW]=new_bytes
    return old,after


def run_tests(root):
    env=os.environ.copy()
    env['PYTHONPATH']=os.pathsep.join([str(root),str(ROOT/'modules'),env.get('PYTHONPATH','')])
    result=subprocess.run([sys.executable,'-m','unittest','discover',
                           '-s',str(ROOT/'tests'),'-p','test_*v266.py','-v'],
                          cwd=str(root),env=env,stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',timeout=70)
    print(result.stdout[-6000:])
    if result.returncode:
        raise RuntimeError('V2.6.6 自测未通过，源码将自动恢复')


def main():
    ap=argparse.ArgumentParser(description='V2.6.6 安全源码升级器')
    ap.add_argument('--root',help='源码根目录，默认从本补丁上一级定位')
    args=ap.parse_args()
    root=Path(args.root).resolve() if args.root else ROOT.parent.resolve()
    print('升级版本：V2.6.6')
    print('源代码基线：',COMMIT)
    print('目标目录：',root)
    print('操作范围：4个Python源码文件及1个新增模块；不碰data及历史记录')
    if 'V2.6.6-independent-ai-evidence' in (root/'config.py').read_text(encoding='utf-8-sig') if (root/'config.py').exists() else False:
        print('检测到V2.6.6版本标记，已停止重复安装。')
        return
    original,modified=prepare(root)
    stamp=datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    directory=root/'_patch_backups'/('v266_'+stamp)
    directory.mkdir(parents=True,exist_ok=False)
    manifest={'version':'V2.6.6','baseline':COMMIT,'created_at':stamp,
              'files':{},'new_files':[NEW]}
    for name,data in original.items():
        (directory/name).write_bytes(data)
        manifest['files'][name]={'before_sha256':b64_sha(data),
                                 'after_sha256':b64_sha(modified[name])}
    manifest['files'][NEW]={'before_sha256':None,'after_sha256':b64_sha(modified[NEW])}
    (directory/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    written=[]
    try:
        for name,data in modified.items():
            atomic(root/name,data);written.append(name)
        print('源码已写入隔离事务，开始回归测试...')
        run_tests(root)
    except BaseException:
        print('测试或写入失败，正在恢复原文件...')
        for name in reversed(written):
            if name in original:atomic(root/name,original[name])
            elif name==NEW and (root/NEW).exists() and b64_sha((root/NEW).read_bytes())==b64_sha(modified[NEW]):
                (root/NEW).unlink()
        print('已尝试自动恢复；备份仍保留于：',directory)
        raise
    print('✓ V2.6.6源码升级和回归测试通过。')
    print('✓ 备份目录：',directory)
    print('✓ GitHub远端、data/、赔率、历史数据、提示词均未修改。')

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('✗ 升级中止：',exc)
        sys.exit(1)
