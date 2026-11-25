# -*- coding: utf-8 -*-
"""真实 socket 级的假 SSH 服务器，仅供单元测试使用。

它监听 ``127.0.0.1`` 的随机端口，用 paramiko 的 ``Transport`` +
``ServerInterface`` 实现：

* ``keyboard-interactive`` 认证：prompt 顺序是「Password:」+「OTP:」，
  只有密码和 OTP 都正确才成功（密码直连永远被拒绝，模拟北大 HPC）；
* exec 通道：识别本插件会执行的命令（echo $HOME / cd ... && pwd /
  sbatch / squeue / scancel / scontrol）；
* SFTP 子系统：把远程 POSIX 路径映射到本机临时目录，支持列目录、
  stat、mkdir、读文件、写文件（``sftp.get`` / ``sftp.putfo`` 都覆盖）。

不联网、不接触真实 HPC；每个测试实例用完必须 ``stop()``。
"""

from __future__ import annotations

import logging
import os
import posixpath
import shlex
import socket
import threading
import time
from pathlib import Path

import paramiko

# 测试进程里不要打印 paramiko 内部的 socket 异常（关闭连接时的噪音）
logging.getLogger('paramiko').propagate = False
logging.getLogger('paramiko').addHandler(logging.NullHandler())

FAKE_HOME = '/home/pkuhpc'
DEFAULT_USER = 'testuser'
DEFAULT_PASSWORD = 'correct-password'
DEFAULT_OTP = '123456'

QUEUE_TEXT = (
    '  JOBID NAME ST TIME REASON\n'
    ' 100001 A1 RUNNING 0:01 (None)\n'
    ' 100002 A1_td PENDING 0:00 (Dependency)\n'
)

SCONTROL_TEXT = (
    'JobId=100001 JobName=A1 JobState=RUNNING RunTime=00:01:23 Reason=None\n'
    'JobId=100002 JobName=A1_td JobState=PENDING RunTime=00:00:00 Reason=Dependency\n'
)


class _FakeSFTPHandle(paramiko.SFTPHandle):
    def stat(self):
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self.filename))
        except OSError as exc:  # pragma: no cover - 异常路径
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def chattr(self, attr):
        return paramiko.SFTP_OK


class FakeSFTPServer(paramiko.SFTPServerInterface):
    """把 ``/home/pkuhpc/...`` 映射到本地临时目录的 SFTP 服务端。"""

    def __init__(self, server, *args, **kwargs):
        # paramiko 5.x 的 SFTPServerInterface.__init__ 只接受 server，额外参数
        # （这里是 root 目录）由本类自己收下。
        super().__init__(server)
        self.root = Path(args[0])

    # ------------------------------------------------------------ 路径映射
    def _local(self, remote):
        path = posixpath.normpath(str(remote or '/'))
        if not path.startswith('/'):
            path = '/' + path
        parts = [part for part in path.split('/') if part and part != '.']
        return self.root.joinpath(*parts)

    def canonicalize(self, path):
        value = posixpath.normpath('/' + str(path or '/').lstrip('/'))
        return value

    # ------------------------------------------------------------ 目录 / 属性
    def list_folder(self, path):
        local = self._local(path)
        try:
            entries = list(local.iterdir())
        except OSError:
            return paramiko.SFTP_NO_SUCH_FILE
        attrs = []
        for entry in entries:
            try:
                attrs.append(paramiko.SFTPAttributes.from_stat(entry.stat(), entry.name))
            except OSError:
                continue
        return attrs

    def stat(self, path):
        local = self._local(path)
        try:
            return paramiko.SFTPAttributes.from_stat(local.stat(), local.name)
        except OSError:
            return paramiko.SFTP_NO_SUCH_FILE

    def lstat(self, path):
        return self.stat(path)

    def mkdir(self, path, attr):
        local = self._local(path)
        try:
            local.mkdir()
        except FileExistsError:
            return paramiko.SFTP_FAILURE
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
        return paramiko.SFTP_OK

    def rmdir(self, path):
        local = self._local(path)
        try:
            local.rmdir()
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
        return paramiko.SFTP_OK

    def remove(self, path):
        local = self._local(path)
        try:
            local.unlink()
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
        return paramiko.SFTP_OK

    def rename(self, oldpath, newpath):
        try:
            self._local(oldpath).rename(self._local(newpath))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
        return paramiko.SFTP_OK

    def chattr(self, path, attr):
        return paramiko.SFTP_OK

    # ------------------------------------------------------------ 文件读写
    def open(self, path, flags, attr):
        local = self._local(path)
        if flags & os.O_WRONLY:
            mode = 'wb'
        elif flags & os.O_RDWR:
            mode = 'rb+'
        elif flags & os.O_CREAT:
            mode = 'wb'
        else:
            mode = 'rb'
        try:
            file_object = open(local, mode)
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
        handle = _FakeSFTPHandle(flags)
        handle.filename = str(local)
        if mode in ('rb', 'rb+'):
            handle.readfile = file_object
        if mode in ('wb', 'rb+'):
            handle.writefile = file_object
        return handle


class _FakeServerInterface(paramiko.ServerInterface):
    def __init__(self, server):
        self.server = server

    def get_allowed_auths(self, username):
        return 'password,keyboard-interactive'

    def check_auth_password(self, username, password):
        self.server.auth_attempts.append(('password', username))
        if (
            self.server.allow_password_auth
            and username == self.server.user
            and password == self.server.password
        ):
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_interactive(self, username, submethods):
        self.server.auth_attempts.append(('keyboard-interactive', username))
        query = paramiko.InteractiveQuery('PKU HPC')
        query.add_prompt('Password: ', False)
        query.add_prompt('Verification code (OTP): ', False)
        return query

    def check_auth_interactive_response(self, responses):
        response_list = list(responses or [])
        self.server.interactive_exchanges.append(response_list)
        if len(response_list) >= 2:
            if (
                response_list[0] == self.server.password
                and response_list[1] == self.server.otp
            ):
                return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        if kind == 'session':
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        text = command.decode('utf-8', 'replace')
        threading.Thread(
            target=self.server._run_exec, args=(channel, text), daemon=True
        ).start()
        return True


class FakeSSHServer:
    """监听 127.0.0.1 的假 SSH 服务器（含 SFTP 子系统）。"""

    def __init__(
        self,
        root,
        user=DEFAULT_USER,
        password=DEFAULT_PASSWORD,
        otp=DEFAULT_OTP,
        host='127.0.0.1',
        host_key=None,
        home=FAKE_HOME,
    ):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.user = user
        self.password = password
        self.otp = otp
        self.home = home
        self.host = host
        self.host_key = host_key or paramiko.RSAKey.generate(2048)
        self.allow_password_auth = False
        self.port = None
        self.sock = None
        self._stop = threading.Event()
        self._thread = None
        self._transports = []
        self._lock = threading.Lock()

        # 测试断言用的记录
        self.auth_attempts = []
        self.interactive_exchanges = []
        self.exec_commands = []
        self.exec_results = []
        self.sbatch_calls = []
        self.queue_commands = []
        self.queue_failures = 0
        self.scontrol_failure = False
        self.next_jobid = 100001
        # 让指定脚本的 sbatch 失败（用来复现「基态提交失败」这条路径）
        self.sbatch_failures = {}

    # ------------------------------------------------------------ 生命周期
    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, 0))
        self.port = int(self.sock.getsockname()[1])
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self._thread = threading.Thread(
            target=self._accept_loop, name='fake-ssh-accept', daemon=True
        )
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        with self._lock:
            transports = list(self._transports)
            self._transports = []
        for transport in transports:
            try:
                transport.close()
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.stop()

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                client, _address = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_client, args=(client,), daemon=True
            ).start()

    def _handle_client(self, client_socket):
        transport = paramiko.Transport(client_socket)
        try:
            transport.add_server_key(self.host_key)
            transport.set_subsystem_handler(
                'sftp', paramiko.SFTPServer, FakeSFTPServer, str(self.root)
            )
            transport.start_server(server=_FakeServerInterface(self))
        except Exception:  # noqa: BLE001 - 测试服务器：连接失败直接放弃
            try:
                transport.close()
            except Exception:  # noqa: BLE001
                pass
            return
        with self._lock:
            self._transports.append(transport)
        try:
            while not self._stop.is_set() and transport.is_active():
                time.sleep(0.05)
        finally:
            try:
                transport.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ 路径工具
    def local_for(self, remote):
        path = posixpath.normpath(str(remote or '/'))
        if not path.startswith('/'):
            path = '/' + path
        return self.root.joinpath(*[part for part in path.split('/') if part])

    # ------------------------------------------------------------ 命令执行
    def _run_exec(self, channel, command):
        try:
            out, err, code = self._handle_command(command)
            if out:
                channel.send(out.encode('utf-8'))
            if err:
                channel.send_stderr(err.encode('utf-8'))
            channel.send_exit_status(code)
            time.sleep(0.01)
        except Exception as exc:  # noqa: BLE001
            try:
                channel.send_stderr(('fake-ssh: %r\n' % (exc,)).encode('utf-8'))
                channel.send_exit_status(1)
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                channel.close()
            except Exception:  # noqa: BLE001
                pass

    def _resolve_dir(self, target):
        value = str(target or '').strip().strip('"\'')
        if value.startswith('~'):
            value = self.home + value[1:]
        if not value.startswith('/'):
            value = posixpath.join(self.home, value)
        return posixpath.normpath(value)

    def _handle_command(self, command):
        text = str(command or '').strip()
        with self._lock:
            self.exec_commands.append(text)
        if text == 'echo $HOME':
            return self.home + '\n', '', 0
        if text.startswith('cd '):
            if ' && ' in text:
                left, right = text.split(' && ', 1)
            else:
                left, right = text, ''
            target = left[3:].strip()
            if target.endswith('2>/dev/null'):
                target = target[: -len('2>/dev/null')].strip()
            directory = self._resolve_dir(target)
            right = right.strip()
            if right == 'pwd':
                if self.local_for(directory).is_dir():
                    return directory + '\n', '', 0
                return '', '', 1
            if right.startswith('sbatch'):
                return self._handle_sbatch(directory, right)
            return '', 'fake-ssh: unsupported cd 命令：%s\n' % text, 127
        if text.startswith('sbatch'):
            return self._handle_sbatch(self.home, text)
        if text.startswith('squeue'):
            with self._lock:
                self.queue_commands.append(text)
                failures = self.queue_failures
                attempt = len(self.queue_commands)
            if attempt <= failures:
                return '', 'squeue: error: fake failure %d\n' % attempt, 1
            return QUEUE_TEXT, '', 0
        if text.startswith('scontrol'):
            if self.scontrol_failure:
                return '', 'scontrol: error: fake failure\n', 1
            return SCONTROL_TEXT, '', 0
        if text.startswith('scancel'):
            return '', '', 0
        return '', 'fake-ssh: unsupported command: %s\n' % text, 127

    def _handle_sbatch(self, cwd, argument_text):
        try:
            tokens = shlex.split(argument_text)
        except ValueError:
            tokens = argument_text.split()
        dependency = ''
        script = ''
        index = 1
        while index < len(tokens):
            token = tokens[index]
            if token.startswith('--dependency='):
                dependency = token.split('=', 1)[1]
            elif token == '--dependency' and index + 1 < len(tokens):
                dependency = tokens[index + 1]
                index += 1
            elif not token.startswith('-'):
                script = token
            index += 1
        if script == '':
            return '', 'sbatch: error: no script\n', 1
        local_script = self.local_for(cwd) / script
        if not local_script.is_file():
            return '', 'sbatch: error: Unable to open file %s\n' % script, 1
        try:
            script_text = local_script.read_text(encoding='utf-8', errors='replace')
        except OSError as exc:  # pragma: no cover - 异常路径
            return '', 'sbatch: error: %s\n' % exc, 1
        with self._lock:
            self.sbatch_calls.append({
                'cwd': cwd,
                'dependency': dependency,
                'script': script,
                'script_text': script_text,
            })
            failure = self.sbatch_failures.get(script)
            if failure:
                return '', failure, 1
            jobid = str(self.next_jobid)
            self.next_jobid += 1
        return 'Submitted batch job %s\n' % jobid, '', 0
