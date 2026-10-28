"""兼容旧入口；运行完整回归测试。"""
import unittest
from pathlib import Path

if __name__ == '__main__':
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).resolve().parent / 'tests'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
