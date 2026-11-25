# -*- coding: utf-8 -*-
"""hpc_bridge HTTP 守护进程的端到端测试（线程 + 127.0.0.1 随机端口）。

覆盖 CONTRACT.md「守护进程 HTTP API」：bearer 401、身份文件、登录（含错误
OTP）、exec、queue、submit（afterok）、files/download、logout、/shutdown、
以及空闲退出 / 硬上限行为。假 SSH 服务器来自 fake_ssh_server.py。
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

_SENTINEL = object()   # 用默认 token
_NO_TOKEN = object()   # 完全不带 Authorization 头

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _path in (str(ROOT), str(HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import paramiko  # noqa: E402

from bridge import hpc_bridge, hpc_core  # noqa: E402
from fake_ssh_server import (  # noqa: E402
    DEFAULT_OTP,
    DEFAULT_PASSWORD,
    DEFAULT_USER,
    FAKE_HOME,
    FakeSSHServer,
)


class BridgeHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._host_key = paramiko.RSAKey.generate(2048)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.ssh = FakeSSHServer(self.base / 'remote', host_key=self._host_key).start()
        self.app = hpc_bridge.BridgeApp(
            self.base / 'data', idle_exit_minutes=720, poll_seconds=0.1
        ).start()

    def tearDown(self):
        try:
            self.app.stop()
        finally:
            self.ssh.stop()
            self._tmp.cleanup()

    # ------------------------------------------------------------ HTTP 工具
    def request(self, method, path, body=None, token=_SENTINEL):
        url = 'http://127.0.0.1:%d%s' % (self.app.port, path)
        data = None if body is None else json.dumps(body).encode('utf-8')
        request = urllib.request.Request(url, data=data, method=method)
        if token is _SENTINEL:
            request.add_header('Authorization', 'Bearer ' + self.app.token)
        elif token is not _NO_TOKEN:
            request.add_header('Authorization', 'Bearer ' + str(token))
        if data is not None:
            request.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode('utf-8'))

    def login(self, otp=DEFAULT_OTP, remember=False, password=DEFAULT_PASSWORD):
        return self.request(
            'POST',
            '/login',
            {
                'host': '127.0.0.1',
                'port': self.ssh.port,
                'user': DEFAULT_USER,
                'password': password,
                'otp': otp,
                'remember': remember,
            },
        )[1]

    def assert_ok(self, payload):
        self.assertTrue(payload.get('ok'), payload)
        return payload

    # ------------------------------------------------------------ 鉴权
    def test_health_requires_bearer_token(self):
        status, payload = self.request('GET', '/health', token=_NO_TOKEN)
        self.assertEqual(status, 401)
        self.assertEqual(payload, {'ok': False, 'error': 'unauthorized'})

        status, payload = self.request('GET', '/health', token='wrong-token')
        self.assertEqual(status, 401)
        self.assertFalse(payload['ok'])

        status, payload = self.request('GET', '/health')
        self.assertEqual(status, 200)
        self.assertTrue(payload['ok'])

    def test_identity_file_and_health_fields(self):
        identity = json.loads(
            (self.app.data_dir / 'bridge.json').read_text(encoding='utf-8')
        )
        self.assertEqual(identity['pid'], os.getpid())
        self.assertEqual(identity['port'], self.app.port)
        self.assertEqual(identity['version'], '1.0.0')
        self.assertEqual(len(identity['token']), 64)
        self.assertRegex(identity['token'], r'^[0-9a-f]{64}$')
        self.assertRegex(identity['startedAt'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$')

        payload = self.assert_ok(self.request('GET', '/health')[1])
        self.assertEqual(payload['pid'], os.getpid())
        self.assertEqual(payload['version'], '1.0.0')
        self.assertEqual(payload['startedAt'], identity['startedAt'])
        self.assertFalse(payload['session']['connected'])
        self.assertFalse(payload['session']['passwordSaved'])
        self.assertTrue((self.app.data_dir / 'bridge.log').is_file())

        state = self.assert_ok(self.request('GET', '/state')[1])
        self.assertFalse(state['session']['connected'])
        self.assertIn('session', state)

    def test_state_exposes_last_login_target_for_prefill(self):
        """插件不内置默认学号（公开仓库），设置页靠 lastUser 回填输入框。"""
        target = self.app.data_dir / 'last-target.json'
        target.write_text(
            json.dumps({'host': 'wmjx2-login.pku.edu.cn', 'user': '123456789'}),
            encoding='utf-8',
        )
        self.app._load_last_target()

        session = self.assert_ok(self.request('GET', '/state')[1])['session']

        self.assertEqual(session['lastHost'], 'wmjx2-login.pku.edu.cn')
        self.assertEqual(session['lastUser'], '123456789')
        # 未连接时 user 仍为空，回填只走 lastUser，不要混淆两者
        self.assertEqual(session['user'], '')

    def test_unknown_route_404_and_bad_json(self):
        status, payload = self.request('GET', '/nope')
        self.assertEqual(status, 404)
        self.assertFalse(payload['ok'])

        url = 'http://127.0.0.1:%d/exec' % self.app.port
        request = urllib.request.Request(
            url, data=b'{not json', method='POST',
            headers={
                'Authorization': 'Bearer ' + self.app.token,
                'Content-Type': 'application/json',
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            body = json.loads(response.read().decode('utf-8'))
        self.assertFalse(body['ok'])
        self.assertIn('JSON', body['error'])

    # ------------------------------------------------------------ 登录
    def test_login_with_wrong_otp_returns_readable_error(self):
        payload = self.login(otp='000000')
        self.assertFalse(payload['ok'])
        self.assertIn('SSH 登录失败', payload['error'])
        self.assertIn('OTP', payload['error'])
        # 失败后不应该有会话
        health = self.assert_ok(self.request('GET', '/health')[1])
        self.assertFalse(health['session']['connected'])

    def test_login_with_saved_password_when_password_empty(self):
        with mock.patch.object(hpc_bridge, 'load_password', return_value=None):
            payload = self.login(password='')
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['error'], '没有已保存的密码，请先输入密码')

        with mock.patch.object(
            hpc_bridge, 'load_password', return_value=DEFAULT_PASSWORD
        ):
            payload = self.login(password='')
        self.assertTrue(payload['ok'], payload)
        self.assertTrue(payload['session']['connected'])

    def test_remember_controls_credential_write(self):
        with mock.patch.object(hpc_bridge, 'save_password', return_value=True) as save:
            payload = self.login(remember=False)
            save.assert_not_called()
        self.assertTrue(payload['ok'], payload)
        self.assertEqual(self.request('POST', '/logout')[1]['session']['connected'], False)

        with mock.patch.object(hpc_bridge, 'save_password', return_value=True) as save:
            payload = self.login(remember=True)
            save.assert_called_once()
            host, user, password = save.call_args[0]
            self.assertEqual((host, user), ('127.0.0.1', DEFAULT_USER))
            self.assertEqual(password, DEFAULT_PASSWORD)
        self.assertTrue(payload['ok'], payload)
        self.assertTrue(payload['passwordSaved'])

    def test_password_never_written_to_disk(self):
        self.login(remember=False)
        for name in ('bridge.json', 'bridge.log'):
            text = (self.app.data_dir / name).read_bytes().decode('utf-8', 'replace')
            self.assertNotIn(DEFAULT_PASSWORD, text)
            self.assertNotIn(DEFAULT_OTP, text)
            self.assertNotIn('wrong-password', text)

    # ------------------------------------------------------------ 操作
    def test_exec_queue_logout(self):
        login = self.assert_ok(self.login())
        self.assertTrue(login['session']['connected'])
        self.assertEqual(login['session']['host'], '127.0.0.1')
        self.assertEqual(login['session']['user'], DEFAULT_USER)
        self.assertEqual(login['session']['port'], self.ssh.port)

        health = self.assert_ok(self.request('GET', '/health')[1])
        self.assertTrue(health['session']['connected'])

        result = self.assert_ok(
            self.request('POST', '/exec', {'cmd': 'echo $HOME', 'timeout': 30})[1]
        )
        self.assertEqual(result['code'], 0)
        self.assertIn(FAKE_HOME, result['output'])

        queue = self.assert_ok(self.request('GET', '/queue')[1])
        self.assertIn('100001', queue['text'])
        self.assertEqual([row['jobid'] for row in queue['rows']], ['100001', '100002'])
        self.assertEqual(queue['rows'][0]['name'], 'A1')

        logout = self.assert_ok(self.request('POST', '/logout')[1])
        self.assertFalse(logout['session']['connected'])
        # 退出后再 exec 应该报中文错误
        payload = self.request('POST', '/exec', {'cmd': 'echo $HOME'})[1]
        self.assertFalse(payload['ok'])
        self.assertIn('尚未登录', payload['error'])

    def test_submit_uploads_scripts_and_afterok(self):
        self.assert_ok(self.login())
        local = self.base / 'jobs'
        local.mkdir()
        files = []
        for name in ('A1', 'A1_td', 'A1_gs', 'A1_fc'):
            path = local / (name + '.gjf')
            path.write_text('# %s\n' % name, encoding='utf-8', newline='\n')
            files.append(str(path))
        options = {
            'cluster': 'jx2',
            'partition': 'compute',
            'qos': '',
            'ntasks': 32,
            'nodes': 1,
            'walltime': '2-00:00:00',
            'sbatch_out': 'job.%j.out',
            'include_env': False,
            'redirect': True,
            'g16root': '$HOME',
            'scr_dir': '$HOME/tmp',
        }
        payload = self.assert_ok(
            self.request(
                'POST',
                '/submit',
                {
                    'remote_dir': '~/g16jobs',
                    'files': files,
                    'options': options,
                    'dependency': True,
                },
            )[1]
        )
        self.assertEqual(payload['remote_dir'], FAKE_HOME + '/g16jobs')
        jobs = payload['jobs']
        self.assertEqual([job['name'] for job in jobs], ['A1', 'A1_gs', 'A1_td', 'A1_fc'])
        self.assertTrue(all(job['ok'] for job in jobs))
        self.assertTrue(all(re.fullmatch(r'\d{3,}', job['jobid']) for job in jobs))

        calls = self.ssh.sbatch_calls
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[0]['dependency'], '')
        self.assertEqual(calls[1]['dependency'], '')
        self.assertEqual(calls[2]['dependency'], 'afterok:' + jobs[0]['jobid'])
        self.assertEqual(calls[3]['dependency'], 'afterok:' + jobs[1]['jobid'])
        self.assertEqual(
            calls[0]['script_text'],
            hpc_core.build_slurm_script('A1', {'cluster': 'jx2'}),
        )
        remote_dir = self.ssh.local_for(FAKE_HOME + '/g16jobs')
        self.assertTrue((remote_dir / 'job_A1.sh').is_file())
        self.assertTrue((remote_dir / 'A1.gjf').is_file())

    def test_submit_validation_errors_are_chinese(self):
        payload = self.request(
            'POST', '/submit', {'files': [str(self.base / 'missing.gjf')]}
        )[1]
        self.assertFalse(payload['ok'])
        self.assertIn('本地文件不存在', payload['error'])

        text_file = self.base / 'a.txt'
        text_file.write_text('x', encoding='utf-8')
        payload = self.request('POST', '/submit', {'files': [str(text_file)]})[1]
        self.assertFalse(payload['ok'])
        self.assertIn('.gjf', payload['error'])

        payload = self.request('POST', '/submit', {'files': []})[1]
        self.assertFalse(payload['ok'])
        self.assertIn('files', payload['error'])

    def test_files_and_download(self):
        self.assert_ok(self.login())
        remote = self.ssh.local_for(FAKE_HOME + '/g16jobs')
        (remote / 'sub').mkdir(parents=True)
        (remote / 'A1.log').write_text('log-a\n', encoding='utf-8')
        (remote / 'sub' / 'A1_td.log').write_text('log-td\n', encoding='utf-8')
        (remote / 'A1.chk').write_bytes(b'chk')

        listing = self.assert_ok(
            self.request(
                'GET', '/files?dir=%s&suffix=.log&depth=2'
                % urllib.parse.quote('~/g16jobs')
            )[1]
        )
        self.assertTrue(listing['exists'])
        self.assertEqual(listing['root'], FAKE_HOME + '/g16jobs')
        names = [item['name'] for item in listing['files']]
        self.assertEqual(sorted(names), ['A1.log', 'sub/A1_td.log'])
        for item in listing['files']:
            self.assertRegex(item['mtime'], r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$')

        local_dir = self.base / 'downloads'
        payload = self.assert_ok(
            self.request(
                'POST',
                '/download',
                {
                    'remote_dir': '~/g16jobs',
                    'names': ['A1.log', 'sub/A1_td.log'],
                    'local_dir': str(local_dir),
                },
            )[1]
        )
        self.assertEqual(len(payload['files']), 2)
        self.assertTrue(all(os.path.isabs(path) for path in payload['files']))
        self.assertTrue((local_dir / 'A1.log').is_file())
        self.assertTrue((local_dir / 'sub' / 'A1_td.log').is_file())

    def test_cancel(self):
        self.assert_ok(self.login())
        payload = self.assert_ok(
            self.request('POST', '/cancel', {'ids': ['100001']})[1]
        )
        self.assertEqual(payload['ids'], ['100001'])
        self.assertIn('scancel 100001', self.ssh.exec_commands)

    def test_shutdown_requests_exit(self):
        payload = self.assert_ok(self.request('POST', '/shutdown', {})[1])
        self.assertEqual(payload, {'ok': True})
        self.assertTrue(self.app.stop_event.wait(5))

    def test_cli_arguments(self):
        args = hpc_bridge.parse_args(
            ['--data-dir', str(self.base / 'x'), '--idle-exit-minutes', '15']
        )
        self.assertEqual(args.data_dir, str(self.base / 'x'))
        self.assertEqual(args.idle_exit_minutes, 15)
        self.assertEqual(args.paramiko_path, '')
        default_args = hpc_bridge.parse_args([])
        self.assertEqual(default_args.idle_exit_minutes, 720)

    def test_chinese_data_dir(self):
        app = hpc_bridge.BridgeApp(
            self.base / '超算 数据', idle_exit_minutes=720, poll_seconds=0.1
        ).start()
        try:
            self.assertTrue((app.data_dir / 'bridge.json').is_file())
            request = urllib.request.Request(
                'http://127.0.0.1:%d/health' % app.port,
                headers={'Authorization': 'Bearer ' + app.token},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode('utf-8'))
            self.assertTrue(payload['ok'])
        finally:
            app.stop()


class LoggerTests(unittest.TestCase):
    def test_log_truncated_when_larger_than_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'bridge.log'
            logger = hpc_bridge.BridgeLogger(path, max_bytes=200)
            for index in range(20):
                logger.info('测试日志行 %d' % index)
            self.assertTrue(path.is_file())
            text = path.read_text(encoding='utf-8')
            self.assertIn('已截断重写', text)
            self.assertLess(path.stat().st_size, 2000)
            # 截断之后仍能继续追加
            logger.info('截断后的日志')
            self.assertIn('截断后的日志', path.read_text(encoding='utf-8'))


class IdleExitTests(unittest.TestCase):
    def _make_app(self, **kwargs):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        app = hpc_bridge.BridgeApp(
            Path(temporary.name) / 'data',
            idle_exit_minutes=kwargs.pop('idle_exit_minutes', 720),
            poll_seconds=kwargs.pop('poll_seconds', 0.05),
            **kwargs,
        )
        app.start()
        self.addCleanup(app.stop)
        return app

    def test_idle_exit_without_session(self):
        # 0.02 分钟 = 1.2 秒；没有 SSH 会话 → 空闲退出
        app = self._make_app(idle_exit_minutes=0.02)
        self.assertTrue(app.stop_event.wait(10))

    def test_idle_does_not_exit_with_active_session(self):
        app = self._make_app(idle_exit_minutes=0.02)
        app.has_live_session = lambda: True
        self.assertFalse(app.stop_event.wait(2.5))

    def test_hard_cap_exits_even_with_active_session(self):
        app = self._make_app(idle_exit_minutes=720, hard_max_seconds=0.3)
        app.has_live_session = lambda: True
        self.assertTrue(app.stop_event.wait(10))


if __name__ == '__main__':
    unittest.main()
