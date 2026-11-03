"""hook_direct.py —— Kimi Code hook 的零常驻直发桥。

设计目标（对应 6 条硬约束）：
1. 读 stdin 的 JSON，事件名取 hook_event_name，容错 event / type / hookEventName，
   大小写与 "" - " 一律归一；解析失败不报错。
2. **不碰 8790 服务**：直接调用 `waitforme.py send`（短命进程，起→发→退）。
3. **恒 exit 0**：Stop 是可阻断事件，退出码 2 会拦下用户回合；任何异常都吞掉。
4. **stdout 必须为空**：hook 的 stdout 可能被塞进模型上下文；调试只走 --debug + stderr。
5. **自控超时**：hook 给的 timeout=10，这里子进程默认 8s，超时也 exit 0。
6. **事件分流**：PermissionRequest / Notification → 推；Stop 及其它 → 只写日志。

手工验证：
    echo '{"hook_event_name":"PermissionRequest","message":"要删订单表"}' | python hook_direct.py --debug
    echo '{"hook_event_name":"Stop"}' | python hook_direct.py --debug   # 只记日志，不推
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common as C  # noqa: E402

# 推送白名单：其余事件（含 Stop / SubagentStop）只落日志
PUSH_EVENTS = {"PermissionRequest", "Notification"}

# 事件名归一表：小写、下划线/连字符/空格都抹平后再查
EVENT_ALIASES = {
    "permissionrequest": "PermissionRequest",
    "permission_request": "PermissionRequest",
    "permission": "PermissionRequest",
    "notification": "Notification",
    "notify": "Notification",
    "stop": "Stop",
    "turnstop": "Stop",
    "turn_stop": "Stop",
    "subagentstop": "SubagentStop",
    "subagent_stop": "SubagentStop",
    "subagent": "SubagentStop",
}

EVENT_TITLE = {
    "PermissionRequest": "等你授权",
    "Notification": "有新通知",
    "Stop": "回合结束",
    "SubagentStop": "子任务结束",
}

DEFAULT_TIMEOUT_S = 8.0  # hook timeout=10，留 2s 余量


def log_line(log_dir: Path, record: dict) -> None:
    """诊断日志：只写 log_dir/hook_direct.log，失败也不抛。"""
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with open(log_dir / "hook_direct.log", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def normalize_event(data: object) -> str:
    """从任意形状的 payload 里取事件名；取不到返回 unknown。"""
    found = None
    if isinstance(data, dict):
        for key in ("hook_event_name", "hookEventName", "event", "type", "name"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                found = value.strip()
                break
    if not found:
        return "unknown"
    slug = found.strip().lower().replace("-", "").replace("_", "").replace(" ", "")
    # 先查抹平表，再退回原始值（保证大小写归一）
    if slug in EVENT_ALIASES:
        return EVENT_ALIASES[slug]
    for alias, canonical in EVENT_ALIASES.items():
        if alias.replace("_", "") == slug:
            return canonical
    return found.strip()


def first_text(data: dict, keys: tuple[str, ...]) -> str:
    """递归找第一个非空字符串字段（宽容解析，和 hooks.py 的口径一致）。"""
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key in keys:
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for value in node.values():
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return ""


def short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text[:limit] + "…" if len(text) > limit else text


def build_message(event: str, data: dict) -> tuple[str, str]:
    """返回 (title 后缀, markdown 正文)。waitforme.py send 会自动加 "⏳ Kimi 需要你：" 前缀。"""
    title = EVENT_TITLE.get(event, event)
    message = first_text(data, ("message", "last_assistant_message", "text", "content"))
    cwd = first_text(data, ("cwd", "project_dir", "working_dir"))
    session = first_text(data, ("session_id", "sessionId", "session"))
    parts: list[str] = []
    if message:
        parts.append(f"「{short(message, 200)}」")
    meta: list[str] = []
    if cwd:
        meta.append(f"目录：{Path(cwd).name or cwd}")
    if session:
        meta.append(f"会话：{session[:8]}")
    if meta:
        parts.append(" / ".join(meta))
    parts.append("手机点下面的链接，回一句话就行。")
    return title, "\n\n".join(parts)


def run_send(python_exe: str, project: Path, config: Path, title: str, body: str,
             timeout: float) -> tuple[bool, str]:
    """调用 waitforme.py send；不接管它的 stdout（直接丢弃）。"""
    cmd = [
        python_exe, str(project / "waitforme.py"), "send",
        "--title", title,
        "--body", body,
        "--urgency", "high",  # 越过静默时段：hook 场景下用户已经明确要被打扰
    ]
    env = dict(os.environ)
    env["WAITFORME_CONFIG"] = str(config)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            cmd, cwd=str(project), env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout,
            text=True, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, f"timeout>{timeout}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    detail = (proc.stderr or "").strip().splitlines()
    tail = detail[-1][:200] if detail else ""
    if proc.returncode != 0:
        return False, f"exit={proc.returncode} {tail}"
    return True, tail or "ok"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hook_direct.py", description="Kimi Code hook → waitforme.py send（零常驻）")
    parser.add_argument("--project", default=None, help="waitforme 项目目录（默认自动找）")
    parser.add_argument("--config", default=None, help="config.json 路径（默认 <project>/config.json）")
    parser.add_argument("--timeout", type=float, default=None, help="内部发送超时秒数（默认 8，可用 WAITFORME_HOOK_TIMEOUT 覆盖）")
    parser.add_argument("--debug", action="store_true", help="把判定与结果打到 stderr")
    args = parser.parse_args(argv)

    started = time.time()
    try:
        raw = sys.stdin.buffer.read()
    except (OSError, ValueError):
        raw = b""

    data: object = {}
    parse_error = ""
    text = raw.decode("utf-8", "replace").strip()
    if text:
        try:
            data = json.loads(text)
        except ValueError as exc:
            parse_error = f"{type(exc).__name__}: {exc}"

    event = normalize_event(data)
    payload = data if isinstance(data, dict) else {}
    ts = datetime.now().isoformat(timespec="seconds")

    timeout = args.timeout
    if timeout is None:
        try:
            timeout = float(os.environ.get("WAITFORME_HOOK_TIMEOUT") or DEFAULT_TIMEOUT_S)
        except ValueError:
            timeout = DEFAULT_TIMEOUT_S

    project = C.resolve_project(args.project)
    record = {
        "ts": ts, "event": event, "action": "log_only", "ok": True,
        "detail": "", "ms": 0, "parse_error": parse_error,
        "session": first_text(payload, ("session_id", "sessionId", "session"))[:8] if payload else "",
    }

    if project is None:
        record.update(ok=False, detail="project_not_found")
        if args.debug:
            C.say("[hook-direct] 找不到 waitforme 项目；事件只记日志")
        # 项目都没有就没有日志目录，只能打 stderr
        print(json.dumps(record, ensure_ascii=False), file=sys.stderr)
        return 0

    # 先读配置：日志目录也以 config 的 log_dir 为准（自测/多实例时不会写串）
    config = C.config_path_for(project, args.config)
    cfg = C.read_config(config)
    log_dir = Path(os.path.expanduser(str(cfg.get("log_dir") or project / "logs")))

    if not cfg:
        record.update(ok=False, action="push", detail=f"config_unreadable:{config}")
        if args.debug:
            C.say(f"[hook-direct] 配置读不到：{config}")
        record["ms"] = int((time.time() - started) * 1000)
        log_line(log_dir, record)
        return 0

    if event not in PUSH_EVENTS:
        if args.debug:
            C.say(f"[hook-direct] {event} → 只记日志（不推）")
        record["ms"] = int((time.time() - started) * 1000)
        log_line(log_dir, record)
        return 0

    title_suffix, body = build_message(event, payload)
    ok, detail = run_send(sys.executable, project, config, title_suffix, body, timeout)
    record.update(action="push", ok=ok, detail=detail, ms=int((time.time() - started) * 1000))
    if args.debug:
        C.say(f"[hook-direct] {event} → {'sent' if ok else 'failed'}: {detail}")
    log_line(log_dir, record)
    return 0


if __name__ == "__main__":
    # 绝不让异常冒到 hook：stdout 一个字都不输出，退出码恒 0（fail-open）。
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - 含 KeyboardInterrupt，都吞掉
        print(f"[hook-direct] 未预期异常：{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(0)
