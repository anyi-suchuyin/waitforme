"""policy.py —— 推送判定与防打扰。

判定靠传入的 `event` / `cfg` / `now`；唯一的副作用是读写 `logs/push_log.jsonl`
（计数依据 + 额度预占），网络和真正的发送不在这里做，方便单测直接构造 cfg 断言。

两个关键取向：

* **额度是预占的**：判定一过就同步写一条 `reserve` 记录，不等发送结果。否则「读日志 →
  后台线程发 → 发完才写」中间有几秒空窗，同一秒进来的同类事件会全部放行（实测同一
  时刻连续两次 gate 都 allowed）。发送结果再补一条 `result`，失败的额度**退还**。
* **失败不吃额度**：一次断网不该白扣掉当日配额；`ok:false` 的 reserve 会被 refund 掉，
  但记录仍然留在日志里可观测。

时间一律本地时间：这套东西是给一个人守着自己电脑用的，跨时区没有意义。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PUSH_LOG_NAME = "push_log.jsonl"
_append_lock = threading.Lock()
# 「查额度 + 预占」必须是一个动作，否则并发事件会同时看到「还有额度」
_gate_lock = threading.RLock()

# 助手文本里出现这些，说明回合停下是在等人回话，不是自己跑完了
QUESTION_RE = re.compile(r"[?？]|需要你|请选择|要不要|确认|等你|决定")

DEFAULTS: dict[str, Any] = {
    "stop_mode": "smart",
    "stop_min_duration_s": 120,
    "min_interval_s": 180,
    "max_per_10min": 2,
    "daily_limit": 20,
    "quiet_hours": {"start": "23:00", "end": "08:30"},
    "dedup_window_s": 600,
}

# `urgency=="high"` 只越静默时段；额度仍然在，只是放宽成硬上限
HIGH_MIN_INTERVAL_S = 0
HIGH_MAX_PER_10MIN = 5
HIGH_DAILY_LIMIT = 50

# 事件名 → 白名单语义。不在表里的事件默认不推。
PUSHABLE_EVENTS = ("PermissionRequest", "Notification")

EVENT_RESERVE = "reserve"
EVENT_RESULT = "result"


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


def _rec_ts(rec: dict[str, Any]) -> float | None:
    raw = rec.get("ts")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    return None


def _local_date(ts: float) -> str:
    """时间戳 → 本地日期串，当日上限按本地日历日算。"""
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def counted_pushes(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """真正占用额度的推送记录 = 预占（reserve）减去被退还的（result ok:false / 已取消）。

    旧格式记录（没有 `event` 字段）按「一次成型」处理：`ok:true` 计，`ok:false` 不计。
    """
    records = read_push_log(cfg)
    refunded = {
        str(r.get("reserve_id"))
        for r in records
        if r.get("event") == EVENT_RESULT and r.get("ok") is False and r.get("reserve_id")
    }
    # 取消是另写一行（jsonl 不能改历史），所以要按 id 反查把原始预占一起剔掉
    cancelled = {
        str(r.get("reserve_id"))
        for r in records
        if r.get("event") == EVENT_RESERVE and r.get("state") == "cancelled" and r.get("reserve_id")
    }
    out: list[dict[str, Any]] = []
    for rec in records:
        ts = _rec_ts(rec)
        if ts is None:
            continue
        event = rec.get("event")
        if event == EVENT_RESULT:
            continue
        if event == EVENT_RESERVE:
            rid = str(rec.get("reserve_id"))
            if rec.get("state") == "cancelled" or rid in refunded or rid in cancelled:
                continue
            out.append(rec)
        elif event is None:
            if rec.get("ok") is True:
                out.append(rec)
    return out


def _recent(cfg: dict[str, Any], window_s: float, now: float) -> list[dict[str, Any]]:
    return [r for r in counted_pushes(cfg) if now - _rec_ts(r) <= window_s]  # type: ignore[operator]


def _has_fp(cfg: dict[str, Any], fp: str, window_s: float, now: float) -> bool:
    """窗口内是否已经为同一段内容占过额度（含还没发完的 pending，这才是去重该有的样子）。"""
    return any(r.get("fingerprint") == fp for r in _recent(cfg, window_s, now))


def should_push(event: dict[str, Any], cfg: dict[str, Any], now: float | None = None) -> tuple[bool, str]:
    """事件类型策略：这类事件值不值得打扰用户？

    返回 `(是否推送, 原因)`，原因串给日志和人看，不参与流程判断。只管「事件种类 +
    Stop 智能判断 + 窗口内内容去重（默认 600 秒）」，频率额度在 `throttle_ok` 里。
    """
    now = time.time() if now is None else float(now)
    cfg = dict(DEFAULTS, **(cfg or {}))
    name = _event_name(event)
    fp = _event_fp(event)
    window = _num(cfg, "dedup_window_s", 600)

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


def throttle_reason(kind: str, cfg: dict[str, Any], now: float | None = None, urgency: str = "normal") -> str | None:
    """被限流时返回原因串，放行时返回 None。`kind` 一般是事件名或 `ask`/`manual`。

    `urgency=="high"` 不再免检：跳过同类间隔，但 10 分钟与当日仍有更宽的硬上限
    （`HIGH_MAX_PER_10MIN` / `HIGH_DAILY_LIMIT`）。否则 payload 里随便一个
    `"priority":"high"` 就能把用户手机刷爆。
    """
    now = time.time() if now is None else float(now)
    cfg = dict(DEFAULTS, **(cfg or {}))
    high = urgency_overrides(urgency)
    valid = counted_pushes(cfg)
    if not valid:
        return None

    if not high:
        same_kind = [r for r in valid if r.get("kind") == kind]
        min_interval = _num(cfg, "min_interval_s", 180)
        if same_kind and min_interval > 0:
            gap = max(0.0, now - max(_rec_ts(r) for r in same_kind))  # type: ignore[type-var]
            if gap < min_interval:
                return f"min_interval_{int(min_interval)}s_{int(gap)}s_ago"

    if high:
        per_10min, day_limit = float(HIGH_MAX_PER_10MIN), float(HIGH_DAILY_LIMIT)
    else:
        per_10min, day_limit = _num(cfg, "max_per_10min", 2), _num(cfg, "daily_limit", 20)

    burst = [r for r in valid if now - _rec_ts(r) <= 600]  # type: ignore[operator]
    if len(burst) >= per_10min:
        return f"max_per_10min_{int(per_10min)}"

    today = _local_date(now)
    day = [r for r in valid if _local_date(_rec_ts(r)) == today]  # type: ignore[arg-type]
    if len(day) >= day_limit:
        return f"daily_limit_{int(day_limit)}"
    return None


def throttle_ok(kind: str, cfg: dict[str, Any], now: float | None = None, urgency: str = "normal") -> bool:
    """额度判定：同类最小间隔 + 10 分钟突发上限 + 当日上限。三个都过才 True。"""
    return throttle_reason(kind, cfg, now, urgency) is None


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
    """`high` 只意味着「可以越静默时段」，不再意味着「无限推送」。"""
    return str(urgency or "").strip().lower() == "high"


def reserve_push(cfg: dict[str, Any], kind: str, fp: str, urgency: str = "normal", extra: dict[str, Any] | None = None) -> str:
    """占一条额度并返回 `reserve_id`。判定通过后必须**立刻**调它，别等发完再记。

    占位记录里带上 kind/fingerprint，所以后续的同类间隔、突发、当日、去重四项都看得到。
    """
    reserve_id = uuid.uuid4().hex[:16]
    record = {
        "ts": round(time.time(), 3),
        "local_time": datetime.now().isoformat(timespec="seconds"),
        "event": EVENT_RESERVE,
        "reserve_id": reserve_id,
        "kind": kind,
        "fingerprint": fp,
        "urgency": urgency,
    }
    record.update(extra or {})
    append_line(push_log_path(cfg), json.dumps(record, ensure_ascii=False))
    return reserve_id


def settle_push(cfg: dict[str, Any], reserve_id: str, ok: bool, channel: str = "", status_code: int = 0,
                error: str | None = None, extra: dict[str, Any] | None = None) -> None:
    """回填发送结果。`ok:false` 会把这条 reserve 的额度**退还**，失败不该白扣配额。"""
    record = {
        "ts": round(time.time(), 3),
        "local_time": datetime.now().isoformat(timespec="seconds"),
        "event": EVENT_RESULT,
        "reserve_id": reserve_id,
        "ok": bool(ok),
        "channel": channel,
        "status_code": int(status_code),
        "error": error,
    }
    record.update(extra or {})
    append_line(push_log_path(cfg), json.dumps(record, ensure_ascii=False))


def cancel_reserve(cfg: dict[str, Any], reserve_id: str, reason: str = "cancelled") -> None:
    """显式退还一条预占（例如发送前就发现消息组不出来）。"""
    record = {
        "ts": round(time.time(), 3),
        "event": EVENT_RESERVE,
        "reserve_id": reserve_id,
        "state": "cancelled",
        "kind": "__cancelled__",
        "reason": reason,
    }
    append_line(push_log_path(cfg), json.dumps(record, ensure_ascii=False))


def record_push(cfg: dict[str, Any], kind: str, fp: str, channel: str, ok: bool, extra: dict[str, Any] | None = None) -> Path:
    """一次性记录（预占 + 结果同一对），给不走 gate 的调用留的兼容入口。"""
    reserve_id = reserve_push(cfg, kind, fp, extra={"channel": channel} if channel else None)
    settle_push(cfg, reserve_id, ok, channel=channel, extra=extra)
    return push_log_path(cfg)


def gate(event: dict[str, Any], cfg: dict[str, Any], kind: str | None = None, now: float | None = None,
         reserve: bool = True) -> dict[str, Any]:
    """把三道判定串成一次结论，并在放行的**同一瞬间**预占额度。

    返回 `{allowed, reason, fingerprint, urgency, reserve_id}`。`reserve=False` 时只判不占，
    给「先看看会不会推」的诊断用。
    """
    now = time.time() if now is None else float(now)
    cfg = dict(DEFAULTS, **(cfg or {}))
    event = dict(event or {})
    urgency = str(event.get("urgency") or "normal").strip().lower()
    fp = _event_fp(event)
    event_kind = kind or _event_name(event)

    with _gate_lock:
        allowed, reason = should_push(event, cfg, now)
        if not allowed:
            return {"allowed": False, "reason": reason, "fingerprint": fp, "urgency": urgency, "reserve_id": None}
        if not urgency_overrides(urgency) and in_quiet_hours(cfg, now):
            return {"allowed": False, "reason": "quiet_hours", "fingerprint": fp, "urgency": urgency, "reserve_id": None}
        blocked = throttle_reason(event_kind, cfg, now, urgency)
        if blocked:
            return {"allowed": False, "reason": blocked, "fingerprint": fp, "urgency": urgency, "reserve_id": None}
        reserve_id = reserve_push(cfg, event_kind, fp, urgency, {"title": str(event.get("title") or "")[:80]}) if reserve else None
    suffix = "_high_skip_quiet_hours" if urgency_overrides(urgency) else ""
    return {"allowed": True, "reason": f"{reason}{suffix}", "fingerprint": fp, "urgency": urgency, "reserve_id": reserve_id}


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


def count_today(cfg: dict[str, Any] | None = None, now: float | None = None, only_ok: bool = True) -> int:
    """今日（本地日历日）占用的推送额度。

    `only_ok=True`（默认）只算已经**确认发送成功**的；算上限时用的是含 pending 的
    `counted_pushes`，两者口径不同是故意的：状态里给用户看「今天真到了几条」。
    """
    now = time.time() if now is None else float(now)
    today = _local_date(now)
    records = read_push_log(cfg)
    if only_ok:
        succeed = {
            str(r.get("reserve_id"))
            for r in records
            if r.get("event") == EVENT_RESULT and r.get("ok") is True and r.get("reserve_id")
        }
        total = 0
        for rec in records:
            ts = _rec_ts(rec)
            if ts is None or _local_date(ts) != today:
                continue
            if rec.get("event") == EVENT_RESULT:
                continue
            rid = str(rec.get("reserve_id"))
            if rec.get("event") == EVENT_RESERVE:
                if rid in succeed:
                    total += 1
            elif rec.get("ok") is True:
                total += 1
        return total
    return sum(1 for r in counted_pushes(cfg) if _local_date(_rec_ts(r)) == today)


def last_push_ts(cfg: dict[str, Any] | None = None) -> str:
    """最后一次占用额度的推送时间（本地 ISO 串），没有记录就空串。"""
    pushes = counted_pushes(cfg)
    if not pushes:
        return ""
    return datetime.fromtimestamp(max(_rec_ts(r) for r in pushes)).isoformat(timespec="seconds")  # type: ignore[type-var]
