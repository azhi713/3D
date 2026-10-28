"""测试目录使用普通权限，兼容Windows受限执行环境。"""
from pathlib import Path
import shutil
import uuid


class TestDirectory:
    def __init__(self):
        self.root = (Path(__file__).resolve().parent / '.test-data').resolve()
        self.root.mkdir(exist_ok=True)
        self.path = self.root / uuid.uuid4().hex
        self.path.mkdir()
        self.name = str(self.path)

    def cleanup(self):
        target = self.path.resolve()
        if target.parent != self.root or not target.name.isalnum():
            raise ValueError('测试清理目录越界')
        shutil.rmtree(target)
