"""policy.py —— 推送判定与防打扰。

这里全是（近似）纯函数：判定靠传入的 `event` / `cfg` / `now`，唯一的副作用是读
`logs/push_log.jsonl` 做历史计数，以及 `record_push()` 追加一行。写日志和发网络的
事不在这里做，方便单测直接构造 cfg 断言。

时间一律本地时间：这套东西是给一个人守着自己电脑用的，跨时区没有意义。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PUSH_LOG_NAME = "push_log.jsonl"
_append_lock = threading.Lock()

# 助手文本里出现这些，说明回合停下是在等人回话，不是自己跑完了
QUESTION_RE = re.compile(r"[?？]|需要你|请选择|要不要|确认|等你|决定")

DEFAULTS: dict[str, Any] = {
    "stop_mode": "smart",
    "stop_min_duration_s": 120,
    "min_interval_s": 180,
    "max_per_10min": 2,
    "daily_limit": 20,
    "quiet_hours": {"start": "23:00", "end": "08:30"},
    "dedup_window_s": 60,
}

# 事件名 → 白名单语义。不在表里的事件默认不推。
PUSHABLE_EVENTS = ("PermissionRequest", "Notification")


def _num(cfg: dict[str, Any], key: str, default: float) -> float:
    """取数值配置；非数字/负数一律回落默认值（配置写错不该导致不推送或狂推送）。"""
    raw = cfg.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        try:
            raw = float(raw)
        except (TypeError, ValueError):
            return default
    value = float(raw)
    return value if value >= 0 else default


def push_log_path(cfg: dict[str, Any] | None = None) -> Path:
    """推送历史所在文件。`cfg["log_dir"]` 可覆盖，便于单测用临时目录。"""
    log_dir = Path(str((cfg or {}).get("log_dir") or HERE / "logs"))
    return log_dir / PUSH_LOG_NAME


def read_push_log(cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """读全部推送记录。坏行跳过、文件不存在返回空 —— 日志永远不该成为故障源。"""
    path = push_log_path(cfg)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def fingerprint(title: str, body: str = "") -> str:
    """内容指纹：sha1(title + "\\n" + body) 前 16 位，用于时间窗内去重。"""
    digest = hashlib.sha1()
    digest.update(f"{title or ''}\n{body or ''}".encode("utf-8", errors="replace"))
    return digest.hexdigest()[:16]


def _event_name(event: dict[str, Any]) -> str:
    for key in ("hook_event_name", "event_name", "event", "hookEvent", "name"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def _event_text(event: dict[str, Any]) -> str:
    """拿来问句匹配 / 指纹的正文：优先最后一条助手消息，其次 message/text。"""
    for key in ("last_assistant_message", "message", "text", "notification_type", "content"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _event_fp(event: dict[str, Any]) -> str:
    if isinstance(event.get("fingerprint"), str) and event["fingerprint"]:
        return event["fingerprint"]
    return fingerprint(str(event.get("title") or ""), _event_text(event))


def _recent(cfg: dict[str, Any], window_s: float, now: float) -> list[dict[str, Any]]:
    return [r for r in read_push_log(cfg) if _rec_ts(r) is not None and now - _rec_ts(r) <= window_s]


def _rec_ts(rec: dict[str, Any]) -> float | None:
    raw = rec.get("ts")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    return None


def _local_date(ts: float) -> str:
    """时间戳 → 本地日期串，当日上限按本地日历日算。"""
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def should_push(event: dict[str, Any], cfg: dict[str, Any], now: float | None = None) -> tuple[bool, str]:
    """事件类型策略：这类事件值不值得打扰用户？

    返回 `(是否推送, 原因)`。原因串是给日志和人看的，不参与流程判断。
    只做「事件种类 + Stop 智能判断 + 60s 内容去重」，频率额度在 `throttle_ok` 里。
    """
    now = time.time() if now is None else float(now)
    cfg = dict(DEFAULTS, **(cfg or {}))
    name = _event_name(event)
    fp = _event_fp(event)
    window = _num(cfg, "dedup_window_s", 60)

    if name in PUSHABLE_EVENTS:
        if _has_fp(cfg, fp, window, now):
            return False, f"{name}_dedup_{int(window)}s"
        return True, name.lower()

    if name == "Stop":
        mode = str(cfg.get("stop_mode") or DEFAULTS["stop_mode"]).strip().lower()
        if mode == "off":
            return False, "stop_off"
        if mode == "always":
            if _has_fp(cfg, fp, window, now):
                return False, "stop_dedup"
            return True, "stop_always"
        # smart：长回合（大概率是真跑完了一大坨）或助手最后在做「问句/等你」的动作
        duration = event.get("duration_s")
        duration = duration if isinstance(duration, (int, float)) and not isinstance(duration, bool) else 0
        min_duration = _num(cfg, "stop_min_duration_s", 120)
        if float(duration) >= min_duration:
            if _has_fp(cfg, fp, window, now):
                return False, "stop_dedup"
            return True, f"stop_smart_duration_{int(duration)}s"
        if _event_text(event) and QUESTION_RE.search(_event_text(event)):
            if _has_fp(cfg, fp, window, now):
                return False, "stop_dedup"
            return True, "stop_smart_question"
        return False, "stop_smart_quiet_turn"

    if name == "SubagentStop":
        return False, "subagent_stop_logged_only"

    return False, f"event_not_pushable:{name}"


def _has_fp(cfg: dict[str, Any], fp: str, window_s: float, now: float) -> bool:
    return any(r.get("fingerprint") == fp for r in _recent(cfg, window_s, now))


def throttle_reason(kind: str, cfg: dict[str, Any], now: float | None = None) -> str | None:
    """被限流时返回原因串，放行时返回 None。`kind` 一般是事件名或 `ask`/`manual`。"""
    now = time.time() if now is None else float(now)
    cfg = dict(DEFAULTS, **(cfg or {}))
    history = read_push_log(cfg)
    valid = [r for r in history if _rec_ts(r) is not None]
    if not valid:
        return None

    same_kind = [r for r in valid if r.get("kind") == kind]
    min_interval = _num(cfg, "min_interval_s", 180)
    if same_kind:
        gap = now - max(_rec_ts(r) for r in same_kind)
        if gap < min_interval:
            return f"min_interval_{int(min_interval)}s_{int(gap)}s_ago"

    per_10min = _num(cfg, "max_per_10min", 2)
    burst = [r for r in valid if now - _rec_ts(r) <= 600]
    if per_10min >= 0 and len(burst) >= per_10min:
        return f"max_per_10min_{int(per_10min)}"

    today = _local_date(now)
    day = [r for r in valid if _local_date(_rec_ts(r)) == today]
    limit = _num(cfg, "daily_limit", 20)
    if limit >= 0 and len(day) >= limit:
        return f"daily_limit_{int(limit)}"
    return None


def throttle_ok(kind: str, cfg: dict[str, Any], now: float | None = None) -> bool:
    """额度判定：同类最小间隔 + 10 分钟突发上限 + 当日上限。三个都过才 True。"""
    return throttle_reason(kind, cfg, now) is None


def _parse_hhmm(raw: Any) -> tuple[int, int] | None:
    if not isinstance(raw, str):
        return None
    match = re.match(r"^\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*$", raw)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def in_quiet_hours(cfg: dict[str, Any], now: float | None = None) -> bool:
    """是否处于免打扰时段。`{"start":"23:00","end":"08:30"}` 这种跨零点的写法必须正确。

    配置缺失或格式不对 → 返回 False（宁可能推，不要因为写错时间把用户变成失联）。
    """
    now = time.time() if now is None else float(now)
    quiet = (cfg or {}).get("quiet_hours")
    if not isinstance(quiet, dict):
        return False
    start = _parse_hhmm(quiet.get("start"))
    end = _parse_hhmm(quiet.get("end"))
    if start is None or end is None:
        return False
    start_m, end_m = start[0] * 60 + start[1], end[0] * 60 + end[1]
    if start_m == end_m:
        return False
    local = time.localtime(now)
    cur = local.tm_hour * 60 + local.tm_min
    if start_m < end_m:
        return start_m <= cur < end_m
    return cur >= start_m or cur < end_m


def urgency_overrides(urgency: Any) -> bool:
    """`high` 表示用户明确要求「一定要吵醒我」，越限静默时段与额度。"""
    return str(urgency or "").strip().lower() == "high"


def gate(event: dict[str, Any], cfg: dict[str, Any], kind: str | None = None, now: float | None = None) -> dict[str, Any]:
    """把三个判定串成一次结论，供 hooks / CLI 复用。

    返回 `{allowed, reason, fingerprint, urgency}`。`urgency=="high"` 时跳过静默时段与
    限流，但**不**跳过事件类型策略 —— 再急也不该把 SubagentStop 推给用户。
    """
    now = time.time() if now is None else float(now)
    cfg = dict(DEFAULTS, **(cfg or {}))
    event = dict(event or {})
    urgency = str(event.get("urgency") or "normal").strip().lower()
    fp = _event_fp(event)
    event_kind = kind or _event_name(event)

    allowed, reason = should_push(event, cfg, now)
    if not allowed:
        return {"allowed": False, "reason": reason, "fingerprint": fp, "urgency": urgency}
    if urgency_overrides(urgency):
        return {"allowed": True, "reason": f"{reason}_high_urgency_skip_limits", "fingerprint": fp, "urgency": urgency}
    if in_quiet_hours(cfg, now):
        return {"allowed": False, "reason": "quiet_hours", "fingerprint": fp, "urgency": urgency}
    blocked = throttle_reason(event_kind, cfg, now)
    if blocked:
        return {"allowed": False, "reason": blocked, "fingerprint": fp, "urgency": urgency}
    return {"allowed": True, "reason": reason, "fingerprint": fp, "urgency": urgency}


def append_line(path: Path, line: str) -> None:
    """整行追加，进程内串行（与 `channels.append_line` 同一套理由）。

    Windows 的 `O_APPEND` 是「seek 到末尾再写」，多线程并发写会互相覆盖丢记录；
    push_log 丢一条，限流就多放行一条骚扰消息，所以必须上锁。
    """
    payload = (line + "\n").encode("utf-8", errors="replace")
    with _append_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o666)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)


def record_push(cfg: dict[str, Any], kind: str, fp: str, channel: str, ok: bool, extra: dict[str, Any] | None = None) -> Path:
    """把一次推送尝试追加进 push_log.jsonl —— 限流计数全靠它，失败也要记。"""
    record = {
        "ts": round(time.time(), 3),
        "local_time": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "fingerprint": fp,
        "channel": channel,
        "ok": bool(ok),
    }
    record.update(extra or {})
    path = push_log_path(cfg)
    append_line(path, json.dumps(record, ensure_ascii=False))
    return path


def count_today(cfg: dict[str, Any] | None = None, now: float | None = None, only_ok: bool = True) -> int:
    """今日（本地日历日）的推送次数。默认只算成功的，`--dry-run` 不入日志所以自然不计。"""
    now = time.time() if now is None else float(now)
    today = _local_date(now)
    total = 0
    for rec in read_push_log(cfg):
        ts = _rec_ts(rec)
        if ts is None or _local_date(ts) != today:
            continue
        if only_ok and not rec.get("ok", False):
            continue
        total += 1
    return total


def last_push_ts(cfg: dict[str, Any] | None = None) -> str:
    """最后一次推送的本地时间 ISO 串，没有记录就空串。"""
    stamps = [_rec_ts(r) for r in read_push_log(cfg)]
    stamps = [s for s in stamps if s is not None]
    if not stamps:
        return ""
    return datetime.fromtimestamp(max(stamps)).isoformat(timespec="seconds")
