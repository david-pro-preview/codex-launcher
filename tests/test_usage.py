import test_accounts as base
from test_accounts import m, auth
import unittest, json
from unittest.mock import patch
import urllib.error

class UsageTests(unittest.TestCase):
    setUp = base.Tests.setUp
    def payload(self):
        return {'plan_type':'prolite','account_id':'account-A','rate_limit':{
            'primary_window':{'used_percent':28,'reset_at':2000000,'limit_window_seconds':18000},
            'secondary_window':{'used_percent':0,'reset_at':2400000,'limit_window_seconds':604800}}}
    def test_plan_mapping(self):
        self.assertEqual([m.plan_label(p) for p in ['free','go','plus','prolite','pro']], ['Free','Go','Plus','Pro 5x','Pro 20x'])
    def test_windows_and_account_validation(self):
        data=self.payload()
        value=m.parse_usage(data,'account-A',1000000)
        self.assertEqual(value['windows'][1]['usedPercent'],0)
        self.assertEqual(value['windows'][0]['durationSeconds'],18000)
        with self.assertRaises(ValueError):m.parse_usage(data,'account-B',0)
        data['rate_limit']['primary_window']['used_percent']=float('nan')
        with self.assertRaises(ValueError):m.parse_usage(data,'account-A',0)
    def test_missing_window_is_not_zero(self):
        data=self.payload();data['rate_limit']['secondary_window']=None
        self.assertEqual(len(m.parse_usage(data,'account-A',0)['windows']),1)
        data['rate_limit']['primary_window']={}
        with self.assertRaises(ValueError):m.parse_usage(data,'account-A',0)
    def test_cache_manual_refresh_and_failure(self):
        with patch.object(m,'fetch_bytes',return_value=json.dumps(self.payload()).encode()) as fetch:
            m.refresh_usage(self.r,self.a)
            m.refresh_usage(self.r,self.a)
            self.assertEqual(fetch.call_count,1)
            m.refresh_usage(self.r,self.a,True)
            self.assertEqual(fetch.call_count,2)
            self.assertEqual(self.r.snapshot()['accounts'][0]['plan'],'Pro 5x')
            self.r.capture(auth('A'))
            self.assertEqual(self.r.snapshot()['accounts'][0]['plan'],'Pro 5x')
        previous=self.r.index['accounts'][self.a]['usage']
        with patch.object(m,'fetch_bytes',side_effect=urllib.error.HTTPError('url',401,'secret',{},None)), patch.object(m,'auth_consumers_running',return_value=True):
            m.refresh_usage(self.r,self.a,True)
        row=self.r.snapshot()['accounts'][0]
        self.assertEqual(row['usage'],previous)
        self.assertIn('等待当前 Codex',row['usageError'])
        self.assertNotIn('secret',json.dumps(self.r.snapshot()))
    def test_five_minute_refresh(self):
        with patch.object(m,'fetch_bytes',return_value=json.dumps(self.payload()).encode()) as fetch, patch.object(m.time,'time',return_value=1000):
            m.refresh_usage(self.r,self.a)
            with patch.object(m.time,'time',return_value=1299):m.refresh_usage(self.r,self.a)
            self.assertEqual(fetch.call_count,1)
            with patch.object(m.time,'time',return_value=1300):m.refresh_usage(self.r,self.a)
            self.assertEqual(fetch.call_count,2)

if __name__ == '__main__':unittest.main()
