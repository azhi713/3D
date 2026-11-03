# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import install_v268,rollback_v268


class SafetyTests(unittest.TestCase):
    def test_lf_and_windows_crlf_preflight_no_write(self):
        for sep in ('\n','\r\n'):
            with tempfile.TemporaryDirectory() as d:
                base=Path(d)
                (base/'config.py').write_text('APP_VERSION="V2.6.7-foo"\n',encoding='utf8')
                source=('A=1\nB=2\n').replace('\n',sep).encode()
                (base/'probe.py').write_bytes(source)
                with patch.dict(install_v268.TARGETS,{'probe.py':lambda s:s.replace('B=2','B=3')},clear=True),\
                     patch.dict(install_v268.UNMODIFIED_BLOBS,{'probe.py':install_v268.git_blob(b'A=1\nB=2\n')},clear=True),\
                     patch.dict(install_v268.V267_MARKERS,{},clear=True):
                    old,new=install_v268.prepare(base)
                self.assertEqual(old['probe.py'],source)
                self.assertEqual(new['probe.py'],('A=1\nB=3\n').replace('\n',sep).encode())
                self.assertEqual((base/'probe.py').read_bytes(),source)

    def test_source_mismatch_fails_before_modification(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'config.py').write_text('APP_VERSION="V2.6.7-foo"\n')
            (root/'probe.py').write_bytes(b'A=UNTRUSTED\n')
            with patch.dict(install_v268.TARGETS,{'probe.py':lambda s:s+'B=3\n'},clear=True),\
                 patch.dict(install_v268.UNMODIFIED_BLOBS,{'probe.py':'bad'},clear=True),\
                 patch.dict(install_v268.V267_MARKERS,{},clear=True):
                with self.assertRaisesRegex(RuntimeError,'非GitHub基线'):
                    install_v268.prepare(root)
            self.assertEqual((root/'probe.py').read_bytes(),b'A=UNTRUSTED\n')

    def test_rollback_checksum_and_reject_later_edits(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);backup=root/'backup';backup.mkdir()
            source=root/'hello.py';source.write_bytes(b'changed\n')
            (backup/'hello.py').write_bytes(b'original\n')
            manifest={'version':'V2.6.8','data_modified':False,'files':{'hello.py':{
                      'before_sha256':rollback_v268.sha(b'original\n'),
                      'after_sha256':rollback_v268.sha(b'changed\n')}}}
            (backup/'MANIFEST_V268.json').write_text(json.dumps(manifest))
            rollback_v268.restore(root,backup)
            self.assertEqual(source.read_bytes(),b'original\n')
            source.write_bytes(b'OTHER\n')
            with self.assertRaisesRegex(ValueError,'已发生新修改'):
                rollback_v268.restore(root,backup)
            self.assertEqual(source.read_bytes(),b'OTHER\n')

    def test_rollback_output_safe_on_gbk(self):
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);backup=root/'backup';backup.mkdir()
            (root/'a.py').write_bytes(b'NEW')
            (backup/'a.py').write_bytes(b'OLD')
            (backup/'MANIFEST_V268.json').write_text(json.dumps({'version':'V2.6.8','files':{
                'a.py':{'before_sha256':rollback_v268.sha(b'OLD'),
                        'after_sha256':rollback_v268.sha(b'NEW')}}}),encoding='utf8')
            stream=io.TextIOWrapper(io.BytesIO(),encoding='gbk',errors='strict')
            with redirect_stdout(stream):
                rollback_v268.restore(root,backup)
                stream.flush()
            self.assertEqual((root/'a.py').read_bytes(),b'OLD')
            stream.detach()


if __name__=='__main__':unittest.main()
