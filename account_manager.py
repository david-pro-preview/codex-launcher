#!/usr/bin/env python3
"""Private account storage and transactional switching for Codex Launcher.

Stdout is a JSON event stream. Credentials and raw login output are never emitted.
"""
import argparse
import base64
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import urllib.error
import os
from pathlib import Path
import queue
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid

AUTH_HOME = Path.home() / '.codex'
DATA_HOME = Path.home() / 'Library/Application Support/Codex Launcher'
APP_DATA = Path.home() / 'Library/Application Support/Codex'
APP_PATH = Path('/Applications/ChatGPT.app')
LEGACY_STORE = Path.home() / 'Library/Application Support/ChatGPT-Account-Switcher/auth-profiles'


class UserError(Exception):
    pass


class Cancelled(UserError):
    pass


class Busy(UserError):
    pass


def emit(kind, **values):
    print(json.dumps(dict(kind=kind, **values), ensure_ascii=False), flush=True)


def claims_from_token(token):
    value = token.split('.')[1]
    claims = json.loads(base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)))
    if not isinstance(claims, dict):
        raise ValueError()
    return claims


def parse_auth(raw):
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError()
        tokens = data['tokens']
        if not isinstance(tokens, dict):
            raise ValueError()
        if data.get('auth_mode') not in (None, 'chatgpt'):
            raise ValueError()
        for key in ('id_token', 'access_token', 'refresh_token', 'account_id'):
            if not isinstance(tokens.get(key), str) or not tokens[key]:
                raise ValueError()
        claims = claims_from_token(tokens['id_token'])
        subject = claims['sub']
        if not isinstance(subject, str) or not subject:
            raise ValueError()
        fingerprint = hashlib.sha256(json.dumps([subject, tokens['account_id']]).encode()).hexdigest()
        return fingerprint, claims, tokens
    except (ValueError, KeyError, IndexError, TypeError):
        raise UserError('登录信息不完整，或当前使用的不是 ChatGPT 文件登录。') from None


def atomic_write(path, raw):
    if path.is_symlink():
        raise UserError('认证文件或账号索引是软链接，请检查后重试。')
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def safe_text(value, fallback='', limit=120):
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else fallback


class Registry:
    def __init__(self, home=AUTH_HOME, root=DATA_HOME, legacy=LEGACY_STORE):
        self.home, self.root, self.legacy = home, root, legacy
        self.auth = home / 'auth.json'
        self.profiles, self.avatars = root / 'profiles', root / 'avatars'
        for path in (root, self.profiles, self.avatars):
            if path.is_symlink():
                raise UserError('账号保存目录不能是软链接。')
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
        self.index_path = root / 'accounts.json'
        if self.index_path.exists():
            try:
                self.index = json.loads(self.index_path.read_text())
                if self.index.get('version') != 1 or not isinstance(self.index.get('accounts'), dict):
                    raise ValueError()
            except (ValueError, AttributeError):
                raise UserError('账号索引无法读取；原有认证备份未改动。') from None
        else:
            self.index = {'version': 1, 'accounts': {}, 'legacyImported': False}

    @contextlib.contextmanager
    def locked(self):
        paths = [self.root / 'operation.lock']
        if self.legacy.is_dir():
            paths.append(self.legacy / '.switch.lock')
        with contextlib.ExitStack() as stack:
            for lock_path in paths:
                if lock_path.is_symlink():
                    raise UserError('操作锁不能是软链接。')
                stream = stack.enter_context(lock_path.open('a'))
                os.chmod(lock_path, 0o600)
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise Busy('另一个账号操作正在进行。') from None
            # Load again after acquiring the lock to avoid stale metadata writers.
            if self.index_path.exists():
                self.index = json.loads(self.index_path.read_text())
            yield

    def save_index(self):
        atomic_write(self.index_path, json.dumps(self.index, ensure_ascii=False, indent=2).encode())

    def profile_path(self, identifier):
        if not re.fullmatch(r'[a-f0-9]{64}', identifier or ''):
            raise UserError('账号标识无效。')
        return self.profiles / (identifier + '.json')

    def read_current(self):
        if self.auth.is_symlink():
            raise UserError('当前 auth.json 是软链接；请先恢复为原目录中的普通文件。')
        if not self.auth.exists():
            return None
        raw = self.auth.read_bytes()
        parse_auth(raw)
        return raw

    def capture(self, raw, legacy_label=None):
        identifier, claims, tokens = parse_auth(raw)
        previous = self.index['accounts'].get(identifier, {})
        email = safe_text(claims.get('email'))
        name = previous.get('name') if previous.get('nameSource') == 'profile' else safe_text(claims.get('name'), email.split('@')[0] if email else 'ChatGPT 账号')
        auth_claims = claims.get('https://api.openai.com/auth') or {}
        if not isinstance(auth_claims, dict):
            auth_claims = {}
        metadata = dict(previous, id=identifier, name=name, email=email,
                        plan=previous.get('plan') if previous.get('planSource') == 'usage' else safe_text(auth_claims.get('chatgpt_plan_type')),
                        addedAt=previous.get('addedAt', time.time()))
        if legacy_label:
            metadata['legacyLabel'] = legacy_label
        path = self.profile_path(identifier)
        if path.is_symlink():
            raise UserError('已保存的认证不能是软链接。')
        if not path.exists() or path.read_bytes() != raw:
            atomic_write(path, raw)
        if previous != metadata:
            self.index['accounts'][identifier] = metadata
            self.save_index()
        return identifier

    def bootstrap(self):
        if not self.index.get('legacyImported'):
            for label in ('A', 'B'):
                path = self.legacy / (label + '.auth.json')
                if path.is_file() and not path.is_symlink():
                    try:
                        self.capture(path.read_bytes(), label)
                    except UserError:
                        pass
            self.index['legacyImported'] = True
            self.save_index()
        current = self.read_current()
        if current:
            self.capture(current)
        return current

    def snapshot(self):
        current = self.read_current()
        current_id = parse_auth(current)[0] if current else None
        accounts = []
        for record in sorted(self.index['accounts'].values(), key=lambda value: value.get('addedAt', 0)):
            avatar = self.avatars / (record['id'] + '.image')
            accounts.append({
                'id': record['id'], 'name': record['name'], 'email': record.get('email', ''),
                'avatarPath': str(avatar) if avatar.is_file() else None,
                'isCurrent': record['id'] == current_id, 'plan': plan_label(record.get('plan', '')),
                'usage': record.get('usage'), 'usageError': record.get('usageError'),
            })
        return {'accounts': accounts, 'currentId': current_id,
                'refreshedAt': max((r.get('usageCheckedAt', 0) for r in self.index['accounts'].values()), default=0)}

    def verify_storage(self):
        path = self.home / 'config.toml'
        if path.exists():
            for line in path.read_text().splitlines():
                match = re.match(r'^\s*cli_auth_credentials_store\s*=\s*[\"\']([^\"\']+)[\"\']', line)
                if match and match.group(1) != 'file':
                    raise UserError('当前配置使用非文件认证存储。启动器不会修改钥匙串或自动更改配置。')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_bytes(url, headers=None, maximum=3 * 1024 * 1024):
    request = urllib.request.Request(url, headers=headers or {})
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(request, timeout=7) as response:
        data = response.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError('Response too large')
        return data


def allowed_avatar(url):
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ''
        domains = ('chatgpt.com', 'openai.com', 'oaistatic.com', 'oaiusercontent.com',
                   'googleusercontent.com', 'gravatar.com', 'blob.core.windows.net')
        return parsed.scheme == 'https' and not parsed.username and not parsed.password and any(host == domain or host.endswith('.' + domain) for domain in domains)
    except ValueError:
        return False


def refresh_profile(registry, identifier):
    raw = registry.profile_path(identifier).read_bytes()
    _, claims, tokens = parse_auth(raw)
    headers = {'Authorization': 'Bearer ' + tokens['access_token'],
               'ChatGPT-Account-ID': tokens['account_id'],
               'User-Agent': 'CodexLauncher/1.0', 'Accept': 'application/json'}
    record = registry.index['accounts'][identifier]
    picture = safe_text(claims.get('picture'), limit=6000)
    for route in ('/backend-api/wham/profiles/me', '/backend-api/me'):
        try:
            data = json.loads(fetch_bytes('https://chatgpt.com' + route, headers, 1024 * 1024))
            profile = data.get('profile') or data
            name = safe_text(profile.get('display_name') or profile.get('name'))
            if name:
                record['name'], record['nameSource'] = name, 'profile'
            picture = safe_text(profile.get('profile_picture_url') or profile.get('picture'), picture, 6000)
            if picture:
                break
        except Exception:
            continue
    if picture and allowed_avatar(picture):
        try:
            # Never forward the bearer token to the avatar host.
            image = fetch_bytes(picture)
            if image.startswith((b'\x89PNG\r\n\x1a\n', b'\xff\xd8\xff', b'GIF8')) or (image[:4] == b'RIFF' and image[8:12] == b'WEBP'):
                atomic_write(registry.avatars / (identifier + '.image'), image)
        except Exception:
            pass
    record['profileCheckedAt'] = time.time()
    registry.save_index()


def plan_label(raw):
    value = safe_text(raw)
    return {'free': 'Free', 'go': 'Go', 'plus': 'Plus', 'pro': 'Pro 20x',
            'prolite': 'Pro 5x', 'pro_lite': 'Pro 5x', 'pro-lite': 'Pro 5x',
            'pro lite': 'Pro 5x'}.get(value.lower(), value.title() or '未知套餐')


def parse_usage(data, account_id, now):
    if not isinstance(data, dict):
        raise ValueError()
    returned_id = data.get('account_id') or data.get('accountId')
    if returned_id and returned_id != account_id:
        raise ValueError('Account mismatch')
    limits = data.get('rate_limit')
    if not isinstance(limits, dict):
        raise ValueError()
    windows = []
    for key in ('primary_window', 'secondary_window'):
        window = limits.get(key)
        if window is None:
            continue
        if not isinstance(window, dict):
            raise ValueError()
        used, reset, duration = [window.get(k) for k in ('used_percent', 'reset_at', 'limit_window_seconds')]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in (used, reset, duration)) or duration <= 0 or reset <= 0:
            raise ValueError()
        windows.append({'id': key, 'usedPercent': min(100, max(0, used)),
                        'resetsAt': reset, 'durationSeconds': duration})
    if not windows:
        raise ValueError()
    return {'windows': windows, 'fetchedAt': now}


class RefreshFailure(UserError):
    def __init__(self, message, permanent=False):
        super().__init__(message)
        self.permanent = permanent


def credential_key(raw):
    return hashlib.sha256(json.dumps(parse_auth(raw)[2], sort_keys=True).encode()).hexdigest()


def current_profile(registry, identifier):
    """Prefer the live file: Codex may already have rotated this account's tokens."""
    current = registry.read_current()
    if current and parse_auth(current)[0] == identifier:
        registry.capture(current)
    return registry.profile_path(identifier).read_bytes()


def auth_consumers_running():
    # Include regular CLI sessions as well as desktop/IDE app-servers. Never stop them.
    return any(is_target(row) or Path(row['executable']).name == 'codex'
               for row in process_table())


def request_token_refresh(raw):
    """Mirror codex-login's JSON refresh grant; never log HTTP bodies or tokens."""
    identifier, _, tokens = parse_auth(raw)
    body = json.dumps({'client_id': 'app_EMoamEEZ73f0CkXaXp7hrann',
                       'grant_type': 'refresh_token',
                       'refresh_token': tokens['refresh_token']}).encode()
    request = urllib.request.Request('https://auth.openai.com/oauth/token', data=body,
        headers={'Content-Type': 'application/json', 'Accept': 'application/json'}, method='POST')
    try:
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=15) as response:
            payload = response.read(1024 * 1024 + 1)
            if len(payload) > 1024 * 1024:
                raise ValueError()
            result = json.loads(payload)
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read(65536))
            value = detail.get('error') if isinstance(detail, dict) else None
            code = value.get('code') or value.get('type') if isinstance(value, dict) else value
            code = code.lower() if isinstance(code, str) else ''
        except Exception:
            code = ''
        finally:
            error.close()
        permanent = error.code == 401 or code in (
            'refresh_token_expired', 'refresh_token_reused', 'refresh_token_invalidated', 'invalid_grant')
        raise RefreshFailure('登录无法自动续期，请重新添加此账号' if permanent
                             else '自动续期暂时失败，稍后重试', permanent) from None
    except Exception:
        raise RefreshFailure('自动续期暂时失败，稍后重试') from None
    try:
        if not isinstance(result, dict) or not any(result.get(name) for name in ('id_token', 'access_token', 'refresh_token')):
            raise ValueError()
        updated = json.loads(raw)
        for name in ('id_token', 'access_token', 'refresh_token'):
            if result.get(name) is not None:
                if not isinstance(result[name], str) or not result[name]:
                    raise ValueError()
                updated['tokens'][name] = result[name]
        updated['last_refresh'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        candidate = json.dumps(updated).encode()
        if parse_auth(candidate)[0] != identifier:
            raise ValueError()
        return candidate
    except Exception:
        raise RefreshFailure('自动续期返回信息异常，已保留原登录') from None


def renew_profile(registry, identifier, expected):
    """Called under Registry.locked(); at most one refresh grant per usage check."""
    latest = current_profile(registry, identifier)
    if credential_key(latest) != credential_key(expected):
        return latest
    record = registry.index['accounts'][identifier]
    key = credential_key(latest)
    if record.get('refreshFailureKey') == key:
        raise RefreshFailure('登录无法自动续期，请重新添加此账号', True)
    _, claims, tokens = parse_auth(latest)
    current = registry.read_current()
    current_claims = parse_auth(current)[1] if current else {}
    # Workspace IDs can differ while sharing one OAuth identity/token family.
    if current_claims.get('sub') == claims['sub'] and auth_consumers_running():
        raise RefreshFailure('等待当前 Codex 自动续期')
    # Also refuse to rotate a saved profile that changed during preflight.
    if credential_key(current_profile(registry, identifier)) != key:
        return current_profile(registry, identifier)
    try:
        refreshed = request_token_refresh(latest)
    except RefreshFailure as error:
        if error.permanent:
            registry.index['accounts'][identifier]['refreshFailureKey'] = key
            registry.save_index()
        raise
    if parse_auth(refreshed)[0] != identifier:
        raise RefreshFailure('自动续期账号不匹配，已保留原登录')
    # Persist rotated credentials before any optional metadata/usage operations.
    # Keep a recovery file if a concurrent external writer changed the profile.
    if credential_key(registry.profile_path(identifier).read_bytes()) != key:
        atomic_write(registry.profiles / ('recovery-' + uuid.uuid4().hex + '.json'), refreshed)
        raise RefreshFailure('登录在续期期间发生变化，请刷新后重试')
    registry.capture(refreshed)
    _, _, new_tokens = parse_auth(refreshed)
    # Profiles for two workspaces may share a refresh token. Propagate the new
    # token family while preserving each workspace's account_id and identity.
    for other_id in list(registry.index['accounts']):
        if other_id == identifier:
            continue
        other_raw = registry.profile_path(other_id).read_bytes()
        _, other_claims, other_tokens = parse_auth(other_raw)
        if other_claims['sub'] == claims['sub'] and other_tokens['refresh_token'] == tokens['refresh_token']:
            other = json.loads(other_raw)
            for name in ('id_token', 'access_token', 'refresh_token'):
                other['tokens'][name] = new_tokens[name]
            other['last_refresh'] = json.loads(refreshed)['last_refresh']
            candidate = json.dumps(other).encode()
            if parse_auth(candidate)[0] == other_id:
                registry.capture(candidate)
    # Do not replace an account selected or refreshed by an external login.
    live = registry.read_current()
    if current and live and parse_auth(live)[1]['sub'] == claims['sub']:
        _, _, live_tokens = parse_auth(live)
        if live_tokens['refresh_token'] == tokens['refresh_token'] and credential_key(live) == credential_key(current):
            live_id = parse_auth(live)[0]
            if live_id in registry.index['accounts']:
                atomic_write(registry.auth, registry.profile_path(live_id).read_bytes())
    registry.index['accounts'][identifier].pop('refreshFailureKey', None)
    registry.save_index()
    return refreshed


def fetch_usage_with_auth(raw):
    _, _, tokens = parse_auth(raw)
    headers = {'Authorization': 'Bearer ' + tokens['access_token'],
               'ChatGPT-Account-Id': tokens['account_id'],
               'User-Agent': 'CodexLauncher/1.2', 'Accept': 'application/json'}
    data = json.loads(fetch_bytes('https://chatgpt.com/backend-api/wham/usage', headers, 1024 * 1024))
    return data, tokens['account_id']


def refresh_usage(registry, identifier, force=False):
    record = registry.index['accounts'][identifier]
    now = time.time()
    if not force and now - record.get('usageCheckedAt', 0) < 300:
        return
    try:
        raw = current_profile(registry, identifier)
        if parse_auth(raw)[0] != identifier:
            raise ValueError()
        try:
            data, account_id = fetch_usage_with_auth(raw)
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise
            error.close()
            # Retry a newer live token before considering any refresh grant.
            latest = current_profile(registry, identifier)
            if credential_key(latest) != credential_key(raw):
                raw = latest
                try:
                    data, account_id = fetch_usage_with_auth(raw)
                except urllib.error.HTTPError as retry_error:
                    if retry_error.code != 401:
                        raise
                    retry_error.close()
                    raw = renew_profile(registry, identifier, raw)
                    data, account_id = fetch_usage_with_auth(raw)
            else:
                raw = renew_profile(registry, identifier, raw)
                data, account_id = fetch_usage_with_auth(raw)
        record = registry.index['accounts'][identifier]
        record['usage'] = parse_usage(data, account_id, time.time())
        plan = safe_text(data.get('plan_type'))
        if plan:
            record['plan'], record['planSource'] = plan, 'usage'
        record.pop('usageError', None)
        record.pop('refreshFailureKey', None)
    except RefreshFailure as error:
        registry.index['accounts'][identifier]['usageError'] = str(error)
    except urllib.error.HTTPError as error:
        registry.index['accounts'][identifier]['usageError'] = '续期后用量仍无法读取，稍后重试' if error.code == 401 else '用量暂时无法更新'
        error.close()
    except Exception:
        registry.index['accounts'][identifier]['usageError'] = '用量暂时无法更新'
    registry.index['accounts'][identifier]['usageCheckedAt'] = time.time()
    registry.save_index()


def process_table():
    def rows(format):
        result = subprocess.run(['/bin/ps', '-axo', format], capture_output=True, text=True, check=True)
        return result.stdout.splitlines()
    commands = {}
    for line in rows('pid=,comm='):
        values = line.strip().split(None, 1)
        if len(values) == 2:
            commands[int(values[0])] = values[1]
    result = []
    for line in rows('pid=,args='):
        values = line.strip().split(None, 1)
        if len(values) != 2:
            continue
        pid, command = int(values[0]), values[1]
        executable = commands.get(pid, '')
        suffix = command[len(executable):].strip() if command.startswith(executable) else ''
        try:
            arguments = shlex.split(suffix)
        except ValueError:
            arguments = []
        result.append({'pid': pid, 'executable': executable, 'arguments': arguments})
    return result


def is_target(row):
    executable = row['executable']
    if executable.endswith(('/Contents/MacOS/ChatGPT', '/Contents/MacOS/Codex')):
        return True
    if Path(executable).name != 'codex':
        return False
    arguments = row['arguments']
    position = 0
    while position < len(arguments):
        argument = arguments[position]
        if argument in ('-c', '--config', '--enable', '--disable', '-p', '--profile'):
            position += 2
        elif argument.startswith('-'):
            position += 1
        else:
            return argument == 'app-server'
    return False


class Runtime:
    def __init__(self, registry, event=emit):
        self.registry, self.event = registry, event

    def targets(self):
        return [row for row in process_table() if is_target(row)]

    def send_signal(self, row, value):
        # Revalidate PID identity immediately before signalling; never signal a process group.
        current = next((item for item in process_table() if item['pid'] == row['pid']), None)
        if current and current['executable'] == row['executable'] and is_target(current):
            try:
                os.kill(row['pid'], value)
            except ProcessLookupError:
                pass
            except PermissionError:
                raise UserError('无法退出部分 ChatGPT 或 Codex 后台，请手动关闭后重试。') from None

    def stop(self):
        self.event('progress', message='正在退出 ChatGPT 和 Codex 后台…', phase='stopping')
        signalled, deadline, quiet_since = {}, time.monotonic() + 22, None
        while True:
            targets = self.targets()
            now = time.monotonic()
            if not targets:
                quiet_since = quiet_since or now
                if now - quiet_since >= 1:
                    return
            else:
                quiet_since = None
                for row in targets:
                    key = (row['pid'], row['executable'])
                    if key not in signalled:
                        self.send_signal(row, signal.SIGTERM)
                        signalled[key] = now
                    elif now - signalled[key] >= 7:
                        self.send_signal(row, signal.SIGKILL)
            if now >= deadline or len(signalled) > 40:
                raise UserError('Codex 后台仍在运行或被 IDE 自动重启。请关闭相关 IDE 后重试。')
            time.sleep(0.2)

    def launch(self):
        if not APP_PATH.is_dir():
            raise UserError('没有找到 /Applications/ChatGPT.app。')
        self.event('progress', message='正在打开 ChatGPT…', phase='launching')
        result = subprocess.run(['/usr/bin/open', '-n', '--env', 'CODEX_HOME=' + str(self.registry.home),
                                 '--env', 'CODEX_ELECTRON_USER_DATA_PATH=' + str(APP_DATA),
                                 str(APP_PATH), '--args', '--user-data-dir=' + str(APP_DATA)],
                                capture_output=True, text=True)
        if result.returncode:
            raise UserError('无法打开 ChatGPT，请从“应用程序”手动启动。')

    def login(self):
        cli = APP_PATH / 'Contents/Resources/codex'
        if not cli.is_file():
            raise UserError('ChatGPT 安装包中的登录程序不存在。')
        environment = os.environ.copy()
        environment['CODEX_HOME'] = str(self.registry.home)
        self.event('progress', message='请在浏览器中登录要添加的账号', phase='login')
        process = subprocess.Popen([str(cli), 'login', '-c', 'cli_auth_credentials_store="file"'],
                                   env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, bufsize=1, start_new_session=True)
        messages = queue.Queue()
        def read():
            try:
                for line in process.stdout:
                    messages.put(line)
            finally:
                messages.put(None)
        threading.Thread(target=read, daemon=True).start()
        deadline = time.monotonic() + 600
        try:
            while True:
                if time.monotonic() >= deadline:
                    raise UserError('登录等待超时，请重新添加账号。')
                try:
                    line = messages.get(timeout=0.2)
                except queue.Empty:
                    if process.poll() is not None:
                        break
                    continue
                if line is None:
                    break
                for url in re.findall(r'https://[^\s<>]+', line):
                    parsed = urllib.parse.urlparse(url)
                    if parsed.hostname == 'auth.openai.com' and parsed.path.startswith('/oauth/'):
                        self.event('loginURL', url=url)
                # All other CLI output is intentionally discarded, never logged or shown.
            if process.wait(timeout=10) != 0:
                raise UserError('登录没有完成。请重新添加账号。')
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=4)
            process.stdout.close()


class Controller:
    def __init__(self, registry, runtime=None, refresh=refresh_profile):
        self.registry = registry
        self.runtime = runtime or Runtime(registry)
        self.refresh = refresh

    def switch(self, identifier):
        registry, runtime = self.registry, self.runtime
        registry.verify_storage()
        if identifier not in registry.index['accounts']:
            raise UserError('账号已不存在，请刷新列表。')
        target = registry.profile_path(identifier).read_bytes()
        if parse_auth(target)[0] != identifier:
            raise UserError('目标账号的认证与记录不一致，未执行切换。')
        registry.read_current()
        runtime.stop()
        previous = registry.read_current()
        if previous:
            registry.capture(previous)
            if parse_auth(previous)[0] == identifier:
                target = previous
        runtime.event('progress', message='正在切换账号…', phase='switching')
        atomic_write(registry.auth, target)
        if parse_auth(registry.read_current())[0] != identifier:
            raise UserError('认证校验失败，请重新打开启动器。')
        runtime.launch()
        return '已切换账号'

    def add(self):
        registry, runtime = self.registry, self.runtime
        registry.verify_storage()
        registry.read_current()
        runtime.stop()
        previous = registry.read_current()
        if previous:
            registry.capture(previous)
        committed = False
        try:
            runtime.login()
            # An IDE may have respawned while the browser was open.
            if runtime.targets():
                runtime.stop()
            chosen = registry.read_current()
            if chosen is None:
                raise UserError('登录结束后未找到认证文件。')
            identifier = parse_auth(chosen)[0]
            existed = identifier in registry.index['accounts']
            registry.capture(chosen)
            committed = True
            runtime.event('progress', message='正在读取账号资料…', phase='profile')
            try:
                self.refresh(registry, identifier)
            except Exception:
                pass
            runtime.launch()
            return '已刷新此账号的登录' if existed else '新账号已添加'
        except BaseException:
            if not committed:
                pending = None
                try:
                    pending = registry.read_current()
                except (UserError, OSError):
                    pass
                if pending:
                    if previous and parse_auth(pending)[0] == parse_auth(previous)[0]:
                        previous = pending
                        registry.capture(previous)
                    elif pending != previous:
                        atomic_write(registry.profiles / ('recovery-' + uuid.uuid4().hex + '.json'), pending)
                # Do not let a respawned server race with rollback.
                if runtime.targets():
                    try:
                        runtime.stop()
                    except UserError:
                        raise UserError('后台持续重启，登录状态尚未恢复。已有认证备份已保留；请关闭相关 IDE 后，再从列表选择原账号。') from None
                if previous:
                    atomic_write(registry.auth, previous)
                    try:
                        runtime.launch()
                    except Exception:
                        pass
                elif registry.auth.exists() and not registry.auth.is_symlink():
                    registry.auth.unlink()
            raise


def cancel_handler(signum, frame):
    raise Cancelled('登录已取消。')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['list', 'poll', 'usage', 'refresh', 'switch', 'add', 'open'])
    parser.add_argument('--id')
    args = parser.parse_args()
    signal.signal(signal.SIGINT, cancel_handler)
    signal.signal(signal.SIGTERM, cancel_handler)
    registry = Registry()
    with registry.locked():
        registry.verify_storage()
        current = registry.bootstrap()
        controller = Controller(registry)
        message = ''
        if args.action == 'switch':
            message = controller.switch(args.id)
        elif args.action == 'add':
            message = controller.add()
        elif args.action == 'open':
            controller.runtime.launch()
        elif args.action == 'refresh':
            if current:
                refresh_profile(registry, parse_auth(current)[0])
        if args.action in ('poll', 'usage', 'refresh', 'add'):
            for identifier in registry.index['accounts']:
                refresh_usage(registry, identifier, force=args.action != 'poll')
        emit('result', message=message, **registry.snapshot())


if __name__ == '__main__':
    try:
        main()
    except Busy as error:
        emit('error', message=str(error), code='busy')
        sys.exit(2)
    except Cancelled as error:
        emit('error', message=str(error), code='cancelled')
        sys.exit(130)
    except UserError as error:
        emit('error', message=str(error), code='user')
        sys.exit(1)
    except Exception:
        # No traceback: exception text from IO or network libraries can contain secrets.
        emit('error', message='操作未完成。请检查应用和账号文件是否可访问，然后重试。', code='internal')
        sys.exit(1)
