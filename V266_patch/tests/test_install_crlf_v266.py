# -*- coding: utf-8 -*-
"""Regression for Windows CRLF preflight: anchors are line-ending agnostic."""
import unittest
import sys
import tempfile
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from patch_logic_v266 import ai_service, config
import install_v266


class WindowsCheckoutTests(unittest.TestCase):
    def test_ai_service_crlf_anchor_resolves_after_normalization(self):
        # The source prefix and comment are copied from the pinned V2.6.5
        # layout. Keep an actual CRLF fixture: this failed in the original ZIP.
        src = (
            "class Probe:\n"
            "    def parse(self, request_data):\n"
            "        instructions = 'test'\n"
            "        instructions += '\\n' + 'V2.5.7双字段协议：旧提示词'\n"
            "        # V257-RECEIPT-FOLLOWUP-20261101: deterministic receipt-backed candidates first.\n"
            "        # Still validated by the normal period-specific candidate pipeline.\n"
            "        from receipt_inference_v257 import infer_receipt_candidates\n"
            "        inferred = infer_receipt_candidates(request_data)\n"
            "        if inferred is not None:\n"
            "            return inferred\n"
            "        return instructions\n"
        )
        crlf = src.replace('\n', '\r\n')
        self.assertRaises(ValueError, ai_service, crlf)
        normalized = crlf.replace('\r\n', '\n')
        result = ai_service(normalized)
        compile(result, 'ai_service_crlf_fixture.py', 'exec')
        self.assertIn('V2.6.6独立识别协议', result)
        self.assertIn("request_data.get('use_full_ai_interpretation')", result)
        self.assertNotIn('V2.5.7双字段协议', result)
        restored = result.replace('\n', '\r\n')
        self.assertNotIn('\n', restored.replace('\r\n', ''))

    def test_installer_prepare_accepts_crlf_at_pinned_blob_hash(self):
        source = (
            "class Probe:\n"
            "    def parse(self, request_data):\n"
            "        instructions='test'\n"
            "        instructions += '\\n' + 'V2.5.7双字段协议：旧提示词'\n"
            "        # V257-RECEIPT-FOLLOWUP-20261101: deterministic receipt-backed candidates first.\n"
            "        from receipt_inference_v257 import infer_receipt_candidates\n"
            "        inferred = infer_receipt_candidates(request_data)\n"
            "        if inferred is not None:\n"
            "            return inferred\n"
            "        return instructions\n"
        )
        windows=source.replace('\n','\r\n').encode('utf-8')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'ai_service.py').write_bytes(windows)
            with patch.object(install_v266,'EXPECTED',{'ai_service.py':install_v266.git_blob(source.encode('utf-8'))}), \
                 patch.object(install_v266,'PREREQUISITES',()), \
                 patch.object(install_v266,'TRANSFORMS',{'ai_service.py':ai_service}):
                original, changed=install_v266.prepare(root)
            self.assertEqual(original['ai_service.py'],windows)
            compiled=changed['ai_service.py']
            self.assertIn(b'\r\n',compiled)
            self.assertNotIn(b'\n',compiled.replace(b'\r\n',b''))
            self.assertIn('V2.6.6独立识别协议',compiled.decode('utf-8'))
            self.assertEqual((root/'ai_service.py').read_bytes(), windows)

    def test_crlf_normalization_is_lossless_for_unmodified_lines(self):
        before = 'A = "中文"\r\nB = "原始文本"\r\n'
        normal = before.replace('\r\n', '\n')
        self.assertEqual(normal.replace('\n', '\r\n'), before)

    def test_crlf_config_still_matches_pinned_version(self):
        source=('APP_VERSION = "V2.6.5-自动识别与安全重试"\r\n'
                'ENGINE_VERSION = "V2.6.5-auto-recognize-safe-retry"\r\n')
        fixed=config(source.replace('\r\n','\n'))
        self.assertIn('V2.6.6-independent-ai-evidence',fixed)


if __name__=='__main__':
    unittest.main()
