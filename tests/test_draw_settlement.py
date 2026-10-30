"""开奖读取和应赔试算：只用本地HTML模拟官网，不实际联网。"""
import unittest
from datetime import date, datetime, time
from unittest.mock import patch
from draw_service import DrawLookupError, fetch_draw, _clean_digits, _from_fc, SOURCE_URLS
from controller import Controller
from data_store import DataStore
from models import CN_TIME
from test_support import TestDirectory


INDEX_FC = '''<table><tr><th>开奖日期</th><th>期号</th><th>详情</th></tr>
<tr><td>2026-10-30</td><td>2026289</td><td><a href="/3D/example.jhtml">点击查看详情</a></td></tr>
<tr><td>2026-10-29</td><td>2026288</td><td><a href="/3D/old.jhtml">详情</a></td></tr></table>'''
DETAIL_FC = '''<h2>中国福利彩票“3D”第2026289期开奖信息</h2><p>开奖日期：2026-10-30</p><h3>中奖号码</h3><span>0</span><span>1</span><span>2</span><p>奖项</p>'''
INDEX_TC = '''<table><tr><th>发布日期</th><th>期号</th><th>开奖号码</th><th>详细</th></tr>
<tr><td>2026-10-29</td><td>26288</td><td>4 1 5</td><td>详细</td></tr>
<tr><td>2026-10-30</td><td>26289</td><td>0 0 9</td><td>详细</td></tr></table>'''


class OfficialDrawParsingTests(unittest.TestCase):
    def test_fc_two_pages_with_same_period_and_leading_zero(self):
        calls = []
        def fetch(url):
            calls.append(url)
            return DETAIL_FC if url.endswith('/3D/example.jhtml') else INDEX_FC
        draw = _from_fc('2026-10-30', SOURCE_URLS['福彩'][1], fetch)
        self.assertEqual((draw.issue,draw.draw_date,draw.number),('2026289','2026-10-30','012'))
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[-1], 'https://www.scflcp.com.cn/3D/example.jhtml')

    def test_tc_history_row_date_verified_and_009_preserved(self):
        result = fetch_draw('体彩', '2026-10-30', fetch=lambda _: INDEX_TC)
        self.assertEqual((result.issue,result.number), ('26289','009'))

    def test_no_today_fails_no_yesterday_substitution(self):
        with self.assertRaisesRegex(DrawLookupError, '没有'):
            fetch_draw('体彩','2026-10-30', fetch=lambda _:INDEX_TC.replace('2026-10-30','2026-10-28'))
        with self.assertRaisesRegex(DrawLookupError, '没有'):
            fetch_draw('福彩','2026-10-30', fetch=lambda _:INDEX_FC.replace('2026-10-30','2026-10-28'))

    def test_conflicting_issue_date_or_external_detail_link_rejected(self):
        with self.assertRaisesRegex(DrawLookupError,'期号'):
            _from_fc('2026-10-30', SOURCE_URLS['福彩'][1], lambda u: INDEX_FC if u.endswith('3dinfo.jhtml') else DETAIL_FC.replace('2026289','2026288'))
        with self.assertRaisesRegex(DrawLookupError,'日期'):
            _from_fc('2026-10-30', SOURCE_URLS['福彩'][1], lambda u: INDEX_FC if u.endswith('3dinfo.jhtml') else DETAIL_FC.replace('2026-10-30','2026-10-29'))
        with self.assertRaisesRegex(DrawLookupError,'跨站'):
            _from_fc('2026-10-30', SOURCE_URLS['福彩'][1], lambda u: INDEX_FC.replace('/3D/example.jhtml','https://evil.invalid/') if u.endswith('3dinfo.jhtml') else DETAIL_FC)

    def test_bad_number_and_date_rejected(self):
        self.assertEqual(_clean_digits('0 0 0'),'000')
        self.assertEqual(_clean_digits('0,1,2'),'012')
        self.assertEqual(_clean_digits('2016'),'')
        with self.assertRaises(DrawLookupError):
            fetch_draw('体彩','2026-02-30',fetch=lambda _:INDEX_TC)
        with self.assertRaises(DrawLookupError):
            fetch_draw('体彩','26289',fetch=lambda _:INDEX_TC)


class DrawAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TestDirectory()
        self.store = DataStore(self.tmp.name)
        self.controller = Controller(self.store)
        self.period=self.controller.period

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_preview_012_000_preserves_lottery_and_exact_payout(self):
        self.controller.add('福彩','012单2倍')
        self.controller.add('福彩','012组1倍')
        self.controller.add('福彩','000直选1倍')
        result=self.controller.preview_draw('福彩','012')
        self.assertEqual(result['payout_yuan'],3900)
        self.assertEqual(result['winning_count'],2)
        self.assertEqual(result['income_yuan'],8)
        self.assertEqual(result['net_income_yuan'],-3892)
        self.assertEqual(self.controller.preview_draw('福彩','000')['payout_yuan'],1600)
        self.assertEqual(self.controller.preview_draw('体彩','012')['payout_yuan'],0)
        self.assertIsNone(self.store.load_settlement(self.period,'福彩'))

    def test_unrecognized_omitted_and_warning_not_zeroed(self):
        self.controller.add('福彩','293单1倍，未知口语，505组1倍')
        result=self.controller.preview_draw('福彩','293')
        self.assertEqual(result['payout_yuan'],1800)
        self.assertEqual(result['income_yuan'],4)
        self.assertFalse(result['complete'])
        self.assertTrue(result['blockers'])
        self.assertTrue(any('未完全识别' in x for x in result['warnings']))

    def test_bad_input_and_ambiguous_prize(self):
        with self.assertRaises(ValueError):
            self.controller.preview_draw('福彩','12')
        with self.assertRaises(ValueError):
            self.controller.preview_draw('福彩','１２３')
        self.controller.add('福彩','293单1.5倍')
        result=self.controller.preview_draw('福彩','293')
        self.assertFalse(result['complete'])
        self.assertTrue(any('小数倍' in s for s in result['blockers']))

    def test_frozen_snapshot_rejects_edits_even_if_same_income(self):
        rec = self.controller.add('福彩','123单2倍')
        self.controller.settings['deadline']['enabled']=True
        cutoff = datetime.combine(date.fromisoformat(self.period), time(21,14), CN_TIME)
        self.controller.tick_deadline(clock=cutoff)
        self.assertEqual(self.controller.record_settlement('福彩','123', draw_info={'source':'手动输入'})['total_payout_yuan'],3600)
        with self.assertRaisesRegex(ValueError, '已经结算'):
            self.controller.record_settlement('福彩','123')

    def test_tampered_snapshot_not_reused_on_formal_settlement(self):
        rec=self.controller.add('福彩','123单2倍')
        self.controller.settings['deadline']['enabled']=True
        cutoff=datetime.combine(date.fromisoformat(self.period),time(21,14),CN_TIME)
        self.controller.tick_deadline(clock=cutoff)
        self.controller.settings['deadline']['enabled']=False
        self.controller.edit(rec,'456单2倍')
        self.controller.settings['deadline']['enabled']=True
        with self.assertRaisesRegex(ValueError,'快照'):
            self.controller.record_settlement('福彩','456')

if __name__ == '__main__':
    unittest.main()
