# -*- coding: utf-8 -*-
"""PKU HPC 后台桥进程（HTTP 守护进程）。

独立于 Harness 进程运行：由 Host 插件（index.js）用 ``pythonw hpc_bridge.py``
启动，绑定 127.0.0.1 的随机端口，把身份写进 ``<data-dir>/bridge.json``。
Harness 重启、插件重装都不影响这里已经登录的 SSH 会话。

命令行（Host 插件传入）：

    python hpc_bridge.py --data-dir <目录> --idle-exit-minutes 720 [--paramiko-path <目录>]

接口语义见仓库根目录 ``CONTRACT.md`` 的「守护进程 HTTP API」一节。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

# 允许两种用法：``python bridge/hpc_bridge.py``（脚本）与
# ``import bridge.hpc_bridge``（测试）；插件目录本身不在 sys.path 时补进去。
try:  # pragma: no cover - 取决于调用方式
    from . import hpc_core
except ImportError:  # 直接运行脚本时按同目录模块导入
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import hpc_core  # type: ignore

# 两种导入方式都绑定到模块对象，避免 ``bridge.hpc_bridge`` 下找不到顶层的 hpc_core
DEFAULT_REMOTE_DIR = hpc_core.DEFAULT_REMOTE_DIR
HpcError = hpc_core.HpcError
HpcClient = hpc_core.HpcClient
VERSION = hpc_core.VERSION
credentials_available = hpc_core.credentials_available
has_password = hpc_core.has_password
load_password = hpc_core.load_password
normalize_submit_options = hpc_core.normalize_submit_options
save_password = hpc_core.save_password
submit_jobs = hpc_core.submit_jobs

DEFAULT_IDLE_EXIT_MINUTES = 720
HARD_MAX_SECONDS = 24 * 3600
LOG_MAX_BYTES = 1024 * 1024


# --------------------------------------------------------------------- 工具
def _iso_now():
    """本地时间 ISO 8601（带时区偏移，例如 2026-10-23T16:30:00+08:00）。"""
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _clamp_int(value, fallback, minimum, maximum):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, number))


class BridgeLogger:
    """追加写 ``<data-dir>/bridge.log``；文件超过 1 MB 就截断重写。"""

    def __init__(self, path, max_bytes=LOG_MAX_BYTES):
        self.path = Path(path)
        self.max_bytes = int(max_bytes)
        self._lock = threading.Lock()

    def log(self, level, message):
        stamp = time.strftime('%Y-%m-%d %H:%M:%S')
        line = '[%s] [%s] %s\n' % (stamp, level, message)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                    with self.path.open('w', encoding='utf-8', newline='\n') as handle:
                        handle.write('[%s] [INFO] 日志超过 1 MB，已截断重写\n' % stamp)
                with self.path.open('a', encoding='utf-8', newline='\n') as handle:
                    handle.write(line)
            except OSError:
                # 日志写失败不能影响守护进程本身
                pass

    def info(self, message):
        self.log('INFO', message)

    def warning(self, message):
        self.log('WARN', message)

    def error(self, message):
        self.log('ERROR', message)


class _Session:
    __slots__ = ('client', 'host', 'port', 'user', 'since')

    def __init__(self):
        self.client = None
        self.host = ''
        self.port = hpc_core.DEFAULT_PORT
        self.user = ''
        self.since = ''

    def reset(self):
        self.client = None
        self.host = ''
        self.port = hpc_core.DEFAULT_PORT
        self.user = ''
        self.since = ''


# ------------------------------------------------------------------ HTTP 服务
class BridgeRequestHandler(BaseHTTPRequestHandler):
    """带 bearer token 鉴权的 JSON handler（错误也用 200 + {ok:false}）。"""

    server_version = 'PkuHpcBridge/' + VERSION
    protocol_version = 'HTTP/1.0'

    # 用 BridgeApp 的日志，不要往 stderr 打（pythonw 下 stderr 不可靠）
    def log_message(self, fmt, *args):  # noqa: A003 - 覆盖标准库命名
        return

    def log_error(self, fmt, *args):
        return

    # ------------------------------------------------------------- 基础
    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        try:
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except OSError:
            pass

    def _read_body(self):
        raw_length = self.headers.get('Content-Length')
        try:
            length = int(raw_length or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return b''
        return self.rfile.read(length)

    def _authorized(self):
        app = self.server.app
        header = self.headers.get('Authorization') or ''
        expected = 'Bearer ' + app.token
        return secrets.compare_digest(header, expected)

    # ------------------------------------------------------------- 路由
    def do_GET(self):  # noqa: N802 - 标准库命名
        self._dispatch('GET')

    def do_POST(self):  # noqa: N802
        self._dispatch('POST')

    def _dispatch(self, method):
        app = self.server.app
        parsed = urlsplit(self.path)
        started = time.time()
        if not self._authorized():
            self._send(401, {'ok': False, 'error': 'unauthorized'})
            return
        route = _ROUTES.get((method, parsed.path))
        if route is None:
            self._send(404, {'ok': False, 'error': 'not found'})
            return
        handler = getattr(app, route)
        body = {}
        if method == 'POST':
            raw = self._read_body()
            if raw:
                try:
                    body = json.loads(raw.decode('utf-8'))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._send(200, {'ok': False, 'error': '请求体不是合法的 JSON'})
                    return
                if not isinstance(body, dict):
                    self._send(200, {'ok': False, 'error': '请求体必须是 JSON 对象'})
                    return
        app.touch()
        try:
            result = handler(query=parse_qs(parsed.query), body=body)
            status = 200
            if not isinstance(result, dict):
                result = {'ok': True}
        except HpcError as exc:
            result = {'ok': False, 'error': str(exc)}
            status = 200
            app.logger.info('%s %s -> 业务错误：%s' % (method, parsed.path, exc))
        except Exception as exc:  # noqa: BLE001 - 任何异常都要变成可读 JSON
            app.logger.error('%s %s 异常：%s\n%s' % (method, parsed.path, exc, traceback.format_exc()))
            result = {'ok': False, 'error': '内部错误：%s' % (str(exc).strip() or exc.__class__.__name__)}
            status = 200
        self._send(status, result)
        if route != 'handle_health':
            elapsed = int((time.time() - started) * 1000)
            app.logger.info('%s %s -> %d (%d ms)' % (method, parsed.path, status, elapsed))
        if route == 'handle_shutdown':
            app.request_stop('收到 /shutdown 请求')


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, BridgeRequestHandler)


# 路由表：HTTP 方法 + 路径 -> BridgeApp 的方法名
_ROUTES = {
    ('GET', '/health'): 'handle_health',
    ('GET', '/state'): 'handle_state',
    ('POST', '/login'): 'handle_login',
    ('POST', '/logout'): 'handle_logout',
    ('POST', '/exec'): 'handle_exec',
    ('GET', '/queue'): 'handle_queue',
    ('POST', '/submit'): 'handle_submit',
    ('POST', '/cancel'): 'handle_cancel',
    ('GET', '/files'): 'handle_files',
    ('POST', '/download'): 'handle_download',
    ('POST', '/shutdown'): 'handle_shutdown',
}


# ------------------------------------------------------------------- 守护进程
class BridgeApp:
    """HTTP 守护进程的全部状态与处理逻辑（可被测试直接实例化，不阻塞）。"""

    def __init__(
        self,
        data_dir,
        idle_exit_minutes=DEFAULT_IDLE_EXIT_MINUTES,
        host='127.0.0.1',
        port=0,
        hard_max_seconds=HARD_MAX_SECONDS,
        poll_seconds=2.0,
    ):
        self.data_dir = Path(str(data_dir)).expanduser()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logger = BridgeLogger(self.data_dir / 'bridge.log')
        self.identity_path = self.data_dir / 'bridge.json'
        self.token = secrets.token_hex(32)
        self.version = VERSION
        self.started_at = _iso_now()
        self.host = host
        self.port = int(port or 0)
        self.server = None
        self.stop_event = threading.Event()
        self.hard_max_seconds = float(hard_max_seconds)
        self.poll_seconds = max(0.01, float(poll_seconds))
        try:
            minutes = float(idle_exit_minutes)
        except (TypeError, ValueError):
            minutes = float(DEFAULT_IDLE_EXIT_MINUTES)
        self.idle_seconds = max(0.0, minutes) * 60.0

        self._state_lock = threading.RLock()
        self._op_lock = threading.RLock()
        self._session = _Session()
        self._password_saved = False
        self._last_host = ''
        self._last_user = ''
        self._load_last_target()
        self._password_saved = self._password_saved_for(self._last_host, self._last_user)
        self._last_activity = time.monotonic()
        self._last_activity_iso = self.started_at
        self._started_monotonic = time.monotonic()
        self._serve_thread = None
        self._idle_thread = None
        self._closed = False

    # --------------------------------------------------------------- 生命周期
    def start(self):
        """绑定端口、写身份文件、启动 HTTP 与空闲退出线程（立即返回）。"""
        self.server = BridgeHTTPServer((self.host, self.port), self)
        self.port = int(self.server.server_address[1])
        self._write_identity()
        self._serve_thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={'poll_interval': 0.2},
            name='pku-hpc-bridge-http',
            daemon=True,
        )
        self._serve_thread.start()
        self._idle_thread = threading.Thread(
            target=self._idle_loop, name='pku-hpc-bridge-idle', daemon=True
        )
        self._idle_thread.start()
        self.logger.info(
            '后台进程启动：pid=%d port=%d idle=%d 分钟 hard_max=%.1f 小时'
            % (os.getpid(), self.port, round(self.idle_seconds / 60), self.hard_max_seconds / 3600)
        )
        return self

    def wait(self):
        """阻塞到空闲退出 / /shutdown / 硬上限触发。"""
        self.stop_event.wait()

    def request_stop(self, reason):
        if not self.stop_event.is_set():
            self.logger.info('请求退出：%s' % reason)
        self.stop_event.set()

    def stop(self):
        """关闭 HTTP 服务、断开 SSH、删除身份文件（幂等）。"""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self.stop_event.set()
        server = self.server
        self.server = None
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                server.server_close()
            except Exception:  # noqa: BLE001
                pass
        with self._state_lock:
            client = self._session.client
            self._session.reset()
        if client is not None:
            client.close()
        self._remove_identity()
        self.logger.info('后台进程退出（pid=%d）' % os.getpid())

    # --------------------------------------------------------------- 身份文件
    def _write_identity(self):
        payload = {
            'pid': os.getpid(),
            'port': self.port,
            'token': self.token,
            'startedAt': self.started_at,
            'version': self.version,
        }
        temporary = self.data_dir / ('.bridge.json.tmp.%d' % os.getpid())
        try:
            with temporary.open('w', encoding='utf-8', newline='\n') as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary), str(self.identity_path))
        except OSError as exc:
            raise HpcError('无法写入身份文件 %s：%s' % (self.identity_path, exc)) from exc
        finally:
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass

    def _remove_identity(self):
        """只在身份文件确实属于本进程时删除，避免误删别的守护进程的。"""
        try:
            if not self.identity_path.exists():
                return
            data = json.loads(self.identity_path.read_text(encoding='utf-8'))
            if int(data.get('pid') or 0) == os.getpid() and data.get('token') == self.token:
                self.identity_path.unlink()
        except (OSError, ValueError, TypeError):
            pass

    # --------------------------------------------------------------- 空闲退出
    def _idle_loop(self):
        while not self.stop_event.wait(self.poll_seconds):
            now = time.monotonic()
            uptime = now - self._started_monotonic
            if uptime >= self.hard_max_seconds:
                self.request_stop(
                    '已连续运行 %.1f 小时，达到硬上限（%.1f 小时）'
                    % (uptime / 3600.0, self.hard_max_seconds / 3600.0)
                )
                return
            if self.idle_seconds > 0 and now - self._last_activity >= self.idle_seconds:
                if not self.has_live_session():
                    self.request_stop('空闲超过 %d 分钟，退出' % round(self.idle_seconds / 60.0))
                    return

    def touch(self):
        """记录一次活动（HTTP 请求 / SSH 操作）。"""
        now = time.monotonic()
        with self._state_lock:
            self._last_activity = now
            self._last_activity_iso = _iso_now()

    def has_live_session(self):
        with self._state_lock:
            client = self._session.client
        try:
            return bool(client is not None and client.is_alive())
        except Exception:  # noqa: BLE001
            return False

    # --------------------------------------------------------------- 会话视图
    def session_view(self):
        with self._state_lock:
            session = self._session
            client = session.client
            connected = False
            if client is not None:
                try:
                    connected = bool(client.is_alive())
                except Exception:  # noqa: BLE001
                    connected = False
            view = {
                'connected': connected,
                'host': session.host or '',
                'port': int(session.port or hpc_core.DEFAULT_PORT),
                'user': session.user or '',
                'since': session.since or '',
                'lastActivity': self._last_activity_iso,
                'passwordSaved': bool(self._password_saved),
            }
            if not connected and self._last_host != '':
                # 未连接时用上次登录的 host/user 查凭据管理器，重启后提示仍然准确。
                view['passwordSaved'] = self._password_saved_for(
                    self._last_host, self._last_user
                )
            # 上次登录留下的 host/user：插件不再内置默认学号，
            # 设置页用它回填输入框，用户就不必每次重填。
            view['lastHost'] = self._last_host or ''
            view['lastUser'] = self._last_user or ''
        return view

    def _require_client(self):
        with self._state_lock:
            client = self._session.client
        if client is None or not client.is_alive():
            raise HpcError('尚未登录 HPC（请先在设置里用密码 + OTP 登录）')
        return client

    def _set_session(self, client, host, port, user):
        with self._state_lock:
            old = self._session.client
            self._session.client = client
            self._session.host = host
            self._session.port = int(port)
            self._session.user = user
            self._session.since = _iso_now()
        if old is not None and old is not client:
            old.close()

    def _password_saved_for(self, host, user):
        if not credentials_available() or host == '' or user == '':
            return False
        return bool(has_password(host, user))

    # 只记 host/user（不含密码），让守护进程重启后仍能报告“密码是否已保存”。
    def _last_target_path(self):
        return self.data_dir / 'last-target.json'

    def _load_last_target(self):
        try:
            data = json.loads(self._last_target_path().read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self._last_host = str(data.get('host') or '')
            self._last_user = str(data.get('user') or '')

    def _save_last_target(self, host, user):
        self._last_host = host or ''
        self._last_user = user or ''
        try:
            temporary = self._last_target_path().with_suffix('.json.tmp')
            temporary.write_text(
                json.dumps({'host': self._last_host, 'user': self._last_user}, ensure_ascii=False),
                encoding='utf-8',
            )
            temporary.replace(self._last_target_path())
        except OSError:
            pass

    # --------------------------------------------------------------- HTTP 处理
    def handle_health(self, query=None, body=None):
        view = self.session_view()
        return {
            'ok': True,
            'pid': os.getpid(),
            'version': self.version,
            'startedAt': self.started_at,
            'session': view,
            'passwordSaved': view['passwordSaved'],
        }

    def handle_state(self, query=None, body=None):
        view = self.session_view()
        return {
            'ok': True,
            'session': view,
            'passwordSaved': view['passwordSaved'],
        }

    def handle_login(self, query=None, body=None):
        body = body or {}
        host = str(body.get('host') or '').strip()
        user = str(body.get('user') or '').strip()
        try:
            port = int(body.get('port') or hpc_core.DEFAULT_PORT)
        except (TypeError, ValueError):
            port = hpc_core.DEFAULT_PORT
        password = body.get('password')
        password = password if isinstance(password, str) else ''
        otp = body.get('otp')
        otp = otp if isinstance(otp, str) else ''
        remember = body.get('remember', False) is True

        if host == '':
            return {'ok': False, 'error': '缺少登录节点地址（host）'}
        if user == '':
            return {'ok': False, 'error': '缺少学号（user）'}

        if password == '':
            try:
                saved_password = load_password(host, user)
            except HpcError:
                saved_password = None
            if not saved_password:
                return {'ok': False, 'error': '没有已保存的密码，请先输入密码'}
            password = saved_password

        client = HpcClient(host=host, port=port, user=user, password=password, otp=otp)
        try:
            client.connect()
        except HpcError as exc:
            self.logger.info('登录失败 %s@%s：%s' % (user, host, exc))
            return {'ok': False, 'error': str(exc)}
        except Exception as exc:  # noqa: BLE001
            self.logger.error('登录异常 %s@%s：%s' % (user, host, exc))
            return {'ok': False, 'error': 'SSH 登录失败：%s' % exc}

        with self._op_lock:
            self._set_session(client, host, port, user)
            self._save_last_target(host, user)
        self.touch()

        if remember:
            try:
                save_password(host, user, password)
                saved = True
            except Exception as exc:  # noqa: BLE001 - best-effort，失败不影响登录
                self.logger.warning('保存密码到凭据管理器失败：%s' % exc)
                saved = False
        else:
            saved = self._password_saved_for(host, user)
        with self._state_lock:
            self._password_saved = saved
        view = self.session_view()
        self.logger.info('登录成功：%s@%s:%d' % (user, host, port))
        return {'ok': True, 'session': view, 'passwordSaved': view['passwordSaved']}

    def handle_logout(self, query=None, body=None):
        with self._op_lock:
            with self._state_lock:
                client = self._session.client
                host = self._session.host
                user = self._session.user
                self._session.reset()
            if client is not None:
                client.close()
            if host and user:
                with self._state_lock:
                    self._password_saved = self._password_saved_for(host, user)
        self.touch()
        return {'ok': True, 'session': self.session_view()}

    def handle_exec(self, query=None, body=None):
        body = body or {}
        command = body.get('cmd')
        if not isinstance(command, str) or command.strip() == '':
            raise HpcError('命令为空')
        timeout = _clamp_int(body.get('timeout'), 120, 1, 600)
        with self._op_lock:
            client = self._require_client()
            result = client.run_command(command, timeout=timeout)
        self.touch()
        return {'ok': True, 'output': result['output'], 'code': result['code']}

    def handle_queue(self, query=None, body=None):
        with self._op_lock:
            client = self._require_client()
            result = client.query_queue()
        self.touch()
        return {'ok': True, 'text': result['text'], 'rows': result['rows']}

    def handle_submit(self, query=None, body=None):
        body = body or {}
        raw_files = body.get('files')
        if not isinstance(raw_files, list) or not raw_files:
            raise HpcError('请给出要提交的 .gjf 文件（files）')
        paths = []
        for item in raw_files:
            if not isinstance(item, str) or item.strip() == '':
                raise HpcError('本地文件路径为空')
            path = Path(item.strip()).expanduser()
            if not path.is_file():
                raise HpcError('本地文件不存在：%s' % path)
            if path.suffix.lower() != '.gjf':
                raise HpcError('不是 .gjf 文件：%s' % path)
            paths.append(str(path))

        remote_dir = body.get('remote_dir')
        remote_dir = str(remote_dir).strip() if isinstance(remote_dir, str) else ''
        if remote_dir == '':
            remote_dir = DEFAULT_REMOTE_DIR
        options = normalize_submit_options(body.get('options') or {})
        dependency = body.get('dependency', True)
        dependency = dependency is not False

        with self._op_lock:
            client = self._require_client()
            result = submit_jobs(client, remote_dir, paths, options, dependency)
        self.touch()

        jobs = result['jobs']
        failed = [job for job in jobs if not job.get('ok')]
        response = {'ok': not failed, 'remote_dir': result['remote_dir'], 'jobs': jobs}
        if failed:
            details = '；'.join(
                '%s：%s' % (job['name'], (job.get('output') or 'sbatch 返回非 0').strip())
                for job in failed[:3]
            )
            response['error'] = '提交失败：%s' % details
        self.logger.info(
            '提交作业到 %s：%s'
            % (result['remote_dir'], '，'.join('%s=%s' % (job['name'], job['jobid']) for job in jobs))
        )
        return response

    def handle_cancel(self, query=None, body=None):
        body = body or {}
        raw_ids = body.get('ids')
        if not isinstance(raw_ids, list) or not raw_ids:
            raise HpcError('缺少要取消的作业号（ids）')
        ids = [str(item).strip() for item in raw_ids if str(item).strip()]
        if not ids:
            raise HpcError('缺少要取消的作业号（ids）')
        with self._op_lock:
            client = self._require_client()
            result = client.cancel_jobs(ids)
        self.touch()
        if result.get('code') != 0:
            return {
                'ok': False,
                'error': '取消失败：%s' % (result.get('output') or 'scancel 返回非 0'),
                'ids': result['ids'],
            }
        return {'ok': True, 'output': result['output'], 'ids': result['ids']}

    def handle_files(self, query=None, body=None):
        query = query or {}
        directory = (query.get('dir') or [DEFAULT_REMOTE_DIR])[0] or DEFAULT_REMOTE_DIR
        suffix = (query.get('suffix') or [''])[0]
        depth = _clamp_int((query.get('depth') or ['2'])[0], 2, 1, 8)
        with self._op_lock:
            client = self._require_client()
            info = client.list_files(directory, depth=depth)
        self.touch()
        files = info['files']
        if suffix:
            lowered = suffix.lower()
            files = [item for item in files if item['name'].lower().endswith(lowered)]
        return {'ok': True, 'root': info['root'], 'exists': info['exists'], 'files': files}

    def handle_download(self, query=None, body=None):
        body = body or {}
        remote_dir = body.get('remote_dir')
        remote_dir = str(remote_dir).strip() if isinstance(remote_dir, str) else ''
        if remote_dir == '':
            remote_dir = DEFAULT_REMOTE_DIR
        raw_names = body.get('names')
        if not isinstance(raw_names, list) or not raw_names:
            raise HpcError('缺少要下载的远程文件（names）')
        names = [str(item) for item in raw_names if str(item).strip() != '']
        if not names:
            raise HpcError('缺少要下载的远程文件（names）')
        local_dir = body.get('local_dir')
        local_dir = str(local_dir).strip() if isinstance(local_dir, str) else ''
        if local_dir == '':
            raise HpcError('缺少本地目录（local_dir）')
        with self._op_lock:
            client = self._require_client()
            saved = client.download_files(remote_dir, names, local_dir)
        self.touch()
        return {'ok': True, 'files': saved}

    def handle_shutdown(self, query=None, body=None):
        return {'ok': True}


# --------------------------------------------------------------------- 入口
def default_data_dir():
    home = os.environ.get('USERPROFILE') or os.environ.get('HOME') or str(Path.home())
    return Path(home) / '.pku-hpc'


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog='hpc_bridge.py',
        description='PKU HPC 后台桥进程：保持 SSH 登录，提供本机 HTTP API。',
    )
    parser.add_argument('--data-dir', default='', help='数据目录（bridge.json / bridge.log）')
    parser.add_argument(
        '--idle-exit-minutes', type=float, default=DEFAULT_IDLE_EXIT_MINUTES,
        help='空闲多少分钟后自动退出（默认 720）',
    )
    parser.add_argument('--paramiko-path', default='', help='额外的 paramiko 搜索目录')
    parser.add_argument('--version', action='store_true', help='打印版本号后退出')
    return parser.parse_args(argv)


def _ensure_std_streams():
    """pythonw.exe 下 sys.stdout/stderr 可能是 None，避免 print/argparse 崩溃。"""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w', encoding='utf-8', errors='replace')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w', encoding='utf-8', errors='replace')


def main(argv=None):
    _ensure_std_streams()
    args = parse_args(argv)
    if args.version:
        print('pku-hpc-bridge %s' % VERSION)
        return 0
    if args.paramiko_path:
        path = str(Path(args.paramiko_path).expanduser())
        if path not in sys.path:
            sys.path.insert(0, path)
    data_dir = Path(args.data_dir).expanduser() if args.data_dir else default_data_dir()

    try:
        app = BridgeApp(data_dir, idle_exit_minutes=args.idle_exit_minutes)
    except OSError as exc:
        print('无法创建数据目录 %s：%s' % (data_dir, exc), file=sys.stderr)
        return 1
    try:
        app.start()
    except Exception as exc:  # noqa: BLE001 - 启动失败必须写日志，Host 侧靠日志诊断
        app.logger.error('启动失败：%s\n%s' % (exc, traceback.format_exc()))
        print('后台进程启动失败：%s' % exc, file=sys.stderr)
        return 1
    try:
        app.wait()
    except KeyboardInterrupt:
        app.request_stop('收到 Ctrl-C')
    finally:
        app.stop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
