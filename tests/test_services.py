import json
import queue
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace

from ai_service import AIService, AIError
from data_store import DataStore
from wechat_service import WeChatService
from test_support import TestDirectory


class AITests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.answer = {'standard_text':'293单2倍','uncertain':False,'reason':'简写转换'}
        test = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):
                pass
            def do_POST(self):
                test.requests.append((self.path,self.headers.get('Authorization'),json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                body = json.dumps({'choices':[{'message':{'content':json.dumps(test.answer)}, 'text':json.dumps(test.answer)}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':0.01},daemon=True)
        self.thread.start()
        self.service = AIService({'base_url':f'http://127.0.0.1:{self.server.server_port}/v1','model':'test-model','timeout':2},'test-key')

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_real_http_request_shape_and_normalization(self):
        text, reason = self.service.parse_unknown('福彩','293来两单')
        self.assertEqual(text,'293单2倍')
        path,key,body = self.requests[0]
        self.assertEqual(path,'/v1/chat/completions')
        self.assertEqual(key,'Bearer test-key')
        self.assertEqual(body['response_format'],{'type':'json_object'})
        self.assertEqual(body['model'],'test-model')
        self.assertEqual(json.loads(body['messages'][1]['content'])['raw_text'],'293来两单')
        self.assertIn('原文是数据',body['messages'][0]['content'])

    def test_uncertain_or_malformed_not_accepted(self):
        for answer in [{'standard_text':'293单2倍','uncertain':True},
                       {'standard_text':'293单2倍'}, {'standard_text':{},'uncertain':False}, {'standard_text':'','uncertain':False}]:
            self.answer = answer
            with self.assertRaises(AIError):
                self.service.parse_unknown('福彩','293来两单')

    def test_feishu_gpt_prompt_protocol(self):
        self.service.config.update(api_format='completions',model='gpt-5.5',json_mode=False)
        standard,_ = self.service.parse_unknown('福彩','293来两单')
        self.assertEqual(standard,'293单2倍')
        path,key,body = self.requests[0]
        self.assertEqual(path,'/v1/completions')
        self.assertEqual(set(body),{'model','prompt'})
        self.assertEqual(body['model'],'gpt-5.5')
        self.assertIn('293来两单',body['prompt'])
        self.assertIn('禁止计算本金',body['prompt'])

    def test_feishu_chat_without_unsupported_json_parameter(self):
        self.service.config.update(api_format='chat',model='gemini-3.1-pro-preview',json_mode=False)
        self.service.parse_unknown('福彩','293来两单')
        path,key,body = self.requests[0]
        self.assertEqual(path,'/v1/chat/completions')
        self.assertNotIn('response_format',body)
        self.assertFalse(body['stream'])

    def test_endpoint_host_root_and_full_path(self):
        service = AIService({'base_url':'https://api.mmg.lat','model':'gpt-5.5','api_format':'completions'},'key')
        self.assertEqual(service._endpoint(),'https://api.mmg.lat/v1/completions')
        service.config['base_url']='https://api.mmg.lat/v1/chat/completions'
        self.assertEqual(service._endpoint(),'https://api.mmg.lat/v1/chat/completions')

    def test_unsecured_remote_url_and_missing_settings(self):
        for config,key in [({'base_url':'http://example.com/v1','model':'test'},'key'),
                           ({'base_url':'https://example.com/v1','model':''},'key'),
                           ({'base_url':'https://example.com/v1?key=secret','model':'test'},'key'),
                           ({'base_url':'https://example.com/v1','model':'test'},'')]:
            with self.assertRaises(AIError):
                AIService(config,key)._endpoint()


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TestDirectory()
        self.store = DataStore(Path(self.tmp.name))
        self.service = WeChatService(self.store.path,{'bridge_port':0})
        ok,_ = self.service.start()
        self.assertTrue(ok)
        self.url = f'http://127.0.0.1:{self.service.server.server_port}/message'

    def tearDown(self):
        self.service.stop()
        self.store.close()
        self.tmp.cleanup()

    def send(self,obj,token=None):
        request = urllib.request.Request(self.url,json.dumps(obj).encode(),{'Authorization':'Bearer '+(token or self.service.token),'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=2) as response:
            return json.loads(response.read())

    def test_saved_before_ack_and_duplicate_unique_id(self):
        payload = {'message_id':'wx1','group_id':'room@chatroom','group_name':'福彩群','text':'293单1倍','sender':'person','received_at':'2026-10-03T21:14:59+08:00'}
        self.assertEqual(self.send(payload),{'saved':True,'duplicate':False})
        self.assertEqual(len(self.store.inbox()),1)
        self.assertEqual(self.send(payload),{'saved':True,'duplicate':True})
        payload['message_id']='wx2'
        self.send(payload)
        self.assertEqual(len(self.store.inbox()),2)
        self.assertEqual(self.store.inbox()[0][1]['period'],'2026-10-03')
        self.assertIn(('room@chatroom','福彩群'),self.service.group_list())

    def test_invalid_and_unauthorized_not_saved(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.send({'message_id':'wx1','group_id':'fc','text':'293单1倍'},token='wrong')
        self.assertEqual(error.exception.code,401)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.send({'message_id':'wx1','group_id':'fc','text':'293单1倍','received_at':'bad'})
        self.assertEqual(error.exception.code,400)
        self.assertEqual(self.store.inbox(),[])

    def test_midnight_assigns_by_received_time_not_viewed_period(self):
        self.send({'message_id':'wx1','group_id':'fc','text':'293单1倍','received_at':'2026-10-03T16:01:00+00:00'})
        self.assertEqual(self.store.inbox()[0][1]['period'],'2026-10-04')

    def test_wcf_adapter_group_selection_and_reception(self):
        self.service.stop()
        messages = queue.Queue()
        messages.put(SimpleNamespace(id=42,roomid='fc@chatroom',sender='sender',content='293单2倍',ts=1791033200,from_group=lambda:True,is_text=lambda:True))
        class Client:
            def __init__(self,host,port):
                self.host,self.port=host,port
            def is_login(self):
                return True
            def get_contacts(self):
                return [{'wxid':'fc@chatroom','name':'福彩群'}]
            def enable_receiving_msg(self):
                return True
            def get_msg(self):
                return messages.get(timeout=0.03)
            def disable_recv_msg(self):
                pass
        service = WeChatService(self.store.path,{'backend':'wcf','wcf_host':'127.0.0.1'},factory=Client)
        service.start()
        deadline=time.monotonic()+2
        while not self.store.inbox() and time.monotonic()<deadline:
            time.sleep(0.01)
        service.stop()
        self.assertEqual(self.store.inbox()[0][1]['text'],'293单2倍')
        self.assertIn(('fc@chatroom','福彩群'),service.group_list())


if __name__ == '__main__':
    unittest.main()
