import io
import json
import unittest
import urllib.error
from unittest.mock import patch
import test_accounts as base
from test_accounts import m, auth


def denied(code=401, error='invalid_grant'):
    return urllib.error.HTTPError('https://test.invalid', code, 'untrusted secret', {},
                                  io.BytesIO(json.dumps({'error': error}).encode()))


class RefreshTests(unittest.TestCase):
    def setUp(self):
        base.Tests.setUp(self)
        self.b = self.r.capture(auth('B', 'old'))
        value = json.loads(auth('B', 'new'))
        value['last_refresh'] = '2026-09-28T00:00:00Z'
        self.new = json.dumps(value).encode()
        self.usage = {'account_id': 'account-B', 'plan_type': 'pro', 'rate_limit': {
            'primary_window': {'used_percent': 7, 'reset_at': 2000000000, 'limit_window_seconds': 604800}}}
        self.consumer_patch = patch.object(m, 'auth_consumers_running', return_value=True)
        self.consumer_patch.start()
        self.addCleanup(self.consumer_patch.stop)

    def fetch_result(self):
        return json.dumps(self.usage).encode()

    def test_inactive_401_refreshes_and_retries_once(self):
        current = self.r.auth.read_bytes()
        with patch.object(m, 'fetch_bytes', side_effect=[denied(), self.fetch_result()]) as fetch, \
             patch.object(m, 'request_token_refresh', return_value=self.new) as refresh:
            with self.r.locked(): m.refresh_usage(self.r, self.b, True)
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(self.r.profile_path(self.b).read_bytes(), self.new)
        self.assertEqual(self.r.auth.read_bytes(), current)
        self.assertEqual(fetch.call_args.args[1]['Authorization'], 'Bearer fake-secret-access-new')
        self.assertNotIn('usageError', self.r.index['accounts'][self.b])
        self.assertEqual(self.r.profile_path(self.b).stat().st_mode & 0o777, 0o600)

    def test_current_live_process_is_not_refreshed(self):
        raw = self.r.auth.read_bytes()
        with patch.object(m, 'fetch_bytes', side_effect=denied()), patch.object(m, 'request_token_refresh') as refresh:
            m.refresh_usage(self.r, self.a, True)
        refresh.assert_not_called()
        self.assertEqual(self.r.auth.read_bytes(), raw)
        self.assertEqual(self.r.index['accounts'][self.a]['usageError'], '等待当前 Codex 自动续期')

    def test_newer_current_file_retried_without_refresh(self):
        def first(*args):
            self.r.auth.write_bytes(auth('A', 'already-refreshed'))
            raise denied()
        payload = dict(self.usage, account_id='account-A')
        calls = 0
        def fetch(*args):
            nonlocal calls
            calls += 1
            if calls == 1: return first(*args)
            self.assertIn('already-refreshed', args[1]['Authorization'])
            return json.dumps(payload).encode()
        with patch.object(m, 'fetch_bytes', side_effect=fetch), patch.object(m, 'request_token_refresh') as refresh:
            m.refresh_usage(self.r, self.a, True)
        refresh.assert_not_called()
        self.assertEqual(calls, 2)
        self.assertNotIn('usageError', self.r.index['accounts'][self.a])

    def test_no_consumers_current_auth_can_refresh(self):
        with patch.object(m, 'auth_consumers_running', return_value=False), \
             patch.object(m, 'request_token_refresh', return_value=auth('A', 'fresh')):
            result = m.renew_profile(self.r, self.a, self.r.auth.read_bytes())
        self.assertEqual(self.r.auth.read_bytes(), result)
        self.assertEqual(self.r.profile_path(self.a).read_bytes(), result)

    def test_same_user_other_workspace_is_protected(self):
        sibling = self.r.capture(auth('A', account='workspace-2'))
        with patch.object(m, 'request_token_refresh') as refresh:
            with self.assertRaisesRegex(m.RefreshFailure, '等待当前'):
                m.renew_profile(self.r, sibling, self.r.profile_path(sibling).read_bytes())
        refresh.assert_not_called()

    def test_shared_inactive_workspace_token_rotates_together(self):
        sibling = self.r.capture(auth('B', 'old', account='workspace-2'))
        with patch.object(m, 'request_token_refresh', return_value=self.new):
            m.renew_profile(self.r, self.b, self.r.profile_path(self.b).read_bytes())
        identifier, _, tokens = m.parse_auth(self.r.profile_path(sibling).read_bytes())
        self.assertEqual(identifier, sibling)
        self.assertEqual(tokens['account_id'], 'workspace-2')
        self.assertEqual(tokens['refresh_token'], 'fake-secret-refresh-new')

    def test_permanent_failure_cached_until_credentials_change(self):
        old = self.r.profile_path(self.b).read_bytes()
        with patch.object(m, 'request_token_refresh', side_effect=m.RefreshFailure('需要重新登录', True)) as refresh:
            for _ in range(2):
                with self.assertRaises(m.RefreshFailure): m.renew_profile(self.r, self.b, old)
            self.assertEqual(refresh.call_count, 1)
            self.r.capture(auth('B', 'relogin'))
            with self.assertRaises(m.RefreshFailure):
                m.renew_profile(self.r, self.b, auth('B', 'relogin'))
            self.assertEqual(refresh.call_count, 2)

    def test_temporary_failure_preserves_auth_and_is_retryable(self):
        old = self.r.profile_path(self.b).read_bytes()
        with patch.object(m, 'request_token_refresh', side_effect=m.RefreshFailure('稍后重试')) as refresh:
            for _ in range(2):
                with self.assertRaises(m.RefreshFailure): m.renew_profile(self.r, self.b, old)
        self.assertEqual(refresh.call_count, 2)
        self.assertEqual(self.r.profile_path(self.b).read_bytes(), old)
        self.assertNotIn('refreshFailureKey', self.r.index['accounts'][self.b])

    def test_non_401_does_not_refresh(self):
        for status in (403, 429, 500):
            with patch.object(m, 'fetch_bytes', side_effect=denied(status)), patch.object(m, 'request_token_refresh') as refresh:
                m.refresh_usage(self.r, self.b, True)
            refresh.assert_not_called()

    def test_second_401_does_not_loop_or_discard_new_token(self):
        with patch.object(m, 'fetch_bytes', side_effect=lambda *a: (_ for _ in ()).throw(denied())), \
             patch.object(m, 'request_token_refresh', return_value=self.new) as refresh:
            m.refresh_usage(self.r, self.b, True)
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(self.r.profile_path(self.b).read_bytes(), self.new)
        self.assertIn('续期后', self.r.index['accounts'][self.b]['usageError'])

    def test_external_login_not_overwritten(self):
        def renew(raw):
            self.r.auth.write_bytes(auth('C', 'external'))
            return auth('A', 'fresh')
        with patch.object(m, 'auth_consumers_running', return_value=False), patch.object(m, 'request_token_refresh', side_effect=renew):
            m.renew_profile(self.r, self.a, self.r.auth.read_bytes())
        self.assertEqual(self.r.auth.read_bytes(), auth('C', 'external'))
        self.assertEqual(self.r.profile_path(self.a).read_bytes(), auth('A', 'fresh'))

    def test_changed_profile_recovery_and_identity_validation(self):
        def renew(raw):
            self.r.capture(auth('B', 'external'))
            return self.new
        with patch.object(m, 'request_token_refresh', side_effect=renew):
            with self.assertRaises(m.RefreshFailure):m.renew_profile(self.r, self.b, auth('B', 'old'))
        self.assertEqual(self.r.profile_path(self.b).read_bytes(), auth('B', 'external'))
        recovery = list(self.r.profiles.glob('recovery-*.json'))
        self.assertEqual(len(recovery), 1)
        self.assertEqual(recovery[0].read_bytes(), self.new)
        with patch.object(m, 'request_token_refresh', return_value=auth('C', 'wrong')):
            with self.assertRaises(m.RefreshFailure):m.renew_profile(self.r, self.b, auth('B', 'external'))
        self.assertEqual(self.r.profile_path(self.b).read_bytes(), auth('B', 'external'))

    def test_oauth_protocol_partial_tokens_and_identity(self):
        with patch.object(m.urllib.request, 'build_opener') as make:
            make.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps({'access_token': 'fresh-access'}).encode()
            updated = m.request_token_refresh(auth('B', 'old'))
            request = make.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, 'https://auth.openai.com/oauth/token')
            self.assertEqual(json.loads(request.data)['grant_type'], 'refresh_token')
            tokens = m.parse_auth(updated)[2]
            self.assertEqual(tokens['refresh_token'], 'fake-secret-refresh-old')
            self.assertEqual(tokens['access_token'], 'fresh-access')
            make.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps({'refresh_token': 'rotated-only'}).encode()
            partial = m.parse_auth(m.request_token_refresh(auth('B', 'old')))[2]
            self.assertEqual(partial['refresh_token'], 'rotated-only')
            self.assertEqual(partial['access_token'], 'fake-secret-access-old')
            make.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps({'access_token':'new', 'id_token':json.loads(auth('C'))['tokens']['id_token']}).encode()
            with self.assertRaises(m.RefreshFailure):m.request_token_refresh(auth('B'))

    def test_oauth_error_classification_and_redaction(self):
        for status, code, permanent in [(400, 'invalid_grant', True), (401, 'other', True),
                (400, {'code':'refresh_token_reused'}, True), (500, 'server_error', False), (429, 'rate_limit', False)]:
            with patch.object(m.urllib.request, 'build_opener') as make:
                make.return_value.open.side_effect = denied(status, code)
                with self.assertRaises(m.RefreshFailure) as caught: m.request_token_refresh(auth('B'))
            self.assertEqual(caught.exception.permanent, permanent)
            self.assertNotIn('secret', str(caught.exception))
