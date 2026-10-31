# waitforme —— AI 卡住时，一条能点开的微信

AI（Kimi Code）干活卡住或需要你拍板时，推一条微信到你手机，消息里带**可点击的跳转链接**，
点开就是手机上的 Kimi Code 远程控制页，你在那里回一句话即可。

不做聊天机器人，不做 Web 面板：只做「该找你的时候找到你，并且一点就能接着干」。

## 5 分钟上手

```bash
cd D:/anyi/waitforme

# 1) 生成 config.json（自动写入随机 hook_token）
python waitforme.py init

# 2) 把通道 key 填进 config.json 的 channels.<你选的通道>，并把 channel 改成它
#    （改完不用重启服务，每次请求都重读配置）

# 3) 看消息长什么样；--live 真发一条自检消息到手机
python waitforme.py test
python waitforme.py test --live

# 4) 起常驻服务（后台，pid 写 run/hooks.pid）
python hooks.py serve --detach
python waitforme.py status        # pid_exists / today_count / last_push_ts

# 5) 把 hook 接到 Kimi Code（由你自己改 ~/.kimi-code 配置，本项目不动它）
#    事件 POST 到 http://127.0.0.1:8790/hook?k=<hook_token>

# 收工
python hooks.py stop
```

自测（默认离线，不联网、不污染真实 `logs/`；含 40 线程并发写与 8 并发 hook 的回归）：

```bash
python selftest.py          # 167 项断言，全绿退出 0
python selftest.py --live   # 再真发一条；通道没配就 SKIP
```

## 三个微信通道，选一个

| 通道 | 怎么拿 key | 填哪儿 |
|---|---|---|
| `wecom_bot` | 企业微信群 → 群机器人 → 添加，复制 Webhook 地址里 `key=` 后面那串 | `channels.wecom_bot.key` |
| `serverchan` | sct.ee 微信登录 → 「Server Key」那一串（Turbo 加送） | `channels.serverchan.sckey` |
| `pushplus` | www.pushplus.plus 微信扫码 → 用户中心里的「用户令牌」 | `channels.pushplus.token` |

`mock` 不联网，把最终消息写进 `logs/mock_sent.jsonl`，用来看排版、跑自测。

排版注意：企业微信 markdown **不认 `**加粗**`**（原样显示星号），所以本项目正文只用
`###` 标题、`>` 引用块、行分隔和 `[]()` 链接；超过 4096 字节会安全截断且不留下半个汉字。

## CLI 契约

```
python waitforme.py init [--force]
python waitforme.py link
python waitforme.py send --title T [--body MD] [--urgency normal|high] [--dry-run] [--channel mock|wecom_bot|serverchan|pushplus]
python waitforme.py ask  --payload q.json [--wait] [--deadline-s N] [--dry-run] [--channel ...]
python waitforme.py test [--mock] [--live]
python waitforme.py status
```

| 子命令 | stdout（唯一一行 JSON） | 退出码 |
|---|---|---|
| `init` | `{"status":"created|exists","config":...,"hook_token":...}` | 0 |
| `link` | `{"primary_url":...,"fallback_url":...,"source":"rc|fallback"}` | 0 |
| `send` | `{"status":"sent|failed|dry_run","ok":...,"url":...,...}` | 0 / 2 通道未配置 |
| `ask` | `sent` \| `answered` \| `timeout` \| `invalid` \| `failed` | 0 / 2 |
| `test` | `{"status":"preview|sent|failed","markdown":...,"link_source":...}`（完整 markdown 也打到 stderr） | 0 / 2 |
| `status` | `{"pid_exists":...,"port":8790,"enabled":...,"channel":...,"today_count":N,"last_push_ts":"..."}` | 0 |

**退出码**：`0` 成功 / `2` 业务失败（payload 非法、等到超时、通道未配置、被限流）/ `1` 程序异常。
**stdout 只有一行 JSON**，诊断全走 stderr；Windows 下 `sys.stdout.reconfigure(encoding="utf-8")`
保证中文不乱码（不加这条，`tail -1 | jq` 会因为 GBK 编码失败）。

`ask --wait` 等的答案文件是 `inbox/<id>.answer.json`：

```json
{"choice": "B", "note": "先上线，旧字段我自己在导出脚本里兜"}
```

`choice` 必须是 payload 里的 key；回错字母不报错，按 `default_if_no_answer` 走并在结果里
留 `raw_choice` / `choice_source:"invalid_reply_fallback"`，让上游看见而不是猜。

## 什么时候推、什么时候闭嘴

三道门，全过才推（`policy.py`）：

1. **事件类型**
   - `PermissionRequest`、`Notification` → 推（同内容 60 秒内去重）
   - `Stop` → 看 `stop_mode`：`smart`（默认）只有「这回合跑了 ≥ `stop_min_duration_s`（120 秒）」
     **或**「最后一段助手话术像在等人回话」（命中 `[?？]|需要你|请选择|要不要|确认|等你|决定`）才推；
     `always` 一律推；`off` 一律不推
   - `SubagentStop` → 不推，只记日志（子任务天天在结束，推了就是噪音）
   - 其它事件 → 不推
2. **静默时段** `quiet_hours`（默认 23:00–08:30，跨零点已处理）。`urgency=="high"` 越限。
3. **额度** `throttle_ok`：同类最小间隔 180 秒 + 10 分钟内最多 2 条 + 当日最多 20 条，
   计数读 `logs/push_log.jsonl`（只记成功推送）。

手动 `send` / `ask` 是你的明确意图，绕过限流但仍受 `enabled` 开关约束；它们**会**计入历史，
所以手动发多了，随后的自动推送会被额度拦下 —— 这是想要的行为。

## 文件

```
waitforme.py       CLI 入口（init/link/send/ask/test/status）
hooks.py           常驻 HTTP 服务：POST /hook?k=<token>，GET /health
channels.py        四个通道适配器，统一 send(...) -> {"ok","channel","status_code","error"}
linker.py          从 ~/.kimi-code/server/rc.json 读远程控制链接，读不到退 fallback
policy.py          事件策略 / 限流 / 静默时段 / 内容指纹（纯函数，好单测）
config.example.json 配置模板（config.json 由 init 生成，已被 gitignore）
selftest.py        160 项离线断言 + 可选 --live
```

运行时产物（都不进版本库）：

| 文件 | 作用 |
|---|---|
| `logs/events.raw.jsonl` | **每个 hook 请求的原始 body**，字段映射以后靠它校准 |
| `logs/push_log.jsonl` | 推送历史，限流计数依据 |
| `logs/mock_sent.jsonl` | mock 通道的最终消息 |
| `logs/hooks.log` | 服务日志，超 5MB 改名 `hooks.1.log` |
| `run/hooks.pid` | 后台服务 pid |
| `inbox/<id>.answer.json` | `ask --wait` 的答案落地处 |

## 已知限制

- **hook payload 的字段没有公开契约**。所以服务是「宽容解析」：递归按候选键名
  （`session_id`/`sessionId`/`cwd`/`hook_event_name`/`message`/`last_assistant_message`/
  `transcript_path` 等）抓值，抓不到不算错；`duration_s` 这类字段大概率不存在，
  `Stop` 的 smart 判断在真实环境里多半只剩问句那条腿能用。所有原始事件都落
  `logs/events.raw.jsonl`，跑几天后按真实字段收紧映射。
- **跳转链接能不能真的连上要你在手机上实测**。`linker` 读的是 `rc.json` 里当时那次会话的
  URL；远程控制进程可能已退出（`rc_is_alive()` 只提示，不阻断），URL 也可能过期。
  读不到 `rc.json` 时退到 `fallback_url`（局域网答题页 `http://10.204.69.40:8791/`），
  那个页面得自己保证在跑。
- 答案回传只有 `inbox/*.answer.json` 这一条路：谁写这个文件不在本项目范围内。
- Windows 判进程存活用不了 `os.kill(pid, 0)`：实测（Python 3.14）它对刚退出但句柄未回收的
  pid 同样不抛异常，会把死进程判成活的，且 `os.kill` 在 Windows 的语义是「终止进程」。
  所以 `linker.pid_alive()` 走 `kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` +
  `GetExitCodeProcess`，只查询、绝不终止。
- Windows 的 `O_APPEND` 也**不是**原子追加（UCRT 用「每次写之前 seek 到末尾」模拟它）：
  20 线程并发压测时真的丢过 3 条 mock、6 条计数记录。现在 `channels.append_line` /
  `policy.append_line` 都走进程内锁串行写，`selftest.py` 里有 40 线程 + 8 并发 hook 两条
  回归盯着。**跨进程**（服务在跑时你手动 `send`）仍是各自上锁，极端情况下可能丢一条计数
  记录，后果只是多推/少推一条，不会写坏文件。
- `quiet_hours` 与「当日」都按本地时间；这台机器换时区就得重看配置。
- 服务只绑 `127.0.0.1`。要让别的设备送事件，自己改 `hook_host` 并把 `hook_token` 换成真随机值。
