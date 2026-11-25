# PKU HPC 插件内部契约（唯一事实来源）

本文件是三个人（Lead / 守护进程 / 客户端 UI）之间**冻结的接口契约**。
实现前先读它；任何接口改动必须先在这里改，并通知另外两方。

## 总体结构

```
Harness 浏览器 (Settings → PKU 超算)
   │  fetch 同源 HTTP
   ▼
Host 插件 index.js  ──spawn/HTTP(bearer token)──▶  Python 守护进程 hpc_bridge.py
   │  ctx.tools.register('pku_hpc')                 │ paramiko（密码 + OTP）
   ▼                                                ▼
Agent 工具                                 北大 HPC 登录节点（Slurm）
```

- Python 守护进程**独立于 Harness 进程**：Harness 重启、插件重装都不影响已登录的 SSH 会话。
- 密码永远不落盘、不进聊天记录；只有「记住密码」时写入 **Windows 凭据管理器**
  （Credential Manager，generic credential，target 见下）。
- OTP 是每次新 SSH 连接一次性的；会话活着就不再要 OTP。
- 所有面向用户的中文文本用 UTF-8。Node 侧用 ESM。

## 目录 / 文件归属（不要改别人的文件）

| 文件 | 负责人 |
| --- | --- |
| `bridge/hpc_bridge.py`, `bridge/hpc_core.py`（如需） | 守护进程（teammate） |
| `tests/**`（Python 测试、假 SSH 服务器） | 守护进程（teammate） |
| `client.js` | 客户端 UI（teammate） |
| `index.js`, `package.json`, `cordis.patch.yml`, `locale/*.json`, `icon.svg`, `README.md` | Lead |
| `CONTRACT.md` | Lead；改动需通知 |

## 默认配置（写死为默认值，可被 plugin config 覆盖）

```json
{
  "cluster": "jx2",
  "user": "",
  "remoteDir": "~/g16jobs",
  "partition": "compute",
  "qos": "",
  "ntasks": 32,
  "nodes": 1,
  "walltime": "2-00:00:00",
  "sbatchOut": "job.%j.out",
  "includeEnv": false,
  "redirect": true,
  "g16root": "$HOME",
  "scrDir": "$HOME/tmp",
  "idleExitMinutes": 720
}
```

集群表（key, 显示名, 登录节点）：

| key | name | host |
| --- | --- | --- |
| jx2 | 教学二号 | wmjx2-login.pku.edu.cn |
| wm1 | 未名一号 | wm1-login.pku.edu.cn |
| wm2 | 未名二号 | wm2-login.pku.edu.cn |
| sk1 | 生科一号 | wmsk1-login.pku.edu.cn |

默认（jx2）提交脚本必须与用户现有 `g16sub.sh` 对齐：

```bash
#!/bin/bash
#SBATCH -o job.%j.out
#SBATCH -p compute
#SBATCH -J <jobname>
#SBATCH -N 1
#SBATCH --ntasks-per-node=32
#SBATCH --get-user-env
g16 < <name>.gjf > <name>.log
```

（wm1/wm2：`#SBATCH --qos=low`、`#SBATCH -p C032M0128G`、`--ntasks`（wm2）、
脚本内 `export g16root=...; export GAUSS_SCRDIR=...; source $g16root/g16/bsd/g16.profile`、
`cd $SLURM_SUBMIT_DIR`、`g16 x.gjf > x.log`。）

## 数据目录与身份文件

- 数据目录：`%USERPROFILE%\.pku-hpc`（Windows），由 Harness 侧传入。
- 身份文件 `<data-dir>/bridge.json`（守护进程启动成功后原子写入，UTF-8）：
  ```json
  { "pid": 12345, "port": 51234, "token": "<64 hex>", "startedAt": "2026-10-23T16:30:00+08:00", "version": "1.0.0" }
  ```
- 日志：`<data-dir>/bridge.log`（追加；>1 MB 时截断重写）。
- 凭据管理器 target：`pku-hpc:<host>:<user>`，CRED_TYPE_GENERIC，user 字段写 `<user>`。

## 守护进程 HTTP API（127.0.0.1，bearer token）

认证：每个请求带 `Authorization: Bearer <token>`，不匹配返回 HTTP 401 `{"ok":false,"error":"unauthorized"}`。
除 401/404 外：**一律 HTTP 200**，成功 `{"ok":true,...}`，业务失败 `{"ok":false,"error":"中文原因"}`。
请求体 JSON、UTF-8；响应 `Content-Type: application/json; charset=utf-8`。

| 方法 路径 | 请求 | 成功响应 |
| --- | --- | --- |
| GET `/health` | — | `{ok,pid,version,startedAt,session:{connected,host,port,user,since,lastActivity},passwordSaved:bool}` |
| GET `/state` | — | 同 `/health` 的 session/passwordSaved 字段（不含 pid 等可选） |
| POST `/login` | `{host,port,user,password,otp,remember}`；`password` 可为空 = 用已保存密码 | `{ok,session,passwordSaved}` |
| POST `/logout` | — | `{ok,session}` |
| POST `/exec` | `{cmd,timeout?}` | `{ok,output,code}` |
| GET `/queue` | — | `{ok,text,rows:[{jobid,name,state,time,reason}]}` |
| POST `/submit` | `{remote_dir,files:[本地绝对路径],options:{cluster,partition,qos,ntasks,nodes,walltime,sbatch_out,include_env,redirect,g16root,scr_dir},dependency?:true}` | `{ok,remote_dir,jobs:[{name,jobid,ok,output}]}` |
| POST `/cancel` | `{ids:["123",...]}` | `{ok,output,ids}` |
| GET `/files?dir=<remote>&suffix=<.log>&depth=2` | — | `{ok,root,exists,files:[{name,size,mtime}]}` |
| POST `/download` | `{remote_dir,names:[...],local_dir}` | `{ok,files:[本地绝对路径]}` |
| POST `/shutdown` | — | `{ok}`（随后进程退出） |

`session` 对象：`{connected,host,port,user,since,lastActivity,passwordSaved}`；
未登录时 `connected:false` 且其余字段可为 `null`/`""`。

### POST /login 细节

1. 若 `password` 为空：从凭据管理器读 `pku-hpc:<host>:<user>`；读不到 → `{ok:false,error:"没有已保存的密码，请先输入密码"}`。
2. paramiko SSHClient.connect 先试 password；失败后走 `Transport.auth_interactive` 的 Keyboard Interactive
   处理器：prompt 含 password/密码 → 密码；含 verification/otp/code/令牌 → OTP；其它 → OTP 优先。
3. 成功后 `transport.set_keepalive(30)`，保存一条 `HpcClient` 到进程内单例（`since`=本地时间 ISO，
   `lastActivity` 每次操作更新）。
4. `remember:true` → 用密码管理器写入（best-effort，失败不影响登录，但返回 `passwordSaved:false`）。
5. 失败 → `{ok:false,error:"SSH 登录失败：..."}`，保留中文可读信息。

### POST /submit 细节

- `remote_dir` 支持 `~/xxx`（用 `echo $HOME` 展开）；不存在则 `mkdir -p`。
- 对每个本地 `.gjf`（文件名 stem 作为作业名）：
  1. 生成并上传 `<name>.gjf`、`job_<name>.sh`（UTF-8，LF）。
  2. `cd <remote_dir> && sbatch [--dependency=afterok:<dep>] job_<name>.sh`，从输出里解析作业号（3 位以上数字）。
  3. 作业名 `_td` 依赖同批 `<base>`；`_fc` 依赖 `<base>_gs`；先提交无依赖的，再提交依赖的。
- 返回每个作业 `{name,jobid,ok,output}`。
- 文件名不安全字符不用处理（用户输入），但禁止路径分隔符；全部文件必须在本地存在，否则 `{ok:false,error}`。

### GET /queue

- 依次尝试（与现有工具一致）：
  1. `squeue -u <user>`
  2. `squeue --user=<user>`
  3. `squeue -u <user> -o "%A %j %T %M %R"`
  4. `squeue`
  5. `scontrol show jobs 2>/dev/null | head -60`
- 取第一个「stdout 非空且 stderr 为空」的结果。全失败 → `{ok:false,error:"查询失败：..."}`。
- `rows` 尽力解析（表头列名不固定：先找含 `JOBID` 的表头行，再按列名取 JOBID/NAME/ST/STATE/TIME/REASON；
  解析不出来就 `rows:[]`，`text` 原样返回）。教学二号是 Crane，`squeue` 输出格式可能与 Slurm 不同，容错优先。

### GET /files / POST /download

- 用 SFTP `listdir_attr` 递归（`depth` 默认 2），跳过 `.` 开头的项；`name` 是相对路径；
  `mtime` 格式 `YYYY-MM-DD HH:MM`；按 mtime 倒序。
- `/download` 从远程把列出的相对路径下载到 `local_dir`（本地 mkdir -p），返回本地绝对路径。

## Host 插件 HTTP API（浏览器同源，给 UI 用）

路径前缀 `/pku-hpc`。只允许 loopback 来源；POST 必须同源（`Origin` 匹配 Host，或
`sec-fetch-site: same-origin` 且 Host 是 loopback）。

| 方法 路径 | 请求 | 成功响应 |
| --- | --- | --- |
| GET `/pku-hpc/state` | — | `{ok:true,daemon:{running,pid,version,startedAt},session,passwordSaved,clusters:[{key,name}],defaults:{...}}` |
| POST `/pku-hpc/login` | `{cluster,user,password,otp,remember}` | `{ok:true,state:{...同上}}` / `{ok:false,error}` |
| POST `/pku-hpc/logout` | — | `{ok:true,state}` |
| POST `/pku-hpc/action` | `{action:"queue"|"cancel"|"files"|"download"|"exec"|"submit",...}` | `{ok:true,...}` / `{ok:false,error}` |

- Host 侧 `clusters`/`defaults` 来自插件 config；UI 用它填表单。
- `/pku-hpc/state` 在守护进程未启动时返回 `daemon.running:false, session.connected:false`（不报错），
  UI 显示「后台进程未启动」。
- 所有失败都返回 HTTP 200 + `{ok:false,error:"中文原因"}`（401/403 除外），UI 直接显示 `error`。

## agent 工具 `pku_hpc`

单工具 + `action` 参数。`action ∈ status|login|logout|queue|exec|submit|cancel|files|download`。
参数（都可选，按 action 用）：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `action` | string | 必填 |
| `cluster` | string enum | `jx2/wm1/wm2/sk1`，默认 config |
| `user` | string | 学号，默认 config |
| `password` | string | 仅 login；建议用户走 UI 登录，工具里留空则用已保存密码 |
| `otp` | string | 仅 login，6 位动态码 |
| `remember_password` | boolean | 仅 login；默认 true（写 Windows 凭据管理器） |
| `command` | string | exec |
| `files` | string[] | submit：本地 .gjf 绝对路径 |
| `remote_dir` | string | submit/files/download |
| `job_ids` | string[] | cancel |
| `suffix` | string | files：如 `.log` |
| `names` | string[] | download：远程相对文件名 |
| `local_dir` | string | download：本地目录 |
| `partition` / `qos` / `ntasks` / `nodes` / `walltime` | | submit 覆盖默认 |
| `include_env` / `redirect` | boolean | submit 覆盖默认 |

返回 `{ok:boolean, action:string, text:string, data?:object}`；`text` 是给人和模型看的摘要。
