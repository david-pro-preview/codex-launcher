import base64
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import signal
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'account_manager.py'
spec = importlib.util.spec_from_file_location('manager', SOURCE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def auth(person, version='1', account=None):
    payload = base64.urlsafe_b64encode(json.dumps({'sub': person, 'name': person + ' Name', 'email': person + '@example.com'}).encode()).decode().rstrip('=')
    return json.dumps({'auth_mode': 'chatgpt', 'tokens': {'id_token': 'header.' + payload + '.sig', 'access_token': 'fake-secret-access-' + version,
        'refresh_token': 'fake-secret-refresh-' + version, 'account_id': account or 'account-' + person}}).encode()


class FakeRuntime:
    def __init__(self):
        self.stops, self.launches, self.events = 0, 0, []
        self.on_stop, self.on_login = lambda: None, lambda: None
        self.target_rows = []
    def event(self, kind, **values):
        self.events.append((kind, values))
    def stop(self):
        self.stops += 1
        self.on_stop()
        self.target_rows = []
    def targets(self):
        return self.target_rows
    def login(self):
        self.on_login()
    def launch(self):
        self.launches += 1


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.home, self.legacy = base / 'home', base / 'legacy'
        self.home.mkdir(); self.legacy.mkdir()
        self.r = m.Registry(self.home, base / 'store', self.legacy)
        self.r.auth.write_bytes(auth('A'))
        self.a = self.r.capture(auth('A'))
        self.rt = FakeRuntime()
        self.c = m.Controller(self.r, self.rt, refresh=lambda r, i: None)

    def test_migrate_deduplicate_and_prefer_current(self):
        (self.legacy / 'A.auth.json').write_bytes(auth('A', 'old'))
        (self.legacy / 'B.auth.json').write_bytes(auth('B'))
        self.r.auth.write_bytes(auth('A', 'new'))
        self.r.bootstrap()
        self.assertEqual(len(self.r.index['accounts']), 2)
        self.assertEqual(self.r.profile_path(self.a).read_bytes(), auth('A', 'new'))

    def test_identity_and_bad_credential(self):
        self.assertEqual(m.parse_auth(auth('A'))[0], m.parse_auth(auth('A', 'new'))[0])
        self.assertNotEqual(m.parse_auth(auth('A'))[0], m.parse_auth(auth('B'))[0])
        self.assertNotEqual(m.parse_auth(auth('A'))[0], m.parse_auth(auth('A', account='other'))[0])
        with self.assertRaises(m.UserError): m.parse_auth(b'{"tokens":"invalid"}')

    def test_metadata_has_no_credentials(self):
        text = self.r.index_path.read_text()
        self.assertNotIn('fake-secret', text)
        self.assertNotIn('refresh_token', text)
        self.assertEqual(stat.S_IMODE(self.r.profile_path(self.a).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.r.root.stat().st_mode), 0o700)

    def test_switch_saves_latest_and_roundtrip(self):
        b = self.r.capture(auth('B'))
        self.rt.on_stop = lambda: self.r.auth.write_bytes(auth('A', 'latest'))
        self.c.switch(b)
        self.assertEqual(self.r.profile_path(self.a).read_bytes(), auth('A', 'latest'))
        self.assertEqual(self.r.auth.read_bytes(), auth('B'))
        self.rt.on_stop = lambda: self.r.auth.write_bytes(auth('B', 'latest'))
        self.c.switch(self.a)
        self.assertEqual(self.r.auth.read_bytes(), auth('A', 'latest'))
        self.assertEqual(self.r.profile_path(b).read_bytes(), auth('B', 'latest'))

    def test_same_account_keeps_latest(self):
        self.r.auth.write_bytes(auth('A', 'new'))
        self.c.switch(self.a)
        self.assertEqual(self.r.auth.read_bytes(), auth('A', 'new'))

    def test_bad_target_fails_before_stop(self):
        b = self.r.capture(auth('B'))
        self.r.profile_path(b).write_bytes(auth('C'))
        with self.assertRaises(m.UserError): self.c.switch(b)
        self.assertEqual(self.rt.stops, 0)

    def test_current_symlink_fails_before_stop(self):
        self.r.auth.unlink(); self.r.auth.symlink_to(self.r.profile_path(self.a))
        with self.assertRaises(m.UserError): self.c.switch(self.a)
        self.assertEqual(self.rt.stops, 0)

    def test_add_account_and_current_badge(self):
        self.rt.on_login = lambda: self.r.auth.write_bytes(auth('B'))
        self.c.add()
        snapshot = self.r.snapshot()
        self.assertEqual(len(snapshot['accounts']), 2)
        self.assertEqual(sum(item['isCurrent'] for item in snapshot['accounts']), 1)
        self.assertEqual(next(item for item in snapshot['accounts'] if item['isCurrent'])['name'], 'B Name')
        self.assertEqual(self.rt.launches, 1)

    def test_duplicate_account_updates_tokens(self):
        self.rt.on_login = lambda: self.r.auth.write_bytes(auth('A', 'rotated'))
        self.assertIn('刷新', self.c.add())
        self.assertEqual(len(self.r.index['accounts']), 1)
        self.assertEqual(self.r.profile_path(self.a).read_bytes(), auth('A', 'rotated'))

    def test_cancel_restores_and_reopens(self):
        def cancel():
            self.r.auth.write_bytes(b'incomplete')
            raise m.Cancelled('cancel')
        self.rt.on_login = cancel
        with self.assertRaises(m.Cancelled): self.c.add()
        self.assertEqual(self.r.auth.read_bytes(), auth('A'))
        self.assertEqual(self.rt.launches, 1)

    def test_failed_same_identity_keeps_rotated_token(self):
        def fail():
            self.r.auth.write_bytes(auth('A', 'new'))
            raise m.UserError('failure')
        self.rt.on_login = fail
        with self.assertRaises(m.UserError): self.c.add()
        self.assertEqual(self.r.auth.read_bytes(), auth('A', 'new'))

    def test_failed_other_identity_saved_for_recovery(self):
        def fail():
            self.r.auth.write_bytes(auth('B'))
            raise m.UserError('failure')
        self.rt.on_login = fail
        with self.assertRaises(m.UserError): self.c.add()
        self.assertEqual(self.r.auth.read_bytes(), auth('A'))
        files = list(self.r.profiles.glob('recovery-*.json'))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), auth('B'))

    def test_failed_first_login_leaves_signed_out(self):
        self.r.auth.unlink()
        def fail():
            self.r.auth.write_bytes(b'incomplete')
            raise m.UserError('failure')
        self.rt.on_login = fail
        with self.assertRaises(m.UserError): self.c.add()
        self.assertFalse(self.r.auth.exists())

    def test_respawn_before_login_commit_stopped(self):
        def login():
            self.r.auth.write_bytes(auth('B'))
            self.rt.target_rows = [{'pid': 123}]
        self.rt.on_login = login
        self.c.add()
        self.assertEqual(self.rt.stops, 2)

    def test_nonfile_store_refused(self):
        (self.home / 'config.toml').write_text('cli_auth_credentials_store="keyring"')
        with self.assertRaises(m.UserError): self.c.add()
        self.assertEqual(self.rt.stops, 0)

    def test_concurrent_operation_refused(self):
        with self.r.locked():
            with self.assertRaises(m.Busy):
                with self.r.locked(): pass

    def test_process_matching_is_precise(self):
        row = lambda exe, args: {'pid': 1, 'executable': exe, 'arguments': args}
        self.assertTrue(m.is_target(row('/Applications/ChatGPT.app/Contents/MacOS/ChatGPT', [])))
        self.assertTrue(m.is_target(row('/Applications/Codex.app/Contents/MacOS/Codex', [])))
        self.assertTrue(m.is_target(row('/extensions/codex', ['app-server'])))
        self.assertTrue(m.is_target(row('/extensions/codex', ['-c', 'x=1', 'app-server'])))
        self.assertFalse(m.is_target(row('/extensions/codex', ['exec', 'app-server'])))
        self.assertFalse(m.is_target(row('/extensions/codex', ['login'])))
        self.assertFalse(m.is_target(row('/Applications/CodexBar.app/Contents/MacOS/CodexBar', [])))
        self.assertFalse(m.is_target(row('/tmp/other', ['app-server'])))

    def test_pid_reuse_not_signalled(self):
        old = {'pid': 123, 'executable': '/bin/codex', 'arguments': ['app-server']}
        new = {'pid': 123, 'executable': '/bin/other', 'arguments': []}
        with patch.object(m, 'process_table', return_value=[new]), patch.object(m.os, 'kill') as kill:
            m.Runtime(self.r).send_signal(old, signal.SIGTERM)
            kill.assert_not_called()

    def test_avatar_urls_and_no_auth_redirect(self):
        self.assertTrue(m.allowed_avatar('https://images.oaiusercontent.com/a.png'))
        for url in ('http://images.oaiusercontent.com/a', 'https://evilopenai.com/a', 'https://127.0.0.1/a', 'https://user:pass@chatgpt.com/a'):
            self.assertFalse(m.allowed_avatar(url))
        self.assertIsNone(m.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.example/'))

    def test_events_do_not_contain_credentials(self):
        b = self.r.capture(auth('B'))
        self.c.switch(b)
        self.assertNotIn('fake-secret', json.dumps(self.rt.events))
        self.assertNotIn('fake-secret', json.dumps(self.r.snapshot()))

    def test_stop_requests_term_then_kill_and_waits_for_quiet(self):
        runtime = m.Runtime(self.r, event=lambda *a, **k: None)
        target = {'pid': 123, 'executable': '/tmp/codex', 'arguments': ['app-server']}
        with patch.object(runtime, 'targets', side_effect=[[target], [target], [], []]), \
             patch.object(runtime, 'send_signal') as send, \
             patch.object(m.time, 'monotonic', side_effect=[0, 0, 8, 9, 11]), \
             patch.object(m.time, 'sleep'):
            runtime.stop()
        self.assertEqual([call.args[1] for call in send.call_args_list], [signal.SIGTERM, signal.SIGKILL])

    def fake_cli(self, body):
        app = Path(self.temp.name) / 'Fake.app'
        binary = app / 'Contents/Resources/codex'
        binary.parent.mkdir(parents=True)
        binary.write_text('#!' + sys.executable + '\n' + body)
        binary.chmod(0o700)
        return app

    def test_real_login_pipe_forwards_only_allowed_url(self):
        app = self.fake_cli('print("secret access_token=DO_NOT_FORWARD", flush=True)\nprint("https://auth.openai.com/oauth/authorize?test=1", flush=True)\nprint("https://evil.example/a", flush=True)\n')
        events = []
        runtime = m.Runtime(self.r, event=lambda kind, **data: events.append((kind, data)))
        with patch.object(m, 'APP_PATH', app):
            runtime.login()
        payload = json.dumps(events)
        self.assertIn('auth.openai.com', payload)
        self.assertNotIn('DO_NOT_FORWARD', payload)
        self.assertNotIn('evil.example', payload)

    def test_cancelled_login_reaps_its_child_process(self):
        app = self.fake_cli('import time\ntime.sleep(30)\n')
        runtime = m.Runtime(self.r, event=lambda *a, **k: None)
        original = m.subprocess.Popen
        created = []
        def popen(*args, **kwargs):
            result = original(*args, **kwargs); created.append(result); return result
        with patch.object(m, 'APP_PATH', app), patch.object(m.subprocess, 'Popen', side_effect=popen), \
             patch.object(m.queue.Queue, 'get', side_effect=m.Cancelled('cancel')):
            with self.assertRaises(m.Cancelled): runtime.login()
        self.assertEqual(len(created), 1)
        self.assertIsNotNone(created[0].poll())


if __name__ == '__main__':
    unittest.main(verbosity=1)
