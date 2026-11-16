"""Real-process crash tests; all databases are temporary, never production."""
import os,sys,json,tempfile,subprocess,sqlite3,socket,unittest,threading
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
try:
    import credentials
except ModuleNotFoundError:
    sys.path.append(r'E:\Python项目\3D风险解析')
from controller import Controller
from data_store import DataStore
from wechat_service import WeChatService
from models import MessageRecord
from parser import split_lottery_message
RAW='福637 387 765 658\n体314 318 194 328\n直选各2倍🈴32'
PERIOD='2026-11-15'
def payload(native='n1',group='g1@chatroom',text=RAW,period=PERIOD):
    return dict(message_id=native,group_id=group,text=text,sender='customer',
                received_at=period+'T12:00:00+08:00',period=period)
def open_app(path):
    ds=DataStore(path);c=Controller(ds);c.activate_period(PERIOD)
    c.settings['deadline']['enabled']=False
    c.settings['wechat']['fc_groups']=['g1@chatroom','g2@chatroom','g3@chatroom']
    c.settings['wechat']['tc_groups']=[]
    return ds,c
def snapshot(ds,c):
    result={}
    for period in ds.periods():
        c.activate_period(period)
        result[period]={lot:dict(income=c.analysis(lot).revenue,
                                payout=c.analysis(lot).payouts) for lot in ('福彩','体彩')}
    records=[dict(lottery=r.lottery,raw=r.raw_text,native_id=r.source_message_id,group=r.group_id,
                  status=r.parse_status,revision=r.revision,bets=[b.to_dict() for b in r.bets])
             for period in ds.periods() for r in ds.load_records(period)]
    return dict(finance=result,records=records,
                inbox=[list(x) for x in ds.db.execute('SELECT source_id,group_id,processed,payload FROM inbox ORDER BY id')],
                audit=[dict(action=a,payload=json.loads(p)) for a,p in ds.db.execute('SELECT action,payload FROM audit ORDER BY id')])
def worker(phase,path):
    def deny(*a,**k):raise AssertionError('network forbidden')
    socket.socket.connect=deny
    ds,c=open_app(path)
    if phase=='enqueue_before':os._exit(91)
    WeChatService(ds.path,c.settings['wechat'])._persist(payload())
    if phase=='enqueue_after':os._exit(91)
    insert=ds.insert;update=ds.update;mark=ds.mark_inbox_processed
    def ins(r,*a,**k):
        if phase=='before_second' and r.lottery=='体彩':os._exit(91)
        result=insert(r,*a,**k)
        if phase==('raw_first' if r.lottery=='福彩' else 'raw_second'):os._exit(91)
        return result
    def upd(r,*a,**k):
        if phase=='update_first_failure' and r.lottery=='福彩':raise sqlite3.OperationalError('injected disk write failure')
        result=update(r,*a,**k)
        if phase==('updated_first' if r.lottery=='福彩' else 'updated_second'):os._exit(91)
        return result
    def marked(i):
        if phase=='before_processed':os._exit(91)
        mark(i)
        if phase=='processed_uncommitted':os._exit(91)
    ds.insert=ins;ds.update=upd;ds.mark_inbox_processed=marked
    if phase=='commit_failure':
        # A failed completion write must roll back all nested business writes.
        def fail(i):
            mark(i)
            raise sqlite3.OperationalError('injected failure at completion boundary')
        ds.mark_inbox_processed=fail
    c.drain_inbox()
    if phase=='after_commit':os._exit(91)
    ds.close()
    os._exit(91)

class InboxRecovery(unittest.TestCase):
    results=[]
    @classmethod
    def tearDownClass(cls):
        report=os.environ.get('P3_REPORT_PATH')
        if report:Path(report).write_text(json.dumps(cls.results,ensure_ascii=False,indent=2),encoding='utf-8')

    def test_nested_store_calls_do_not_commit_outer_transaction(self):
        with tempfile.TemporaryDirectory(prefix='p3-') as tmp:
            ds,c=open_app(tmp)
            other=sqlite3.connect(ds.path)
            try:
                with self.assertRaises(RuntimeError):
                    with ds.transaction():
                        c.add('福彩','福689直选1倍',period=PERIOD)
                        self.assertEqual(other.execute('SELECT count(*) FROM records').fetchone()[0],0)
                        raise RuntimeError('rollback')
                self.assertEqual(ds.db.execute('SELECT count(*) FROM records').fetchone()[0],0)
                self.assertEqual(ds.db.execute('SELECT count(*) FROM record_revisions').fetchone()[0],0)
                self.assertEqual(ds.db.execute('SELECT count(*) FROM audit WHERE action=?',('添加原文',)).fetchone()[0],0)
                self.results.append(dict(case='nested_visibility_and_rollback',passed=True))
            finally:
                other.close();ds.close()

    def test_process_crash_boundaries(self):
        for phase in ('enqueue_before','enqueue_after','raw_first','update_first_failure',
                      'updated_first','before_second','raw_second','updated_second',
                      'before_processed','processed_uncommitted','commit_failure','after_commit'):
            with self.subTest(phase=phase),tempfile.TemporaryDirectory(prefix='p3-') as tmp:
                run=subprocess.run([sys.executable,'-B','-X','utf8',__file__,'--worker',phase,tmp],
                                   capture_output=True,text=True,timeout=30)
                self.assertEqual(run.returncode,91,run.stdout+run.stderr)
                ds,c=open_app(tmp)
                try:
                    c.activate_period('2026-11-16') # restart on another date
                    WeChatService(ds.path,c.settings['wechat'])._persist(payload())
                    _,errors=c.drain_inbox();self.assertFalse(errors)
                    a=snapshot(ds,c)
                    self.assertEqual(a['finance'][PERIOD]['福彩']['income'],1600)
                    self.assertEqual(a['finance'][PERIOD]['体彩']['income'],1600)
                    self.assertEqual(len(a['records']),2)
                    self.assertEqual({r['native_id'] for r in a['records']},{'n1'})
                    self.assertTrue(all(r['status']=='verified_local' for r in a['records']))
                    self.assertEqual([x[2] for x in a['inbox']],[1])
                    self.assertEqual(sum(x['action']=='添加原文' for x in a['audit']),2)
                    self.assertEqual(sum(x['action']=='消息全部业务片段完成' for x in a['audit']),1)
                    for r in a['records']:
                        lot=r['lottery'];codes=['637','387','765','658'] if lot=='福彩' else ['314','318','194','328']
                        for code,value in a['finance'][PERIOD][lot]['payout'].items():
                            self.assertEqual(value,3600 if code in codes else 0)
                    c.drain_inbox()
                    self.assertEqual(snapshot(ds,c),a)
                    self.results.append(dict(case=phase,passed=True,result=a))
                finally:ds.close()

    def test_legacy_pristine_partial_resumes_without_historical_reparse(self):
        with tempfile.TemporaryDirectory(prefix='p3-') as tmp:
            ds,c=open_app(tmp)
            try:
                raw=split_lottery_message(RAW,'福彩')['福彩']
                rec=MessageRecord('福彩','微信',raw,source_message_id='n1',group_id='g1@chatroom',
                                  period=PERIOD,source_original_text=RAW)
                ds.insert(rec)
                WeChatService(ds.path,c.settings['wechat'])._persist(payload())
                _,errors=c.drain_inbox();self.assertFalse(errors)
                a=snapshot(ds,c)
                self.assertEqual([a['finance'][PERIOD][l]['income'] for l in ('福彩','体彩')],[1600,1600])
                self.assertEqual(sum(x['action']=='添加原文' for x in a['audit']),2)
                self.results.append(dict(case='legacy_pristine',passed=True,result=a))
            finally:ds.close()

    def test_legacy_manual_or_deleted_partial_is_protected(self):
        for deleted in (False,True):
            with tempfile.TemporaryDirectory(prefix='p3-') as tmp:
                ds,c=open_app(tmp)
                try:
                    rec=MessageRecord('福彩','微信',split_lottery_message(RAW,'福彩')['福彩'],
                                      source_message_id='n1',group_id='g1@chatroom',period=PERIOD,source_original_text=RAW)
                    ds.insert(rec)
                    if deleted:ds.delete(rec)
                    else:c.edit(rec,'福689直选3倍')
                    before=list(ds.db.execute('SELECT payload,deleted FROM records'))
                    WeChatService(ds.path,c.settings['wechat'])._persist(payload())
                    _,errors=c.drain_inbox();self.assertTrue(errors)
                    self.assertEqual(list(ds.db.execute('SELECT payload,deleted FROM records')),before)
                    self.assertEqual(ds.db.execute('SELECT processed FROM inbox').fetchone()[0],0)
                    self.results.append(dict(case='protected_deleted' if deleted else 'protected_manual',passed=True,result=snapshot(ds,c)))
                finally:ds.close()

    def test_repeated_sections_ids_and_late_callbacks(self):
        with tempfile.TemporaryDirectory(prefix='p3-') as tmp:
            ds,c=open_app(tmp)
            try:
                service=WeChatService(ds.path,c.settings['wechat'])
                raw='福689直选1倍\n体012直选2倍\n福689直选3倍'
                self.assertTrue(service._persist(payload(text=raw)))
                self.assertFalse(service._persist(payload(text=raw)))
                self.assertTrue(service._persist(payload('n2',text=raw)))
                self.assertTrue(service._persist(payload('n1','g2@chatroom',raw)))
                while ds.inbox():self.assertFalse(c.drain_inbox()[1])
                a=snapshot(ds,c)
                self.assertEqual(a['finance'][PERIOD]['福彩']['income'],2400)
                self.assertEqual(a['finance'][PERIOD]['体彩']['income'],1200)
                self.assertEqual(len(a['records']),6)
                self.assertEqual(sum(len(r['bets']) for r in a['records'] if r['lottery']=='福彩'),6)
                r=ds.load_records(PERIOD)[0];old_revision=r.revision
                c.edit(r,'福689直选5倍')
                self.assertFalse(c.apply_ai_candidate(r.record_id,old_revision,'{}'))
                r2=ds.load_records(PERIOD)[1];ds.delete(r2)
                self.assertFalse(c.apply_ai_candidate(r2.record_id,r2.revision,'{}'))
                before=snapshot(ds,c);service._persist(payload(text=raw));c.drain_inbox()
                self.assertEqual(snapshot(ds,c),before)
                self.results.append(dict(case='same_lottery_sections_ids_edit_delete_ai',passed=True,result=before))
            finally:ds.close()

    def test_multigroup_concurrent_cross_date(self):
        with tempfile.TemporaryDirectory(prefix='p3-') as tmp:
            ds,c=open_app(tmp)
            try:
                service=WeChatService(ds.path,c.settings['wechat']);errors=[]
                def produce(group):
                    try:
                        for i in range(20):
                            service._persist(payload(str(i),group,period='2026-11-14' if i%2 else PERIOD))
                    except BaseException as e:errors.append(repr(e))
                threads=[threading.Thread(target=produce,args=(f'g{i}@chatroom',)) for i in range(1,4)]
                for t in threads:t.start()
                for t in threads:t.join(20);self.assertFalse(t.is_alive())
                self.assertFalse(errors)
                while ds.inbox():self.assertFalse(c.drain_inbox()[1])
                a=snapshot(ds,c)
                self.assertEqual(len(a['records']),120)
                for period in ('2026-11-14',PERIOD):
                    for lot in ('福彩','体彩'):self.assertEqual(a['finance'][period][lot]['income'],48000)
                self.assertTrue(all(x[2]==1 for x in a['inbox']))
                self.results.append(dict(case='multigroup_concurrent_cross_date',passed=True,result=a))
            finally:ds.close()

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--worker':worker(sys.argv[2],sys.argv[3])
    else:unittest.main()
