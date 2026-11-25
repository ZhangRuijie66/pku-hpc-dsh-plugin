/**
 * PKU HPC bridge — host half.
 *
 * Keeps a PKU high-performance-computing SSH session alive in an independent
 * Python daemon (bridge/hpc_bridge.py) so the user logs in once with password +
 * OTP and can then submit Gaussian jobs, inspect the queue, cancel jobs and
 * download logs from chat, without reopening the standalone Gaussian tool.
 *
 * The daemon survives Harness restarts. This host half:
 *   - spawns / health-checks the daemon (identity file ~/.pku-hpc/bridge.json),
 *   - proxies same-origin browser routes under /pku-hpc for the settings page,
 *   - registers the `pku_hpc` agent tool over the same operations.
 */
import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'pku-hpc'
export const inject = ['tools', 'webServer']

const PLUGIN_DIR = path.dirname(fileURLToPath(import.meta.url))
const BRIDGE_SCRIPT = path.join(PLUGIN_DIR, 'bridge', 'hpc_bridge.py')
const VERSION = '1.0.0'
const ROUTE_PREFIX = '/pku-hpc'
const MAX_BODY_BYTES = 64 * 1024
const SPAWN_RETRY_MS = 30_000

const CLUSTERS = [
  { key: 'jx2', name: '教学二号', host: 'wmjx2-login.pku.edu.cn' },
  { key: 'wm1', name: '未名一号', host: 'wm1-login.pku.edu.cn' },
  { key: 'wm2', name: '未名二号', host: 'wm2-login.pku.edu.cn' },
  { key: 'sk1', name: '生科一号', host: 'wmsk1-login.pku.edu.cn' },
]

/** Per-cluster sbat ch defaults from the existing tool (gcalc/hpc.py CLUSTER_DEFAULTS). */
const CLUSTER_DEFAULTS = {
  jx2: { partition: 'compute', qos: '', walltime: '2-00:00:00', includeEnv: false, redirect: true },
  wm1: { partition: 'C032M0128G', qos: 'low', walltime: '5-00:00:00', includeEnv: true, redirect: false },
  wm2: { partition: 'C032M0128G', qos: 'low', walltime: '5-00:00:00', includeEnv: true, redirect: false },
  sk1: { partition: 'C032M0128G', qos: 'low', walltime: '5-00:00:00', includeEnv: true, redirect: false },
}

const DEFAULT_CONFIG = {
  cluster: 'jx2',
  // 学号留空：每个人在「设置 → PKU 超算」里填自己的。
  // （可在此填默认值方便本机使用，但公开仓库里不要写真实学号。）
  user: '',
  remoteDir: '~/g16jobs',
  partition: 'compute',
  qos: '',
  ntasks: 32,
  nodes: 1,
  walltime: '2-00:00:00',
  sbatchOut: 'job.%j.out',
  includeEnv: false,
  redirect: true,
  g16root: '$HOME',
  scrDir: '$HOME/tmp',
  idleExitMinutes: 720,
  pythonPath: '',
  paramikoPath: '',
}

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error)
}

function stringOr(value, fallback) {
  return typeof value === 'string' && value.trim() !== '' ? value.trim() : fallback
}

function normalizeConfig(raw) {
  const v = typeof raw === 'object' && raw !== null ? raw : {}
  const num = (value, fallback) => (Number.isFinite(value) ? Math.trunc(value) : fallback)
  const bool = (value, fallback) => (typeof value === 'boolean' ? value : fallback)
  return {
    cluster: stringOr(v.cluster, DEFAULT_CONFIG.cluster),
    user: stringOr(v.user, DEFAULT_CONFIG.user),
    remoteDir: stringOr(v.remoteDir, DEFAULT_CONFIG.remoteDir),
    partition: stringOr(v.partition, DEFAULT_CONFIG.partition),
    qos: typeof v.qos === 'string' ? v.qos.trim() : DEFAULT_CONFIG.qos,
    ntasks: num(v.ntasks, DEFAULT_CONFIG.ntasks),
    nodes: num(v.nodes, DEFAULT_CONFIG.nodes),
    walltime: stringOr(v.walltime, DEFAULT_CONFIG.walltime),
    sbatchOut: stringOr(v.sbatchOut, DEFAULT_CONFIG.sbatchOut),
    includeEnv: bool(v.includeEnv, DEFAULT_CONFIG.includeEnv),
    redirect: bool(v.redirect, DEFAULT_CONFIG.redirect),
    g16root: stringOr(v.g16root, DEFAULT_CONFIG.g16root),
    scrDir: stringOr(v.scrDir, DEFAULT_CONFIG.scrDir),
    idleExitMinutes: Math.max(5, num(v.idleExitMinutes, DEFAULT_CONFIG.idleExitMinutes)),
    pythonPath: typeof v.pythonPath === 'string' ? v.pythonPath.trim() : '',
    paramikoPath: typeof v.paramikoPath === 'string' ? v.paramikoPath.trim() : '',
  }
}

function clusterByKey(key) {
  return CLUSTERS.find((c) => c.key === key) ?? CLUSTERS[0]
}

function clusterByHost(host) {
  const h = String(host ?? '').toLowerCase()
  return CLUSTERS.find((c) => h !== '' && (h === c.host || h.includes(c.host.split('.')[0]))) ?? null
}

function emptySession() {
  return {
    connected: false,
    host: '',
    port: 22,
    user: '',
    // 上次登录留下的学号：插件不内置默认学号（公开仓库），设置页用它回填。
    lastUser: '',
    since: '',
    lastActivity: '',
    cluster: '',
    clusterName: '',
    passwordSaved: false,
  }
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/** Synchronous PATH scan; skips UNC roots so a dead share cannot hang boot. */
function whichSync(fileName) {
  const dirs = String(process.env.PATH ?? '').split(path.delimiter).filter(Boolean)
  const exts = process.platform === 'win32'
    ? String(process.env.PATHEXT ?? '.EXE;.CMD;.BAT;.COM').split(';').filter(Boolean)
    : ['']
  for (const dir of dirs) {
    if (dir.startsWith('\\\\') || dir.startsWith('//')) continue
    for (const ext of exts) {
      const candidate = path.join(dir, fileName + ext.toLowerCase())
      try {
        if (existsSync(candidate)) return candidate
      } catch {
        /* unreadable entry */
      }
    }
  }
  return null
}

function sendJson(res, statusCode, value) {
  const body = JSON.stringify(value)
  res.statusCode = statusCode
  res.setHeader('content-type', 'application/json; charset=utf-8')
  res.setHeader('cache-control', 'no-store')
  res.setHeader('content-length', String(Buffer.byteLength(body)))
  res.end(body)
}

function isLoopbackAddress(address) {
  return address === '127.0.0.1' || address === '::1' || address === '::ffff:127.0.0.1'
}

function isLoopbackOrigin(value) {
  if (typeof value !== 'string' || value === '') return false
  // Sandboxed frames / opaque origins still reach this loopback-only server.
  if (value === 'null') return true
  let parsed
  try {
    parsed = new URL(value)
  } catch {
    return false
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return false
  const hostname = parsed.hostname
  return hostname === '127.0.0.1' || hostname === 'localhost' || hostname === '[::1]' || hostname === '::1'
}

/**
 * Mutations must come from the local page. The DSH desktop shell may load the
 * page from a different loopback host/port than the one in the Host header, so
 * accept any loopback Origin + same-site fetch metadata, while rejecting
 * cross-site origins (remote CSRF) and any non-loopback origin.
 */
function isSameOriginMutation(req) {
  const host = req.headers.host
  if (typeof host !== 'string' || host === '') return false
  const origin = req.headers.origin
  const site = req.headers['sec-fetch-site']
  if (typeof origin === 'string' && origin !== '') {
    if (!isLoopbackOrigin(origin)) return false
    if (site === 'cross-site') return false
    return true
  }
  // No Origin header: fall back to fetch metadata when the browser sends it.
  return site === undefined || site === '' || site === 'same-origin' || site === 'same-site'
}

async function readJsonBody(req) {
  req.setEncoding('utf8')
  let text = ''
  for await (const chunk of req) {
    text += chunk
    if (Buffer.byteLength(text) > MAX_BODY_BYTES) throw new Error('request body exceeds 64 KiB')
  }
  if (text.length === 0) return {}
  const value = JSON.parse(text)
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error('request body must be an object')
  }
  return value
}

export async function apply(ctx, rawConfig) {
  const config = normalizeConfig(rawConfig)
  const dataDir = path.join(homedir(), '.pku-hpc')
  const idFile = path.join(dataDir, 'bridge.json')
  const logFile = path.join(dataDir, 'bridge.log')
  const logger = ctx.logger
  const warn = (message) => {
    try {
      logger?.warn?.(`pku-hpc: ${message}`)
    } catch {
      /* logger optional */
    }
  }

  let spawnFailure = { at: 0, message: '' }
  let pendingSpawn = null
  let stopped = false

  // ------------------------------------------------------------ daemon management

  function resolvePython() {
    if (config.pythonPath !== '') return config.pythonPath
    const found = whichSync('python')
    if (found !== null) return found
    if (process.platform === 'win32') {
      for (const candidate of ['C:\\Python311\\python.exe', 'C:\\Python312\\python.exe', 'C:\\Python310\\python.exe']) {
        if (existsSync(candidate)) return candidate
      }
    }
    return 'python'
  }

  /** pythonw next to python.exe keeps a console window from flashing on first launch. */
  function resolvePythonWindowless(pythonPath) {
    if (process.platform !== 'win32') return pythonPath
    const dir = path.dirname(pythonPath)
    const candidate = path.join(dir, 'pythonw.exe')
    try {
      if (existsSync(candidate)) return candidate
    } catch {
      /* ignore */
    }
    return pythonPath
  }

  function readIdentity() {
    try {
      const value = JSON.parse(readFileSync(idFile, 'utf8'))
      if (value !== null && typeof value === 'object' && Number.isFinite(value.port) && typeof value.token === 'string') {
        return value
      }
    } catch {
      /* missing or partially written */
    }
    return null
  }

  async function fetchDaemon(identity, route, options = {}) {
    const { method = 'GET', body, timeoutMs = 30_000 } = options
    const response = await fetch(`http://127.0.0.1:${identity.port}${route}`, {
      method,
      headers: {
        authorization: `Bearer ${identity.token}`,
        ...(body === undefined ? {} : { 'content-type': 'application/json' }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    })
    if (response.status === 401) throw new Error('后台进程鉴权失败：请重启后台进程（设置页点「断开」后重试）')
    let data
    try {
      data = await response.json()
    } catch {
      throw new Error(`后台进程响应无法解析（HTTP ${response.status}）`)
    }
    if (data === null || typeof data !== 'object') throw new Error('后台进程返回了无效响应')
    return data
  }

  async function healthy(identity) {
    try {
      const data = await fetchDaemon(identity, '/health', { timeoutMs: 2000 })
      return data && data.ok ? data : null
    } catch {
      return null
    }
  }

  async function startDaemon() {
    mkdirSync(dataDir, { recursive: true })
    try {
      rmSync(idFile, { force: true })
    } catch {
      /* stale id file */
    }
    if (!existsSync(BRIDGE_SCRIPT)) {
      throw new Error(`找不到后台进程脚本：${BRIDGE_SCRIPT}`)
    }
    const python = resolvePython()
    const exe = resolvePythonWindowless(python)
    const args = [BRIDGE_SCRIPT, '--data-dir', dataDir, '--idle-exit-minutes', String(config.idleExitMinutes)]
    if (config.paramikoPath !== '') args.push('--paramiko-path', config.paramikoPath)
    let child = null
    let spawnError = null
    try {
      child = spawn(exe, args, {
        cwd: dataDir,
        detached: true,
        stdio: 'ignore',
        windowsHide: true,
        env: process.env,
      })
      child.on('error', (error) => {
        spawnError = error
      })
      child.unref()
    } catch (error) {
      throw new Error(`无法启动后台进程（${exe}）：${errorMessage(error)}`)
    }
    const deadline = Date.now() + 20_000
    while (Date.now() < deadline) {
      if (spawnError !== null) {
        throw new Error(`无法启动后台进程（${exe}）：${errorMessage(spawnError)}`)
      }
      const identity = readIdentity()
      if (identity !== null) {
        const health = await healthy(identity)
        if (health !== null) return { identity, health }
      }
      await delay(200)
    }
    throw new Error(
      `后台进程启动超时（python: ${exe}）。请确认 ${BRIDGE_SCRIPT} 存在且 Python 可导入 paramiko；日志见 ${logFile}`,
    )
  }

  async function ensureDaemon() {
    if (stopped) throw new Error('插件正在卸载')
    const existing = readIdentity()
    if (existing !== null) {
      const health = await healthy(existing)
      if (health !== null) return { identity: existing, health }
    }
    if (spawnFailure.message !== '' && Date.now() - spawnFailure.at < SPAWN_RETRY_MS) {
      throw new Error(spawnFailure.message)
    }
    if (pendingSpawn !== null) return pendingSpawn
    pendingSpawn = startDaemon()
      .then((result) => {
        spawnFailure = { at: 0, message: '' }
        return result
      })
      .catch((error) => {
        spawnFailure = { at: Date.now(), message: errorMessage(error) }
        throw error
      })
      .finally(() => {
        pendingSpawn = null
      })
    return pendingSpawn
  }

  async function rpc(route, options) {
    const { identity } = await ensureDaemon()
    return fetchDaemon(identity, route, options)
  }

  function sessionView(health) {
    const session = health && typeof health.session === 'object' && health.session !== null ? health.session : {}
    const host = typeof session.host === 'string' ? session.host : ''
    const cluster = clusterByHost(host)
    return {
      connected: session.connected === true,
      host,
      port: Number.isFinite(session.port) ? session.port : 22,
      user: typeof session.user === 'string' ? session.user : '',
      lastUser: typeof session.lastUser === 'string' ? session.lastUser : '',
      since: typeof session.since === 'string' ? session.since : '',
      lastActivity: typeof session.lastActivity === 'string' ? session.lastActivity : '',
      cluster: cluster !== null ? cluster.key : '',
      clusterName: cluster !== null ? cluster.name : '',
      passwordSaved: health?.passwordSaved === true,
    }
  }

  async function stateView(options = {}) {
    const { includeQueue = false } = options
    const state = {
      ok: true,
      daemon: { running: false, pid: 0, version: VERSION, startedAt: '' },
      session: emptySession(),
      passwordSaved: false,
      clusters: CLUSTERS.map(({ key, name: clusterName }) => ({ key, name: clusterName })),
      defaults: {
        cluster: config.cluster,
        user: config.user,
        remoteDir: config.remoteDir,
        partition: config.partition,
        qos: config.qos,
        ntasks: config.ntasks,
        nodes: config.nodes,
        walltime: config.walltime,
      },
      error: null,
    }
    try {
      const { identity, health } = await ensureDaemon()
      state.daemon = {
        running: true,
        pid: Number.isFinite(health.pid) ? health.pid : Number(identity.pid ?? 0),
        version: typeof health.version === 'string' ? health.version : VERSION,
        startedAt: typeof health.startedAt === 'string' ? health.startedAt : '',
      }
      state.session = sessionView(health)
      state.passwordSaved = health?.passwordSaved === true
    } catch (error) {
      state.error = errorMessage(error)
    }
    if (includeQueue && state.session.connected) {
      try {
        const queue = await rpc('/queue', { timeoutMs: 30_000 })
        state.queue = queue
      } catch (error) {
        state.queue = { ok: false, error: errorMessage(error) }
      }
    }
    return state
  }

  // --------------------------------------------------------------- operations

  async function login(params) {
    const cluster = clusterByKey(stringOr(params.cluster, config.cluster))
    const user = stringOr(params.user, config.user)
    const otp = stringOr(params.otp, '')
    if (user === '') {
      return { ok: false, error: '请先填学号（设置 → PKU 超算 → 学号）' }
    }
    if (otp === '') return { ok: false, error: '请输入 OTP 动态码（手机令牌 6 位数字）' }
    const data = await rpc('/login', {
      method: 'POST',
      timeoutMs: 60_000,
      body: {
        host: cluster.host,
        port: 22,
        user,
        password: typeof params.password === 'string' ? params.password : '',
        otp,
        remember: params.remember !== false,
      },
    })
    if (!data.ok) return { ok: false, error: stringOr(data.error, '登录失败') }
    return { ok: true, state: await stateView() }
  }

  async function logout() {
    try {
      const data = await rpc('/logout', { method: 'POST', timeoutMs: 15_000 })
      if (!data.ok) return { ok: false, error: stringOr(data.error, '断开失败') }
    } catch (error) {
      return { ok: false, error: errorMessage(error) }
    }
    return { ok: true, state: await stateView() }
  }

  /** Expand directories into their .gjf files and reject anything missing. */
  function resolveLocalFiles(entries) {
    const list = Array.isArray(entries) ? entries : []
    if (list.length === 0) throw new Error('请给出要提交的 .gjf 文件（files）')
    const files = []
    for (const entry of list) {
      if (typeof entry !== 'string' || entry.trim() === '') continue
      const target = path.isAbsolute(entry.trim()) ? entry.trim() : path.resolve(entry.trim())
      if (!existsSync(target)) throw new Error(`本地文件不存在：${target}`)
      const info = statSync(target)
      if (info.isDirectory()) {
        const inside = readdirSync(target)
          .filter((file) => file.toLowerCase().endsWith('.gjf'))
          .map((file) => path.join(target, file))
        if (inside.length === 0) throw new Error(`目录里没有 .gjf 文件：${target}`)
        files.push(...inside)
      } else if (info.isFile()) {
        files.push(target)
      } else {
        throw new Error(`不是普通文件：${target}`)
      }
    }
    if (files.length === 0) throw new Error('没有可提交的文件')
    return [...new Set(files)]
  }

  async function submit(params) {
    const files = resolveLocalFiles(params.files)
    const remoteDir = stringOr(params.remote_dir, config.remoteDir)
    const clusterKey = stringOr(params.cluster, config.cluster)
    // Plugin config describes the default cluster (jx2). Switching cluster in a
    // submit call must also switch partition/qos/env/redirect, otherwise a wm1
    // job would silently inherit the jx2 teaching-cluster script defaults.
    const fallback = clusterKey === config.cluster
      ? {
          partition: config.partition,
          qos: config.qos,
          walltime: config.walltime,
          includeEnv: config.includeEnv,
          redirect: config.redirect,
        }
      : (CLUSTER_DEFAULTS[clusterKey] ?? CLUSTER_DEFAULTS.jx2)
    const options = {
      cluster: clusterKey,
      partition: stringOr(params.partition, fallback.partition),
      qos: typeof params.qos === 'string' ? params.qos : fallback.qos,
      ntasks: Number.isFinite(params.ntasks) ? Math.trunc(params.ntasks) : config.ntasks,
      nodes: Number.isFinite(params.nodes) ? Math.trunc(params.nodes) : config.nodes,
      walltime: stringOr(params.walltime, fallback.walltime),
      sbatch_out: stringOr(params.sbatch_out, config.sbatchOut),
      include_env: typeof params.include_env === 'boolean' ? params.include_env : fallback.includeEnv,
      redirect: typeof params.redirect === 'boolean' ? params.redirect : fallback.redirect,
      g16root: config.g16root,
      scr_dir: config.scrDir,
    }
    const data = await rpc('/submit', {
      method: 'POST',
      timeoutMs: 600_000,
      body: { remote_dir: remoteDir, files, options, dependency: true },
    })
    if (!data.ok) return { ok: false, error: stringOr(data.error, '提交失败') }
    return {
      ok: true,
      remote_dir: data.remote_dir,
      jobs: Array.isArray(data.jobs) ? data.jobs : [],
      text: '',
    }
  }

  async function performAction(action, params = {}) {
    switch (action) {
      case 'status': {
        const state = await stateView({ includeQueue: true })
        return { ok: true, action, data: state, text: statusText(state) }
      }
      case 'login': {
        const result = await login(params)
        if (!result.ok) return { ok: false, action, error: result.error, text: `登录失败：${result.error}` }
        return { ok: true, action, data: result.state, text: loginText(result.state) }
      }
      case 'logout': {
        const result = await logout()
        if (!result.ok) return { ok: false, action, error: result.error, text: `断开失败：${result.error}` }
        return { ok: true, action, data: result.state, text: '已断开 SSH 连接。' }
      }
      case 'queue': {
        const data = await rpc('/queue', { timeoutMs: 30_000 })
        if (!data.ok) return { ok: false, action, error: stringOr(data.error, '查询失败'), text: `查询队列失败：${stringOr(data.error, '未知错误')}` }
        // Top-level rows/text keep the browser route simple; `data` keeps the daemon result.
        return { ok: true, action, data, rows: Array.isArray(data.rows) ? data.rows : [], text: queueText(data) }
      }
      case 'exec': {
        const command = stringOr(params.command, '')
        if (command === '') return { ok: false, action, error: '缺少 command', text: '缺少要执行的命令（command）。' }
        const data = await rpc('/exec', { method: 'POST', timeoutMs: 300_000, body: { cmd: command, timeout: 120 } })
        if (!data.ok) return { ok: false, action, error: stringOr(data.error, '执行失败'), text: `执行失败：${stringOr(data.error, '未知错误')}` }
        return { ok: true, action, data, text: execText(command, data) }
      }
      case 'submit': {
        const result = await submit(params)
        if (!result.ok) return { ok: false, action, error: result.error, text: `提交失败：${result.error}` }
        return { ok: true, action, data: result, text: submitText(result) }
      }
      case 'cancel': {
        const ids = (Array.isArray(params.job_ids) ? params.job_ids : [])
          .map((id) => String(id).trim())
          .filter((id) => id !== '')
        if (ids.length === 0) return { ok: false, action, error: '缺少 job_ids', text: '缺少要取消的作业号（job_ids）。' }
        const data = await rpc('/cancel', { method: 'POST', timeoutMs: 60_000, body: { ids } })
        if (!data.ok) return { ok: false, action, error: stringOr(data.error, '取消失败'), text: `取消失败：${stringOr(data.error, '未知错误')}` }
        return { ok: true, action, data, text: `已请求取消作业 ${ids.join('、')}。\n${String(data.output ?? '').trim()}`.trim() }
      }
      case 'files': {
        const remoteDir = stringOr(params.remote_dir, config.remoteDir)
        const suffix = typeof params.suffix === 'string' ? params.suffix : ''
        const data = await rpc(
          `/files?dir=${encodeURIComponent(remoteDir)}&suffix=${encodeURIComponent(suffix)}&depth=2`,
          { timeoutMs: 60_000 },
        )
        if (!data.ok) return { ok: false, action, error: stringOr(data.error, '列文件失败'), text: `列文件失败：${stringOr(data.error, '未知错误')}` }
        return { ok: true, action, data, text: filesText(remoteDir, data) }
      }
      case 'download': {
        const remoteDir = stringOr(params.remote_dir, config.remoteDir)
        const names = (Array.isArray(params.names) ? params.names : []).map(String).filter((n) => n !== '')
        if (names.length === 0) return { ok: false, action, error: '缺少 names', text: '缺少要下载的远程文件（names）。' }
        const localDir = stringOr(params.local_dir, path.join(process.cwd(), 'hpc_download'))
        const data = await rpc('/download', {
          method: 'POST',
          timeoutMs: 600_000,
          body: { remote_dir: remoteDir, names, local_dir: localDir },
        })
        if (!data.ok) return { ok: false, action, error: stringOr(data.error, '下载失败'), text: `下载失败：${stringOr(data.error, '未知错误')}` }
        const files = Array.isArray(data.files) ? data.files : []
        return { ok: true, action, data, text: `已下载 ${files.length} 个文件到 ${files[0] ? path.dirname(files[0]) : localDir}：\n${files.join('\n')}` }
      }
      default:
        return { ok: false, action, error: `不支持的操作：${action}`, text: `不支持的操作：${action}` }
    }
  }

  // -------------------------------------------------------------- text builders

  function statusText(state) {
    const lines = []
    lines.push(state.daemon.running ? `后台进程：运行中（pid ${state.daemon.pid}）` : `后台进程：未启动${state.error ? ` —— ${state.error}` : ''}`)
    if (state.session.connected) {
      lines.push(`SSH：已连接 ${state.session.clusterName || state.session.host} · ${state.session.user}`)
      lines.push(`连接时间：${state.session.since || '—'}`)
    } else {
      lines.push('SSH：未连接（在设置 → PKU 超算 里输入密码 + OTP 登录）')
    }
    lines.push(`已保存密码：${state.passwordSaved ? '是（Windows 凭据管理器）' : '否'}`)
    lines.push(`默认远程目录：${state.defaults.remoteDir}`)
    if (state.queue) {
      if (state.queue.ok) lines.push('', queueText(state.queue))
      else lines.push('', `队列查询失败：${state.queue.error}`)
    }
    return lines.join('\n')
  }

  function loginText(state) {
    const s = state.session
    return `登录成功：${s.clusterName || s.host} · ${s.user}\n会话会一直保持（重启 Harness 也不用重新登录，除非连接断开或后台进程退出）。`
  }

  function queueText(data) {
    const rows = Array.isArray(data.rows) ? data.rows : []
    if (rows.length === 0) return `队列输出：\n${String(data.text ?? '').trim() || '(空)'}`
    const header = ['JOBID', 'NAME', 'STATE', 'TIME', 'REASON']
    const body = rows.map((row) => [row.jobid ?? '', row.name ?? '', row.state ?? '', row.time ?? '', row.reason ?? ''].join('\t'))
    return ['队列：', header.join('\t'), ...body].join('\n')
  }

  function execText(command, data) {
    return `$ ${command}\n${String(data.output ?? '').trim() || '(无输出)'}\n[退出码 ${data.code ?? '?'}]`
  }

  function submitText(result) {
    const lines = [`提交到 ${result.remote_dir}：`]
    for (const job of result.jobs) {
      lines.push(job.ok ? `✓ ${job.name} → 作业号 ${job.jobid || '(未解析到)'}` : `✗ ${job.name}：${job.output || '提交失败'}`)
    }
    return lines.join('\n')
  }

  function filesText(remoteDir, data) {
    const files = Array.isArray(data.files) ? data.files : []
    if (!data.exists) return `远程目录不存在：${remoteDir}`
    if (files.length === 0) return `${remoteDir} 里没有匹配的文件`
    const size = (bytes) => {
      if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
      if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`
      return `${bytes} B`
    }
    return [`${remoteDir}：`, ...files.map((f) => `${f.name}\t${size(Number(f.size ?? 0))}\t${f.mtime ?? ''}`)].join('\n')
  }

  // ------------------------------------------------------------------- browser

  function guardLoopback(req, res) {
    const address = req.socket?.remoteAddress
    if (!isLoopbackAddress(address)) {
      sendJson(res, 403, { ok: false, error: `仅允许本机访问（来源 ${String(address)}）。` })
      return false
    }
    return true
  }

  function rejected(res, error) {
    sendJson(res, 200, { ok: false, error: errorMessage(error) })
  }

  const route = (method, pathname, handler) => {
    ctx.effect(() => {
      try {
        return ctx.webServer.register({
          kind: 'exact',
          path: pathname,
          async handler(req, res) {
            if (!guardLoopback(req, res)) return
            if (req.method !== method) {
              res.setHeader('allow', method)
              sendJson(res, 405, { ok: false, error: 'Method not allowed.' })
              return
            }
            if (method === 'POST' && !isSameOriginMutation(req)) {
              sendJson(res, 403, {
                ok: false,
                error: '写操作被拒绝：来源校验未通过。请刷新页面后重试。',
                detail: {
                  host: req.headers.host ?? null,
                  origin: req.headers.origin ?? null,
                  secFetchSite: req.headers['sec-fetch-site'] ?? null,
                },
              })
              return
            }
            try {
              const body = method === 'POST' ? await readJsonBody(req) : {}
              const result = await handler(body, req)
              if (result !== undefined) sendJson(res, 200, result)
            } catch (error) {
              warn(`route ${pathname} failed: ${errorMessage(error)}`)
              rejected(res, error)
            }
          },
        })
      } catch (error) {
        warn(`route register failed (${pathname}): ${errorMessage(error)}`)
        return () => {}
      }
    }, `pku-hpc: ${method} ${pathname}`)
  }

  route('GET', `${ROUTE_PREFIX}/state`, async () => stateView())
  route('POST', `${ROUTE_PREFIX}/login`, async (body) => {
    const result = await login(body)
    if (!result.ok) return result
    return { ok: true, state: result.state }
  })
  route('POST', `${ROUTE_PREFIX}/logout`, async () => logout())
  route('POST', `${ROUTE_PREFIX}/action`, async (body) => {
    const action = stringOr(body.action, '')
    if (action === '') return { ok: false, error: '缺少 action' }
    const result = await performAction(action, body)
    return result
  })

  // ---------------------------------------------------------------------- tool

  ctx.effect(() => ctx.tools.register({
    name: 'pku_hpc',
    description:
      '北大高性能计算平台（PKU HPC，Slurm）通道。一次登录（密码 + OTP，建议让用户在「设置 → PKU 超算」页面输入）后，可提交 Gaussian 输入文件、查作业队列、执行登录节点命令、取消作业、列远程文件、下载日志/chk。'
      + ' action 取值：status（查看登录状态与队列）、login（工具内登录，需 otp；用户不知 OTP 时请让用户到设置页登录）、logout、queue、exec（command）、submit（files 本地 .gjf 路径，可用 remote_dir 覆盖默认 ~/g16jobs）、cancel（job_ids）、files（remote_dir, suffix 可选如 .log）、download（remote_dir, names 远程文件名, local_dir 本地目录）。'
      + ' 提交默认集群为教学二号（jx2）：-p compute、--ntasks-per-node=32、g16 < x.gjf > x.log；_td/_fc 会自动用 afterok 依赖基态作业。',
    parameters: {
      type: 'object',
      properties: {
        action: {
          type: 'string',
          enum: ['status', 'login', 'logout', 'queue', 'exec', 'submit', 'cancel', 'files', 'download'],
          description: '要执行的操作。',
        },
        cluster: { type: 'string', enum: ['jx2', 'wm1', 'wm2', 'sk1'], description: '集群：jx2 教学二号（默认）、wm1 未名一号、wm2 未名二号、sk1 生科一号。' },
        user: { type: 'string', description: '学号（默认取插件配置）。' },
        password: { type: 'string', description: '仅 login 用；留空则使用 Windows 凭据管理器里已保存的密码。' },
        otp: { type: 'string', description: '仅 login 用；手机令牌 6 位动态码，一次连接有效。' },
        remember_password: { type: 'boolean', description: '仅 login 用；是否把密码写入 Windows 凭据管理器（默认 true）。' },
        command: { type: 'string', description: 'exec：在登录节点执行的命令，如 squeue -u $USER、sbatch job_A1.sh、ls -lh *.log。' },
        files: { type: 'array', items: { type: 'string' }, description: 'submit：本地 .gjf 文件绝对路径（也可以是包含 .gjf 的目录）。' },
        remote_dir: { type: 'string', description: '远程目录，默认 ~/g16jobs。' },
        job_ids: { type: 'array', items: { type: 'string' }, description: 'cancel：要取消的作业号。' },
        suffix: { type: 'string', description: 'files：按后缀过滤，如 .log、.chk。' },
        names: { type: 'array', items: { type: 'string' }, description: 'download：远程相对文件名（files 返回的 name）。' },
        local_dir: { type: 'string', description: 'download：本地保存目录，默认当前工作目录下 hpc_download。' },
        partition: { type: 'string', description: 'submit：覆盖分区（默认 compute）。' },
        qos: { type: 'string', description: 'submit：覆盖 QOS。' },
        ntasks: { type: 'integer', description: 'submit：每节点核数（默认 32）。' },
        nodes: { type: 'integer', description: 'submit：节点数（默认 1）。' },
        walltime: { type: 'string', description: 'submit：墙钟，如 2-00:00:00。' },
        include_env: { type: 'boolean', description: 'submit：脚本里是否 source g16.profile（未名/生科集群需要）。' },
        redirect: { type: 'boolean', description: 'submit：是否用 g16 < x.gjf 重定向写法（教学二号默认 true）。' },
      },
      required: ['action'],
      additionalProperties: false,
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render: (_args, value) => {
        const text = value !== null && typeof value === 'object' && typeof value.text === 'string'
          ? value.text
          : JSON.stringify(value, null, 2)
        return [{ type: 'text', text }]
      },
    },
    async execute(args) {
      const params = args !== null && typeof args === 'object' ? args : {}
      const action = stringOr(params.action, '')
      try {
        return await performAction(action, params)
      } catch (error) {
        return { ok: false, action, error: errorMessage(error), text: `操作失败：${errorMessage(error)}` }
      }
    },
  }), 'pku-hpc: tool')

  ctx.effect(() => () => {
    stopped = true
  }, 'pku-hpc: teardown')
}
