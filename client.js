/**
 * @local/pku-hpc — client half (web settings page).
 *
 * Registers the "PKU 超算 / PKU HPC" section in the web settings panel and
 * talks to the host plugin routes (`/pku-hpc/state`, `/pku-hpc/login`,
 * `/pku-hpc/logout`, `/pku-hpc/action`) which proxy the resident Python
 * bridge daemon. See CONTRACT.md for the frozen request/response shapes.
 *
 * i18n: dictionaries are registered with the framework locale service under
 * namespace `pku-hpc` (zh + en). The active locale follows DSH's locale
 * settings; the component re-renders on `locale` snapshot changes and the nav
 * label is a thunk re-read by the shell.
 *
 * The only imports are `react` — no other Harness package is required here.
 */
window.__ModuleLoader__.load({
  id: '@local/pku-hpc',
  factory: (require) => {
    const module = { exports: {} }
    const React = require('react')
    const h = React.createElement

    const STATE_ROUTE = '/pku-hpc/state'
    const LOGIN_ROUTE = '/pku-hpc/login'
    const LOGOUT_ROUTE = '/pku-hpc/logout'
    const ACTION_ROUTE = '/pku-hpc/action'
    const STATE_POLL_MS = 3000
    const QUEUE_POLL_MS = 10000
    const QUEUE_LIMIT = 50
    const LOCALE_NS = 'pku-hpc'

    const MESSAGES = {
      zh: {
        section: 'PKU 超算',
        status: '状态',
        'badge.loading': '加载中…',
        'badge.connected': '已连接',
        'badge.disconnected': '未连接',
        'badge.daemonOff': '后台进程未启动',
        'badge.connError': '连接错误',
        'detail.connected': '{cluster} · 学号 {user} · 已连接 {duration}',
        'duration.seconds': '{n} 秒',
        'duration.minutes': '{n} 分钟',
        'duration.hour': '{h} 小时',
        'duration.hours': '{h} 小时 {m} 分钟',
        'duration.day': '{d} 天',
        'duration.days': '{d} 天 {h} 小时',
        'heading.login': '登录',
        'heading.queue': '队列',
        'label.cluster': '集群',
        'label.user': '学号',
        'label.password': '密码',
        'label.otp': 'OTP 动态码',
        'label.remember': '记住密码（Windows 凭据管理器）',
        'placeholder.cluster': '集群 key',
        'placeholder.user': '请输入学号',
        'placeholder.password': '请输入密码',
        'placeholder.passwordSaved': '已保存，可留空',
        'placeholder.otp': '6 位动态码',
        'btn.login': '登录',
        'btn.loggingIn': '登录中…',
        'btn.logout': '断开',
        'btn.loggingOut': '断开中…',
        'btn.refresh': '刷新',
        'btn.refreshing': '刷新中…',
        'btn.cancel': '取消作业',
        'btn.cancelling': '取消中…',
        'queue.loading': '查询中…',
        'queue.empty': '队列为空',
        'queue.count': '{n} 个作业',
        'queue.truncated': '仅显示前 50 条',
        'col.jobid': 'JOBID',
        'col.name': 'NAME',
        'col.state': 'STATE',
        'col.time': 'TIME',
        'col.reason': 'REASON',
        'label.cancelIds': '按作业号取消',
        'placeholder.cancelIds': '作业号，空格分隔',
        'footer.remoteDir': '默认远程目录：{dir}',
        'footer.security':
          '密码只存在 Windows 凭据管理器，OTP 只在登录时用一次；后台进程常驻，重启 Harness 不用重新登录。',
        'error.clusterRequired': '请选择集群',
        'error.userRequired': '请输入学号',
        'error.passwordRequired': '请输入密码（没有已保存的密码）',
        'error.otpRequired': '请输入 OTP 动态码',
        'error.jobIdsRequired': '请输入要取消的作业号',
        'error.unknown': '请求失败，请稍后再试',
      },
      en: {
        section: 'PKU HPC',
        status: 'Status',
        'badge.loading': 'Loading…',
        'badge.connected': 'Connected',
        'badge.disconnected': 'Not connected',
        'badge.daemonOff': 'Background process not running',
        'badge.connError': 'Connection error',
        'detail.connected': '{cluster} · ID {user} · connected for {duration}',
        'duration.seconds': '{n}s',
        'duration.minutes': '{n} min',
        'duration.hour': '{h} h',
        'duration.hours': '{h} h {m} min',
        'duration.day': '{d} d',
        'duration.days': '{d} d {h} h',
        'heading.login': 'Login',
        'heading.queue': 'Queue',
        'label.cluster': 'Cluster',
        'label.user': 'Student ID',
        'label.password': 'Password',
        'label.otp': 'OTP code',
        'label.remember': 'Remember password (Windows Credential Manager)',
        'placeholder.cluster': 'Cluster key',
        'placeholder.user': 'Enter your student ID',
        'placeholder.password': 'Enter password',
        'placeholder.passwordSaved': 'Saved — leave blank',
        'placeholder.otp': '6-digit code',
        'btn.login': 'Log in',
        'btn.loggingIn': 'Logging in…',
        'btn.logout': 'Disconnect',
        'btn.loggingOut': 'Disconnecting…',
        'btn.refresh': 'Refresh',
        'btn.refreshing': 'Refreshing…',
        'btn.cancel': 'Cancel jobs',
        'btn.cancelling': 'Cancelling…',
        'queue.loading': 'Loading…',
        'queue.empty': 'Queue is empty',
        'queue.count': '{n} jobs',
        'queue.truncated': 'Showing the first 50 rows only',
        'col.jobid': 'JOBID',
        'col.name': 'NAME',
        'col.state': 'STATE',
        'col.time': 'TIME',
        'col.reason': 'REASON',
        'label.cancelIds': 'Cancel by job ID',
        'placeholder.cancelIds': 'Job IDs, space-separated',
        'footer.remoteDir': 'Default remote directory: {dir}',
        'footer.security':
          'The password is stored only in Windows Credential Manager, and the OTP is used once per login; the background process stays resident, so restarting Harness does not require logging in again.',
        'error.clusterRequired': 'Select a cluster',
        'error.userRequired': 'Enter your student ID',
        'error.passwordRequired': 'Enter your password (none is saved)',
        'error.otpRequired': 'Enter the OTP code',
        'error.jobIdsRequired': 'Enter the job IDs to cancel',
        'error.unknown': 'Request failed; please try again',
      },
    }

    // Column layout for the narrow settings pane: fixed layout keeps a long
    // REASON from blowing the card out horizontally.
    const QUEUE_COLUMNS = [
      { key: 'jobid', width: '18%' },
      { key: 'name', width: '22%' },
      { key: 'state', width: '16%' },
      { key: 'time', width: '16%' },
      { key: 'reason', width: '28%' },
    ]

    const styles = {
      panel: {
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
        fontSize: 13,
        lineHeight: 1.6,
        color: 'var(--dsw-alias-label-primary)',
        maxWidth: '100%',
        boxSizing: 'border-box',
      },
      row: { display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 },
      statusRow: {
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: 8,
      },
      statusLabel: {
        fontSize: 12,
        fontWeight: 600,
        opacity: 0.7,
        color: 'var(--dsw-alias-label-secondary)',
      },
      dim: { opacity: 0.65, fontSize: 12, wordBreak: 'break-word' },
      cardTitle: { fontSize: 13, fontWeight: 600, flex: 1 },
      card: {
        border: '1px solid var(--dsw-alias-border-l2)',
        borderRadius: 8,
        padding: '10px 12px',
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
        minWidth: 0,
      },
      form: { display: 'flex', flexDirection: 'column', gap: 8, minWidth: 0 },
      field: { display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 },
      fieldLabel: { fontSize: 12, color: 'var(--dsw-alias-label-secondary)' },
      input: {
        border: '1px solid var(--dsw-alias-border-l2)',
        borderRadius: 6,
        background: 'transparent',
        color: 'var(--dsw-alias-label-primary)',
        padding: '4px 8px',
        font: 'inherit',
        width: '100%',
        boxSizing: 'border-box',
        minHeight: 30,
      },
      select: {
        border: '1px solid var(--dsw-alias-border-l2)',
        borderRadius: 6,
        background: 'transparent',
        color: 'var(--dsw-alias-label-primary)',
        padding: '4px 8px',
        font: 'inherit',
        width: '100%',
        boxSizing: 'border-box',
        minHeight: 30,
      },
      // 原生 <option> 弹层不继承页面主题，Windows 下默认白底白字；逐项给主题色。
      option: {
        background: 'var(--dsw-alias-bg-overlay)',
        color: 'var(--dsw-alias-label-primary)',
      },
      checkRow: { display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' },
      checkbox: {
        width: 16,
        height: 16,
        accentColor: 'var(--dsw-alias-label-primary)',
        cursor: 'pointer',
        flex: '0 0 auto',
      },
      checkLabel: {
        fontSize: 12,
        color: 'var(--dsw-alias-label-secondary)',
        lineHeight: 1.4,
      },
      button: {
        minHeight: 32,
        padding: '0 13px',
        border: '1px solid var(--dsw-alias-border-l2)',
        borderRadius: 16,
        background: 'transparent',
        color: 'var(--dsw-alias-label-primary)',
        cursor: 'pointer',
        font: 'inherit',
        flex: '0 1 auto',
      },
      buttonGrow: { flex: '1 1 0', minWidth: 0 },
      buttonPrimary: { fontWeight: 600 },
      buttonDisabled: { opacity: 0.5, cursor: 'default' },
      buttonRow: { display: 'flex', gap: 8, minWidth: 0 },
      badgeBase: {
        display: 'inline-block',
        padding: '2px 10px',
        borderRadius: 999,
        fontSize: 12,
        border: '1px solid var(--dsw-alias-border-l2)',
        whiteSpace: 'nowrap',
      },
      badgeOn: { color: 'var(--dsw-state-success-primary)' },
      badgeWarn: { color: 'var(--dsw-state-warn-label)' },
      badgeOff: { color: 'var(--dsw-alias-label-tertiary)' },
      badgeErr: { color: 'var(--dsw-state-error-primary)' },
      error: {
        color: 'var(--dsw-state-error-primary)',
        fontSize: 12,
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-all',
        minWidth: 0,
      },
      hint: {
        fontSize: 11,
        opacity: 0.65,
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
      },
      footer: { display: 'flex', flexDirection: 'column', gap: 2, marginTop: 2, minWidth: 0 },
      tableWrap: { width: '100%', overflowX: 'auto', minWidth: 0 },
      table: { width: '100%', borderCollapse: 'collapse', fontSize: 12, tableLayout: 'fixed' },
      th: {
        textAlign: 'left',
        padding: '3px 4px',
        borderBottom: '1px solid var(--dsw-alias-border-l2)',
        color: 'var(--dsw-alias-label-secondary)',
        fontWeight: 600,
        whiteSpace: 'nowrap',
      },
      td: {
        padding: '3px 4px',
        borderBottom: '1px solid var(--dsw-alias-border-l2)',
        verticalAlign: 'top',
        wordBreak: 'break-word',
        overflowWrap: 'anywhere',
      },
      pre: {
        margin: 0,
        padding: 0,
        fontSize: 11,
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
        maxHeight: 240,
        overflowY: 'auto',
        fontFamily: 'monospace',
        opacity: 0.85,
      },
    }

    function interpolate(template, params) {
      if (params === undefined || params === null) return template
      return String(template).replace(/\{(\w+)\}/g, (match, name) =>
        name in params ? String(params[name]) : match,
      )
    }

    function asText(value) {
      if (value === undefined || value === null) return ''
      if (typeof value === 'string') return value.trim()
      if (typeof value.message === 'string') return value.message.trim()
      return String(value).trim()
    }

    function nonEmpty(value) {
      return value !== undefined && value !== null && String(value) !== ''
    }

    function clusterList(state) {
      if (state === null || typeof state !== 'object') return []
      const list = state.clusters
      if (!Array.isArray(list)) return []
      const result = []
      for (const item of list) {
        if (item === null || typeof item !== 'object' || !nonEmpty(item.key)) continue
        result.push({
          key: String(item.key),
          name: nonEmpty(item.name) ? String(item.name) : String(item.key),
          host: nonEmpty(item.host) ? String(item.host) : '',
        })
      }
      return result
    }

    // The `session` object is documented with `host` only; host plugins may
    // additionally expose the cluster key/display name. Try each candidate so
    // the badge can show a cluster name whenever any of them is available.
    function resolveClusterName(state, session, fallbackKey) {
      const clusters = clusterList(state)
      const nameByKey = (key) => {
        const found = clusters.find((item) => item.key === String(key))
        return found !== undefined ? found.name : ''
      }

      if (session !== null && typeof session === 'object') {
        const directName = nonEmpty(session.clusterName) ? session.clusterName : session.name
        if (nonEmpty(directName)) return String(directName)

        const key = nonEmpty(session.cluster) ? session.cluster : session.clusterKey
        if (nonEmpty(key)) {
          const name = nameByKey(key)
          return name !== '' ? name : String(key)
        }

        if (nonEmpty(session.host)) {
          const host = String(session.host)
          const byHost = clusters.find((item) => item.host !== '' && item.host === host)
          if (byHost !== undefined) return byHost.name
          return host
        }
      }

      if (nonEmpty(fallbackKey)) {
        const name = nameByKey(fallbackKey)
        return name !== '' ? name : String(fallbackKey)
      }
      return '—'
    }

    function formatDuration(since, t) {
      if (!nonEmpty(since)) return '—'
      const started = Date.parse(String(since))
      if (!Number.isFinite(started)) return '—'
      let seconds = Math.floor((Date.now() - started) / 1000)
      if (!Number.isFinite(seconds) || seconds < 0) seconds = 0
      if (seconds < 60) return t('duration.seconds', { n: seconds })
      const minutes = Math.floor(seconds / 60)
      if (minutes < 60) return t('duration.minutes', { n: minutes })
      const hours = Math.floor(minutes / 60)
      const restMinutes = minutes % 60
      if (hours < 24) {
        return restMinutes > 0
          ? t('duration.hours', { h: hours, m: restMinutes })
          : t('duration.hour', { h: hours })
      }
      const days = Math.floor(hours / 24)
      const restHours = hours % 24
      return restHours > 0
        ? t('duration.days', { d: days, h: restHours })
        : t('duration.day', { d: days })
    }

    /** Prefer the Host's JSON error text over a bare "HTTP 403". */
    function responseError(response) {
      return response
        .json()
        .then((body) => {
          if (body !== null && typeof body === 'object' && typeof body.error === 'string' && body.error !== '') {
            const detail = body.detail !== undefined && body.detail !== null
              ? '（' + Object.keys(body.detail)
                  .map((key) => key + '=' + String(body.detail[key]))
                  .join(', ') + '）'
              : ''
            return body.error + detail
          }
          return `HTTP ${response.status}`
        })
        .catch(() => `HTTP ${response.status}`)
    }

    function fetchState() {
      return fetch(STATE_ROUTE, {
        cache: 'no-store',
        headers: { accept: 'application/json' },
      })
        .then((response) => {
          if (!response.ok) return responseError(response).then((message) => { throw new Error(message) })
          return response.json()
        })
        .then((body) => {
          if (body === null || typeof body !== 'object') throw new Error('')
          if (body.ok === false) throw new Error(typeof body.error === 'string' ? body.error : '')
          return body
        })
    }

    function postJson(route, payload) {
      return fetch(route, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(payload === undefined || payload === null ? {} : payload),
        cache: 'no-store',
      }).then((response) => {
        if (!response.ok) return responseError(response).then((message) => { throw new Error(message) })
        return response.json()
      })
    }

    function field(label, control) {
      return h(
        'label',
        { style: styles.field },
        h('span', { style: styles.fieldLabel }, label),
        control,
      )
    }

    function queueCellText(value) {
      if (value === undefined || value === null) return ''
      return typeof value === 'string' ? value : String(value)
    }

    function createSection(t) {
      function Section() {
        const [state, setState] = React.useState(null)
        const [stateError, setStateError] = React.useState('')
        const [form, setForm] = React.useState({
          cluster: '',
          user: '',
          password: '',
          otp: '',
          remember: true,
        })
        const [formError, setFormError] = React.useState('')
        const [busy, setBusy] = React.useState('')
        const [queue, setQueue] = React.useState(null)
        const [queueError, setQueueError] = React.useState('')
        const [queueBusy, setQueueBusy] = React.useState(false)
        const [cancelIds, setCancelIds] = React.useState('')
        const [cancelError, setCancelError] = React.useState('')
        const [cancelBusy, setCancelBusy] = React.useState(false)

        const mounted = React.useRef(true)
        const defaultsApplied = React.useRef(false)
        const stateBusy = React.useRef(false)
        // A state poll that started before a login/logout POST must not write
        // its stale snapshot over the fresh one the mutation returned.
        const authAt = React.useRef(0)

        const session = state !== null && state.session !== null && typeof state.session === 'object'
          ? state.session
          : null
        const connected = session !== null && session.connected === true
        const clusters = clusterList(state)
        const daemon = state !== null && state.daemon !== null && typeof state.daemon === 'object'
          ? state.daemon
          : null
        const daemonOff = daemon !== null && daemon.running === false
        // Host surfaces a daemon start failure here (may be null).
        const daemonError = state !== null && typeof state.error === 'string' ? state.error.trim() : ''
        const passwordSaved = state !== null && state.passwordSaved === true
        const defaults = state !== null && state.defaults !== null && typeof state.defaults === 'object'
          ? state.defaults
          : null
        const remoteDir = defaults !== null && nonEmpty(defaults.remoteDir)
          ? String(defaults.remoteDir)
          : '—'
        // A select always shows its first option, so keep the effective value
        // in sync with the visible selection even before defaults are applied.
        const selectedCluster = form.cluster !== ''
          ? form.cluster
          : clusters.length > 0
            ? clusters[0].key
            : ''

        React.useEffect(() => {
          mounted.current = true
          return () => {
            mounted.current = false
          }
        }, [])

        function refreshState() {
          if (stateBusy.current) return
          stateBusy.current = true
          const requestedAt = Date.now()
          fetchState()
            .then((body) => {
              if (!mounted.current) return
              if (authAt.current >= requestedAt) return
              setState(body)
              setStateError('')
            })
            .catch((err) => {
              if (!mounted.current) return
              const message = asText(err)
              setStateError(message !== '' ? message : t('error.unknown'))
            })
            .finally(() => {
              stateBusy.current = false
            })
        }

        React.useEffect(() => {
          refreshState()
          const timer = setInterval(refreshState, STATE_POLL_MS)
          return () => clearInterval(timer)
        }, [])

        React.useEffect(() => {
          if (state === null || defaultsApplied.current) return
          defaultsApplied.current = true
          const fallbackDefaults = state.defaults !== null && typeof state.defaults === 'object'
            ? state.defaults
            : {}
          const keys = clusters.map((item) => item.key)
          let cluster = nonEmpty(fallbackDefaults.cluster) ? String(fallbackDefaults.cluster) : ''
          if (cluster !== '' && keys.length > 0 && keys.indexOf(cluster) === -1) cluster = keys[0]
          if (cluster === '' && keys.length > 0) cluster = keys[0]
          // 学号优先级：插件默认值 →（没有默认值时）上次登录留下的学号 → 空。
          // 插件不再内置默认学号，靠 lastUser 回填，用户不必每次重填。
          const session = state.session !== null && typeof state.session === 'object'
            ? state.session
            : {}
          const savedUser = nonEmpty(fallbackDefaults.user)
            ? String(fallbackDefaults.user)
            : (nonEmpty(session.lastUser) ? String(session.lastUser) : '')
          const user = savedUser
          setForm((prev) => ({
            ...prev,
            cluster: prev.cluster !== '' ? prev.cluster : cluster,
            user: prev.user !== '' ? prev.user : user,
          }))
        }, [state])

        function loadQueue() {
          if (!mounted.current) return
          setQueueBusy(true)
          postJson(ACTION_ROUTE, { action: 'queue' })
            .then((body) => {
              if (!mounted.current) return
              if (body === null || typeof body !== 'object' || body.ok !== true) {
                throw new Error((body && body.error) || t('error.unknown'))
              }
              const data = body.data !== null && typeof body.data === 'object' ? body.data : null
              const rows = Array.isArray(body.rows)
                ? body.rows
                : data !== null && Array.isArray(data.rows)
                  ? data.rows
                  : null
              const text = typeof body.text === 'string'
                ? body.text
                : data !== null && typeof data.text === 'string'
                  ? data.text
                  : ''
              setQueue({ rows, text })
              setQueueError('')
            })
            .catch((err) => {
              if (!mounted.current) return
              const message = asText(err)
              setQueueError(message !== '' ? message : t('error.unknown'))
            })
            .finally(() => {
              if (mounted.current) setQueueBusy(false)
            })
        }

        React.useEffect(() => {
          if (!connected) {
            setQueue(null)
            setQueueError('')
            return undefined
          }
          loadQueue()
          const timer = setInterval(loadQueue, QUEUE_POLL_MS)
          return () => clearInterval(timer)
        }, [connected])

        function submitLogin(event) {
          if (event !== undefined && event !== null && typeof event.preventDefault === 'function') {
            event.preventDefault()
          }
          if (busy !== '') return
          const cluster = selectedCluster.trim()
          const user = form.user.trim()
          const password = typeof form.password === 'string' ? form.password : ''
          const otp = String(form.otp).trim()
          let problem = ''
          if (cluster === '') problem = t('error.clusterRequired')
          else if (user === '') problem = t('error.userRequired')
          else if (password === '' && !passwordSaved) problem = t('error.passwordRequired')
          else if (otp === '') problem = t('error.otpRequired')
          if (problem !== '') {
            setFormError(problem)
            return
          }
          setFormError('')
          setBusy('login')
          const wasConnected = connected
          postJson(LOGIN_ROUTE, {
            cluster,
            user,
            password,
            otp,
            remember: form.remember === true,
          })
            .then((body) => {
              if (!mounted.current) return
              if (body === null || typeof body !== 'object' || body.ok !== true) {
                throw new Error((body && body.error) || t('error.unknown'))
              }
              if (body.state !== undefined && body.state !== null) {
                authAt.current = Date.now()
                setState(body.state)
              } else {
                refreshState()
              }
              setStateError('')
              setForm((prev) => ({ ...prev, password: '', otp: '' }))
              setQueue(null)
              setQueueError('')
              if (wasConnected) loadQueue()
            })
            .catch((err) => {
              if (!mounted.current) return
              const message = asText(err)
              setFormError(message !== '' ? message : t('error.unknown'))
            })
            .finally(() => {
              if (mounted.current) setBusy('')
            })
        }

        function submitLogout(event) {
          if (event !== undefined && event !== null && typeof event.preventDefault === 'function') {
            event.preventDefault()
          }
          if (busy !== '' || !connected) return
          setFormError('')
          setBusy('logout')
          postJson(LOGOUT_ROUTE, {})
            .then((body) => {
              if (!mounted.current) return
              if (body === null || typeof body !== 'object' || body.ok !== true) {
                throw new Error((body && body.error) || t('error.unknown'))
              }
              if (body.state !== undefined && body.state !== null) {
                authAt.current = Date.now()
                setState(body.state)
              } else {
                refreshState()
              }
              setStateError('')
              setForm((prev) => ({ ...prev, password: '', otp: '' }))
              setQueue(null)
              setQueueError('')
              setCancelIds('')
              setCancelError('')
            })
            .catch((err) => {
              if (!mounted.current) return
              const message = asText(err)
              setFormError(message !== '' ? message : t('error.unknown'))
            })
            .finally(() => {
              if (mounted.current) setBusy('')
            })
        }

        function submitCancel(event) {
          if (event !== undefined && event !== null && typeof event.preventDefault === 'function') {
            event.preventDefault()
          }
          if (cancelBusy) return
          const ids = String(cancelIds)
            .split(/\s+/)
            .map((item) => item.trim())
            .filter((item) => /^[0-9][0-9_.]*$/.test(item))
          if (ids.length === 0) {
            setCancelError(t('error.jobIdsRequired'))
            return
          }
          setCancelError('')
          setCancelBusy(true)
          postJson(ACTION_ROUTE, { action: 'cancel', job_ids: ids })
            .then((body) => {
              if (!mounted.current) return
              if (body === null || typeof body !== 'object' || body.ok !== true) {
                throw new Error((body && body.error) || t('error.unknown'))
              }
              setCancelIds('')
              loadQueue()
            })
            .catch((err) => {
              if (!mounted.current) return
              const message = asText(err)
              setCancelError(message !== '' ? message : t('error.unknown'))
            })
            .finally(() => {
              if (mounted.current) setCancelBusy(false)
            })
        }

        let badgeText = t('badge.loading')
        let badgeTone = styles.badgeOff
        if (stateError !== '') {
          badgeText = t('badge.connError')
          badgeTone = styles.badgeErr
        } else if (state !== null) {
          if (daemonOff) {
            badgeText = t('badge.daemonOff')
            badgeTone = styles.badgeWarn
          } else if (connected) {
            badgeText = t('badge.connected')
            badgeTone = styles.badgeOn
          } else {
            badgeText = t('badge.disconnected')
            badgeTone = styles.badgeOff
          }
        }

        const userLabel = connected && session !== null && nonEmpty(session.user)
          ? String(session.user)
          : form.user.trim() !== ''
            ? form.user.trim()
            : '—'

        const statusDetail = connected && session !== null
          ? h(
              'div',
              { style: styles.dim },
              t('detail.connected', {
                cluster: resolveClusterName(state, session, selectedCluster),
                user: userLabel,
                duration: formatDuration(session.since, t),
              }),
            )
          : null

        const clusterOptions = clusters.map((item) =>
          h('option', { key: item.key, value: item.key, style: styles.option }, item.name),
        )
        const clusterControl = clusters.length > 0
          ? h(
              'select',
              {
                style: styles.select,
                value: selectedCluster,
                onChange: (event) => {
                  setFormError('')
                  setForm((prev) => ({ ...prev, cluster: event.target.value }))
                },
              },
              clusterOptions,
            )
          : h('input', {
              type: 'text',
              style: styles.input,
              value: form.cluster,
              placeholder: t('placeholder.cluster'),
              onChange: (event) => {
                setFormError('')
                setForm((prev) => ({ ...prev, cluster: event.target.value }))
              },
            })

        const loginForm = h(
          'form',
          { style: styles.form, onSubmit: submitLogin },
          field(t('label.cluster'), clusterControl),
          field(
            t('label.user'),
            h('input', {
              type: 'text',
              style: styles.input,
              value: form.user,
              placeholder: t('placeholder.user'),
              autoComplete: 'username',
              onChange: (event) => {
                setFormError('')
                setForm((prev) => ({ ...prev, user: event.target.value }))
              },
            }),
          ),
          field(
            t('label.password'),
            h('input', {
              type: 'password',
              style: styles.input,
              value: form.password,
              placeholder: passwordSaved
                ? t('placeholder.passwordSaved')
                : t('placeholder.password'),
              autoComplete: 'current-password',
              onChange: (event) => {
                setFormError('')
                setForm((prev) => ({ ...prev, password: event.target.value }))
              },
            }),
          ),
          field(
            t('label.otp'),
            h('input', {
              type: 'text',
              style: styles.input,
              value: form.otp,
              placeholder: t('placeholder.otp'),
              inputMode: 'numeric',
              autoComplete: 'one-time-code',
              maxLength: 8,
              onChange: (event) => {
                setFormError('')
                setForm((prev) => ({ ...prev, otp: event.target.value }))
              },
            }),
          ),
          h(
            'label',
            { style: styles.checkRow },
            h('input', {
              type: 'checkbox',
              style: styles.checkbox,
              checked: form.remember === true,
              onChange: (event) =>
                setForm((prev) => ({ ...prev, remember: event.target.checked === true })),
            }),
            h('span', { style: styles.checkLabel }, t('label.remember')),
          ),
          formError !== ''
            ? h('div', { style: styles.error }, formError)
            : null,
          h(
            'div',
            { style: styles.buttonRow },
            h(
              'button',
              {
                type: 'submit',
                style: {
                  ...styles.button,
                  ...styles.buttonGrow,
                  ...styles.buttonPrimary,
                  ...(busy !== '' ? styles.buttonDisabled : null),
                },
                disabled: busy !== '',
              },
              busy === 'login' ? t('btn.loggingIn') : t('btn.login'),
            ),
            h(
              'button',
              {
                type: 'button',
                style: {
                  ...styles.button,
                  ...styles.buttonGrow,
                  ...(!connected || busy !== '' ? styles.buttonDisabled : null),
                },
                disabled: !connected || busy !== '',
                onClick: submitLogout,
              },
              busy === 'logout' ? t('btn.loggingOut') : t('btn.logout'),
            ),
          ),
        )

        let queueBody = null
        if (queueError !== '') {
          queueBody = h('div', { style: styles.error }, queueError)
        } else if (queue === null) {
          queueBody = h('div', { style: styles.dim }, t('queue.loading'))
        } else {
          const rows = Array.isArray(queue.rows) ? queue.rows : null
          const rawText = typeof queue.text === 'string' ? queue.text.trim() : ''
          if (rows === null || rows.length === 0) {
            // No parsed rows: fall back to the raw squeue text when we have it.
            if (rawText !== '') {
              queueBody = h('pre', { style: styles.pre }, rawText)
            } else {
              queueBody = h('div', { style: styles.dim }, t('queue.empty'))
            }
          } else {
            const shown = rows.slice(0, QUEUE_LIMIT)
            const truncated = rows.length > QUEUE_LIMIT
            queueBody = h(
              React.Fragment,
              null,
              h(
                'div',
                { style: styles.tableWrap },
                h(
                  'table',
                  { style: styles.table },
                  h(
                    'thead',
                    null,
                    h(
                      'tr',
                      null,
                      QUEUE_COLUMNS.map((column) =>
                        h(
                          'th',
                          {
                            key: column.key,
                            style: { ...styles.th, width: column.width },
                          },
                          t('col.' + column.key),
                        ),
                      ),
                    ),
                  ),
                  h(
                    'tbody',
                    null,
                    shown.map((row, index) => {
                      const item = row !== null && typeof row === 'object' ? row : {}
                      const jobid = queueCellText(item.jobid)
                      const name = queueCellText(item.name)
                      const jobState = queueCellText(item.state)
                      const time = queueCellText(item.time)
                      const reason = queueCellText(item.reason)
                      return h(
                        'tr',
                        { key: jobid + ':' + String(index) },
                        h('td', { style: styles.td, title: jobid }, jobid),
                        h('td', { style: styles.td, title: name }, name),
                        h('td', { style: styles.td, title: jobState }, jobState),
                        h('td', { style: styles.td, title: time }, time),
                        h('td', { style: styles.td, title: reason }, reason),
                      )
                    }),
                  ),
                ),
              ),
              h('div', { style: styles.dim }, t('queue.count', { n: rows.length })),
              truncated
                ? h('div', { style: styles.dim }, t('queue.truncated'))
                : null,
            )
          }
        }

        const queueCard = connected
          ? h(
              'div',
              { style: styles.card },
              h(
                'div',
                { style: styles.row },
                h('span', { style: styles.cardTitle }, t('heading.queue')),
                h(
                  'button',
                  {
                    type: 'button',
                    style: {
                      ...styles.button,
                      ...(queueBusy ? styles.buttonDisabled : null),
                    },
                    disabled: queueBusy,
                    onClick: loadQueue,
                  },
                  queueBusy ? t('btn.refreshing') : t('btn.refresh'),
                ),
              ),
              queueBody,
              h(
                'div',
                { style: styles.field },
                h('span', { style: styles.fieldLabel }, t('label.cancelIds')),
                h(
                  'div',
                  { style: styles.row },
                  h('input', {
                    type: 'text',
                    style: {
                      ...styles.input,
                      width: 'auto',
                      flex: '1 1 auto',
                      minWidth: 0,
                    },
                    value: cancelIds,
                    placeholder: t('placeholder.cancelIds'),
                    onChange: (event) => {
                      setCancelError('')
                      setCancelIds(event.target.value)
                    },
                    onKeyDown: (event) => {
                      if (event.key === 'Enter') {
                        event.preventDefault()
                        submitCancel()
                      }
                    },
                  }),
                  h(
                    'button',
                    {
                      type: 'button',
                      style: {
                        ...styles.button,
                        ...(cancelBusy || cancelIds.trim() === ''
                          ? styles.buttonDisabled
                          : null),
                      },
                      disabled: cancelBusy || cancelIds.trim() === '',
                      onClick: submitCancel,
                    },
                    cancelBusy ? t('btn.cancelling') : t('btn.cancel'),
                  ),
                ),
                cancelError !== ''
                  ? h('div', { style: styles.error }, cancelError)
                  : null,
              ),
            )
          : null

        return h(
          'div',
          { style: styles.panel },
          h(
            'div',
            { style: styles.statusRow },
            h('span', { style: styles.statusLabel }, t('status')),
            h('span', { style: { ...styles.badgeBase, ...badgeTone } }, badgeText),
          ),
          statusDetail,
          stateError !== '' ? h('div', { style: styles.error }, stateError) : null,
          stateError === '' && daemonError !== ''
            ? h('div', { style: styles.error }, daemonError)
            : null,
          h(
            'div',
            { style: styles.card },
            h('div', { style: styles.cardTitle }, t('heading.login')),
            loginForm,
          ),
          queueCard,
          h(
            'div',
            { style: styles.footer },
            h('div', { style: styles.hint }, t('footer.remoteDir', { dir: remoteDir })),
            h('div', { style: styles.hint }, t('footer.security')),
          ),
        )
      }

      return Section
    }

    function apply(ctx) {
      const slots = ctx.get('slots')
      if (slots === undefined) return
      const locale = ctx.get('locale')

      if (locale !== undefined) {
        try {
          ctx.effect(() => {
            const disposes = [
              locale.register(LOCALE_NS, 'zh', MESSAGES.zh),
              locale.register(LOCALE_NS, 'en', MESSAGES.en),
            ]
            return () => {
              for (const dispose of disposes) dispose()
            }
          })
        } catch (error) {
          console.warn('pku-hpc: locale registration failed', error)
        }
      }

      const t = locale !== undefined
        ? locale.bind(LOCALE_NS)
        : (key, params) => interpolate(MESSAGES.zh[key] ?? key, params)

      const Section = createSection(t)

      // Subscribe to locale changes once per Section mount so zh/en switches
      // re-render the whole page (labels are re-read through `t`).
      function LocaleAwareSection() {
        const [, bump] = React.useReducer((x) => x + 1, 0)
        React.useEffect(() => {
          if (locale === undefined) return undefined
          return locale.subscribe(() => bump())
        }, [])
        return h(Section)
      }

      slots.inject('settings.section', () => slots.register({
        name: 'settings.section',
        id: 'pku-hpc',
        order: 165,
        label: () => t('section'),
      }, LocaleAwareSection))
    }

    module.exports.apply = apply
    module.exports.inject = ['slots']
    return module.exports
  },
})
