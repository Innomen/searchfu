import threading,unittest
from unittest.mock import patch
import gpu_lease
class LeaseTests(unittest.TestCase):
    def test_priority_lease_releases_after_embedding_failure(self):
        calls=[]
        def call(route,body):
            calls.append((route,body));return {'ok':True,'token':'synthetic-token'}
        with patch.object(gpu_lease,'call',side_effect=call):
            with self.assertRaises(ValueError):
                with gpu_lease.lease():raise ValueError('synthetic failure')
        self.assertEqual(calls[0],('/lease/acquire',{'ttl_secs':3600,'priority':True}))
        self.assertEqual(calls[-1],('/lease/release',{'token':'synthetic-token'}))
    def test_denied_lease_never_enters_embedding(self):
        with patch.object(gpu_lease,'call',return_value={'ok':False}) as call:
            with self.assertRaises(RuntimeError):
                with gpu_lease.lease():self.fail('entered GPU work')
        self.assertEqual(call.call_count,1)
    def test_lost_renewal_stops_embedding_and_still_releases(self):
        stop=threading.Event();lost=threading.Event();calls=[]
        def call(route,body):
            calls.append(route);return {'ok':route!='/lease/renew','token':'synthetic-token'}
        with patch.object(gpu_lease,'call',side_effect=call),patch.object(gpu_lease.threading,'Event',side_effect=[stop,lost]),patch.object(stop,'wait',return_value=False):
            with gpu_lease.lease() as check:
                self.assertTrue(lost.wait(1))
                with self.assertRaises(RuntimeError):check()
        self.assertEqual(calls,['/lease/acquire','/lease/renew','/lease/release'])
