import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from patch_logic_v266 import only_one,between,config

class PatcherSafetyTests(unittest.TestCase):
    def test_repeated_anchor_refused(self):
        with self.assertRaises(ValueError):only_one('XX','X','Y','test')
    def test_missing_anchor_refused(self):
        with self.assertRaises(ValueError):between('first\nthird','first','second','X','test')
    def test_only_expected_version_mutated(self):
        base='APP_VERSION = "V2.6.5-自动识别与安全重试"\nENGINE_VERSION = "V2.6.5-auto-recognize-safe-retry"\nCUSTOM_PROMPT = "不要覆盖我的提示词"'
        result=config(base)
        self.assertIn('V2.6.6-AI独立识别与证据校验',result)
        self.assertIn('CUSTOM_PROMPT = "不要覆盖我的提示词"',result)
    def test_no_unsupported_old_version_overwrite(self):
        with self.assertRaises(ValueError):config('APP_VERSION="V2.5.4"\nENGINE_VERSION="old"')

if __name__=='__main__':unittest.main()
