"""官方CWL优先/备用、非真实网络; 风险表与原始下注来源分离。"""
import json
import tkinter as tk
import unittest
from unittest.mock import patch
from draw_service import fetch_draw, DrawLookupError, CWL_PAGE, CWL_API
from test_support import TestDirectory
from ui import RiskApp

class OfficialSourceTests(unittest.TestCase):
    def test_cwl_first_and_exact_3d_with_leading_zero(self):
        calls = []
        def fetch(url):
            calls.append(url)
            if url == CWL_PAGE: return '<html>cookie page</html>'
            if url.startswith(CWL_API):
                self.assertIn('issueCount=', url)
                self.assertIn('name=3d', url)
                return json.dumps({'state': 0, 'result': [
                    {'code':'2026288','date':'2026-10-29(四)','red':'6,1,2'},
                    {'code':'2026289','date':'2026-10-30(五)','red':'0,0,9'}]})
            self.fail('主源成功不应查询备用来源')
        found = fetch_draw('福彩', '2026-10-30', fetch=fetch)
        self.assertEqual((found.number, found.issue, found.draw_date), ('009','2026289','2026-10-30'))
        self.assertEqual(len(calls), 2)
        self.assertIn('中国福彩网', found.source_name)

    def test_stale_cwl_falls_back_but_never_uses_yesterday(self):
        def fetch(url):
            if url == CWL_PAGE: return 'ok'
            if url.startswith(CWL_API):
                return json.dumps({'result':[{'code':'2026288','date':'2026-10-29(四)','red':'6,1,2'}]})
            if url.endswith('3dinfo.jhtml'):
                return '<table><tr><td>2026-10-29</td><td>2026288</td><td><a href="/3D/old.jhtml">详情</a></td></tr></table>'
            self.fail('昨日数据不需要读取详情')
        with self.assertRaises(DrawLookupError) as ctx:
            fetch_draw('福彩','2026-10-30',fetch=fetch)
        self.assertIn('最新', str(ctx.exception))

    def test_missing_cwl_switches_to_official_backup_for_same_date(self):
        def fetch(url):
            if url == CWL_PAGE: return 'ok'
            if url.startswith(CWL_API): return json.dumps({'result':[{'code':'2026288','date':'2026-10-29(四)','red':'6,1,2'}]})
            if url.endswith('3dinfo.jhtml'):
                return '<table><tr><td>2026-10-30</td><td>2026289</td><td><a href="/3D/example.jhtml">详情</a></td></tr></table>'
            if url.endswith('/3D/example.jhtml'):
                return '<div>3D第2026289期 开奖日期：2026-10-30 中奖号码 0 1 2</div>'
            self.fail(url)
        r=fetch_draw('福彩','2026-10-30',fetch=fetch)
        self.assertEqual(r.number,'012')
        self.assertIn('备用',r.source_name)

    def test_cwl_conflicts_are_not_auto_accepted(self):
        def fetch(url):
            if url == CWL_PAGE: return 'ok'
            if url.startswith(CWL_API):
                return json.dumps({'result':[
                    {'code':'2026289','date':'2026-10-30','red':'1,2,3'},
                    {'code':'2026289','date':'2026-10-30','red':'3,2,1'}]})
            if url.endswith('3dinfo.jhtml'):
                return '<table><tr><td>2026-10-29</td><td>2026288</td></tr></table>'
            self.fail(url)
        with self.assertRaises(DrawLookupError) as ctx:
            fetch_draw('福彩','2026-10-30',fetch=fetch)
        self.assertIn('矛盾',str(ctx.exception))

class RiskUITests(unittest.TestCase):
    def setUp(self):
        self.tmp=TestDirectory()
        self.app=RiskApp(self.tmp.name)
        self.app.withdraw()
        self.app.controller.settings['wechat']['auto_start']=False

    def tearDown(self):
        self.app.pool.shutdown(wait=True)
        self.app.close()
        self.tmp.cleanup()

    def test_risk_columns_are_unambiguous_and_source_is_separate(self):
        self.app.controller.add('福彩','438直选10倍')
        self.app.controller.settings['risk_threshold']=5000
        self.app.refresh()
        cols=list(self.app.risks_tree['columns'])
        self.assertEqual(cols,['number','payout','loss','plan'])
        self.assertIn('补单',self.app.risks_tree.heading('plan','text'))
        self.app.risks_tree.selection_set('438')
        self.app.show_sources()
        self.assertEqual(self.app.risks_tree.item('438','values')[-1],'10')
        self.assertEqual(self.app.risks_tree.item('438','values')[1],'18,000')
        self.assertEqual(self.app.risks_tree.item('438','values')[2],'17,980')
        self.assertEqual(len(self.app.source_rows.get_children()),1)
        self.assertIn('10',self.app.source_rows.item(self.app.source_rows.get_children()[0],'values')[2])
        self.assertIn('438',self.app.sources.get('1.0','end'))
        self.app.sort_risks('plan')
        self.assertIn('438',self.app.risks_tree.get_children())

    def test_short_network_error_has_details_and_preserves_manual_number(self):
        self.app.draw_number.set('456')
        self.app._apply_draw_lookup(self.app._draw_lookup_request, self.app.lottery.get(), self.app.controller.period,
                                    None,'中国福彩网：接口HTTP 403；四川福彩备用：网页格式变化，无法解析数据')
        self.assertEqual(self.app.draw_number.get(),'456')
        self.assertLess(len(self.app.draw_source_label.cget('text')),75)
        self.assertTrue(self.app._draw_lookup_error_detail)
        with patch('ui.messagebox.showinfo') as popup:
            self.app.show_draw_lookup_error()
        popup.assert_called_once()

if __name__=='__main__':
    unittest.main()

# Regression: callbacks from a previous date must not replace a newer manual selection.
class StaleDrawResultUITests(unittest.TestCase):
    def setUp(self):
        self.tmp=TestDirectory()
        self.app=RiskApp(self.tmp.name)
        self.app.withdraw()
    def tearDown(self):
        self.app.pool.shutdown(wait=True)
        self.app.close()
        self.tmp.cleanup()
    def test_stale_lookup_cannot_replace_new_manual_result(self):
        from draw_service import DrawResult
        old_request=self.app._draw_lookup_request
        old_period=self.app.controller.period
        self.app.controller.activate_period('2026-10-20')
        self.app.period.set('2026-10-20')
        self.app.refresh()
        self.app.draw_number.set('123')
        result=DrawResult('福彩',old_period,'2026289','009','模拟官网','https://www.cwl.gov.cn/ygkj/')
        self.app._apply_draw_lookup(old_request,'福彩',old_period,result,'')
        self.assertEqual(self.app.draw_number.get(),'123')
        self.assertIsNone(self.app._draw_online)
    def test_edited_number_clears_stale_query_details(self):
        self.app._draw_lookup_error_detail='上次官网错误'
        self.app.draw_number.set('123')
        self.app._draw_edited()
        self.assertEqual(self.app._draw_lookup_error_detail,'')
