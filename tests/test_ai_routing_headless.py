"""在无图形桌面的环境验证UI自动AI提交门禁，不触发真实外部接口。"""
import queue
import unittest

from controller import Controller
from data_store import DataStore
from test_support import TestDirectory
from ui import RiskApp


class DeferredPool:
    def __init__(self):
        self.tasks = []
    def submit(self, fn):
        self.tasks.append(fn)


class FakeAI:
    def __init__(self):
        self.requests = []
    def parse_unknown(self, lottery, text):
        self.requests.append((lottery, text))
        return '123单1倍', ''


class AppHarness:
    def __init__(self, controller, store):
        self.controller = controller
        self.store = store
        self.controller.settings['ai']['enabled'] = True
        self.controller.settings['ai']['model'] = 'test-model'
        self.ai_key = 'test-placeholder'
        self.ai = FakeAI()
        self.events = queue.Queue()
        self.pool = DeferredPool()
        self.ai_busy = set()
        self._manual_ai = set()
        self.refreshed = 0
    def refresh(self):
        self.refreshed += 1
    def fail(self, error):
        raise AssertionError(f'不应发送AI：{error}')


class HeadlessAIRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TestDirectory()
        self.store = DataStore(self.tmp.name)
        self.controller = Controller(self.store)
        self.app = AppHarness(self.controller, self.store)
    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_leopard_round_and_group_never_submit_to_ai(self):
        for raw in ('555转圈打1倍', '222组1倍', '293单0倍'):
            record = self.controller.add('福彩', raw)
            RiskApp.queue_ai(self.app, record)
        self.assertEqual(self.app.pool.tasks, [])
        self.assertEqual(self.app.ai.requests, [])

    def test_only_unresolved_fragment_is_sent(self):
        record = self.controller.add('福彩','293单1倍，未知口语，505组1倍')
        RiskApp.queue_ai(self.app, record)
        self.assertEqual(len(self.app.pool.tasks),1)
        self.app.pool.tasks[0]()
        self.assertEqual(len(self.app.ai.requests),1)
        sent = self.app.ai.requests[0][1]
        self.assertEqual(self.app.ai.requests[0][0],'福彩')
        self.assertIn('未知口语',sent)
        self.assertIn('293单1倍',sent)
        self.assertIn('505组1倍',sent)
        self.assertIn('本地已确认明细',sent)
        self.assertEqual(self.store.get(record.record_id).status,'AI识别中')


if __name__ == '__main__':
    unittest.main()
