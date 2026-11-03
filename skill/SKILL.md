---
name: waitforme
description: 安装、配置并验证 waitforme —— AI 卡住或需要你拍板时，把一条带可点击跳转链接的消息推到你手机（微信/企业微信/飞书）。当用户说「安装 waitforme skill」「装/配置 waitforme」「让 Kimi 卡住时微信通知我」「把 hook 接到 Kimi Code」「waitforme 收不到消息 / 不推送 / 怎么配通道」时使用。包含安装器、通道配置向导、Kimi Code hooks 接入片段、以及 289 项离线自检。
type: prompt
whenToUse: 用户要安装、配置、验证或排错 waitforme；或想把 Kimi Code 的 hook 事件接到本地推送服务时
---

# waitforme：AI 卡住时，一条能点开的微信

waitforme 的核心能力：Kimi Code 的 hook 事件进来，按「事件类型 / 静默时段 / 限流」三道门
判断该不该打扰你，该推就推一条消息（微信/企业微信/飞书），消息里带 **可点击的跳转链接**，点开是手机上的 Kimi
Code 远程控制页。

**两种接法，默认零常驻：**
- **直发（默认，推荐）**：事件来了起一个短命进程 → 直接调 `waitforme.py send` → 立刻退出。
  不占端口、不用开机自启、不需要服务一直跑。
- **常驻服务（可选）**：`hooks.py serve` 一直在跑，桥脚本 POST 到 `127.0.0.1:8790`。
  适合想集中看 `/health`、或一个服务接多个来源的场景。

本 skill 负责把它装好、配好、验证好。**核心代码（`waitforme.py` / `hooks.py` /
`channels.py` / `linker.py` / `policy.py`）不要改**，改了就偏离了上游，且必须重跑 289 项自检。

## AI 执行清单（照着做，别跳步）

1. `install.py --check` 看清现状（项目在哪、config 在不在、什么通道）。
2. 项目找不到 → 问用户要路径，或 `--clone` 克隆一份。
3. `install.py --non-interactive --channel mock` 先离线装通（不联网、不打扰人）。
4. **直发模式不用起服务**：直接测 `echo '{"hook_event_name":"Notification"}' | python scripts/hook_direct.py --debug`。
   只有用户明确要常驻时，才 `hooks.py serve --detach` + `verify.py --service`。
5. 问用户要真实通道和 key → `configure.py` 写入 → `verify.py --live` 真发一条。
6. `connect_kimi.py --mode direct` 打印 hooks 片段/替换 patch → **给用户看，用户自己决定写不写**。
7. 用户写完后开**新会话**，hooks 才生效。

每一步的 stdout 最后一行都是 JSON；过程提示在 stderr。退出码：`0` 成功 / `2` 业务失败 / `1` 异常。

## 0. 先看清现状

```bash
python "${KIMI_SKILL_DIR}/scripts/install.py" --check
```

输出示例（只读，不写任何文件）：

```
[install] Python 3.14.3（C:\Python314\python.exe）✓
[install] 项目目录：D:\anyi\waitforme（auto）
{"status":"ok","mode":"check","project":"D:\\anyi\\waitforme","config_exists":true,"channel":"pushplus",...}
```

- `project_not_found`：项目没找到。三选一 ——
  1. `--project <waitforme 目录>`
  2. `--clone`（默认克隆到 `~/waitforme`）
  3. 让用户手动 `git clone https://github.com/anyi-suchuyin/waitforme.git`
- 如果这个 skill 就在 waitforme 仓库里的 `skill/` 目录下，会自动认出 `<repo>`，不用给 `--project`。

**找项目的顺序**（`_common.resolve_project`）：`--project` → `WAITFORME_HOME` →
skill 目录里的 `.waitforme_home` → skill 的上级目录（仓库内布局）→ 当前目录 →
`~`、`~/projects`、`~/code`、`~/Desktop` 下的 `waitforme`。

## 1. 安装前检查

| 检查项 | 要求 | 怎么查 |
|---|---|---|
| Python | ≥ 3.10（本机 3.14.3 验证过） | `install.py --check` 第一行 |
| 项目完整 | `waitforme.py` `hooks.py` `channels.py` `linker.py` `policy.py` `selftest.py` `config.example.json` | 缺一个就报 `project_incomplete` 并列出缺谁 |
| 端口 | 默认 8790 没被别的程序占 | 冲突就改 `config.json` 的 `hook_port` |
| 网络 | mock 通道不需要；真实通道要能出网 | 只有 `--live` 才会真发 |
| git | 只在 `--clone` 时需要 | `git --version` |

## 2. 安装（先 mock 跑通，再换真通道）

```bash
# ① 生成 config.json（随机 hook_token）+ 选通道；mock = 不联网
python "${KIMI_SKILL_DIR}/scripts/install.py" --non-interactive --channel mock

# ② 起常驻服务（后台，pid 写 run/hooks.pid）
python "<项目目录>/hooks.py" serve --detach

# ③ 验证：status + mock 消息 + 289 项自检 + 服务起停
python "${KIMI_SKILL_DIR}/scripts/verify.py" --service
```

`<项目目录>` 就是上一步 JSON 里的 `project`。Windows 上路径有空格就加引号。

安装器参数：

| 参数 | 作用 |
|---|---|
| `--project PATH` | 指定项目目录（默认自动找） |
| `--config PATH` | 指定 config.json（默认 `<project>/config.json`，也认 `WAITFORME_CONFIG`） |
| `--channel mock\|wecom_bot\|feishu\|pushplus` | 通道；非交互默认 mock |
| `--key KEY` | 通道 key（mock 不需要） |
| `--force` | 已有 config 也覆盖（**会换 hook_token**） |
| `--non-interactive` | 一句话都不问；已有 config 时默认保留，除非加 `--force` |
| `--check` | 只体检不写文件 |
| `--clone` / `--clone-url` / `--clone-dir` | 找不到项目时克隆 |

**已有 config.json 时**：交互模式会问「覆盖吗」；非交互模式默认保留，必须显式 `--force`。
覆盖会换 hook_token —— 本 skill 的桥脚本是运行时从 config 读 token 的，所以 Kimi 的
`config.toml` 不用跟着改。

## 3. 配置真实通道（3 个问题）

```bash
python "${KIMI_SKILL_DIR}/scripts/configure.py"
```

三个问题：① 通道 ② key ③ 现在要不要接 hook（只打印片段，不改配置）。
对着终端输入 key 时不会回显（走 getpass）。

| 通道 | key 去哪拿 | 落到 config 的哪 |
|---|---|---|
| `wecom_bot` | 企业微信群 → 群机器人 → 添加，Webhook 里 `key=` 后面那串 | `channels.wecom_bot.key` |
| `feishu` | 飞书群 → 设置 → 群机器人 → 添加自定义机器人 → 复制 Webhook URL；开了签名校验再填 `channels.feishu.secret` | `channels.feishu.webhook` |
| `pushplus` | www.pushplus.plus 微信扫码 → 用户中心 → 用户令牌 | `channels.pushplus.token` |
| `mock` | 不需要 | — |

非交互写法（key 别写进脚本文件，更别提交）：

```bash
python "${KIMI_SKILL_DIR}/scripts/configure.py" --channel pushplus --key "<用户的 TOKEN>" --non-interactive
```

**服务每次请求都重读配置**，改通道/key 不用重启。改完用 `--live` 真发一条：

```bash
python "${KIMI_SKILL_DIR}/scripts/verify.py" --live
```

## 4. 接 Kimi Code hooks（必须用户确认后再改）

原理（别搞错）：Kimi Code 的 hook 是「**跑一条命令，事件 JSON 从 stdin 进来**」，
不是「POST 一个 URL」。所以中间要一个桥脚本。

### 4a. 零常驻直发（默认，推荐）

`hook_direct.py`：读 stdin → **在进程内过 `policy.gate`** → 通过后 `channels.send` → 退出。
**完全不碰 8790**，不需要常驻服务。三道门原样生效：

`事件类型 → 内容去重(dedup_window_s) → 静默时段 → 同类间隔(min_interval_s) → 10 分钟上限 → 当日上限`

放行时才预占额度；发送失败会退还（`settle_push`）。6 条硬约束：

| # | 约束 | 实现 |
|---|---|---|
| 1 | 事件名取 `hook_event_name`，容错 `event`/`type`，大小写与 `-`/`_` 归一 | `normalize_event()` |
| 2 | 不碰 8790 服务 | 进程内 `policy.gate` + `channels.send` |
| 3 | **恒 exit 0**（Stop 是可阻断事件） | 顶层 `BaseException` 兜底 |
| 4 | **stdout 必须为空**，调试只走 `--debug` + stderr | 不向 stdout 写任何内容 |
| 5 | 自控超时（hook timeout=10 → 网络 8s），超时也 exit 0 | `--timeout` / `WAITFORME_HOOK_TIMEOUT` |
| 6 | PermissionRequest / Notification → 过闸后推；Stop 及其它 → 只写 `logs/hook_direct.log` | `PUSH_EVENTS` |

**默认 `urgency=normal`**：静默时段、20 条/日、2 条/10 分钟、180s 同类间隔全部生效。
确实要突破静默（例如急事），显式加 `--urgency high` 或环境变量 `WAITFORME_HOOK_URGENCY=high`；
high 会跳过静默与同类间隔，但仍有 5 条/10 分钟、50 条/日的硬上限。

```bash
python "${KIMI_SKILL_DIR}/scripts/connect_kimi.py" --mode direct    # 默认就是 direct
```

输出片段（路径是本机的）：

```toml
# === waitforme：零常驻直发（事件来了起进程 → 推送 → 退出）===
[[hooks]]
event = 'Stop'
command = '"C:\Python314\python.exe" "...\skills\waitforme\scripts\hook_direct.py"'
timeout = 10

[[hooks]]
event = 'PermissionRequest'
command = '"C:\Python314\python.exe" "...\hook_direct.py"'
timeout = 10

[[hooks]]
event = 'Notification'
command = '"C:\Python314\python.exe" "...\hook_direct.py"'
timeout = 10
```

**如果 config.toml 里已经有 `kimi_hook.py`**：不要追加，会双推。`connect_kimi.py`
会自动改出「替换 patch」（把 3 处 `kimi_hook.py` 换成 `hook_direct.py`）。

### 4b. 常驻服务（可选）

`kimi_hook.py` 读 stdin → POST 到 `http://127.0.0.1:8790/hook?k=<hook_token>`。
需要 `hooks.py serve --detach` 一直在跑，否则事件被静默丢弃。

```bash
python "${KIMI_SKILL_DIR}/scripts/connect_kimi.py" --mode service
```

**硬规则**：`~/.kimi-code/config.toml` 是用户的文件。
- 只打印片段 / patch，**不要自动写、不要用 sed 追加**。
- 给用户看 patch，明确说「追加 / 替换」，用户同意后由用户自己动手（或用户明确授权你改）。
- 已经出现过同名桥脚本就别重复追加，否则同一事件跑两遍。
- `[[hooks]]` **只允许** `event` / `matcher` / `command` / `timeout` 四个字段，多一个整个
  config 都加载失败。
- 改完要**开新会话**才生效（Kimi Code 启动时读 config）。

片段配好后，先手工发一条事件验证桥是通的（不会真打扰，除非策略判定该推）：

```bash
# 直发模式：Stop 只写日志
echo '{"hook_event_name":"Stop","session_id":"manual-test","cwd":"."}' | python "<skill>/scripts/hook_direct.py" --debug
# 想看推送：用 Notification 或 PermissionRequest
echo '{"hook_event_name":"Notification","message":"手工验证"}' | python "<skill>/scripts/hook_direct.py" --debug
```

`--debug` 会打印 `[hook-direct] Notification → sent: ok`（或失败原因）。

## 5. 验证

```bash
python "${KIMI_SKILL_DIR}/scripts/verify.py"                 # 标准验证（约 20 秒，含 289 项自检）
python "${KIMI_SKILL_DIR}/scripts/verify.py" --service       # 额外真起一次服务、探活、停掉
python "${KIMI_SKILL_DIR}/scripts/verify.py" --live          # 额外真发一条到手机
python "${KIMI_SKILL_DIR}/scripts/verify.py" --skip-selftest # 快速检查（少一半证据）
```

| 检查项 | 通过长什么样 | 失败了先看 |
|---|---|---|
| `python` | `3.14.3 @ C:\Python314\python.exe` | 装 3.10+ |
| `project` | `文件齐全` | 重新 clone，别拿半份代码 |
| `config` | config.json 路径 | 跑 `install.py` |
| `status` | `pid_exists=... port=8790 channel=... today=0` | 跑 `init`；看 stderr |
| `test_mock` | `status=preview link_kept=True` | `link_kept=False` = 消息把跳转链接挤掉了 |
| `selftest` | `PASS 289 / FAIL 0` | 手动跑 `python selftest.py` 看第一个 FAIL |
| `service` | `health ok，pid=...` | `logs/hooks.stdout.log`；端口冲突 |
| `service_stop` | `stop=stopped health_after=False` | pid 文件在 `run/hooks.pid` |
| `test_live` | `status=sent` | key 错 / 网络 / 静默时段（用 `--force`） |

退出码 `0` = 全绿；`2` = 有检查没过（摘要里会逐条给修复建议）。

## 6. 日常命令（装在项目目录里）

```bash
python waitforme.py status                     # pid / 端口 / 通道 / 今日推送数
python waitforme.py link                       # 当前该用的跳转链接（rc.json 优先）
python waitforme.py send --title "冒烟测试"     # 手动推一条
python waitforme.py send --title "看排版" --dry-run
python waitforme.py ask --payload q.json --wait --deadline-s 300
python waitforme.py test --live                # 真发自检消息
python hooks.py stop                           # 停服务
```

`q.json` 最小示例（`options` 2~8 项，`default_if_no_answer` 必须是已有 key）：

```json
{"id":"q-001","title":"用哪个方案","situation":"两个都能跑，取舍不同。",
 "options":[{"key":"A","label":"先上线","tradeoff":"旧字段欠着"},
            {"key":"B","label":"先补字段","tradeoff":"晚一天"}],
 "default_if_no_answer":"A","urgency":"normal"}
```

## 7. 排错表

| 现象 | 原因 | 修法 |
|---|---|---|
| `{"error":"channel_not_configured"}` / 退出码 2 | 通道选的是真通道但 key 是空的 | `configure.py` 填 key；或先 `--channel mock` |
| 微信收不到，`status` 里 `today_count` 不涨 | 服务没起 / hook 没接 / 被限流 | `status` 看 `pid_exists`；`connect_kimi.py` 看片段；看 `logs/push_log.jsonl` |
| 服务起了但 `4xx` | `?k=` 不是 config 里的 `hook_token` | 重跑 `init` 会换 token；桥脚本自己读 config，别手工拼 URL |
| `pid_exists=false` 但进程在 | pid 文件路径和服务的 `run_dir` 不一致 | 别在 config 里乱写 `run_dir`；`hooks.py serve --detach` 会写对的 |
| 端口占用 `bind_failed` | 8790 被别的程序占了 | 改 `hook_port`，再改 Kimi 片段（片段是桥脚本读 config，不用改） |
| `hook 收不到事件` | Kimi config 没生效 / 写错字段 | 开新会话；`[[hooks]]` 只许四个字段；手工 `echo ... \| kimi_hook.py --debug` |
| `selftest` 偶发 FAIL | 时间相关的限流/静默断言 | 重跑一次；仍失败就把 FAIL 行贴出来，别改核心代码 |
| 微信里点链接提示「请使用浏览器打开」 | 微信/企业微信内置浏览器不支持 Kimi Code 远程页 | 点右上角 `···` → 在浏览器打开；或长按消息里那条纯文本链接复制到 Safari/Chrome（推送正文已带复制提示） |
| 推送看不出要决策什么 | payload 里没有工具/命令字段 | 看 `logs/events.raw.jsonl` 的真实字段；`hook_direct.py` 已按 `tool_name`/`command`/`message` 尽力提取 |
| 中文乱码 | 控制台不是 UTF-8 | 本 skill 脚本已 `reconfigure`；项目脚本也有；还乱就是终端本身 |

## 8. 常见坑

- **静默时段**默认 `23:00–08:30`，`normal` 的 `send`/`ask` 会被拦（退出码 2）。测试加
  `--force`，或把 `urgency` 设成 `high`。
- **限流**：同类最小间隔 180s、10 分钟最多 2 条、当日最多 20 条。手动 `send`/`ask` 也会
  计数，发多了随后的自动推送会被额度拦下 —— 这是设计，不是 bug。
- **企业微信 markdown 不认 `**加粗**`**，正文只用 `###`、`>`、行分隔和 `[]()` 链接。
  超过 4096 字节会安全截断，`test --mock` 里看 `link_kept` 是否仍为 `true`。
- **服务只绑 `127.0.0.1`**。要让别的设备送事件，自己改 `hook_host` 并把 `hook_token`
  换成真随机值（默认就是 32 字符随机）。
- **config.json 已被 gitignore**，`.waitforme_home` 也是。key 只进本地 config，不进版本库。
- **hook 只在会话启动时加载**；改完 `config.toml` 必须开新会话。
- **Stop 的 smart 判断**依赖「最后一段助手话术像在等人回话」，真实环境里字段可能抓不到；
  这时只剩 `PermissionRequest` / `Notification` 两类会推。原始事件都落在
  `logs/events.raw.jsonl`，跑几天再按真实字段收紧（那是上游的事，不是本 skill 的）。
- **跳转链接要手机实测**。`linker` 读 `~/.kimi-code/server/rc.json` 里那次会话的 URL，
  远程控制进程退出后链接可能失效；读不到就退 `fallback_url`（默认局域网答题页）。

## 9. 安全边界（给 AI 的硬规则）

1. 不改 `waitforme.py` / `hooks.py` / `channels.py` / `linker.py` / `policy.py`；确需改动，
   必须 `python selftest.py` 保持 `PASS 289 / FAIL 0`。
2. 不自动改 `~/.kimi-code/config.toml`（或任何用户配置）。只打印片段 + patch，让用户确认。
3. 不把真实 key / token 写进仓库、日志、提交信息；示例一律用占位符。
4. 不 `git push`，不改 git 历史。
5. 测试用临时 `config.json`（`--config` 或 `WAITFORME_CONFIG`）和临时 `log_dir`/`run_dir`，
   不污染用户真实配置与 `logs/`。

## 10. skill 目录里有什么

```
SKILL.md               本文件
scripts/install.py     安装器：查 Python → 找项目 → init → 写通道/key → 打印下一步
scripts/configure.py   配置向导：通道 / key / 要不要接 hook
scripts/verify.py      status + mock 消息 + selftest + 可选 live / 服务起停
scripts/connect_kimi.py 打印 [[hooks]] 片段与 patch（不写用户配置）
scripts/kimi_hook.py   Kimi hook → waitforme 服务的桥（stdin JSON → POST，恒 exit 0）
scripts/_common.py     共用：找项目、跑子进程、读写配置、输出约定
.waitforme_home        install/configure 记下的项目路径（生成物，已 gitignore）
```
