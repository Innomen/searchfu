import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import comparison
from evidence_bank import EvidenceBank


class ExperimentRetentionTests(unittest.TestCase):
    def test_monotonic_observations_and_private_evidence(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'SEARCHFU_METRICS_DB':str(Path(directory)/'metrics.sqlite3')}):
            observer=comparison.Observations('keyword','shadow')
            observer.checkpoint(2,3,event=True,read_bytes=100,cpu_seconds=1)
            observer.checkpoint(4,1,event=True,read_bytes=150,cpu_seconds=2)
            values=comparison.report()['modes']['keyword']
            self.assertEqual(values['shadow_observed_seconds'],4)
            self.assertEqual(values['shadow_observed_peak_results'],3)
            self.assertEqual(values['shadow_observed_first_seconds'],2)
            self.assertEqual(values['shadow_observed_read_bytes'],150)
            evidence=EvidenceBank('gui')
            evidence.append({'stage':'keyword','results':[{'snippet':'private synthetic evidence'}]})
            evidence.append({'stage':'cancelled','results':[{'snippet':'private synthetic evidence'}]})
            self.assertEqual(evidence.path.stat().st_mode&0o777,0o700)
            self.assertEqual((evidence.path/'latest.json').stat().st_mode&0o777,0o600)
            self.assertEqual(json.loads((evidence.path/'latest.json').read_text())['stage'],'cancelled')
            self.assertEqual(len((evidence.path/'events.jsonl').read_text().splitlines()),2)
            self.assertNotIn('private',json.dumps(comparison.report()))

    def test_actual_supervisor_banks_partial_timeout_and_unlimited_completion(self):
        # Delayed fixture crosses the former one-second deadline. Its partial
        # summary arrives before the deadline and must survive a hard kill.
        child="""import sys,json,time
sys.stdin.read()
s={'seconds':.1,'first_seconds':.1,'count':3,'ids':['a','b','c']}
print(json.dumps({'summary':s}),flush=True)
time.sleep(1.3)
s['seconds']=1.4
print(json.dumps({'outcome':'paired','summary':s}),flush=True)
"""
        program="""import comparison,subprocess,sys
original=subprocess.Popen
def delayed(args,**kw):return original([sys.executable,'-c',CHILD],**kw)
comparison.subprocess.Popen=delayed
comparison.supervisor()
""".replace('CHILD',repr(child))
        for limit in (1,0):
            with self.subTest(limit=limit),tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'SEARCHFU_METRICS_DB':str(Path(directory)/'metrics.sqlite3')}):
                cfg={'enabled':True,'usb_db':'/synthetic/index.sqlite3','usb_ann':'','usb_candidates':'','timeout_seconds':limit}
                comparison.validate(cfg)
                request={'config':cfg,'query':'synthetic','options':{'max_seconds':None},'mode':'keyword'}
                primary={'outcome':'paired','primary':{'seconds':.1,'first_seconds':.1,'count':3,'ids':['a','b','c']}}
                result=subprocess.run([sys.executable,'-c',program],input=json.dumps(request)+'\n'+json.dumps(primary)+'\n',capture_output=True,text=True,timeout=6)
                self.assertEqual(result.returncode,0,result.stderr)
                values=comparison.report()['modes']['keyword']
                self.assertEqual(values['shadow_observed_peak_results'],3)
                self.assertEqual(values['shadow_attempts'],1)
                if limit:
                    self.assertEqual(values['shadow_timeouts'],1);self.assertEqual(values['shadow_partial_count_sum'],3)
                    self.assertNotIn('paired',values)
                else:
                    self.assertEqual(values['paired'],1);self.assertEqual(values['shadow_completed'],1)

    def test_unlimited_shadow_preserves_no_retrieval_deadline(self):
        import io
        request={'config':{'usb_db':'/synthetic/db','timeout_seconds':0},'options':{'max_seconds':None},'query':'synthetic'}
        def retrieve(*args,**options):
            self.assertIsNone(options['max_seconds'])
            yield {'stage':'done','elapsed_seconds':1,'results':[]}
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'SEARCHFU_METRICS_DB':str(Path(directory)/'metrics.sqlite3')}),patch('sys.stdin',io.StringIO(json.dumps(request))),patch('sys.stdout',io.StringIO()),patch('retrieval.retrieve',retrieve):comparison.shadow()

    def test_unlimited_supervisor_remains_cancelable_after_primary_finishes(self):
        import time
        child="import sys,json,time;sys.stdin.read();print(json.dumps({'summary':{'seconds':.1,'first_seconds':.1,'count':3,'ids':['a','b','c']}}),flush=True);time.sleep(30)"
        program="import comparison,subprocess,sys;original=subprocess.Popen;comparison.subprocess.Popen=lambda args,**kw:original([sys.executable,'-c',"+repr(child)+"],**kw);comparison.supervisor()"
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'SEARCHFU_METRICS_DB':str(Path(directory)/'metrics.sqlite3')}):
            request={'config':{'enabled':True,'usb_db':'/synthetic/index','usb_ann':'','usb_candidates':'','timeout_seconds':0},'query':'synthetic','options':{},'mode':'keyword'}
            primary={'outcome':'paired','primary':{'seconds':.1,'first_seconds':.1,'count':3,'ids':['a','b','c']}}
            process=subprocess.Popen([sys.executable,'-c',program],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,text=True)
            try:
                process.stdin.write(json.dumps(request)+'\n'+json.dumps(primary)+'\n');process.stdin.close()
                deadline=time.monotonic()+3
                while comparison.report()['modes'].get('keyword',{}).get('shadow_observed_peak_results',0)<3:
                    if time.monotonic()>deadline:self.fail('Synthetic partial event did not arrive')
                    time.sleep(.05)
                self.assertTrue(comparison.cancel_active())
                self.assertEqual(process.wait(timeout=7),0)
                values=comparison.report()['modes']['keyword']
                self.assertEqual(values['shadow_cancelled'],1)
                self.assertEqual(values['shadow_partial_count_sum'],3)
                self.assertFalse(comparison.activity()['active'])
            finally:
                if process.poll() is None:process.kill();process.wait()
