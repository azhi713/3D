import json, os, sqlite3, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from controller import Controller
from data_store import DataStore
from analysis_input_cache import records_version, prepare
from dashboard_metrics_v280 import purchased_metrics

class PerformanceGuards(unittest.TestCase):
    def test_preview_invalidates_external_write_and_preserves_manual_path(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,FC3D_PREVIEW_CACHE='1'):
            store=DataStore(Path(directory))
            try:
                c=Controller(store);c.activate_period('2026-11-15')
                c.settings['deadline']['enabled']=False
                r=c.add('福彩','福689直选1倍',period=c.period)
                a=c.preview_draw('福彩','689',reuse=True)
                a['lines'].clear()
                self.assertTrue(c.preview_draw('福彩','689',reuse=True)['lines'])
                other=sqlite3.connect(store.path)
                r.received_amount=0
                with other:other.execute('UPDATE records SET payload=? WHERE id=?',(json.dumps(r.to_dict()),r.record_id))
                other.close()
                self.assertEqual(c.preview_draw('福彩','689',reuse=True),c.preview_draw('福彩','689'))
                self.assertEqual(c.preview_draw('福彩','689',reuse=True)['income_yuan'],0)
            finally:store.close()

    def test_exact_record_version_distinguishes_filter_save_and_external_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            store=DataStore(Path(directory))
            try:
                c=Controller(store);c.activate_period('2026-11-15');c.settings['deadline']['enabled']=False
                r=c.add('福彩','福689直选1倍',period=c.period)
                version=records_version(store,c.period)
                app=SimpleNamespace(controller=c)
                first,_=prepare(app,'福彩',c.records(),version)
                c.settings['risk_filters']={'福彩':{'threshold':1,'metric':'payout'}}
                store.save_settings(c.settings)
                self.assertEqual(records_version(store,c.period),version)
                second,_=prepare(app,'福彩',c.records(),version)
                self.assertIs(first[2][1],second[2][1])
                self.assertEqual(second,c.analysis_inputs('福彩'))
                other=sqlite3.connect(store.path)
                r.received_amount=0
                with other:other.execute('UPDATE records SET payload=? WHERE id=?',(json.dumps(r.to_dict()),r.record_id))
                other.close()
                current=records_version(store,c.period)
                self.assertNotEqual(current,version)
                third,_=prepare(app,'福彩',c.records(),current)
                self.assertEqual(third,c.analysis_inputs('福彩'))
                self.assertEqual(third[3],0)
            finally:store.close()

    def test_display_cache_is_equal_and_purchase_mutation_invalidates(self):
        with patch.dict(os.environ,FC3D_DISPLAY_CACHE='1'):
            payouts=[0]*1000;payouts[689]=180000
            purchase={'rows':[['689',1]],'cost_cents':200,'direct_prize_cents':180000,'leopard_prize_cents':160000}
            first=purchased_metrics(200,payouts,purchase)
            self.assertIs(first,purchased_metrics(200,payouts,purchase))
            self.assertEqual(first,purchased_metrics.__wrapped__(200,payouts,purchase))
            purchase['rows'][0][1]=2;purchase['cost_cents']=400
            second=purchased_metrics(200,payouts,purchase)
            self.assertNotEqual(first,second)
            self.assertEqual(second,purchased_metrics.__wrapped__(200,payouts,purchase))

    def test_default_features_active_and_explicit_opt_out(self):
        from analysis_input_cache import enabled
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ):
            for name in ('FC3D_INPUT_CACHE','FC3D_PREVIEW_CACHE','FC3D_DISPLAY_CACHE'):
                os.environ.pop(name,None)
            self.assertTrue(enabled())
            store=DataStore(Path(directory))
            try:
                c=Controller(store);c.activate_period('2026-11-15');c.settings['deadline']['enabled']=False
                self.assertTrue(c.settings['recognition_outer_compat'])
                raw='157、975、381、338、875\\n共5注直选 20 倍'
                self.assertEqual(len(c.add('福彩',raw,period=c.period).bets),5)
                c.settings['recognition_outer_compat']=False
                self.assertFalse(c.add('福彩',raw,period=c.period).bets)
                with patch.object(store,'load_records',wraps=store.load_records) as read:
                    c.preview_draw('福彩','157',reuse=True);c.preview_draw('福彩','157',reuse=True)
                    self.assertEqual(read.call_count,1)
                metrics=purchased_metrics(123,[0]*1000,None)
                self.assertIs(metrics,purchased_metrics(123,[0]*1000,None))
                os.environ['FC3D_INPUT_CACHE']='0'
                self.assertFalse(enabled())
            finally:store.close()
