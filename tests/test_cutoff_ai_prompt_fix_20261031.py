"""本次专项回归：不启用封盘不弹窗、重复彩种、多码和定位、提示词升级。"""
import hashlib
import unittest
from unittest.mock import patch
from controller import Controller
from config import DEFAULT_AI_PROMPT
from data_store import DataStore
from parser import BetParser
from recognition_policy import ai_candidate_validation
from test_support import TestDirectory


class CutoffAIFixCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TestDirectory()
        self.store=DataStore(self.tmp.name)
        self.controller=Controller(self.store)
    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_repeated_lottery_marker_and_mixed_position(self):
        p=BetParser()
        for source,candidate,expected in [
            ('841直选2倍，福0127组六40倍','841直选2倍，福0127组六40倍',2),
            ('福定位\n百069\n十568\n个14\n福0127组六40倍',
             '福百069十568个14定位8倍，福0127组六40倍',2),
            ('福841直选2倍，福0127组六40倍',
             '福841直选2倍，福0127组六40倍',2),
        ]:
            if '福定位' in source:
                # 源文没有倍数，AI不能凭空补上8倍，所以只测试完整有8倍的原文。
                source=source.replace('个14\n福','个14\n8倍8*36=288\n福')
            bets,_,err=p.parse('福彩',candidate)
            self.assertEqual(err,'',err)
            self.assertEqual(len(bets),expected)
            self.assertTrue(ai_candidate_validation(source,candidate,bets)[0],source)

    def test_candidate_multiple_play_matches_original_once(self):
        rec=self.controller.add('福彩','841两单一组')
        # 已确认简写现在完全由本地处理，第三方AI不应重复调用。
        self.assertEqual(rec.status,'已识别')
        self.assertEqual(self.controller.ai_request_text(rec),'')
        candidate,_,error=self.controller.parser.parse('福彩','841单2倍，841组1倍')
        self.assertEqual(error,'')
        self.assertTrue(ai_candidate_validation('841两单一组','841单2倍，841组1倍',candidate)[0])
        item=self.store.get(rec.record_id)
        self.assertEqual(item.status,'已识别')
        self.assertEqual(item.computed_amount_yuan,6)
        self.assertEqual(len(item.bets),2)

    def test_candidate_cannot_substitute_or_invent(self):
        examples=[('841两单一组','758单2倍，758组1倍'),
                  ('841两单一组','841单5倍，841组1倍'),
                  ('福0127组六40倍','福0127组六45倍')]
        p=BetParser()
        for source,candidate in examples:
            bets,_,_=p.parse('福彩',candidate)
            self.assertTrue(bets,candidate)
            self.assertFalse(ai_candidate_validation(source,candidate,bets)[0],candidate)

    def test_prompt_upgrades_only_old_builtin_default(self):
        old=self.store.load_settings()
        # 存储的旧版默认值以固定hash精准匹配，个人自定义不替换。
        self.assertTrue(len(DEFAULT_AI_PROMPT)<1600)
        self.assertIn('同一条消息完整原文',DEFAULT_AI_PROMPT)
        old['ai']['prompt']='这是一段客户自己编辑过的特殊提示词'
        self.store.save_settings(old)
        self.assertEqual(self.store.load_settings()['ai']['prompt'],old['ai']['prompt'])
        old['ai']['prompt']='旧自带提示词的其他不同版本'
        self.store.save_settings(old)
        self.assertEqual(self.store.load_settings()['ai']['prompt'],old['ai']['prompt'])
        self.assertNotEqual(hashlib.sha256(DEFAULT_AI_PROMPT.encode()).hexdigest(),
                            '4c417912d05a502218648d78283337bd4f8e4ffab18b468eb3788a075e80df7f')


class CutoffAIFixUITests(unittest.TestCase):
    def setUp(self):
        from ui import RiskApp
        self.tmp=TestDirectory()
        self.app=RiskApp(self.tmp.name)
        self.app.withdraw()
    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def test_popup_only_when_cutoff_enabled(self):
        rec=self.app.controller.add('福彩','4')
        self.assertFalse(self.app.controller.deadline_enabled())
        with patch.object(self.app,'fail') as fail:
            self.app.queue_ai(rec,force=True)
            fail.assert_not_called()
        self.assertIn('未找到',self.app.add_feedback.cget('text'))
        with patch.object(self.app.controller,'cutoff_applies',return_value=True):
            with patch.object(self.app,'fail') as fail:
                self.app.queue_ai(rec,force=True)
                fail.assert_called_once()
                self.assertIn('封盘',str(fail.call_args[0][0]))

    def test_hard_violation_not_sent_and_no_popup_if_unlocked(self):
        rec=self.app.controller.add('福彩','555转圈打1倍')
        self.assertEqual(rec.status,'规则违规')
        with patch.object(self.app,'fail') as fail,patch.object(self.app.pool,'submit') as submit:
            self.app.queue_ai(rec,force=True)
            fail.assert_not_called()
            submit.assert_not_called()

if __name__ == '__main__':
    unittest.main()
