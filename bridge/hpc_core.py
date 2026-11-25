# -*- coding: utf-8 -*-
"""北大高性能计算平台（PKU HPC）核心逻辑。

本文件只依赖 Python 标准库 + paramiko，被 ``hpc_bridge.py`` 直接导入使用；
也可以单独导入做单元测试。设计要点：

* SSH：先尝试 password 直连，失败后改用 ``Transport.auth_interactive``
  处理「密码 + OTP」的 Keyboard Interactive 认证（北大 HPC 不支持密钥登录）。
* 密码只保存在内存里，绝不写日志/JSON；只有用户勾选「记住密码」时才会写入
  Windows 凭据管理器（generic credential，target = ``pku-hpc:<host>:<user>``）。
* Slurm 脚本生成与用户现有 ``g16sub.sh`` 对齐（jx2 默认逐行一致）。
* SFTP 列文件 / 上传 / 下载；远程路径用 POSIX 字符串，本地路径用 pathlib。
"""

from __future__ import annotations

import ctypes
import io
import os
import posixpath
import re
import shlex
import socket
import stat as stat_module
import time
from pathlib import Path, PurePosixPath

VERSION = '1.0.0'

# 默认配置（与 CONTRACT.md「默认配置」一致；作为 /submit options 的兜底值）
DEFAULT_OPTIONS = {
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

DEFAULT_REMOTE_DIR = '~/g16jobs'
DEFAULT_PORT = 22

# 集群表（key, 显示名, 登录节点）
CLUSTERS = [
    {'key': 'jx2', 'name': '教学二号', 'host': 'wmjx2-login.pku.edu.cn'},
    {'key': 'wm1', 'name': '未名一号', 'host': 'wm1-login.pku.edu.cn'},
    {'key': 'wm2', 'name': '未名二号', 'host': 'wm2-login.pku.edu.cn'},
    {'key': 'sk1', 'name': '生科一号', 'host': 'wmsk1-login.pku.edu.cn'},
]
_CLUSTER_BY_KEY = {c['key']: c for c in CLUSTERS}

# 每个集群的默认提交参数（与 gcalc/hpc.py 的分支一致）
CLUSTER_DEFAULTS = {
    'jx2': {
        'partition': 'compute', 'qos': '', 'ntasks': 32, 'nodes': 1,
        'walltime': '2-00:00:00', 'sbatch_out': 'job.%j.out',
        'include_env': False, 'redirect': True,
        'cd_submit_dir': False, 'ntasks_option': 'ntasks-per-node',
        'g16root': '$HOME', 'scr_dir': '$HOME/tmp',
    },
    'wm1': {
        'partition': 'C032M0128G', 'qos': 'low', 'ntasks': 32, 'nodes': 1,
        'walltime': '5-00:00:00', 'sbatch_out': 'job.%j.out',
        'include_env': True, 'redirect': False,
        'cd_submit_dir': True, 'ntasks_option': 'ntasks-per-node',
        'g16root': '$HOME', 'scr_dir': '$HOME/tmp',
    },
    'wm2': {
        'partition': 'C032M0128G', 'qos': 'low', 'ntasks': 32, 'nodes': 1,
        'walltime': '5-00:00:00', 'sbatch_out': 'job.%j.out',
        'include_env': True, 'redirect': False,
        'cd_submit_dir': True, 'ntasks_option': 'ntasks',
        'g16root': '$HOME', 'scr_dir': '$HOME/tmp',
    },
    'sk1': {
        'partition': 'C032M0128G', 'qos': 'low', 'ntasks': 32, 'nodes': 1,
        'walltime': '5-00:00:00', 'sbatch_out': 'job.%j.out',
        'include_env': True, 'redirect': False,
        'cd_submit_dir': True, 'ntasks_option': 'ntasks-per-node',
        'g16root': '$HOME', 'scr_dir': '$HOME/tmp',
    },
}

# 凭据管理器常量（wincred.h）
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168
CRED_TARGET_PREFIX = 'pku-hpc'


class HpcError(RuntimeError):
    """面向用户的业务错误，message 直接展示（中文）。"""


class CredentialError(HpcError):
    """Windows 凭据管理器操作失败。"""


# --------------------------------------------------------------------------- 集群
def cluster_info(key):
    """按 key 返回集群信息；未知 key 回退到 jx2。"""
    return dict(_CLUSTER_BY_KEY.get(str(key or '').strip(), _CLUSTER_BY_KEY['jx2']))


def cluster_by_host(host):
    """按登录节点主机名找集群；找不到返回 None。"""
    value = str(host or '').strip().lower()
    if not value:
        return None
    for cluster in CLUSTERS:
        if value == cluster['host'].lower() or value in cluster['host'].lower():
            return dict(cluster)
    return None


# ------------------------------------------------------------------- 提交参数
def _as_bool(value, fallback):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ('true', '1', 'yes', 'on'):
            return True
        if text in ('false', '0', 'no', 'off'):
            return False
    return fallback


def _as_int(value, fallback, minimum=1):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return fallback
    return max(minimum, number)


def normalize_submit_options(raw=None):
    """把 /submit 的 options 规范成完整字典（未知/缺省项用集群默认值）。"""
    raw = dict(raw or {}) if isinstance(raw, dict) else {}
    cluster_key = str(raw.get('cluster') or 'jx2').strip() or 'jx2'
    cluster = cluster_info(cluster_key)
    cluster_key = cluster['key']
    defaults = CLUSTER_DEFAULTS[cluster_key]

    def text(key, fallback):
        value = raw.get(key)
        if value is None:
            return fallback
        value = str(value).strip()
        return value if value != '' else fallback

    options = {
        'cluster': cluster_key,
        'partition': text('partition', defaults['partition']),
        'qos': (str(raw.get('qos')).strip()
                if raw.get('qos') is not None else defaults['qos']),
        'ntasks': _as_int(raw.get('ntasks'), defaults['ntasks']),
        'nodes': _as_int(raw.get('nodes'), defaults['nodes']),
        'walltime': text('walltime', defaults['walltime']),
        'sbatch_out': text('sbatch_out', defaults['sbatch_out']),
        'include_env': _as_bool(raw.get('include_env'), defaults['include_env']),
        'redirect': _as_bool(raw.get('redirect'), defaults['redirect']),
        'g16root': text('g16root', defaults['g16root']),
        'scr_dir': text('scr_dir', defaults['scr_dir']),
    }
    return options


# --------------------------------------------------------------- Slurm 脚本
def build_slurm_script(name, options=None):
    """生成单个 .gjf 的 Slurm 作业脚本（UTF-8，LF 由调用方保证）。

    jx2（教学二号）默认脚本与用户现有 ``g16sub.sh`` 逐行一致：默认的
    2 天墙钟不再单独写 ``#SBATCH -t``（g16sub.sh 里也没有这一行）。
    """
    name = str(name or '').strip()
    if name == '':
        raise HpcError('作业名为空，无法生成提交脚本')
    if '/' in name or '\\' in name:
        raise HpcError('作业名不能包含路径分隔符：%s' % name)
    options = normalize_submit_options(options)
    cluster_key = options['cluster']
    defaults = CLUSTER_DEFAULTS[cluster_key]

    lines = [
        '#!/bin/bash',
        '#SBATCH -o %s' % options['sbatch_out'],
        '#SBATCH -p %s' % options['partition'],
    ]
    if options['qos']:
        lines.append('#SBATCH --qos=%s' % options['qos'])
    lines.append('#SBATCH -J %s' % name[:60])
    lines.append('#SBATCH -N %d' % options['nodes'])
    if defaults.get('ntasks_option') == 'ntasks':
        lines.append('#SBATCH --ntasks=%d' % options['ntasks'])
    else:
        lines.append('#SBATCH --ntasks-per-node=%d' % options['ntasks'])
    lines.append('#SBATCH --get-user-env')

    walltime = options['walltime']
    # jx2 的默认脚本对齐用户的 g16sub.sh（没有 -t）；换成自定义墙钟时才写。
    if walltime and not (cluster_key == 'jx2' and walltime == '2-00:00:00'):
        lines.append('#SBATCH -t %s' % walltime)

    if options['include_env']:
        lines.append('export g16root=%s' % options['g16root'])
        lines.append('export GAUSS_SCRDIR=%s' % options['scr_dir'])
        lines.append('source $g16root/g16/bsd/g16.profile')

    if defaults.get('cd_submit_dir'):
        lines.append('cd $SLURM_SUBMIT_DIR')

    gjf = name + '.gjf'
    if options['redirect']:
        lines.append('g16 < %s > %s.log' % (gjf, name))
    else:
        lines.append('g16 %s > %s.log' % (gjf, name))
    return '\n'.join(lines) + '\n'


def dependency_base(name):
    """``_td`` 依赖同批基态 ``<base>``；``_fc`` 依赖 ``<base>_gs``；其余返回 None。"""
    name = str(name or '')
    low = name.lower()
    if low.endswith('_td'):
        return name[:-3]
    if low.endswith('_fc'):
        return name[:-3] + '_gs'
    return None


def submission_order(names):
    """先提交无依赖的基态作业，再提交 _td/_fc（保持输入相对顺序）。"""
    bases, dependent = [], []
    for raw in names:
        name = str(raw or '')
        if name == '':
            continue
        if dependency_base(name) is None:
            if name not in bases:
                bases.append(name)
        else:
            if name not in dependent:
                dependent.append(name)
    return bases + dependent


def parse_jobid(text, code=0):
    """从 sbatch 输出里取作业号；没成功提交就返回空串。

    只认 ``Submitted batch job <id>`` 这一句，并且要求退出码为 0。
    旧实现用「3 位以上数字」在整个输出里乱找，于是错误信息里的数字
    （分区名 C032M0128G → 032、路径 /home/<学号> → 学号）
    会被当成作业号，给 ``_td`` 挂上 ``--dependency=afterok:032`` 这种
    永不满足的依赖，作业永远停在 DependencyNeverSatisfied。
    """
    if code not in (0, None):
        return ''
    match = re.search(r'Submitted\s+batch\s+job\s+(\d+)', str(text or ''), re.IGNORECASE)
    return match.group(1) if match else ''


# ------------------------------------------------------------------- 队列解析
_ROW_KEYS = ('jobid', 'name', 'state', 'time', 'reason')


def _empty_row():
    return {key: '' for key in _ROW_KEYS}


def parse_queue_rows(text):
    """尽力从 squeue / scontrol 输出解析行；解析不出来返回 []。"""
    lines = [line for line in str(text or '').splitlines() if line.strip()]
    if not lines:
        return []

    # 1) 有表头：按列名定位
    header_index = None
    columns = None
    for index, line in enumerate(lines):
        tokens = line.split()
        upper = [token.upper() for token in tokens]
        if any(token == 'JOBID' for token in upper):
            header_index = index
            columns = upper
            break
    if header_index is not None and columns is not None:
        def find_column(*candidates):
            for candidate in candidates:
                if candidate in columns:
                    return columns.index(candidate)
            return None

        indexes = {
            'jobid': find_column('JOBID', 'JOB', 'JOBID(JOBARRAY)'),
            'name': find_column('NAME', 'JOBNAME'),
            # 教学二号（Crane）的 squeue 表头是 STATUS，不是 Slurm 的 ST/STATE。
            'state': find_column('ST', 'STATE', 'STATUS', 'S'),
            'time': find_column('TIME', 'ELAPSED'),
            'reason': find_column(
                'REASON',
                'NODELIST(REASON)',
                'NODELIST/REASON',
                'NODELIST',
            ),
        }
        rows = []
        for line in lines[header_index + 1:]:
            tokens = line.split()
            if len(tokens) < 2 or tokens[0].upper() == 'JOBID':
                continue
            row = _empty_row()
            for key, index in indexes.items():
                if index is not None and index < len(tokens):
                    row[key] = tokens[index]
            if row['jobid']:
                rows.append(row)
        if rows:
            return rows

    # 2) 没有表头（squeue -o 自定义格式）：按「5 列且首列是数字」猜
    rows = []
    for line in lines:
        tokens = line.split()
        if len(tokens) >= 5 and tokens[0].split('.')[0].isdigit():
            rows.append({
                'jobid': tokens[0],
                'name': tokens[1],
                'state': tokens[2],
                'time': tokens[3],
                'reason': ' '.join(tokens[4:]),
            })
    if rows:
        return rows

    # 3) scontrol show jobs：Key=Value 形式
    rows = []
    for line in lines:
        match = re.match(r'\s*JobId\s*=\s*(\S+)', line, re.IGNORECASE)
        if not match:
            continue
        pairs = dict(
            (key.lower(), value)
            for key, value in re.findall(r'(\w[\w/()]*)=(\S+)', line)
        )
        row = _empty_row()
        row['jobid'] = pairs.get('jobid', match.group(1))
        row['name'] = pairs.get('jobname', '')
        row['state'] = pairs.get('jobstate', '')
        row['time'] = pairs.get('runtime', pairs.get('timelimit', ''))
        row['reason'] = pairs.get('reason', '')
        rows.append(row)
    return rows


# ------------------------------------------------------------ 凭据管理器
class _FILETIME(ctypes.Structure):
    _fields_ = [
        ('dwLowDateTime', ctypes.c_ulong),
        ('dwHighDateTime', ctypes.c_ulong),
    ]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ('Flags', ctypes.c_ulong),
        ('Type', ctypes.c_ulong),
        ('TargetName', ctypes.c_wchar_p),
        ('Comment', ctypes.c_wchar_p),
        ('LastWritten', _FILETIME),
        ('CredentialBlobSize', ctypes.c_ulong),
        ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)),
        ('Persist', ctypes.c_ulong),
        ('AttributeCount', ctypes.c_ulong),
        ('Attributes', ctypes.c_void_p),
        ('TargetAlias', ctypes.c_wchar_p),
        ('UserName', ctypes.c_wchar_p),
    ]


_PCREDENTIALW = ctypes.POINTER(_CREDENTIALW)
_ADVAPI32 = None


def credentials_available():
    """当前平台是否支持 Windows 凭据管理器。"""
    return os.name == 'nt'


def _advapi32():
    global _ADVAPI32
    if os.name != 'nt':
        raise CredentialError('Windows 凭据管理器只在 Windows 上可用')
    if _ADVAPI32 is None:
        advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)
        advapi32.CredWriteW.argtypes = [_PCREDENTIALW, ctypes.c_ulong]
        advapi32.CredWriteW.restype = ctypes.c_int
        advapi32.CredReadW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.POINTER(_PCREDENTIALW),
        ]
        advapi32.CredReadW.restype = ctypes.c_int
        advapi32.CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong]
        advapi32.CredDeleteW.restype = ctypes.c_int
        advapi32.CredFree.argtypes = [ctypes.c_void_p]
        advapi32.CredFree.restype = None
        _ADVAPI32 = advapi32
    return _ADVAPI32


def cred_target(host, user):
    """契约里的凭据 target：``pku-hpc:<host>:<user>``。"""
    return '%s:%s:%s' % (CRED_TARGET_PREFIX, str(host or '').strip(), str(user or '').strip())


def cred_save(target, username, secret):
    """把密码写入 Windows 凭据管理器（generic credential，本机持久化）。"""
    target = str(target or '').strip()
    if target == '':
        raise CredentialError('凭据 target 不能为空')
    advapi32 = _advapi32()
    blob = str(secret or '').encode('utf-16-le')
    buffer = None
    if blob:
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    credential = _CREDENTIALW()
    credential.Flags = 0
    credential.Type = CRED_TYPE_GENERIC
    credential.TargetName = target
    credential.Comment = 'PKU HPC 密码（由 pku-hpc 插件保存）'
    credential.Persist = CRED_PERSIST_LOCAL_MACHINE
    credential.CredentialBlobSize = len(blob)
    credential.CredentialBlob = (
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)) if buffer is not None else None
    )
    credential.UserName = str(username or '')
    ok = advapi32.CredWriteW(ctypes.byref(credential), 0)
    if not ok:
        error = ctypes.get_last_error()
        raise CredentialError(
            '写入 Windows 凭据管理器失败：%s' % ctypes.WinError(error)  # type: ignore[attr-defined]
        )
    return True


def cred_load(target):
    """读取凭据；不存在返回 None。"""
    target = str(target or '').strip()
    if target == '':
        raise CredentialError('凭据 target 不能为空')
    advapi32 = _advapi32()
    pointer = _PCREDENTIALW()
    ok = advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer))
    if not ok:
        error = ctypes.get_last_error()
        if error == ERROR_NOT_FOUND:
            return None
        raise CredentialError(
            '读取 Windows 凭据管理器失败：%s' % ctypes.WinError(error)  # type: ignore[attr-defined]
        )
    try:
        credential = pointer.contents
        size = int(credential.CredentialBlobSize)
        if size <= 0 or not credential.CredentialBlob:
            return ''
        data = ctypes.string_at(credential.CredentialBlob, size)
        try:
            return data.decode('utf-16-le')
        except UnicodeDecodeError:
            return data.decode('utf-8', 'replace')
    finally:
        advapi32.CredFree(pointer)


def cred_delete(target):
    """删除凭据；本来就不存在返回 False。"""
    target = str(target or '').strip()
    if target == '':
        raise CredentialError('凭据 target 不能为空')
    advapi32 = _advapi32()
    ok = advapi32.CredDeleteW(target, CRED_TYPE_GENERIC, 0)
    if not ok:
        error = ctypes.get_last_error()
        if error == ERROR_NOT_FOUND:
            return False
        raise CredentialError(
            '删除 Windows 凭据管理器条目失败：%s' % ctypes.WinError(error)  # type: ignore[attr-defined]
        )
    return True


def save_password(host, user, password):
    """remember=true 时调用：把密码写进 Windows 凭据管理器（best-effort 由调用方处理）。"""
    return cred_save(cred_target(host, user), user, password)


def load_password(host, user):
    """读取已保存密码；没有返回 None。"""
    return cred_load(cred_target(host, user))


def delete_password(host, user):
    return cred_delete(cred_target(host, user))


def has_password(host, user):
    """凭据管理器里是否已有该 host/user 的密码（无法访问时按 False 处理）。"""
    if not credentials_available():
        return False
    try:
        return load_password(host, user) is not None
    except CredentialError:
        return False


# --------------------------------------------------------------- SSH 客户端
_PARAMIKO = None


def require_paramiko():
    """延迟导入 paramiko（便于 --paramiko-path 在导入前生效）。"""
    global _PARAMIKO
    if _PARAMIKO is None:
        try:
            import paramiko  # type: ignore
        except ImportError as exc:  # pragma: no cover - 环境相关
            raise HpcError(
                '未找到 paramiko：请先安装 paramiko，或在设置里指定 paramiko-path（%s）' % exc
            )
        _PARAMIKO = paramiko
    return _PARAMIKO


def _quote_remote_path(path):
    """给远程 shell 用的路径加引号：``~/x y`` 保持 ~ 展开。"""
    value = str(path or '')
    if re.fullmatch(r'[A-Za-z0-9._/+=:,-]+', value):
        return value
    if value.startswith('~/'):
        return '~/' + shlex.quote(value[2:])
    if value == '~':
        return '~'
    return shlex.quote(value)


class HpcClient:
    """密码 + OTP 的 SSH/SFTP 会话（密码只在内存，连接成功后立即清空）。"""

    def __init__(self, host, port=DEFAULT_PORT, user='', password='', otp='', timeout=25):
        self.host = str(host or '').strip()
        self.port = int(port or DEFAULT_PORT)
        self.user = str(user or '').strip()
        self.password = str(password or '')
        self.otp = str(otp or '')
        self.timeout = int(timeout or 25)
        self.client = None

    # ---------------------------------------------------------------- 连接
    def connect(self):
        paramiko = require_paramiko()
        if self.host == '':
            raise HpcError('SSH 登录失败：主机地址为空')
        if self.user == '':
            raise HpcError('SSH 登录失败：学号（用户名）为空')

        first_error = None
        candidate = paramiko.SSHClient()
        candidate.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            candidate.connect(
                self.host,
                port=self.port,
                username=self.user,
                password=self.password,
                timeout=self.timeout,
                banner_timeout=self.timeout,
                auth_timeout=self.timeout,
                allow_agent=False,
                look_for_keys=False,
            )
            self._adopt(candidate)
            return self
        except Exception as exc:  # noqa: BLE001 - 统一转成中文错误
            first_error = exc
            try:
                candidate.close()
            except Exception:  # noqa: BLE001
                pass

        transport = None
        try:
            transport = paramiko.Transport((self.host, self.port))
            transport.banner_timeout = self.timeout
            transport.start_client(timeout=self.timeout)
            transport.auth_interactive(self.user, self._interactive_handler)
            if not transport.is_authenticated():
                raise paramiko.AuthenticationException('Keyboard Interactive 认证未通过')
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client._transport = transport  # noqa: SLF001 - paramiko 公开做法
            self._adopt(client)
            return self
        except Exception as exc:  # noqa: BLE001
            if transport is not None:
                try:
                    transport.close()
                except Exception:  # noqa: BLE001
                    pass
            raise self._login_error(exc, first_error) from exc

    def _adopt(self, client):
        self.client = client
        try:
            self.client.get_transport().set_keepalive(30)
        except Exception:  # noqa: BLE001
            pass
        # 密码 / OTP 只在连接过程中用一次，成功后就从内存里清掉。
        self.password = ''
        self.otp = ''

    def _interactive_handler(self, title, instructions, prompt_list):
        answers = []
        for prompt in prompt_list or []:
            text = str(prompt[0] or '').lower() if prompt else ''
            if 'password' in text or '密码' in text:
                answers.append(self.password)
            elif any(key in text for key in ('verification', 'otp', 'code', '令牌', '动态')):
                answers.append(self.otp)
            else:
                answers.append(self.otp or self.password)
        return answers

    def _login_error(self, exc, first_error=None):
        paramiko = require_paramiko()
        if isinstance(exc, paramiko.AuthenticationException):
            return HpcError('SSH 登录失败：密码或 OTP 不正确（请检查学号、密码和 6 位动态码）')
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return HpcError(
                'SSH 登录失败：连接 %s:%s 超时（请确认在校园网内、主机地址正确）'
                % (self.host, self.port)
            )
        if isinstance(exc, socket.gaierror):
            return HpcError('SSH 登录失败：无法解析主机名 %s（请检查集群地址或校园网）' % self.host)
        if isinstance(exc, ConnectionRefusedError):
            return HpcError('SSH 登录失败：%s:%s 拒绝连接' % (self.host, self.port))
        message = str(exc).strip() or exc.__class__.__name__
        if first_error is not None and not isinstance(first_error, paramiko.AuthenticationException):
            first_text = str(first_error).strip()
            if first_text and first_text not in message:
                message = '%s（密码直连：%s）' % (message, first_text)
        return HpcError('SSH 登录失败：%s' % message)

    # ---------------------------------------------------------------- 状态
    def is_alive(self):
        try:
            transport = self.client.get_transport() if self.client is not None else None
            return bool(transport is not None and transport.is_active())
        except Exception:  # noqa: BLE001
            return False

    def close(self):
        self.password = ''
        self.otp = ''
        client, self.client = self.client, None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------------- 执行
    def _require_client(self):
        if self.client is None or not self.is_alive():
            raise HpcError('SSH 连接未建立或已断开，请重新登录')
        return self.client

    def run(self, cmd, timeout=60):
        """执行一条远程命令，返回 (stdout, stderr, exit_code)。"""
        cmd = str(cmd or '')
        if cmd.strip() == '':
            raise HpcError('命令为空')
        client = self._require_client()
        paramiko = require_paramiko()
        try:
            stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
            try:
                stdin.close()
            except Exception:  # noqa: BLE001
                pass
            out = stdout.read().decode('utf-8', 'replace')
            err = stderr.read().decode('utf-8', 'replace')
            code = stdout.channel.recv_exit_status()
        except (socket.timeout, TimeoutError):
            raise HpcError('执行命令超时（%s 秒）：%s' % (timeout, cmd[:120]))
        except paramiko.SSHException as exc:
            raise HpcError('SSH 连接错误：%s' % exc)
        except OSError as exc:
            raise HpcError('SSH 网络错误：%s' % exc)
        return out, err, code

    def run_command(self, cmd, timeout=300):
        """执行命令，stdout + stderr 合并返回（给 /exec 用）。"""
        out, err, code = self.run(cmd, timeout=timeout)
        return {'output': ((out or '') + (err or '')).strip(), 'code': code}

    # ---------------------------------------------------------------- 路径
    def resolve_remote(self, remote_dir):
        """把 ``~/xxx`` 展开成绝对路径（SFTP 不认 ~）。"""
        value = str(remote_dir or '').strip() or DEFAULT_REMOTE_DIR
        if not value.startswith('~'):
            return value
        out, _err, code = self.run('cd %s 2>/dev/null && pwd' % _quote_remote_path(value))
        text = (out or '').strip()
        if code == 0 and text:
            return text.splitlines()[-1].strip()
        home_out, _err2, _code2 = self.run('echo $HOME')
        home = (home_out or '').strip().splitlines()
        home = home[-1].strip() if home else ''
        return (home + value[1:]) if home else value

    def mkdir_p(self, remote_dir):
        target = self.resolve_remote(remote_dir).replace('\\', '/').rstrip('/')
        if target == '':
            return
        sftp = self.sftp()
        try:
            if target.startswith('/'):
                current = '/'
                parts = [part for part in target.split('/') if part]
            else:
                current = ''
                parts = [part for part in target.split('/') if part]
            for part in parts:
                current = current.rstrip('/') + '/' + part
                try:
                    sftp.stat(current)
                except IOError:
                    sftp.mkdir(current)
        finally:
            sftp.close()

    def sftp(self):
        client = self._require_client()
        try:
            return client.open_sftp()
        except Exception as exc:  # noqa: BLE001
            raise HpcError('打开 SFTP 通道失败：%s' % exc)

    def upload_bytes(self, remote_path, data):
        """把 bytes 写到远程文件（UTF-8 内容由调用方编码）。"""
        sftp = self.sftp()
        try:
            sftp.putfo(io.BytesIO(data), remote_path)
        except Exception as exc:  # noqa: BLE001
            raise HpcError('上传文件失败（%s）：%s' % (remote_path, exc))
        finally:
            sftp.close()

    def list_files(self, remote_dir, depth=2):
        """递归列远程文件（跳过 . 开头项），name 为相对路径，mtime 按 mtime 倒序。"""
        root = self.resolve_remote(remote_dir)
        try:
            depth = max(1, min(int(depth or 2), 8))
        except (TypeError, ValueError):
            depth = 2
        sftp = self.sftp()
        files = []
        exists = False

        def walk(path, prefix, level):
            nonlocal exists
            try:
                attrs = sftp.listdir_attr(path)
            except IOError:
                return
            if level == 1:
                exists = True
            for attr in attrs:
                filename = attr.filename
                if not filename or filename.startswith('.'):
                    continue
                full = path.rstrip('/') + '/' + filename
                relative = prefix + filename
                mode = int(attr.st_mode or 0)
                if stat_module.S_ISDIR(mode):
                    if level < depth:
                        walk(full, relative + '/', level + 1)
                else:
                    files.append({
                        'name': relative,
                        'size': int(attr.st_size or 0),
                        'mtime': time.strftime(
                            '%Y-%m-%d %H:%M', time.localtime(attr.st_mtime or 0)
                        ),
                        '_mtime': float(attr.st_mtime or 0),
                    })

        try:
            walk(root, '', 1)
        finally:
            sftp.close()
        files.sort(key=lambda item: item['_mtime'], reverse=True)
        for item in files:
            item.pop('_mtime', None)
        return {'root': root, 'exists': exists, 'files': files}

    def download_files(self, remote_dir, names, local_dir):
        """把远程相对路径下载到本地目录，返回本地绝对路径列表。"""
        root = self.resolve_remote(remote_dir)
        local_root = Path(str(local_dir or '')).expanduser()
        if str(local_root) in ('', '.'):
            raise HpcError('本地目录为空，无法下载')
        local_root.mkdir(parents=True, exist_ok=True)
        sftp = self.sftp()
        saved = []
        try:
            for raw_name in names or []:
                relative = str(raw_name or '').replace('\\', '/').lstrip('/')
                if relative == '':
                    continue
                pure = PurePosixPath(relative)
                if any(part in ('..', '') for part in pure.parts):
                    raise HpcError('远程文件名不合法：%s' % raw_name)
                target = local_root.joinpath(*pure.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                remote_path = posixpath.join(root.rstrip('/'), relative)
                try:
                    sftp.get(remote_path, str(target))
                except IOError as exc:
                    raise HpcError('下载失败（%s）：%s' % (relative, exc))
                saved.append(str(target.resolve()))
        finally:
            sftp.close()
        return saved

    # ---------------------------------------------------------------- 作业
    def query_queue(self):
        """按契约顺序尝试 squeue 写法，返回 {text, rows}；全失败抛 HpcError。"""
        candidates = [
            'squeue -u %s' % self.user,
            'squeue --user=%s' % self.user,
            'squeue -u %s -o "%%A %%j %%T %%M %%R"' % self.user,
            'squeue',
            'scontrol show jobs 2>/dev/null | head -60',
        ]
        tried = []
        for command in candidates:
            try:
                out, err, _code = self.run(command, timeout=20)
            except HpcError as exc:
                tried.append('%s -> %s' % (command, exc))
                continue
            text = (out or '').strip()
            error_text = (err or '').strip()
            if text and not error_text:
                return {'text': text, 'rows': parse_queue_rows(text)}
            tried.append('%s -> %s' % (command, error_text or text or '(空输出)'))
        raise HpcError(
            '查询失败：所有 squeue 写法都没有成功\n' + '\n'.join('  $ ' + item for item in tried)
        )

    def cancel_jobs(self, ids):
        ids = [str(item).strip() for item in (ids or []) if str(item).strip()]
        if not ids:
            raise HpcError('没有作业号')
        out, err, code = self.run('scancel ' + ' '.join(ids), timeout=60)
        text = ((out or '') + (err or '')).strip()
        return {'output': text, 'ids': ids, 'code': code}


# ------------------------------------------------------------------- 提交逻辑
def submit_jobs(client, remote_dir, local_paths, options=None, dependency=True):
    """上传 .gjf + 生成 job_<name>.sh，并提交（_td/_fc 用 afterok 依赖同批基态）。

    返回 ``{'remote_dir': 绝对远程目录, 'jobs': [{name, jobid, ok, output}, ...]}``。
    """
    options = normalize_submit_options(options)
    paths = []
    for raw in local_paths or []:
        path = Path(str(raw)).expanduser()
        if not path.is_file():
            raise HpcError('本地文件不存在：%s' % path)
        if path.suffix.lower() != '.gjf':
            raise HpcError('不是 .gjf 文件：%s' % path)
        paths.append(path)
    if not paths:
        raise HpcError('没有要提交的 .gjf 文件')

    remote = client.resolve_remote(remote_dir or DEFAULT_REMOTE_DIR)
    client.mkdir_p(remote)

    names = []
    for path in paths:
        name = path.stem
        if '/' in name or '\\' in name:
            raise HpcError('作业名不合法：%s' % name)
        try:
            gjf_data = path.read_bytes()
        except OSError as exc:
            raise HpcError('读取本地文件失败：%s（%s）' % (path, exc))
        script = build_slurm_script(name, options).encode('utf-8')
        client.upload_bytes(posixpath.join(remote, name + '.gjf'), gjf_data)
        client.upload_bytes(posixpath.join(remote, 'job_%s.sh' % name), script)
        names.append(name)

    jobs = []
    submitted_ids = {}
    for name in submission_order(names):
        dependency_flag = ''
        if dependency:
            base_name = dependency_base(name)
            base_id = submitted_ids.get(base_name) if base_name else None
            if base_id:
                dependency_flag = '--dependency=afterok:%s ' % base_id
        command = 'cd %s && sbatch %sjob_%s.sh' % (
            _quote_remote_path(remote), dependency_flag, name,
        )
        out, err, code = client.run(command, timeout=120)
        text = ((out or '') + (err or '')).strip()
        # 只有真提交成功才记下 id；否则不记，后续依赖作业就不会吃到假 id。
        jobid = parse_jobid(text, code)
        if jobid:
            submitted_ids[name] = jobid
        jobs.append({
            'name': name,
            'jobid': jobid,
            'ok': code == 0 and bool(jobid),
            'output': text,
        })
    return {'remote_dir': remote, 'jobs': jobs}
