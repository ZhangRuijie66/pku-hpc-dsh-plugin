# -*- coding: utf-8 -*-
"""hpc_core 的单元测试 + 真实 socket 级假 SSH 服务器集成测试。

所有测试都在 127.0.0.1 内完成，不联网、不接触真实 HPC；假服务器每个测试
结束时关闭（thread + 随机端口）。
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _path in (str(ROOT), str(HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import paramiko  # noqa: E402

from bridge import hpc_core  # noqa: E402
from fake_ssh_server import (  # noqa: E402
    DEFAULT_OTP,
    DEFAULT_PASSWORD,
    DEFAULT_USER,
    FAKE_HOME,
    QUEUE_TEXT,
    SCONTROL_TEXT,
    FakeSSHServer,
)

_JX2_OPTIONS = hpc_core.normalize_submit_options({'cluster': 'jx2'})


class ScriptTests(unittest.TestCase):
    def test_jx2_default_script_matches_g16sub(self):
        script = hpc_core.build_slurm_script('A1', _JX2_OPTIONS)
        expected = (
            '#!/bin/bash\n'
            '#SBATCH -o job.%j.out\n'
            '#SBATCH -p compute\n'
            '#SBATCH -J A1\n'
            '#SBATCH -N 1\n'
            '#SBATCH --ntasks-per-node=32\n'
            '#SBATCH --get-user-env\n'
            'g16 < A1.gjf > A1.log\n'
        )
        self.assertEqual(script, expected)

    def test_jx2_never_writes_default_walltime_but_writes_custom(self):
        default_script = hpc_core.build_slurm_script('A1', _JX2_OPTIONS)
        self.assertNotIn('#SBATCH -t', default_script)
        custom = hpc_core.build_slurm_script(
            'A1', {'cluster': 'jx2', 'walltime': '1-00:00:00'}
        )
        self.assertIn('#SBATCH -t 1-00:00:00', custom)

    def test_wm2_script_uses_ntasks_and_gaussian_env(self):
        options = hpc_core.normalize_submit_options({'cluster': 'wm2'})
        script = hpc_core.build_slurm_script('A1', options)
        self.assertIn('#SBATCH -p C032M0128G', script)
        self.assertIn('#SBATCH --qos=low', script)
        self.assertIn('#SBATCH --ntasks=32', script)
        self.assertNotIn('--ntasks-per-node', script)
        self.assertIn('export g16root=$HOME', script)
        self.assertIn('export GAUSS_SCRDIR=$HOME/tmp', script)
        self.assertIn('source $g16root/g16/bsd/g16.profile', script)
        self.assertIn('cd $SLURM_SUBMIT_DIR', script)
        self.assertIn('g16 A1.gjf > A1.log', script)
        self.assertTrue(script.endswith('\n'))
        self.assertNotIn('\r\n', script)

    def test_wm1_uses_ntasks_per_node(self):
        options = hpc_core.normalize_submit_options({'cluster': 'wm1'})
        script = hpc_core.build_slurm_script('A1', options)
        self.assertIn('#SBATCH --ntasks-per-node=32', script)
        self.assertNotIn('#SBATCH --ntasks=', script)

    def test_options_override_defaults(self):
        options = hpc_core.normalize_submit_options({
            'cluster': 'jx2',
            'partition': 'test',
            'ntasks': 8,
            'nodes': 2,
            'sbatch_out': 'out.%j',
            'include_env': True,
        })
        script = hpc_core.build_slurm_script('x', options)
        self.assertIn('#SBATCH -p test', script)
        self.assertIn('#SBATCH -N 2', script)
        self.assertIn('#SBATCH --ntasks-per-node=8', script)
        self.assertIn('#SBATCH -o out.%j', script)
        self.assertIn('export g16root=$HOME', script)

    def test_script_rejects_path_separators_in_name(self):
        with self.assertRaises(hpc_core.HpcError):
            hpc_core.build_slurm_script('../evil', _JX2_OPTIONS)

    def test_jobid_parsing(self):
        self.assertEqual(hpc_core.parse_jobid('Submitted batch job 123456'), '123456')
        self.assertEqual(hpc_core.parse_jobid('sbatch: error: invalid partition'), '')
        self.assertEqual(hpc_core.parse_jobid('job 12'), '')
        # 裸数字不再当作业号：错误信息里的数字（分区名、学号路径）曾经被误当成 id，
        # 给 _td 挂上永不满足的 afterok 依赖。回归用例见 test_submit_*。
        self.assertEqual(hpc_core.parse_jobid('12345\n'), '')
        self.assertEqual(
            hpc_core.parse_jobid('sbatch: error: Invalid partition name specified: C032M0128G', 1), '')
        # 退出码非 0 时，即使输出里真的有一句 Submitted batch job 也不认
        self.assertEqual(hpc_core.parse_jobid('Submitted batch job 123456', 1), '')

    def test_dependency_rules(self):
        self.assertEqual(hpc_core.dependency_base('A1_td'), 'A1')
        self.assertEqual(hpc_core.dependency_base('A1_fc'), 'A1_gs')
        self.assertIsNone(hpc_core.dependency_base('A1'))
        self.assertIsNone(hpc_core.dependency_base('A1_gs'))
        order = hpc_core.submission_order(['A1_td', 'A1', 'A1_gs', 'A1_fc'])
        self.assertEqual(order, ['A1', 'A1_gs', 'A1_td', 'A1_fc'])


class QueueParseTests(unittest.TestCase):
    def test_slurm_header(self):
        text = (
            'JOBID PARTITION NAME USER ST TIME NODES NODELIST(REASON)\n'
            '12345 compute A1 123456789 R 0:01 1 node01\n'
            '12346 compute A1_td 123456789 PD 0:00 1 (Dependency)\n'
        )
        rows = hpc_core.parse_queue_rows(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['jobid'], '12345')
        self.assertEqual(rows[0]['name'], 'A1')
        self.assertEqual(rows[0]['state'], 'R')
        self.assertEqual(rows[0]['time'], '0:01')
        self.assertEqual(rows[1]['state'], 'PD')
        self.assertIn('Dependency', rows[1]['reason'])

    def test_crane_status_header(self):
        # 教学二号（Crane）的真实 squeue 输出：STATE 叫 STATUS，最后一列是 NODELIST/REASON。
        text = (
            'JOBID PARTITION NAME             USER       ACCOUNT    STATUS  TYPE  TIME     TIMELIMIT  NODES NODELIST/REASON \n'
            '58793 compute   A1_v2            123456789 123456789 Pending Batch -        2-00:00:00 1     Resource        \n'
            '58780 compute   B1_v2            123456789 123456789 Running Batch 02:15:31 2-00:00:00 1     cn12            \n'
            '58811 compute   A1_v2_td         123456789 123456789 Pending Batch -        2-00:00:00 1     Dependency      \n'
        )
        rows = hpc_core.parse_queue_rows(text)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]['state'], 'Pending')
        self.assertEqual(rows[0]['reason'], 'Resource')
        self.assertEqual(rows[1]['state'], 'Running')
        self.assertEqual(rows[1]['time'], '02:15:31')
        self.assertEqual(rows[1]['reason'], 'cn12')
        self.assertEqual(rows[2]['state'], 'Pending')
        self.assertEqual(rows[2]['reason'], 'Dependency')

    def test_positional_without_header(self):
        rows = hpc_core.parse_queue_rows('12345 A1 RUNNING 0:01 (None)\n')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['jobid'], '12345')
        self.assertEqual(rows[0]['state'], 'RUNNING')
        self.assertEqual(rows[0]['reason'], '(None)')

    def test_scontrol_output(self):
        rows = hpc_core.parse_queue_rows(SCONTROL_TEXT)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['jobid'], '100001')
        self.assertEqual(rows[0]['name'], 'A1')
        self.assertEqual(rows[0]['state'], 'RUNNING')
        self.assertEqual(rows[0]['time'], '00:01:23')

    def test_unparseable_returns_empty(self):
        self.assertEqual(hpc_core.parse_queue_rows(''), [])
        self.assertEqual(hpc_core.parse_queue_rows('nothing useful here'), [])


class CredentialTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows 凭据管理器只在 Windows 上测试')
    def test_target_format(self):
        self.assertEqual(
            hpc_core.cred_target('wmjx2-login.pku.edu.cn', '123456789'),
            'pku-hpc:wmjx2-login.pku.edu.cn:123456789',
        )

    @unittest.skipUnless(os.name == 'nt', 'Windows 凭据管理器只在 Windows 上测试')
    def test_write_read_delete_roundtrip(self):
        # 只用 pku-hpc-test: 开头的测试 target，绝不碰 pku-hpc: 的真实条目。
        target = 'pku-hpc-test:%s' % uuid.uuid4().hex
        try:
            self.assertEqual(hpc_core.cred_save(target, 'tester', 'first-secret'), True)
            self.assertEqual(hpc_core.cred_load(target), 'first-secret')
            # 覆盖写
            hpc_core.cred_save(target, 'tester', 'second-secret')
            self.assertEqual(hpc_core.cred_load(target), 'second-secret')
            self.assertEqual(hpc_core.cred_delete(target), True)
            self.assertIsNone(hpc_core.cred_load(target))
            # 再删一次：不存在返回 False，不抛异常
            self.assertEqual(hpc_core.cred_delete(target), False)
        finally:
            try:
                hpc_core.cred_delete(target)
            except hpc_core.HpcError:
                pass


class FakeSSHHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._host_key = paramiko.RSAKey.generate(2048)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._base = Path(self._tmp.name)
        self.server = FakeSSHServer(
            self._base / 'remote', host_key=self._host_key
        ).start()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.server.stop)

    def make_client(self, otp=DEFAULT_OTP):
        client = hpc_core.HpcClient(
            host='127.0.0.1',
            port=self.server.port,
            user=DEFAULT_USER,
            password=DEFAULT_PASSWORD,
            otp=otp,
            timeout=10,
        )
        self.addCleanup(client.close)
        return client

    # ---------------------------------------------------------------- 登录
    def test_login_success_with_otp(self):
        client = self.make_client()
        client.connect()
        self.assertTrue(client.is_alive())
        # 密码/OTP 用完之后要从内存清掉
        self.assertEqual(client.password, '')
        self.assertEqual(client.otp, '')
        out, err, code = client.run('echo $HOME')
        self.assertEqual(out.strip(), FAKE_HOME)
        self.assertEqual(err, '')
        self.assertEqual(code, 0)
        # 假服务器确实走了键盘交互（password + otp 两个 prompt）
        self.assertTrue(self.server.interactive_exchanges)
        self.assertEqual(self.server.interactive_exchanges[-1], [DEFAULT_PASSWORD, DEFAULT_OTP])

    def test_login_wrong_otp_fails_with_chinese_error(self):
        client = self.make_client(otp='000000')
        with self.assertRaises(hpc_core.HpcError) as ctx:
            client.connect()
        message = str(ctx.exception)
        self.assertIn('SSH 登录失败', message)
        self.assertIn('OTP', message)
        self.assertFalse(client.is_alive())

    def test_exec_output_and_exit_code(self):
        client = self.make_client()
        client.connect()
        out, _err, code = client.run('echo $HOME')
        self.assertEqual(out.strip(), FAKE_HOME)
        self.assertEqual(code, 0)
        result = client.run_command('echo $HOME')
        self.assertEqual(result['code'], 0)
        self.assertIn(FAKE_HOME, result['output'])

    # ---------------------------------------------------------------- 队列
    def test_queue_fallback_picks_third_command(self):
        client = self.make_client()
        client.connect()
        self.server.queue_failures = 2
        result = client.query_queue()
        self.assertEqual(result['text'], QUEUE_TEXT.strip())
        self.assertEqual(len(result['rows']), 2)
        self.assertEqual(result['rows'][0]['jobid'], '100001')
        self.assertEqual(result['rows'][0]['name'], 'A1')
        self.assertEqual(result['rows'][1]['state'], 'PENDING')
        self.assertEqual(len(self.server.queue_commands), 3)
        self.assertIn('-u %s' % DEFAULT_USER, self.server.queue_commands[0])
        self.assertIn('--user=', self.server.queue_commands[1])
        self.assertIn('-o ', self.server.queue_commands[2])

    def test_queue_all_fail_raises(self):
        client = self.make_client()
        client.connect()
        self.server.queue_failures = 99
        self.server.scontrol_failure = True
        with self.assertRaises(hpc_core.HpcError) as ctx:
            client.query_queue()
        self.assertIn('查询失败', str(ctx.exception))
        self.assertEqual(len(self.server.queue_commands), 4)

    # ---------------------------------------------------------------- 提交
    def test_submit_uploads_and_chains_afterok(self):
        client = self.make_client()
        client.connect()
        local = self._base / 'local'
        local.mkdir()
        files = []
        for name in ('A1', 'A1_td', 'A1_gs', 'A1_fc'):
            path = local / (name + '.gjf')
            path.write_text('# %s\n' % name, encoding='utf-8', newline='\n')
            files.append(str(path))

        result = hpc_core.submit_jobs(client, '~/g16jobs', files, {'cluster': 'jx2'})

        self.assertEqual(result['remote_dir'], FAKE_HOME + '/g16jobs')
        self.assertEqual(
            [job['name'] for job in result['jobs']],
            ['A1', 'A1_gs', 'A1_td', 'A1_fc'],
        )
        self.assertTrue(all(job['ok'] for job in result['jobs']))
        self.assertTrue(all(job['jobid'] for job in result['jobs']))

        calls = self.server.sbatch_calls
        self.assertEqual(len(calls), 4)
        self.assertEqual([call['script'] for call in calls],
                         ['job_A1.sh', 'job_A1_gs.sh', 'job_A1_td.sh', 'job_A1_fc.sh'])
        # 先提交无依赖的，_td 依赖同批基态，_fc 依赖 <base>_gs
        self.assertEqual(calls[0]['dependency'], '')
        self.assertEqual(calls[1]['dependency'], '')
        self.assertEqual(calls[2]['dependency'], 'afterok:' + result['jobs'][0]['jobid'])
        self.assertEqual(calls[3]['dependency'], 'afterok:' + result['jobs'][1]['jobid'])
        # 脚本内容与 build_slurm_script 完全一致，且是 LF / UTF-8
        self.assertEqual(
            calls[0]['script_text'],
            hpc_core.build_slurm_script('A1', {'cluster': 'jx2'}),
        )
        self.assertIn('#SBATCH --ntasks-per-node=32', calls[0]['script_text'])
        self.assertIn('g16 < A1.gjf > A1.log', calls[0]['script_text'])
        # .gjf 也上传了
        remote_dir = self.server.local_for(FAKE_HOME + '/g16jobs')
        self.assertEqual((remote_dir / 'A1.gjf').read_text(encoding='utf-8'), '# A1\n')
        self.assertTrue((remote_dir / 'job_A1.sh').is_file())

    def test_submit_without_dependency_has_no_afterok(self):
        client = self.make_client()
        client.connect()
        local = self._base / 'local2'
        local.mkdir()
        path = local / 'A1_td.gjf'
        path.write_text('# td\n', encoding='utf-8', newline='\n')
        result = hpc_core.submit_jobs(
            client, '~/g16jobs', [str(path)], {'cluster': 'jx2'}, dependency=False
        )
        self.assertTrue(result['jobs'][0]['ok'])
        self.assertEqual(self.server.sbatch_calls[-1]['dependency'], '')

    def test_submit_only_td_has_no_dependency(self):
        """只提交 _td：没有同批基态，就不该挂任何依赖。"""
        client = self.make_client()
        client.connect()
        local = self._base / 'local_td_only'
        local.mkdir()
        path = local / 'A1_td.gjf'
        path.write_text('# td\n', encoding='utf-8', newline='\n')

        result = hpc_core.submit_jobs(client, '~/g16jobs', [str(path)], {'cluster': 'jx2'})

        self.assertEqual([job['name'] for job in result['jobs']], ['A1_td'])
        self.assertTrue(result['jobs'][0]['ok'])
        self.assertEqual(self.server.sbatch_calls[-1]['dependency'], '')

    def test_submit_base_failure_never_leaks_number_as_dependency(self):
        """基态 sbatch 失败、错误里带数字 → TD 绝不能拿到 afterok:<数字>。

        分区名 C032M0128G 里的 032 曾被当成作业号，TD 因此永远
        DependencyNeverSatisfied。这里锁死：失败就不挂依赖，且 ok=False。
        """
        client = self.make_client()
        client.connect()
        local = self._base / 'local_fail'
        local.mkdir()
        files = []
        for name in ('A1', 'A1_td'):
            path = local / (name + '.gjf')
            path.write_text('# %s\n' % name, encoding='utf-8', newline='\n')
            files.append(str(path))
        self.server.sbatch_failures['job_A1.sh'] = (
            'sbatch: error: Batch job submission failed: '
            'Invalid partition name specified: C032M0128G\n'
        )

        result = hpc_core.submit_jobs(client, '~/g16jobs', files, {'cluster': 'wm1'})

        base_job = next(job for job in result['jobs'] if job['name'] == 'A1')
        td_job = next(job for job in result['jobs'] if job['name'] == 'A1_td')
        self.assertFalse(base_job['ok'])
        self.assertEqual(base_job['jobid'], '')
        # TD 照常提交，但不能带依赖
        td_call = self.server.sbatch_calls[-1]
        self.assertEqual(td_call['script'], 'job_A1_td.sh')
        self.assertEqual(td_call['dependency'], '')

    # ------------------------------------------------------------ 列文件/下载
    def test_files_and_download(self):
        client = self.make_client()
        client.connect()
        remote = self.server.local_for(FAKE_HOME + '/g16jobs')
        (remote / 'sub').mkdir(parents=True)
        (remote / 'A1.log').write_text('log-a\n', encoding='utf-8')
        (remote / 'sub' / 'A1_td.log').write_text('log-td\n', encoding='utf-8')
        (remote / 'A1.chk').write_bytes(b'chk')
        (remote / '.hidden.log').write_text('hidden\n', encoding='utf-8')

        info = client.list_files('~/g16jobs', depth=2)
        self.assertTrue(info['exists'])
        self.assertEqual(info['root'], FAKE_HOME + '/g16jobs')
        names = [item['name'] for item in info['files']]
        self.assertIn('A1.log', names)
        self.assertIn('sub/A1_td.log', names)
        self.assertIn('A1.chk', names)
        self.assertNotIn('.hidden.log', names)
        for item in info['files']:
            self.assertRegex(item['mtime'], r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$')
            self.assertIsInstance(item['size'], int)

        local_dir = self._base / 'download'
        saved = client.download_files(
            '~/g16jobs', ['A1.log', 'sub/A1_td.log'], str(local_dir)
        )
        self.assertEqual(len(saved), 2)
        for path in saved:
            self.assertTrue(os.path.isabs(path))
            self.assertTrue(Path(path).is_file())
        self.assertEqual(
            (local_dir / 'A1.log').read_text(encoding='utf-8'), 'log-a\n'
        )
        self.assertEqual(
            (local_dir / 'sub' / 'A1_td.log').read_text(encoding='utf-8'), 'log-td\n'
        )

    def test_list_missing_directory(self):
        client = self.make_client()
        client.connect()
        info = client.list_files('~/no-such-dir', depth=1)
        self.assertFalse(info['exists'])
        self.assertEqual(info['files'], [])

    def test_chinese_local_and_remote_paths(self):
        # 中文目录名 / 中文文件名 / 中文远程目录都要能工作
        client = self.make_client()
        client.connect()
        local = self._base / '中文 目录'
        local.mkdir()
        gjf = local / '作业甲.gjf'
        gjf.write_text('# 中文作业\n', encoding='utf-8', newline='\n')
        result = hpc_core.submit_jobs(
            client, '~/中文作业', [str(gjf)], {'cluster': 'jx2'}
        )
        self.assertTrue(result['jobs'][0]['ok'])
        self.assertEqual(result['remote_dir'], FAKE_HOME + '/中文作业')
        remote_dir = self.server.local_for(result['remote_dir'])
        self.assertEqual(
            (remote_dir / '作业甲.gjf').read_text(encoding='utf-8'), '# 中文作业\n'
        )
        self.assertIn('#SBATCH -J 作业甲', self.server.sbatch_calls[-1]['script_text'])

        download_dir = self._base / '下载'
        saved = client.download_files(
            '~/中文作业', ['作业甲.gjf'], str(download_dir)
        )
        self.assertEqual(
            Path(saved[0]).read_text(encoding='utf-8'), '# 中文作业\n'
        )

    def test_cancel_jobs(self):
        client = self.make_client()
        client.connect()
        result = client.cancel_jobs(['100001', '100002'])
        self.assertEqual(result['ids'], ['100001', '100002'])
        self.assertIn('scancel 100001 100002', self.server.exec_commands)

    def test_not_connected_error(self):
        client = hpc_core.HpcClient(host='127.0.0.1', port=self.server.port,
                                    user=DEFAULT_USER, password=DEFAULT_PASSWORD,
                                    otp=DEFAULT_OTP)
        with self.assertRaises(hpc_core.HpcError):
            client.run('echo $HOME')


if __name__ == '__main__':
    unittest.main()
