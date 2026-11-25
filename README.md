# PKU 超算桥（@local/pku-hpc）

把北大高性能计算平台（教学二号 / 未名一号 / 未名二号 / 生科一号）接进 DeepSeek Harness：

- **登录一次**（密码 + OTP）之后，SSH 会话由一个**独立后台进程**保持，重启 Harness 也不用重新登录；
- 在对话里直接让 agent 提交 Gaussian 作业、查队列、取消、下载日志 / chk；
- 设置里有「PKU 超算」页面：登录表单、连接状态、队列表、按作业号取消；
- 密码只存在 **Windows 凭据管理器**（可选“记住密码”），OTP 用一次即弃；聊天记录里不出现密码。

## 当前状态

- 安装方式：本地链接安装（DSH profile 里加一条 `"@local/pku-hpc": "link:<本仓库路径>"`）。
- 插件行 id：`pku-hpc`；工具名：`pku_hpc`；设置页：**设置 → PKU 超算**。
- 后台进程数据目录：`%USERPROFILE%\.pku-hpc\`
  - `bridge.json`：守护进程端口 + bearer token（本机 127.0.0.1 随机端口）
  - `bridge.log`：守护进程日志
  - `last-target.json`：上次登录的 host / 学号（不含密码）

## 第一次使用

1. 打开 **设置 → PKU 超算**；
2. 集群选「教学二号」（默认），填你自己的**学号**（插件不预置任何学号）；
3. 第一次填密码；勾选「记住密码」后，以后只需填 **OTP 动态码**（手机令牌 6 位）；
4. 点「登录」。登录成功后，状态徽章变「已连接」，下方出现队列。

之后在对话里直接说，例如：

- “查一下超算队列”
- “把 `/path/to/A1.gjf` 提交到教学二号”
- “读取 ~/g16jobs 下的 log 文件列表”
- “把 A1.log 下载到那个 calculation 目录”
- “取消作业 12345”

agent 会调用 `pku_hpc` 工具完成；所有操作复用同一条 SSH 会话。

## 为什么不用每次打开高斯计算工具

原来的「高斯计算工具」是一个本地网页进程，登录会话存在它自己的内存里，关掉 / 重启就要重新输 OTP。
本插件的 SSH 会话放在**独立守护进程**里：

```
Harness（可重启） ──HTTP──▶ ~/.pku-hpc 下的 Python 守护进程（常驻）──SSH──▶ 北大 HPC
```

守护进程空闲 12 小时自动退出；有活跃 SSH 会话时不因空闲退出（24 小时硬上限）。
OTP 仍然只在**新建连接**时需要——平台规定密码 + OTP、不支持密钥，这一点无法绕过。

## agent 工具 `pku_hpc`

| action | 说明 | 关键参数 |
| --- | --- | --- |
| `status` | 守护进程 / SSH 状态 + 队列 | — |
| `login` | 工具内登录（不推荐，密码会进会话记录；优先用设置页） | `cluster`, `user`, `password`, `otp`, `remember_password` |
| `logout` | 断开 SSH（守护进程继续常驻） | — |
| `queue` | `squeue` 队列（多种写法兜底，Crane 也可） | — |
| `exec` | 登录节点执行命令 | `command` |
| `submit` | 上传 .gjf + 生成 `job_<name>.sh` + `sbatch`；`_td`/`_fc` 自动 `afterok` 依赖基态 | `files`（绝对路径或目录）, `remote_dir`, `partition`, `ntasks`, `walltime` … |
| `cancel` | `scancel` | `job_ids` |
| `files` | SFTP 列远程文件（带大小/修改时间） | `remote_dir`, `suffix`（如 `.log`） |
| `download` | 下载远程文件 | `remote_dir`, `names`, `local_dir` |

提交脚本默认值与用户现有 `g16sub.sh` 对齐（教学二号）：

```bash
#!/bin/bash
#SBATCH -o job.%j.out
#SBATCH -p compute
#SBATCH -J <name>
#SBATCH -N 1
#SBATCH --ntasks-per-node=32
#SBATCH --get-user-env
g16 < <name>.gjf > <name>.log
```

切到未名一号 / 未名二号 / 生科一号时，Host 会自动换成对应默认（`C032M0128G`、`--qos=low`、
`source g16.profile`、`g16 x.gjf > x.log`），也可以在 submit 参数里覆盖。

## 目录

```
_pku_hpc_plugin/
  index.js            Host 插件：守护进程生命周期、/pku-hpc/* 路由、pku_hpc 工具
  client.js           设置页（settings.section），fetch 上面的路由
  bridge/
    hpc_bridge.py     常驻守护进程：HTTP API + 生命周期 + 空闲退出
    hpc_core.py       SSH（密码 + Keyboard-Interactive OTP）、凭据管理器、Slurm、SFTP
  tests/              48 个 unittest（含真实 socket 的假 SSH 服务器，不需要真实账号）
  locale/  icon.svg   插件元数据
  CONTRACT.md         内部接口契约（改接口先改它）
```

## 维护

- 改了 `index.js` / `client.js` 后，插件是本地链接安装，但 Host 模块不会热替换：
  在插件管理里禁用再启用（`plugin_manager set_bundle`），或重启 Harness。
  设置页的前端模块会随客户端模块刷新加载。
- 改 `bridge/*.py` 后需要重启守护进程才生效：设置页点「断开」不会杀进程；
  调 `POST /shutdown`（带 token）或在任务管理器结束 `pythonw.exe`，下次操作会自动重启它。
- 跑测试：

```bat
cd /d <本仓库目录>
python -m unittest discover -s tests -v
node --check index.js && node --check client.js
```

## 已知限制 / 注意

- 平台不支持密钥登录，**OTP 无法保存**；Windows 重启 / 守护进程硬上限到期后要重新登录一次。
- 真实 HPC 的 `squeue` 在教学二号（Crane）上的输出格式可能解析不出表格，此时 UI 回退显示原始文本。
- `.chk` 可以直接下载，但没有做远程 `formchk`；需要 `.fchk` 时用 `exec` 手动跑 `formchk`。
- 守护进程只监听 `127.0.0.1`，用随机端口 + bearer token 鉴权；token 在 `%USERPROFILE%\.pku-hpc\bridge.json`。
