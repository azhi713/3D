# -*- coding: utf-8 -*-
import unittest
from ai_retry_v265 import parse_with_safe_retry

class SafeRetryTests(unittest.TestCase):
    def test_pre_send_two_failures_then_success(self):
        class Service:
            n=0
            last_metrics={}
            def parse_unknown(self,lot,original):
                self.n+=1
                if self.n<3:
                    self.last_metrics={'billing_state':'not_sent','transport_attempts':['urllib:pre_send_failed']}
                    raise ConnectionError('before sending')
                return '{"ok":true}',''
        s=Service(); tm={}
        self.assertEqual(parse_with_safe_retry(s,'福彩','test',tm,sleeper=lambda _:None),('{"ok":true}',''))
        self.assertEqual(tm['retry_attempts'],3)
        self.assertEqual(s.n,3)
    def test_after_send_timeout_never_retries(self):
        class Service:
            n=0
            last_metrics={'billing_state':'unknown_after_send','transport_attempts':['urllib:delivery_unknown']}
            def parse_unknown(self,lot,original):
                self.n+=1
                raise ConnectionError('read timed out')
        s=Service()
        with self.assertRaises(ConnectionError):
            parse_with_safe_retry(s,'福彩','test',sleeper=lambda _:None)
        self.assertEqual(s.n,1)
    def test_http_429_retries_with_limit(self):
        class Service:
            n=0
            last_metrics={'billing_state':'unknown_after_send'}
            def parse_unknown(self,lot,original):
                self.n+=1
                raise RuntimeError('AI请求失败：HTTP 429（请求过于频繁）')
        s=Service()
        with self.assertRaises(RuntimeError):parse_with_safe_retry(s,'福彩','test',sleeper=lambda _:None)
        self.assertEqual(s.n,3)
    def test_invalid_request_no_retry(self):
        class Service:
            n=0
            last_metrics={'billing_state':'unknown_after_send'}
            def parse_unknown(self,lot,original):
                self.n+=1
                raise RuntimeError('AI请求失败：HTTP 400')
        s=Service()
        with self.assertRaises(RuntimeError):parse_with_safe_retry(s,'福彩','test',sleeper=lambda _:None)
        self.assertEqual(s.n,1)
    def test_transient_missing_backend_not_retried(self):
        class Service:
            n=0
            last_metrics={'billing_state':'not_sent','transport_attempts':['curl:unavailable']}
            def parse_unknown(self,lot,original):
                self.n+=1
                raise RuntimeError('backend unavailable')
        s=Service()
        with self.assertRaises(RuntimeError):parse_with_safe_retry(s,'福彩','test',sleeper=lambda _:None)
        self.assertEqual(s.n,1)

if __name__=='__main__':unittest.main()
