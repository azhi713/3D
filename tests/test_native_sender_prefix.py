import unittest
from wechat_text_v281 import normalize_payload

class NativeSenderPrefix(unittest.TestCase):
    def test_native_group_chat_non_bet_keeps_audit(self):
        raw='wxid_cs8bx0h079kq22:\n滴滴'
        result=normalize_payload(dict(text=raw,sender='1518',group_id='trusted@chatroom',transport_source='wechat4'))
        self.assertEqual(result['text'],'滴滴')
        self.assertEqual(result['sender'],'wxid_cs8bx0h079kq22')
        self.assertEqual(result['transport_original_text'],raw)
    def test_plain_or_personal_prefix_not_removed(self):
        raw='wxid_cs8bx0h079kq22:\n滴滴'
        for group,source in [('trusted@chatroom',''),('person','wechat4')]:
            self.assertEqual(normalize_payload(dict(text=raw,sender='1518',group_id=group,transport_source=source))['text'],raw)
