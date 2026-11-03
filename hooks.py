"""hooks.py —— 常驻 HTTP 服务，接 Kimi Code 的 hook 事件并转成微信推送。

只用 stdlib（`http.server.ThreadingHTTPServer`）。核心取向：

* **先应答再干活**：无论推不推、判定结果如何，HTTP 都立即 200，推送丢到有界线程池。
  hook 是别人主流程里的一环，把人家卡住是最糟的失败模式。
* **额度在应答前就占掉**：判定一过立即同步写预占记录，再回 200、再后台发。否则同一秒
  进来的同类事件都看到「还有额度」，会一起推出去。
* **宽容解析**：官方没公开 hook payload 的字段，所以原始 body 一律先落盘
  `logs/events.raw.jsonl`，再递归按候选键名抓值。抓不到不算错 —— 这个文件就是
  以后校准字段映射的依据。
* **不打扰优先**：事件类型策略 + 限流 + 静默时段，三道门都过才推。
* **凭证不进日志**：`?k=<hook_token>` 在访问日志里一律打码。

用法（无参数不再默认起服务，避免前台静默阻塞）：
    python hooks.py serve              # 前台跑
    python hooks.py serve --detach     # 后台起，pid 写 run/hooks.pid
    python hooks.py stop               # 按 pid 停
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from channels import append_line  # noqa: E402  进程内串行的整行追加（Windows 的 O_APPEND 不原子）

LOG_NAME = "hooks.log"
RAW_EVENTS_NAME = "events.raw.jsonl"
MAX_LOG_BYTES = 5 * 1024 * 1024
MAX_BODY_BYTES = 256 * 1024

# 默认落在这里；load_config() 会按 config 的 log_dir / run_dir 覆盖（自测用临时目录，
# 不去污染真实运行目录）
LOGS_DIR = HERE / "logs"
RUN_DIR = HERE / "run"

# 事件中文名：推送标题用，也让人一眼看得懂日志
EVENT_ZH = {
    "PermissionRequest": "等你授权",
    "Notification": "有新通知",
    "Stop": "回合结束，可能在等你",
    "SubagentStop": "子任务结束",
    "unknown": "有事件发生",
}

# 已知事件全集（含不推的），用于把 `NOTIFICATION` / `permission_request` 这类写法归一
KNOWN_EVENTS = ("PermissionRequest", "Notification", "Stop", "SubagentStop", "PreToolUse", "PostToolUse",
                "PostToolUseFailure", "UserPromptSubmit", "PreCompact", "SessionStart", "SessionEnd")
EVENT_ALIASES = {name.lower().replace("_", "").replace("-", ""): name for name in KNOWN_EVENTS}
EVENT_ALIASES["unknown"] = "unknown"

# 候选键名（大小写不敏感匹配），按优先级排
KEY_CANDIDATES: dict[str, tuple[str, ...]] = {
    "session_id": ("session_id", "sessionid", "session", "conversation_id", "conversationid"),
    "cwd": ("cwd", "working_dir", "workingdir", "project_path", "workdir"),
    "hook_event_name": ("hook_event_name", "hookeventname", "event", "event_name", "eventname", "type"),
    "notification_type": ("notification_type", "notificationtype", "kind"),
    "message": ("message", "msg", "text", "content", "body"),
    "last_assistant_message": ("last_assistant_message", "lastassistantmessage", "assistant_message", "response"),
    "transcript_path": ("transcript_path", "transcriptpath", "transcript"),
    "duration_s": ("duration_s", "duration", "elapsed_s", "elapsed"),
    "urgency": ("urgency", "priority"),
    "tool_name": ("tool_name", "toolname", "tool"),
    "tool_command": ("command", "cmd", "script", "shell_command", "bash_command",
                     "tool_input", "toolinput", "input", "arguments", "args"),
}

_lock = threading.Lock()

# 推送用的有界线程池：无上限起线程的话，通道卡住时会把进程拖成一堆 10 秒阻塞的线程
PUSH_WORKERS = 4
PUSH_QUEUE_LIMIT = 64
_push_pool = concurrent.futures.ThreadPoolExecutor(max_workers=PUSH_WORKERS, thread_name_prefix="wf-push")
_pending_lock = threading.Lock()
_pending = 0

# 访问日志里绝不能留 token
_TOKEN_IN_URL = re.compile(r"([?&]k=)[^&\s\"']+")


def redact_token(line: str) -> str:
    """把 URL 里的 `?k=<hook_token>` 打成 `?k=***`。"""
    return _TOKEN_IN_URL.sub(r"\1***", str(line or ""))


def _submit_push(cfg: dict[str, Any], found: dict[str, Any], event_name: str, reason: str, urgency: str, reserve_id: str | None) -> str:
    """把推送丢进有界池。队列满就当场退还额度，别攒一堆迟早发不出去的请求。"""
    global _pending
    import policy

    with _pending_lock:
        if _pending >= PUSH_QUEUE_LIMIT:
            if reserve_id:
                policy.cancel_reserve(cfg, reserve_id, "queue_full")
            log_line(f"[push] 队列已满（{_pending}>={PUSH_QUEUE_LIMIT}），丢弃 {event_name} 并退还额度")
            return "dropped_queue_full"
        _pending += 1

    def _run() -> None:
        global _pending
        try:
            do_push(cfg, found, event_name, reason, urgency, reserve_id)
        finally:
            with _pending_lock:
                _pending -= 1

    try:
        _push_pool.submit(_run)
    except Exception as exc:  # noqa: BLE001 - 池子挂了也不能影响 HTTP
        with _pending_lock:
            _pending -= 1
        if reserve_id:
            policy.settle_push(cfg, reserve_id, False, error=f"submit_failed:{type(exc).__name__}")
        log_line(f"[push] 提交失败：{type(exc).__name__}: {exc}")
        return "submit_failed"
    return "queued"


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------


def _echo_stderr() -> bool:
    """只有对着终端才回显 stderr。

    服务被 `subprocess.PIPE` 之类接管时（自测、被别的程序拉起），没人读管道会让
    `print` 卡死，把整个请求线程拖住 —— 日志已经进 hooks.log 了，不值得冒这个风险。
    """
    if sys.stderr is None:
        return False
    try:
        return bool(sys.stderr.isatty())
    except (ValueError, OSError):
        return False


def log_line(message: str) -> None:
    """写 logs/hooks.log，超过 5MB 改名 .1（简单轮转，不留一堆历史）。"""
    stamp = datetime.now().isoformat(timespec="seconds")
    line = f"[{stamp}] {message}"
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        path = LOGS_DIR / LOG_NAME
        with _lock:
            try:
                if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
                    path.replace(path.with_suffix(".1.log"))
            except OSError:
                pass
            append_line(path, line)
    except OSError:
        pass
    if _echo_stderr():
        try:
            print(line, file=sys.stderr, flush=True)
        except OSError:
            pass


def append_jsonl(name: str, record: dict[str, Any]) -> None:
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        append_line(LOGS_DIR / name, json.dumps(record, ensure_ascii=False))
    except OSError as exc:
        log_line(f"[log] 写 {name} 失败：{exc}")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def load_config() -> dict[str, Any]:
    """每次请求都重读配置，改 key 不用重启服务。读不到就回落到默认值。"""
    import waitforme

    cfg = waitforme.load_config(waitforme.CONFIG_PATH)
    global LOGS_DIR, RUN_DIR
    LOGS_DIR = Path(str(cfg.get("log_dir") or LOGS_DIR))
    RUN_DIR = Path(str(cfg.get("run_dir") or RUN_DIR))
    return cfg


# ---------------------------------------------------------------------------
# 宽容解析
# ---------------------------------------------------------------------------


def _walk_strings(obj: Any, depth: int = 0):
    """深度优先遍历 JSON，产出 (小写键名, 值) —— 先遇到的键优先，浅层优先。"""
    if depth > 12:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str):
                yield key.lower(), value
            if isinstance(value, (dict, list)):
                yield from _walk_strings(value, depth + 1)
    elif isinstance(obj, list):
        for item in obj[:50]:
            if isinstance(item, (dict, list)):
                yield from _walk_strings(item, depth + 1)


def extract_fields(data: Any) -> dict[str, Any]:
    """按候选键名递归抓值。抓不到就是抓不到，不报错。"""
    found: dict[str, Any] = {}
    pairs = list(_walk_strings(data))
    for field, candidates in KEY_CANDIDATES.items():
        for name in candidates:
            for key, value in pairs:
                if key != name:
                    continue
                if isinstance(value, (str, int, float)) and str(value).strip():
                    found[field] = value
                    break
            if field in found:
                break
    return found


def decode_body(raw: bytes) -> str:
    """尽力把 body 解成文本：UTF-8 优先，退回 GBK，最后 replace。

    中文 Windows 上有些客户端（比如 Git Bash 里的原生 curl）会把参数转成 GBK 再发出来，
    硬按 UTF-8 解会得到一串 U+FFFD 推给用户手机。判不出来源时，能救一条是一条。
    """
    if not raw:
        return ""
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def normalize_event_name(raw: Any) -> str | None:
    """事件名归一：大小写/下划线风格不同也认（`NOTIFICATION`、`permission_request`）。"""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text in EVENT_ZH or text in KNOWN_EVENTS:
        return text
    slug = text.lower().replace("_", "").replace("-", "")
    return EVENT_ALIASES.get(slug, text)


def decide_event_name(found: dict[str, Any], query: dict[str, list[str]]) -> str:
    """事件名：payload 里的 hook_event_name/event > ?evt= > unknown。"""
    from_event = normalize_event_name(found.get("hook_event_name"))
    if from_event:
        return from_event
    evt = normalize_event_name((query.get("evt") or [""])[0])
    return evt or "unknown"


def short_session(session_id: Any) -> str:
    text = str(session_id or "").strip()
    return text[:8] if text else "?"


def truncate(text: Any, limit: int = 300) -> str:
    flat = " ".join(str(text or "").split())
    return flat[:limit] + "…" if len(flat) > limit else flat


def build_event_body(found: dict[str, Any], event_name: str) -> str:
    """推送正文：会话线索（目录名 + 短 id）+ 事件说明 + 消息截断。"""
    cwd = str(found.get("cwd") or "").strip()
    cwd_name = Path(cwd).name if cwd else "?"
    message = found.get("last_assistant_message") or found.get("message") or ""
    lines = [
        f"> 项目：{cwd_name}",
        f"> 会话：{short_session(found.get('session_id'))}",
        f"> 事件：{EVENT_ZH.get(event_name, event_name)}",
    ]
    if message:
        lines.append("")
        lines.append(f"「{truncate(message)}」")
    if found.get("notification_type"):
        lines.append(f"> 通知类型：{found['notification_type']}")
    lines.append("")
    lines.append("手机点下面的链接，回一句话就行。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 推送（后台线程里跑，异常只写日志）
# ---------------------------------------------------------------------------


def do_push(cfg: dict[str, Any], found: dict[str, Any], event_name: str, reason: str, urgency: str,
            reserve_id: str | None = None) -> None:
    """真正发送。额度已在 HTTP 线程里预占过，这里只回填结果。异常一律吞掉只写日志。"""
    try:
        import waitforme

        title = f"⏳ Kimi 需要你：{EVENT_ZH.get(event_name, event_name)}"
        body = build_event_body(found, event_name)
        result = waitforme.push_message(cfg, kind=event_name, title=title, body_md=body,
                                        urgency=urgency, fingerprint_value=found.get("_fp"),
                                        reserve_id=reserve_id)
        log_line(f"[push] {event_name} ok={result.get('ok')} channel={result.get('channel')} "
                 f"code={result.get('status_code')} error={redact_token(result.get('error') or '')} "
                 f"reason={reason} link={result.get('link_source')}")
    except Exception as exc:
        log_line(f"[push] 异常（已吞掉，不影响 HTTP）：{type(exc).__name__}: {exc}\n{traceback.format_exc()[:800]}")


def top_level_value(parsed: Any, names: tuple[str, ...]) -> Any:
    """只认**顶层**键。

    `urgency`/`priority` 决定能不能越静默时段，所以不许从任意嵌套层里抓 —— 实测
    子结构里一个 `"priority":"high"` 就能把用户的无限推送打开。
    """
    if not isinstance(parsed, dict):
        return None
    for key in names:
        for actual, value in parsed.items():
            if isinstance(actual, str) and actual.lower() == key and isinstance(value, (str, int, float)):
                return value
    return None


def handle_hook(raw_body: bytes, query: dict[str, list[str]], cfg: dict[str, Any]) -> dict[str, Any]:
    """解析 → 判定（并预占额度）→ 丢进有界池推送。返回给 HTTP 响应用的小摘要。"""
    import policy

    text = decode_body(raw_body).strip()
    parsed: Any = None
    parse_error = None
    if text:
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            parse_error = f"{type(exc).__name__}: {exc}"
    # 原始 body 先落盘：字段映射以后要靠它校准
    append_jsonl(RAW_EVENTS_NAME, {
        "ts": round(time.time(), 3),
        "local_time": datetime.now().isoformat(timespec="seconds"),
        "query": {k: v for k, v in query.items() if k != "k"},
        "raw": text[:4000],
        "parse_error": parse_error,
    })

    found = extract_fields(parsed) if parsed is not None else {}
    event_name = decide_event_name(found, query)
    urgency = str(top_level_value(parsed, ("urgency", "priority")) or "normal").strip().lower()
    title = f"⏳ Kimi 需要你：{EVENT_ZH.get(event_name, event_name)}"
    body = build_event_body(found, event_name)

    found["_fp"] = policy.fingerprint(title, body)
    event = dict(found)
    event["hook_event_name"] = event_name
    event["fingerprint"] = found["_fp"]
    event["urgency"] = urgency

    decision = policy.gate(event, cfg, kind=event_name)
    log_line(f"[event] {event_name} allowed={decision['allowed']} reason={decision['reason']} "
             f"fp={found['_fp']} parse_error={parse_error or '-'}")
    dispatched = None
    if decision["allowed"]:
        dispatched = _submit_push(cfg, found, event_name, decision["reason"], urgency, decision.get("reserve_id"))
        if dispatched != "queued":
            decision = {"allowed": False, "reason": dispatched, "fingerprint": found["_fp"], "urgency": urgency}
    return {"event": event_name, "push": bool(decision["allowed"]), "reason": decision["reason"]}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "waitforme/1.0"
    protocol_version = "HTTP/1.1"

    def _read_body(self) -> bytes:
        """把请求体读干净。

        keep-alive 下不读 body 就发响应，残留字节会被当成下一个请求的开头 ——
        实测同一个 Session 里先 403 再正常 POST，第二次直接收到 400。
        """
        try:
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                chunks, size = [], 0
                while size < MAX_BODY_BYTES:
                    line = self.rfile.readline(65536).strip()
                    if not line:
                        break
                    try:
                        n = int(line.split(b";")[0], 16)
                    except ValueError:
                        break
                    if n == 0:
                        self.rfile.readline(65536)
                        break
                    chunk = self.rfile.read(min(n, MAX_BODY_BYTES - size))
                    size += len(chunk)
                    chunks.append(chunk)
                    self.rfile.readline(65536)
                return b"".join(chunks)
            length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY_BYTES)
        except (ValueError, OSError):
            return b""
        if length <= 0:
            return b""
        try:
            return self.rfile.read(length)
        except (OSError, TimeoutError):
            return b""

    def _send(self, code: int, payload: dict[str, Any], close: bool = False) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if close:
            self.send_header("Connection", "close")
        self.end_headers()
        if close:
            self.close_connection = True
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        parsed = urlparse(self.path)
        self._read_body()  # GET 也可能带 body，一律读掉再说
        if parsed.path.rstrip("/") != "/health":
            self._send(404, {"ok": False, "error": "not_found"}, close=True)
            return
        try:
            import policy

            cfg = load_config()
            self._send(200, {"ok": True, "push_today": policy.count_today(cfg)})
        except Exception as exc:
            log_line(f"[health] 异常：{type(exc).__name__}: {exc}")
            self._send(200, {"ok": True, "push_today": 0})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        parsed = urlparse(self.path)
        raw = self._read_body()
        if parsed.path.rstrip("/") != "/hook":
            self._send(404, {"ok": False, "error": "not_found"}, close=True)
            return
        query = parse_qs(parsed.query)
        cfg = load_config()
        token_expected = str(cfg.get("hook_token") or "")
        token_got = (query.get("k") or [""])[0]
        # 定长时间比较：`!=` 会因短路在时间上泄露 token 前缀（本机服务也值得防一手）
        if not token_expected or not hmac.compare_digest(token_got.encode("utf-8"), token_expected.encode("utf-8")):
            log_line("[hook] 403 token 不匹配")
            self._send(403, {"ok": False, "error": "bad_token"}, close=True)
            return
        try:
            summary = handle_hook(raw, query, cfg)
            self._send(200, {"ok": True, **summary})
        except Exception as exc:
            # 解析炸了也要 200：hook 侧不该因为我们的 bug 收到失败
            log_line(f"[hook] 处理异常：{type(exc).__name__}: {exc}\n{traceback.format_exc()[:800]}")
            self._send(200, {"ok": True, "event": "unknown", "push": False, "reason": "handler_error"})

    def log_message(self, format: str, *args: Any) -> None:
        # requestline 原样落盘会把 ?k=<hook_token> 写进日志，必须先打码
        log_line(f"[http] {redact_token(format % args)}")


def serve_forever(host: str, port: int) -> int:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    (RUN_DIR / "hooks.pid").write_text(str(os.getpid()), encoding="utf-8")
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        log_line(f"[serve] 绑定 {host}:{port} 失败：{type(exc).__name__}: {exc}")
        print(json.dumps({"status": "error", "reason": f"bind_failed:{exc}", "host": host, "port": port}, ensure_ascii=False))
        return 2
    httpd.daemon_threads = True
    log_line(f"[serve] 监听 http://{host}:{port}/hook（pid={os.getpid()}）")
    print(json.dumps({"status": "serving", "host": host, "port": port, "pid": os.getpid()}, ensure_ascii=False), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log_line("[serve] 收到 Ctrl-C，退出")
    finally:
        httpd.server_close()
        try:
            (RUN_DIR / "hooks.pid").unlink()
        except OSError:
            pass
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = load_config()
    host = args.host or str(cfg.get("hook_host") or "127.0.0.1")
    port = int(args.port or cfg.get("hook_port") or 8790)
    if args.detach:
        if not args.no_pid_check and _running_pid():
            log_line("[serve] 已有服务在跑，不再重复起")
            print(json.dumps({"status": "already_running", "pid": _running_pid(), "port": port}, ensure_ascii=False))
            return 0
        # 目录不存在时 open() 会直接抛，后台起不来（实测 exit=1）
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        out = (LOGS_DIR / "hooks.stdout.log").open("ab")
        flags = 0
        if sys.platform == "win32":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        proc = subprocess.Popen(
            [sys.executable, str(HERE / "hooks.py"), "serve", "--host", host, "--port", str(port)],
            stdout=out, stderr=out, stdin=subprocess.DEVNULL, cwd=str(HERE), creationflags=flags, close_fds=True,
        )
        (RUN_DIR / "hooks.pid").write_text(str(proc.pid), encoding="utf-8")
        log_line(f"[serve] 已后台启动 pid={proc.pid}")
        print(json.dumps({"status": "detached", "pid": proc.pid, "host": host, "port": port}, ensure_ascii=False))
        return 0
    return serve_forever(host, port)


def _running_pid() -> int | None:
    path = RUN_DIR / "hooks.pid"
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    import linker

    return pid if linker.pid_alive(pid) else None


def cmd_stop(args: argparse.Namespace) -> int:
    load_config()  # 让 RUN_DIR / LOGS_DIR 指向配置里的位置，否则找不到别人起的 pid
    pid = _running_pid()
    if not pid:
        print(json.dumps({"status": "not_running", "pid": None}, ensure_ascii=False))
        return 2
    killed = False
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
        if handle:
            killed = bool(ctypes.windll.kernel32.TerminateProcess(handle, 0))
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 15)
            killed = True
        except OSError:
            killed = False
    try:
        (RUN_DIR / "hooks.pid").unlink()
    except OSError:
        pass
    log_line(f"[stop] pid={pid} killed={killed}")
    print(json.dumps({"status": "stopped" if killed else "failed", "pid": pid}, ensure_ascii=False))
    return 0 if killed else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hooks.py", description="waitforme hook 服务")
    sub = parser.add_subparsers(dest="command")
    p_serve = sub.add_parser("serve", help="起服务（默认前台）")
    p_serve.add_argument("--detach", action="store_true", help="后台起，pid 写 run/hooks.pid")
    p_serve.add_argument("--no-pid-check", action="store_true", help="跳过已在运行的检查")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.set_defaults(func=cmd_serve)
    p_stop = sub.add_parser("stop", help="停掉后台服务")
    p_stop.set_defaults(func=cmd_stop)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv:
        # 静默起前台服务会直接把调用方挂住；无参数一律当用法错误
        parser.print_usage(sys.stderr)
        print(json.dumps({"status": "usage_error", "reason": "需要子命令 serve|stop"}, ensure_ascii=False))
        return 2
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_usage(sys.stderr)
        print(json.dumps({"status": "usage_error", "reason": "需要子命令 serve|stop"}, ensure_ascii=False))
        return 2
    try:
        return int(args.func(args))
    except Exception as exc:
        log_line(f"[fatal] {type(exc).__name__}: {exc}\n{traceback.format_exc()[:1200]}")
        print(json.dumps({"status": "error", "reason": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
