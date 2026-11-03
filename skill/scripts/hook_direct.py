"""hook_direct.py —— Kimi Code hook 的零常驻直发桥（带三道门）。

设计目标（对应 6 条硬约束）：
1. 读 stdin 的 JSON，事件名取 hook_event_name，容错 event / type / hookEventName，
   大小写与 "-" / "_" 一律归一；解析失败不报错。
2. **不碰 8790 服务**：在进程内直接调用 `policy.gate` + `channels.send`（短命进程）。
3. **恒 exit 0**：Stop 是可阻断事件，退出码 2 会拦下用户回合；任何异常都吞掉。
4. **stdout 必须为空**：hook 的 stdout 可能被塞进模型上下文；调试只走 --debug + stderr。
5. **自控超时**：hook 给的 timeout=10，这里网络超时默认 8s，超时也 exit 0。
6. **事件分流**：PermissionRequest / Notification → 过闸后推；Stop 及其它 → 只写日志。

关键：不再用 `waitforme.py send --urgency high`（那会跳过静默/180s/20 条上限）。
现在每次推送都经过 `policy.gate` 的同一套判定：
  事件类型 → 内容去重（dedup_window_s）→ 静默时段 → 同类间隔（min_interval_s）
  → 10 分钟上限 → 当日上限；放行时才预占额度，发送失败会退还。

手工验证：
    echo '{"hook_event_name":"PermissionRequest","message":"要删订单表"}' | python hook_direct.py --debug
    echo '{"hook_event_name":"Notification","message":"同一条"}' | python hook_direct.py --debug  # 再跑一次会被去重
"""

from __future__ import annotations

import argparse
import json
import os
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


def first_number(data: dict, keys: tuple[str, ...]) -> float | None:
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key in keys:
                value = node.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return float(value)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return None


def short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text[:limit] + "…" if len(text) > limit else text


def gather_pairs(data: object) -> list[tuple[str, object]]:
    """把 payload 拍平成 (小写键, 值) 列表，供宽容查找。"""
    pairs: list[tuple[str, object]] = []
    stack: list[object] = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                pairs.append((str(key).lower(), value))
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return pairs


def pick(pairs: list[tuple[str, object]], names: tuple[str, ...]) -> str:
    """按候选键名取第一个非空标量；遇到 dict 时再往里看一层常见字段。"""
    nested_keys = ("command", "cmd", "description", "query", "url", "path", "tool_name", "tool")
    for name in names:
        for key, value in pairs:
            if key != name:
                continue
            if isinstance(value, (str, int, float)) and str(value).strip():
                return str(value).strip()
            if isinstance(value, dict):
                for inner in nested_keys:
                    inner_value = value.get(inner)
                    if isinstance(inner_value, str) and inner_value.strip():
                        return inner_value.strip()
    return ""


def build_message(event: str, data: dict) -> tuple[str, str]:
    """返回 (标题后缀, 正文)：正文要讲清「卡在哪」和「要你决定什么」。"""
    pairs = gather_pairs(data)
    message = pick(pairs, ("message", "msg", "text", "content", "body"))
    last_message = pick(pairs, ("last_assistant_message", "lastassistantmessage",
                                "assistant_message", "response"))
    tool = pick(pairs, ("tool_name", "toolname", "tool"))
    command = pick(pairs, ("command", "cmd", "script", "shell_command", "bash_command"))
    if not command:
        command = pick(pairs, ("tool_input", "toolinput", "input", "arguments", "args",
                               "parameters", "request", "permission"))
    notification_type = pick(pairs, ("notification_type", "notificationtype"))
    cwd = pick(pairs, ("cwd", "working_dir", "workingdir", "project_path", "workdir"))
    session = pick(pairs, ("session_id", "sessionid", "session", "conversation_id"))

    summary = message or last_message
    if event == "PermissionRequest":
        label = tool or ("命令" if command else "")
        title = f"等你授权：{label}" if label else "等你授权"
        what = summary or (f"请求执行 {tool or '一个操作'}" if tool else "Kimi 请求执行一个操作")
        lines = [f"Kimi 卡在这里：{short(what, 120)}"]
        if command:
            lines.append(f"命令：{short(command, 160)}")
        lines += ["", "要你决定：批准还是拒绝？（回「批准」或「拒绝」，也可以直接说你的决定）"]
    elif event == "Notification":
        label = short(summary or notification_type, 16)
        title = f"有新通知：{label}" if label else "有新通知"
        what = summary or "Kimi 发来一条通知"
        lines = [f"Kimi 发来通知：{short(what, 140)}"]
        if notification_type:
            lines.append(f"类型：{notification_type}")
        lines += ["", "要你决定：看一眼，需要的话回一句。"]
    else:
        title = EVENT_TITLE.get(event, event)
        what = summary or "Kimi 有一个动作需要你确认"
        lines = [f"Kimi 卡在这里：{short(what, 140)}", "", "要你决定：看一眼，需要的话回一句。"]

    details: list[str] = []
    if cwd:
        details.append(f"目录：{Path(cwd).name or cwd}")
    if session:
        details.append(f"会话：{session[:8]}")
    if details:
        lines += ["", " / ".join(details)]
    return title, "\n".join(lines)


def push_with_gates(cfg: dict, event: str, title: str, body: str, urgency: str,
                    timeout: float) -> tuple[bool, bool, str, str]:
    """过 policy.gate → channels.send → settle。返回 (attempted, ok, reason, detail)。"""
    import channels
    import linker
    import policy

    fp = policy.fingerprint(title, body)
    gate_event = {
        "hook_event_name": event,
        "fingerprint": fp,
        "title": title,
        "message": body,
        "urgency": urgency,
    }
    decision = policy.gate(gate_event, cfg, kind=event)
    if not decision.get("allowed"):
        return False, False, str(decision.get("reason") or "blocked"), ""

    links = linker.build_links(cfg)
    url = str(links.get("primary_url") or "")
    # 网络超时以 hook 预算为准（send 内部读模块常量）
    channels.NET_TIMEOUT_S = max(1.0, min(float(channels.NET_TIMEOUT_S), timeout))
    result = channels.send(cfg, title, body, url, urgency=urgency)
    policy.settle_push(
        cfg,
        str(decision.get("reserve_id") or ""),
        bool(result.get("ok")),
        channel=str(result.get("channel") or ""),
        status_code=int(result.get("status_code") or 0),
        error=(str(result.get("error")) if result.get("error") else None),
    )
    return True, bool(result.get("ok")), str(decision.get("reason") or ""), str(result.get("error") or "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hook_direct.py", description="Kimi Code hook → waitforme（零常驻 + 三道门）")
    parser.add_argument("--project", default=None, help="waitforme 项目目录（默认自动找）")
    parser.add_argument("--config", default=None, help="config.json 路径（默认 <project>/config.json）")
    parser.add_argument("--timeout", type=float, default=None, help="网络超时秒数（默认 8，可用 WAITFORME_HOOK_TIMEOUT 覆盖）")
    parser.add_argument("--urgency", choices=("normal", "high"), default=None,
                        help="默认 normal（守静默/额度）；high 会跳过静默与同类间隔，慎用")
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

    urgency = args.urgency or os.environ.get("WAITFORME_HOOK_URGENCY") or "normal"
    if urgency not in ("normal", "high"):
        urgency = "normal"

    project = C.resolve_project(args.project)
    record = {
        "ts": ts, "event": event, "action": "log_only", "ok": True,
        "detail": "", "ms": 0, "parse_error": parse_error,
        "urgency": urgency,
        "session": first_text(payload, ("session_id", "sessionId", "session"))[:8] if payload else "",
    }

    if project is None:
        record.update(ok=False, detail="project_not_found")
        if args.debug:
            C.say("[hook-direct] 找不到 waitforme 项目；事件只记日志")
        print(json.dumps(record, ensure_ascii=False), file=sys.stderr)
        return 0

    config = C.config_path_for(project, args.config)
    cfg = C.read_config(config)
    log_dir = Path(os.path.expanduser(str(cfg.get("log_dir") or project / "logs")))

    if not cfg:
        record.update(action="skipped", ok=False, detail=f"config_unreadable:{config}")
        if args.debug:
            C.say(f"[hook-direct] 配置读不到：{config}")
        record["ms"] = int((time.time() - started) * 1000)
        log_line(log_dir, record)
        return 0

    if event not in PUSH_EVENTS:
        if args.debug:
            C.say(f"[hook-direct] {event} → 只记日志（不推）")
        known = event in EVENT_TITLE
        record["detail"] = "not_pushable_event" if known else f"unknown_event:{event}"
        if not known:
            record["warn"] = "未识别的事件名：原样记录、不推送"
        record["ms"] = int((time.time() - started) * 1000)
        log_line(log_dir, record)
        return 0

    # 让项目目录里的模块可导入（policy / channels / linker）
    if str(project) not in sys.path:
        sys.path.insert(0, str(project))

    title_suffix, body = build_message(event, payload)
    title = f"⏳ Kimi 需要你：{title_suffix}"
    try:
        attempted, ok, reason, error = push_with_gates(cfg, event, title, body, urgency, timeout)
    except Exception as exc:  # noqa: BLE001 - 绝不让异常穿给 hook
        attempted, ok, reason, error = True, False, "internal_error", f"{type(exc).__name__}: {exc}"

    record.update(action="push" if attempted else "blocked", ok=ok, reason=reason,
                  detail=error[:200], ms=int((time.time() - started) * 1000))
    if args.debug:
        C.say(f"[hook-direct] {event} → ok={ok} reason={reason} {error[:120]}")
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
