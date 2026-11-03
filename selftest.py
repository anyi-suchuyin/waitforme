"""selftest.py —— waitforme 离线自测。

默认 `--dry-run`：全程本地断言，不联网（mock 通道 + 临时目录）。`--live` 额外真发一条
自检消息，通道没配 key 就 skip 并说明。

隔离方式：所有临时状态写在系统临时目录里，通过 `WAITFORME_CONFIG` 环境变量把整套配置
（含 log_dir / run_dir / inbox_dir）指过去。跑自测不会污染真实 `logs/`，也不会误停
正在服务的 hooks。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import channels  # noqa: E402
import hooks as hooks_module  # noqa: E402
import linker  # noqa: E402
import policy  # noqa: E402
import waitforme  # noqa: E402

hooks_redact = hooks_module.redact_token

PASS = 0
FAIL = 0
FAILED_NAMES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILED_NAMES.append(name)
        print(f"  FAIL  {name}  {detail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def local_ts(hour: int, minute: int) -> float:
    """构造「今天 hour:minute」的本地时间戳，用来测静默时段而不真的等到半夜。"""
    return datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()


def make_cfg(tmp: Path, **overrides) -> dict:
    cfg = json.loads(json.dumps(waitforme.DEFAULT_CONFIG))
    cfg["log_dir"] = str(tmp / "logs")
    cfg["run_dir"] = str(tmp / "run")
    cfg["inbox_dir"] = str(tmp / "inbox")
    cfg["rc_json_path"] = str(tmp / "rc.json")
    cfg["fallback_url"] = "http://192.168.1.10:8791/"
    cfg["hook_token"] = "test-token"
    # 静默时段设成 start==end（等于关闭），免得自测在半夜跑就全红
    cfg["quiet_hours"] = {"start": "00:00", "end": "00:00"}
    cfg.update(overrides)
    (tmp / "logs").mkdir(parents=True, exist_ok=True)
    (tmp / "run").mkdir(parents=True, exist_ok=True)
    (tmp / "inbox").mkdir(parents=True, exist_ok=True)
    return cfg


def write_tmp_config(tmp: Path, cfg: dict) -> Path:
    path = tmp / "config.json"
    disk = {k: v for k, v in cfg.items()}
    path.write_text(json.dumps(disk, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# 1. payload 校验
# ---------------------------------------------------------------------------

VALID_PAYLOAD = {
    "id": "ask-20261031-01",
    "title": "billing 历史数据动不动",
    "situation": "重构 billing 模块时纠结要不要保留历史数据读取路径。",
    "options": [
        {"key": "A", "label": "稳妥重构", "tradeoff": "多2天，不动历史数据"},
        {"key": "B", "label": "快删旧脚本", "tradeoff": "半天上线，历史订单可能读不到旧字段"},
    ],
    "default_if_no_answer": "A",
    "urgency": "normal",
}

INVALID_CASES = [
    ("title 为空", {"id": "x", "title": "", "situation": "s", "options": VALID_PAYLOAD["options"]}, "title"),
    ("situation 为空", {"id": "x", "title": "t", "situation": "", "options": VALID_PAYLOAD["options"]}, "situation"),
    ("options 少于 2 项", {"id": "x", "title": "t", "situation": "s", "options": [{"key": "A", "label": "a"}]}, "options"),
    ("options key 重复", {"id": "x", "title": "t", "situation": "s", "options": [
        {"key": "A", "label": "a"}, {"key": "A", "label": "b"}]}, "key 重复"),
    ("label 为空", {"id": "x", "title": "t", "situation": "s", "options": [
        {"key": "A", "label": ""}, {"key": "B", "label": "b"}]}, "label"),
    ("default 不在 options", {**VALID_PAYLOAD, "default_if_no_answer": "Z"}, "default_if_no_answer"),
    ("urgency 非法", {**VALID_PAYLOAD, "urgency": "asap"}, "urgency"),
    ("payload 不是对象", [1, 2, 3], "JSON 对象"),
]


def test_payload_validation() -> None:
    section("payload 校验")
    payload, reason = waitforme.validate_payload(VALID_PAYLOAD)
    check("合法 payload 通过", payload is not None, reason)
    if payload:
        check("合法 payload 保留 id/title", payload["id"] == "ask-20261031-01" and payload["title"] == "billing 历史数据动不动", str(payload))
        check("合法 payload 选项数=2", len(payload["options"]) == 2)
        check("合法 payload default=A", payload["default_if_no_answer"] == "A")

    for name, raw, expect_keyword in INVALID_CASES:
        bad, reason = waitforme.validate_payload(raw)
        check(f"非法：{name}", bad is None and expect_keyword in reason, f"reason={reason!r}")

    # === E1 另一半：入口就得挡住超长，别把截断赌在出口 ===
    long_cases = [
        ("title 超 40 字", {**VALID_PAYLOAD, "title": "长" * 41}, "title 超长"),
        ("situation 超 2000 字", {**VALID_PAYLOAD, "situation": "情" * 2001}, "situation 超长"),
        ("tradeoff 超 200 字", {**VALID_PAYLOAD, "options": [
            {"key": "A", "label": "a", "tradeoff": "代" * 201}, {"key": "B", "label": "b"}]}, "tradeoff 超长"),
        ("options 超过 8 项", {**VALID_PAYLOAD, "options": [{"key": chr(65 + i), "label": f"选{i}"} for i in range(9)]}, "options 太多"),
        ("id 超 80 字", {**VALID_PAYLOAD, "id": "x" * 81}, "id 超长"),
        ("label 超 40 字", {**VALID_PAYLOAD, "options": [
            {"key": "A", "label": "标" * 41}, {"key": "B", "label": "b"}]}, "label 超长"),
    ]
    for name, raw, keyword in long_cases:
        bad, reason = waitforme.validate_payload(raw)
        check(f"非法：{name}", bad is None and keyword in reason, f"reason={reason!r}")
    ok_len, _ = waitforme.validate_payload({**VALID_PAYLOAD, "title": "长" * 40, "situation": "情" * 2000})
    check("刚好在上限内仍通过", ok_len is not None)

    # default_if_no_answer 留空 → 取第一个
    payload, _ = waitforme.validate_payload({**VALID_PAYLOAD, "default_if_no_answer": ""})
    check("default 留空取第一项", payload is not None and payload["default_if_no_answer"] == "A", str(payload))
    # urgency 留空 → normal
    payload, _ = waitforme.validate_payload({**VALID_PAYLOAD, "urgency": ""})
    check("urgency 留空默认 normal", payload is not None and payload["urgency"] == "normal", str(payload))


# ---------------------------------------------------------------------------
# 2. markdown 组装
# ---------------------------------------------------------------------------


def test_markdown() -> None:
    section("markdown 组装")
    payload, _ = waitforme.validate_payload(VALID_PAYLOAD)
    body = waitforme.ask_body_md(payload, deadline_s=600)
    check("正文含情境", "重构 billing" in body)
    check("正文含选项 A/B 标签", "稳妥重构" in body and "快删旧脚本" in body)
    check("正文含取舍", "多2天" in body and "半天上线" in body)
    check("正文含默认项", "默认 A" in body, body)
    check("正文含截止提示", "600 秒后截止" in body)
    check("企微排版不用星号加粗", "**" not in body, body)

    content = channels.build_content("⏳ 要你拍板：billing 历史数据动不动", body, "http://x.example/rc", "normal")
    check("完整消息含标题", content.startswith("### ⏳ 要你拍板：billing 历史数据动不动"), content[:60])
    check("完整消息含链接", "[👉 点击打开会话继续对话](http://x.example/rc)" in content)
    check("完整消息含选项", "稳妥重构" in content)
    check("high 标题加红点", channels.build_content("t", "b", "u", "high").startswith("### 🔴"))
    no_url = channels.build_content("t", "b", "", "normal")
    check("无链接时给提示而非空链接", "没有可用跳转链接" in no_url and "](" not in no_url)
    # === E1 回归：截断绝不能吃掉链接（审核实测 8519 字节正文截完链接整段消失）===
    url = "https://code-rc.kimi.com/devices/dev-1/?rc=1&from=kimi_code_cli"
    wecom_cap = channels.channel_limits("wecom_bot")["max_bytes"]
    for size, label in ((2000, "2KB 正文"), (8519 // 3, "8.5KB 正文"), (50000 // 3, "50KB 正文")):
        big = channels.build_content("⏳ Kimi 需要你：长正文", "字" * size, url, "normal", max_bytes=wecom_cap)
        check(f"{label}：仍在 {wecom_cap} 字节预算内", len(big.encode("utf-8")) <= wecom_cap, str(len(big.encode("utf-8"))))
        check(f"{label}：链接没被截掉", url in big and "[👉" in big, big[-80:])
    huge = channels.build_content("标题", "啊" * 100000, url, "normal", max_bytes=300)
    check("预算极小时也保住链接（宁可丢正文）", url in huge and len(huge.encode("utf-8")) <= 300, f"{len(huge.encode('utf-8'))} 字节")
    check("正文过长时留截断提示", "…（内容过长已截断）" in channels.build_content("t", "字" * 4000, url, "normal", max_bytes=1000))
    no_body = channels.build_content("标题", "", url, "normal", max_bytes=wecom_cap)
    check("空正文只剩标题与链接时不出多余空行", "\n\n\n" not in no_body, repr(no_body[-60:]))

    # === W12：出本机的内容先脱敏 ===
    leaky = channels.build_content("标题", "密钥 sk-abcdefghijklmnop1234 和 token=abcdefghijklmnop1234567890 还有 webhook?key=SUPERSECRETKEY123", url, "normal")
    check("sk- 密钥被打码", "sk-abcdefghijklmnop1234" not in leaky and "sk-***" in leaky, leaky)
    check("key= 参数值被打码", "SUPERSECRETKEY123" not in leaky)
    check("脱敏不伤跳转链接", url in leaky)
    conn = channels.build_content("t", "postgres://user:suppersecret@10.0.0.9/db", url, "normal")
    check("连接串口令被打码", "suppersecret" not in conn, conn)
    check("redact_secrets 对空串安全", channels.redact_secrets("") == "")


# ---------------------------------------------------------------------------
# 3. linker
# ---------------------------------------------------------------------------


def test_linker(tmp: Path) -> None:
    section("linker.build_links")
    cfg = make_cfg(tmp)
    good = {"pid": os.getpid(), "device_id": "dev-1", "url": "https://code-rc.kimi.com/devices/dev-1/?rc=1&from=kimi_code_cli"}
    Path(cfg["rc_json_path"]).write_text(json.dumps(good), encoding="utf-8")
    links = linker.build_links(cfg)
    check("正常 rc.json → source=rc", links["source"] == "rc", str(links))
    check("正常 rc.json 取到 url", links["primary_url"] == good["url"])
    check("正常 rc.json 带 fallback_url", links["fallback_url"] == "http://192.168.1.10:8791/")

    missing = make_cfg(tmp / "missing", rc_json_path=str(tmp / "nope" / "rc.json"))
    links = linker.build_links(missing)
    check("文件缺失 → source=fallback", links["source"] == "fallback", str(links))
    check("文件缺失 → primary 落到 fallback", links["primary_url"] == "http://192.168.1.10:8791/")

    broken = make_cfg(tmp / "broken")
    Path(broken["rc_json_path"]).write_text("{不是合法 JSON", encoding="utf-8")
    links = linker.build_links(broken)
    check("JSON 损坏 → source=fallback 且不抛异常", links["source"] == "fallback", str(links))

    empty = make_cfg(tmp / "empty")
    Path(empty["rc_json_path"]).write_text("", encoding="utf-8")
    check("空文件 → fallback", linker.build_links(empty)["source"] == "fallback")

    nourl = make_cfg(tmp / "nourl")
    Path(nourl["rc_json_path"]).write_text(json.dumps({"pid": 1}), encoding="utf-8")
    check("缺 url 字段 → fallback", linker.build_links(nourl)["source"] == "fallback")

    # info 回归：合法但带前导空格的 url 不该被当成非法
    padded = make_cfg(tmp / "padded")
    Path(padded["rc_json_path"]).write_text(json.dumps({"pid": os.getpid(), "url": "  https://code-rc.kimi.com/devices/pad/  "}), encoding="utf-8")
    links = linker.build_links(padded)
    check("带前导空格的 url 仍走 rc", links["source"] == "rc", str(links))
    check("空格被 strip 干净", links["primary_url"] == "https://code-rc.kimi.com/devices/pad/", links["primary_url"])
    nonhttp = make_cfg(tmp / "nonhttp")
    Path(nonhttp["rc_json_path"]).write_text(json.dumps({"url": "ftp://x"}), encoding="utf-8")
    check("非 http(s) 协议 → fallback", linker.build_links(nonhttp)["source"] == "fallback")
    check("cfg 传 None 不抛", linker.build_links(None)["source"] in ("rc", "fallback"))

    check("rc_is_alive 对活 pid 为真", linker.rc_is_alive(cfg) is True)
    dead = make_cfg(tmp / "dead")
    Path(dead["rc_json_path"]).write_text(json.dumps({"pid": 999999, "url": "https://x"}), encoding="utf-8")
    check("rc_is_alive 对不存在 pid 为假", linker.rc_is_alive(dead) is False)
    Path(dead["rc_json_path"]).write_text("{bad", encoding="utf-8")
    check("rc_is_alive 文件坏了也不抛", linker.rc_is_alive(dead) is False)
    check("pid_alive 拒绝垃圾输入", linker.pid_alive("abc") is False and linker.pid_alive(None) is False and linker.pid_alive(0) is False)


# ---------------------------------------------------------------------------
# 4. policy
# ---------------------------------------------------------------------------


def seed_pushes(cfg: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    """直接铺 push_log.jsonl 历史：每行 `{kind, fp, age_s, ok, urgency}`。

    自测要精确控制「多久前推过」，所以不靠真实时钟慢慢等；一次推送 = reserve + result 一对，
    与 `policy.reserve_push/settle_push` 写出来的形状完全一致。
    """
    lines: list[str] = []
    now = time.time()
    for index, row in enumerate(rows):
        rid = f"seed{index}"
        ts = round(now - float(row.get("age_s", 0)), 3)
        lines.append(json.dumps({
            "ts": ts, "event": "reserve", "reserve_id": rid, "kind": row.get("kind", "Notification"),
            "fingerprint": row.get("fp", f"fp{index}"), "urgency": row.get("urgency", "normal"),
        }, ensure_ascii=False))
        lines.append(json.dumps({
            "ts": ts + 0.5, "event": "result", "reserve_id": rid, "ok": bool(row.get("ok", True)),
            "channel": "mock", "status_code": 200 if row.get("ok", True) else 0,
        }, ensure_ascii=False))
    path = policy.push_log_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def test_policy(tmp: Path) -> None:
    section("policy")

    # --- Stop smart：问句命中 / 不命中
    cfg = make_cfg(tmp / "stop")
    hit = {"hook_event_name": "Stop", "duration_s": 5, "last_assistant_message": "这个要不要改成 B？"}
    ok, reason = policy.should_push(hit, cfg)
    check("Stop smart 命中问句 → 推", ok is True and reason == "stop_smart_question", reason)
    miss = {"hook_event_name": "Stop", "duration_s": 5, "last_assistant_message": "已经写完了三个文件"}
    ok, reason = policy.should_push(miss, cfg)
    check("Stop smart 未命中且短回合 → 不推", ok is False and reason == "stop_smart_quiet_turn", reason)
    longturn = {"hook_event_name": "Stop", "duration_s": 300, "last_assistant_message": "编译通过"}
    ok, reason = policy.should_push(longturn, cfg)
    check("Stop smart 长回合 → 推", ok is True and reason.startswith("stop_smart_duration"), reason)
    check("Stop 阈值可配（300s < 600 门限则不推）", policy.should_push(longturn, make_cfg(tmp / "stop2", stop_min_duration_s=600))[0] is False)
    ok, reason = policy.should_push({"hook_event_name": "Stop", "duration_s": 300, "text": "好了"}, make_cfg(tmp / "stop3", stop_mode="always"))
    check("stop_mode=always → 推", ok is True and reason == "stop_always", reason)
    ok, reason = policy.should_push({"hook_event_name": "Stop", "duration_s": 300}, make_cfg(tmp / "stop4", stop_mode="off"))
    check("stop_mode=off → 不推", ok is False and reason == "stop_off", reason)
    ok, reason = policy.should_push({"hook_event_name": "SubagentStop"}, cfg)
    check("SubagentStop → 默认不推", ok is False and "subagent_stop" in reason, reason)
    ok, reason = policy.should_push({"hook_event_name": "PreToolUse"}, cfg)
    check("其它事件 → 不推", ok is False and "not_pushable" in reason, reason)
    ok, reason = policy.should_push({"hook_event_name": "PermissionRequest", "message": "允许执行 rm?"}, cfg)
    check("PermissionRequest → 推", ok is True, reason)
    ok, reason = policy.should_push({"hook_event_name": "Notification", "message": "任务完成"}, cfg)
    check("Notification → 推", ok is True, reason)

    # --- W5：去重窗口 10 分钟（方案要求），而且用户改得了
    cfg = make_cfg(tmp / "dedup")
    event = {"hook_event_name": "PermissionRequest", "message": "允许跑 build?"}
    fp = policy.fingerprint("", "允许跑 build?")
    check("第一次放行", policy.should_push(event, cfg)[0] is True)
    seed_pushes(cfg, [{"kind": "PermissionRequest", "fp": fp, "age_s": 30}])
    check("30s 内同指纹去重", policy.should_push(event, cfg)[0] is False)
    seed_pushes(cfg, [{"kind": "PermissionRequest", "fp": fp, "age_s": 590}])
    check("590s（10 分钟内）仍去重 —— 窗口不是 60s", policy.should_push(event, cfg)[0] is False, "dedup 窗口应为 600s")
    seed_pushes(cfg, [{"kind": "PermissionRequest", "fp": fp, "age_s": 660}])
    check("660s 后可再推", policy.should_push(event, cfg)[0] is True)
    check("dedup_window_s 默认 600", policy.DEFAULTS["dedup_window_s"] == 600)
    check("dedup_window_s 写进了配置模板", "dedup_window_s" in json.loads((HERE / "config.example.json").read_text(encoding="utf-8")))
    check("dedup_window_s 在 DEFAULT_CONFIG 里", "dedup_window_s" in waitforme.DEFAULT_CONFIG)
    seed_pushes(cfg, [{"kind": "PermissionRequest", "fp": fp, "age_s": 120}])
    check("dedup_window_s 可配成 60s", policy.should_push(event, dict(cfg, dedup_window_s=60))[0] is True)
    seed_pushes(cfg, [{"kind": "PermissionRequest", "fp": fp, "age_s": 30, "ok": False}])
    check("失败的那条不占去重窗口（可以重试）", policy.should_push(event, cfg)[0] is True)
    check("fingerprint 稳定且 16 位", policy.fingerprint("a", "b") == policy.fingerprint("a", "b") and len(policy.fingerprint("a", "b")) == 16)
    check("fingerprint 区分内容", policy.fingerprint("a", "b") != policy.fingerprint("a", "c"))

    # --- 静默时段（跨零点）
    quiet_cfg = {"quiet_hours": {"start": "23:00", "end": "08:30"}}
    check("23:30 在静默时段", policy.in_quiet_hours(quiet_cfg, local_ts(23, 30)) is True)
    check("07:00 在静默时段", policy.in_quiet_hours(quiet_cfg, local_ts(7, 0)) is True)
    check("12:00 不在静默时段", policy.in_quiet_hours(quiet_cfg, local_ts(12, 0)) is False)
    check("23:00 整点算进入", policy.in_quiet_hours(quiet_cfg, local_ts(23, 0)) is True)
    check("08:30 整点算结束", policy.in_quiet_hours(quiet_cfg, local_ts(8, 30)) is False)
    check("08:29 仍在静默", policy.in_quiet_hours(quiet_cfg, local_ts(8, 29)) is True)
    same = {"quiet_hours": {"start": "03:00", "end": "03:00"}}
    check("start==end 视为不静默", policy.in_quiet_hours(same, local_ts(3, 0)) is False)
    day = {"quiet_hours": {"start": "01:00", "end": "02:00"}}
    check("不跨零点的区间也正确", policy.in_quiet_hours(day, local_ts(1, 30)) is True and policy.in_quiet_hours(day, local_ts(2, 30)) is False)
    check("坏配置不静默也不抛", policy.in_quiet_hours({"quiet_hours": {"start": "abc", "end": "08:30"}}, local_ts(23, 30)) is False)
    check("缺配置不静默", policy.in_quiet_hours({}, local_ts(23, 30)) is False)

    # --- 限流：连发 5 次只放 2 次（max_per_10min=2）
    cfg = make_cfg(tmp / "throttle", min_interval_s=0, max_per_10min=2, daily_limit=99)
    allowed = 0
    for i in range(5):
        if policy.throttle_ok("Notification", cfg):
            allowed += 1
            rid = policy.reserve_push(cfg, "Notification", policy.fingerprint("t", str(i)), "normal")
            policy.settle_push(cfg, rid, True, channel="mock")
    check("连发 5 次只放 2 次", allowed == 2, f"allowed={allowed}")

    cfg = make_cfg(tmp / "interval", min_interval_s=180, max_per_10min=99, daily_limit=99)
    check("同类间隔未到 → 不放行", policy.throttle_ok("Notification", cfg) is True)
    seed_pushes(cfg, [{"kind": "Notification", "fp": "fp1", "age_s": 10}])
    check("同类间隔已到 → 拦截", policy.throttle_ok("Notification", cfg) is False)
    check("换个类别仍可放行", policy.throttle_ok("Stop", cfg) is True)
    now = time.time()
    aged_cfg = make_cfg(tmp / "interval_aged", min_interval_s=30, max_per_10min=99, daily_limit=99)
    seed_pushes(aged_cfg, [{"kind": "Notification", "fp": "fp", "age_s": 45}])
    check("间隔过完后放行", policy.throttle_ok("Notification", aged_cfg, now) is True)

    cfg = make_cfg(tmp / "daily", min_interval_s=0, max_per_10min=99, daily_limit=3)
    day_allowed = 0
    for i in range(6):
        if policy.throttle_ok("Notification", cfg):
            day_allowed += 1
            rid = policy.reserve_push(cfg, "Notification", f"fp-{i}", "normal")
            policy.settle_push(cfg, rid, True, channel="mock")
    check("当日上限生效（3 次）", day_allowed == 3, f"allowed={day_allowed}")

    # === E3 回归：失败不吃额度，但记录留着可观测 ===
    cfg = make_cfg(tmp / "e3", min_interval_s=0, max_per_10min=99, daily_limit=3)
    seed_pushes(cfg, [{"kind": "Notification", "fp": f"x{i}", "ok": False} for i in range(3)])
    check("3 条失败不吃当日上限", policy.throttle_ok("Notification", cfg) is True, "断网一次就白扣配额是不可接受的")
    check("失败记录仍在日志里可查", len(policy.read_push_log(cfg)) == 6, str(len(policy.read_push_log(cfg))))
    check("count_today 只算成功", policy.count_today(cfg) == 0)
    check("count_today(only_ok=False) 算占用中的额度", policy.count_today(cfg, only_ok=False) == 0)
    seed_pushes(cfg, [{"kind": "Notification", "fp": f"x{i}", "ok": True} for i in range(3)])
    check("成功 3 条后才触顶", policy.throttle_ok("Notification", cfg) is False)
    cfg = make_cfg(tmp / "e3b", min_interval_s=180, max_per_10min=99, daily_limit=99)
    seed_pushes(cfg, [{"kind": "Notification", "fp": "f1", "ok": False, "age_s": 5}])
    check("失败的同类不占最小间隔", policy.throttle_ok("Notification", cfg) is True)
    cfg = make_cfg(tmp / "e3c", min_interval_s=180, max_per_10min=99, daily_limit=99)
    seed_pushes(cfg, [{"kind": "Notification", "fp": "f1", "ok": True, "age_s": 5}])
    check("成功的同类仍占最小间隔", policy.throttle_ok("Notification", cfg) is False)

    # === W6 回归：额度是预占的，同刻并发不会一起放行 ===
    cfg = make_cfg(tmp / "w6", min_interval_s=180, max_per_10min=2, daily_limit=99)
    d1 = policy.gate({"hook_event_name": "Notification", "message": "并发一"}, cfg, kind="Notification", now=time.time())
    d2 = policy.gate({"hook_event_name": "Notification", "message": "并发二"}, cfg, kind="Notification", now=time.time())
    check("gate 返回 reserve_id", bool(d1.get("reserve_id")), str(d1))
    check("同刻第二条被预占的同类间隔挡下", d1["allowed"] is True and d2["allowed"] is False, f"{d1} / {d2}")
    policy.settle_push(cfg, d1["reserve_id"], False, channel="mock", error="network_error:Timeout@x")
    d3 = policy.gate({"hook_event_name": "Notification", "message": "并发二"}, cfg, kind="Notification", now=time.time() + 1)
    check("失败退还后同内容可重试", d3["allowed"] is True, str(d3))
    # 还没发完的 pending 也要算进额度，否则空窗期照样放行
    cfg = make_cfg(tmp / "w6b", min_interval_s=0, max_per_10min=2, daily_limit=99)
    p1 = policy.reserve_push(cfg, "Notification", "fp-p1", "normal")
    check("1 条 pending 就占额度（还差 1 到顶）", policy.throttle_ok("Notification", cfg) is True)
    p2 = policy.reserve_push(cfg, "Notification", "fp-p2", "normal")
    check("2 条 pending 顶满突发上限", policy.throttle_ok("Notification", cfg) is False, "预占没回填也该算，不然空窗期会一起放行")
    policy.settle_push(cfg, p2, False, channel="mock", error="network_error:Timeout@x")
    check("pending 失败回填后额度退回", policy.throttle_ok("Notification", cfg) is True)
    policy.settle_push(cfg, p1, True, channel="mock", status_code=200)
    p3 = policy.reserve_push(cfg, "Notification", "fp-p3", "normal")
    policy.settle_push(cfg, p3, True, channel="mock", status_code=200)
    check("成功的那两条额度不退还", policy.throttle_ok("Notification", cfg) is False)
    cfg = make_cfg(tmp / "w6c", min_interval_s=0, max_per_10min=99, daily_limit=99)
    rid = policy.reserve_push(cfg, "Notification", "fp-x", "normal")
    policy.cancel_reserve(cfg, rid, "queue_full")
    check("显式 cancel 也退还额度", len(policy.counted_pushes(cfg)) == 0)
    check("gate(reserve=False) 只判不占", policy.gate({"hook_event_name": "Notification", "message": "诊断"},
          make_cfg(tmp / "w6d"), kind="Notification", reserve=False).get("reserve_id") is None)

    # === W8 回归：high 只越静默时段，不再无限推 ===
    cfg = make_cfg(tmp / "w8", min_interval_s=180, max_per_10min=2, daily_limit=99,
                   quiet_hours={"start": "23:00", "end": "08:30"})
    night = local_ts(23, 30)
    high = {"hook_event_name": "Notification", "message": "紧急一", "urgency": "high"}
    check("high 越静默时段", policy.gate(high, cfg, kind="Notification", now=night)["allowed"] is True)
    # 注意用真实 now：预占记录写的是墙上时钟，拿未来的假 now 去比会把它们全当成「很久以前」。
    # 也另起一份 cfg：上面那次「high 越静默时段」已经占掉一格额度，混在一起数会算错口径。
    cfg_high = make_cfg(tmp / "w8burst", min_interval_s=180, max_per_10min=2, daily_limit=99,
                        quiet_hours={"start": "23:00", "end": "08:30"})
    burst_high = 0
    for i in range(8):
        d = policy.gate(dict(high, message=f"紧急{i}"), cfg_high, kind="Notification")
        if d["allowed"]:
            burst_high += 1
            policy.settle_push(cfg_high, d["reserve_id"], True, channel="mock")
    check("high 也受 10 分钟硬上限（≤5）", burst_high == 5, f"放行了 {burst_high} 条")
    check("high 的硬上限与配置里的 2 无关", burst_high > 2, str(burst_high))
    cfg2 = make_cfg(tmp / "w8b", min_interval_s=0, max_per_10min=99, daily_limit=2,
                    quiet_hours={"start": "00:00", "end": "00:00"})
    seed_pushes(cfg2, [{"kind": "Notification", "fp": f"a{i}", "urgency": "high"} for i in range(50)])
    check("high 有当日硬上限（≤50）", policy.throttle_ok("Notification", cfg2, urgency="high") is False)
    check("high 不越事件类型策略", policy.gate({"hook_event_name": "SubagentStop", "urgency": "high"}, cfg, now=night)["allowed"] is False)

    # --- gate：静默时段拦普通、high 越限、不放过非法事件
    cfg = make_cfg(tmp / "gate", quiet_hours={"start": "23:00", "end": "08:30"}, min_interval_s=0, max_per_10min=99, daily_limit=99)
    night = local_ts(23, 30)
    plain = {"hook_event_name": "Notification", "message": "普通通知", "urgency": "normal"}
    check("静默时段拦 normal", policy.gate(plain, cfg, now=night)["allowed"] is False)
    urgent = dict(plain, message="紧急通知", urgency="high")
    check("high 越限静默", policy.gate(urgent, cfg, now=night)["allowed"] is True)
    check("high 也不放过 SubagentStop", policy.gate({"hook_event_name": "SubagentStop", "urgency": "high"}, cfg, now=night)["allowed"] is False)
    check("gate 返回 fingerprint", len(policy.gate(plain, cfg, now=night)["fingerprint"]) == 16)

    check("坏日志行不影响计数", _tolerant_log(tmp))


def _tolerant_log(tmp: Path) -> bool:
    cfg = make_cfg(tmp / "tolerant")
    policy.record_push(cfg, "Notification", "fp", "mock", True)
    with policy.push_log_path(cfg).open("a", encoding="utf-8") as fh:
        fh.write("{坏行\n\n")
    return policy.count_today(cfg) == 1 and policy.throttle_ok("Notification", make_cfg(tmp / "tolerant2")) is True


# ---------------------------------------------------------------------------
# 5. channels.mock 落盘 + 未配置降级
# ---------------------------------------------------------------------------


def test_concurrent_append(tmp: Path) -> None:
    """并发写同一个日志文件：Windows 的 O_APPEND 不原子，必须有进程内锁（回归用）。"""
    section("并发落盘")
    cfg = make_cfg(tmp / "conc")
    path = Path(cfg["log_dir"]) / "conc.jsonl"
    total = 40

    def worker(i: int) -> None:
        try:
            channels.append_line(path, json.dumps({"i": i, "txt": "中文" * 60}, ensure_ascii=False))
            policy.record_push(cfg, "Notification", f"fp-{i}", "mock", True)
        except Exception as exc:  # noqa: BLE001
            print(f"    worker {i} 抛了：{exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(total)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    parsed, bad = [], 0
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            bad += 1
    check(f"{total} 线程并发写不丢行不写串", len(lines) == total and bad == 0, f"只 {len(lines)} 行，坏 {bad}")
    check("内容完整（i 全齐）", sorted(p.get("i", -1) for p in parsed) == list(range(total)), f"缺 {sorted(set(range(total)) - {p.get('i', -1) for p in parsed})[:6]}")
    pushes = policy.counted_pushes(cfg)
    check("push_log 并发也不丢计数", len(pushes) == total, f"{len(pushes)}/{total}")
    check("并发写的 reserve/result 都能配对", len(policy.read_push_log(cfg)) == total * 2, str(len(policy.read_push_log(cfg))))


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class _FakeRequests:
    """假 requests：只替换 channels 模块命名空间里的名字，不碰真 requests 模块，
    这样同一进程里 hooks 端到端测试仍能正常发本地 HTTP。"""

    RequestException = type("RequestException", (Exception,), {})
    Timeout = type("Timeout", (RequestException,), {})

    def __init__(self, mode: str):
        self.mode = mode
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, "params": kwargs.get("params"), "json": kwargs.get("json"),
                          "data": kwargs.get("data"), "timeout": kwargs.get("timeout")})
        if self.mode == "timeout":
            raise self.Timeout("simulated timeout")
        if self.mode == "leak":
            # requests 的真实异常串里会带上完整 URL（含 ?key=...），拿来验脱敏
            raise self.RequestException(
                "HTTPSConnectionPool(host='qyapi.weixin.qq.com', port=443): Max retries exceeded with url: "
                "/cgi-bin/webhook/send?key=SUPERSECRETKEY0123456789abcdef (Caused by NewConnectionError)"
            )
        if self.mode == "leak_feishu":
            raise self.RequestException(
                "HTTPSConnectionPool(host='open.feishu.cn', port=443): Max retries exceeded with url: "
                "/open-apis/bot/v2/hook/FEISHUWEBHOOK0123456789abcdef (Caused by NewConnectionError)"
            )
        if self.mode == "remote_fail":
            return _FakeResponse(401, {"code": -1000, "message": "simulated bad key"})
        if self.mode == "feishu_ok":
            return _FakeResponse(200, {"code": 0, "msg": "success"})
        if self.mode == "feishu_fail":
            return _FakeResponse(200, {"code": 19021, "msg": "sign match fail or timestamp is not within one hour from current time"})
        return _FakeResponse(200, {"errcode": 0, "errmsg": "ok"})


_real_requests = channels.requests


def _stub_net(mode: str) -> None:
    channels.requests = _FakeRequests(mode)


def _restore_net() -> None:
    channels.requests = _real_requests


def test_channels(tmp: Path) -> None:
    section("channels")
    cfg = make_cfg(tmp / "ch")
    result = channels.send(cfg, "标题", "正文", "http://url", "normal")
    check("mock 返回 ok", result["ok"] is True and result["channel"] == "mock", str(result))
    check("mock 带 status_code", result["status_code"] == 200)
    path = Path(cfg["log_dir"]) / "mock_sent.jsonl"
    check("mock 落盘到 logs/mock_sent.jsonl", path.exists())
    if path.exists():
        rec = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
        check("落盘内容含最终 markdown", "### 标题" in rec["content"] and "http://url" in rec["content"], str(rec)[:200])
        check("落盘含 title/url 字段", rec["title"] == "标题" and rec["url"] == "http://url")

    unconfigured = make_cfg(tmp / "unconf", channel="wecom_bot")
    result = channels.send(unconfigured, "t", "b", "u")
    check("未配 key → ok:false channel_not_configured",
          result["ok"] is False and result["error"] == "channel_not_configured", str(result))
    result = channels.send(make_cfg(tmp / "unconf2", channel="feishu"), "t", "b", "u")
    check("feishu 未配 → channel_not_configured", result["error"] == "channel_not_configured", str(result))
    result = channels.send(make_cfg(tmp / "unconf3", channel="pushplus"), "t", "b", "u")
    check("pushplus 未配 → channel_not_configured", result["error"] == "channel_not_configured", str(result))
    result = channels.send(make_cfg(tmp / "unknown", channel="telegram"), "t", "b", "u")
    check("未知通道 → ok:false 不抛", result["ok"] is False and result["error"] == "unknown_channel", str(result))
    _stub_net("remote_fail")
    result = channels.send(make_cfg(tmp / "netfail", channel="feishu", channels={"feishu": {"webhook": "test-webhook"}}), "t", "b", "u")
    check("远端非成功码 → ok:false，不抛", result["ok"] is False and result["channel"] == "feishu" and "http_401" in (result["error"] or ""), str(result))
    _stub_net("timeout")
    result = channels.send(make_cfg(tmp / "neterr", channel="wecom_bot", channels={"wecom_bot": {"key": "test-key"}}), "t", "b", "u")
    check("网络超时折成 ok:false network_error", result["ok"] is False and "network_error" in (result["error"] or ""), str(result))
    _stub_net("ok")
    result = channels.send(make_cfg(tmp / "netok", channel="wecom_bot", channels={"wecom_bot": {"key": "test-key"}}), "t", "b", "u")
    check("远端 errcode=0 → ok:true", result["ok"] is True and result["status_code"] == 200, str(result))
    _restore_net()

    # === E4 回归：异常串不许把 webhook key / SCKEY 带出去 ===
    leaky_cfg = make_cfg(tmp / "leak", channel="wecom_bot", channels={"wecom_bot": {"key": "SUPERSECRETKEY0123456789abcdef"}})
    _stub_net("leak")
    result = channels.send(leaky_cfg, "t", "b", "http://u")
    check("异常也折成 ok:false", result["ok"] is False and result["error"].startswith("network_error"), str(result))
    check("error 里不含明文 key", "SUPERSECRETKEY0123456789abcdef" not in json.dumps(result), str(result["error"]))
    check("error 里保留了主机名便于排障", "qyapi.weixin.qq.com" in result["error"], str(result["error"]))
    rid = policy.reserve_push(leaky_cfg, "manual", "fp-leak", "normal")
    policy.settle_push(leaky_cfg, rid, False, channel="wecom_bot", error=result["error"])
    disk = policy.push_log_path(leaky_cfg).read_text(encoding="utf-8")
    check("写进 push_log 的 error 同样是脱敏过的", "SUPERSECRETKEY0123456789abcdef" not in disk, disk[:200])
    _stub_net("leak_feishu")
    result = channels.send(make_cfg(tmp / "leakfs", channel="feishu",
                                    channels={"feishu": {"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/FEISHUWEBHOOK0123456789abcdef"}}),
                           "t", "b", "http://u")
    check("飞书异常也折成 ok:false", result["ok"] is False and result["error"].startswith("network_error"), str(result))
    check("飞书 webhook token 不进 error", "FEISHUWEBHOOK0123456789abcdef" not in json.dumps(result), str(result["error"]))
    check("飞书 error 保留主机名", "open.feishu.cn" in result["error"], str(result["error"]))
    _restore_net()

    # === W13 回归：分通道标题上限；所有网络调用 timeout=10 ===
    _stub_net("ok")
    long_title = "⏳ 要你拍板：billing 历史数据到底要不要动，这一句已经很长了" + "尾巴" * 6
    fs_cfg = make_cfg(tmp / "title_fs", channel="feishu", channels={"feishu": {"webhook": "test-webhook"}})
    channels.send(fs_cfg, long_title + "拖" * 40, "b", "http://u")
    fs_payload = channels.requests.calls[-1]["json"]  # type: ignore[attr-defined]
    fs_title = fs_payload["content"]["post"]["zh_cn"]["title"]
    check("feishu 标题被压到 64 字内", len(fs_title) <= 64, f"{len(fs_title)} 字：{fs_title}")
    check("feishu 标题截断带省略号", fs_title.endswith("…"), fs_title)
    check("feishu 用 post 富文本", fs_payload.get("msg_type") == "post", str(fs_payload)[:200])
    fs_paragraphs = fs_payload["content"]["post"]["zh_cn"]["content"]
    fs_links = [el for para in fs_paragraphs for el in para if el.get("tag") == "a"]
    check("feishu 链接区带 href", bool(fs_links) and fs_links[-1].get("href") == "http://u", str(fs_links)[:200])
    check("feishu 走官方 webhook 地址", "open.feishu.cn/open-apis/bot/v2/hook/test-webhook" in channels.requests.calls[-1]["url"], channels.requests.calls[-1]["url"])  # type: ignore[attr-defined]
    pp_cfg = make_cfg(tmp / "title_pp", channel="pushplus", channels={"pushplus": {"token": "test-token"}})
    channels.send(pp_cfg, long_title, "b", "http://u")
    check("pushplus 标题上限 100 字", len(channels.requests.calls[-1]["json"]["title"]) <= 100, str(len(long_title)))  # type: ignore[attr-defined]
    wc_cfg = make_cfg(tmp / "title_wc", channel="wecom_bot", channels={"wecom_bot": {"key": "test-key"}})
    channels.send(wc_cfg, long_title, "b", "http://u")
    content_sent = channels.requests.calls[-1]["json"]["markdown"]["content"]  # type: ignore[attr-defined]
    check("企微标题按 40 字上限收敛", len(content_sent.splitlines()[0]) <= 44, content_sent.splitlines()[0])
    check("所有通道调用 timeout=10", all(c["timeout"] == channels.NET_TIMEOUT_S for c in channels.requests.calls), str([c["timeout"] for c in channels.requests.calls]))  # type: ignore[attr-defined]
    check("企微 key 走 params 不进 URL 字符串", "key" not in channels.requests.calls[-1]["url"], channels.requests.calls[-1]["url"])  # type: ignore[attr-defined]
    sec_cfg = make_cfg(tmp / "title_fs_sec", channel="feishu",
                       channels={"feishu": {"webhook": "test-webhook", "secret": "SIGN_SECRET"}})
    channels.send(sec_cfg, "t", "b", "http://u")
    sec_payload = channels.requests.calls[-1]["json"]  # type: ignore[attr-defined]
    check("feishu 配了 secret 会带 timestamp/sign",
          "timestamp" in sec_payload and "sign" in sec_payload, str(sec_payload)[:160])
    expect = base64.b64encode(
        hmac.new((sec_payload["timestamp"] + "\nSIGN_SECRET").encode(), b"", hashlib.sha256).digest()
    ).decode()
    check("feishu 签名按官方算法生成", sec_payload.get("sign") == expect, str(sec_payload.get("sign")))
    _restore_net()
    check("resolve_channel 覆盖优先", channels.resolve_channel({"channel": "wecom_bot"}, "mock") == "mock")
    check("resolve_channel 没配当 mock", channels.resolve_channel({"channel": ""}, None) == "mock")
    check("resolve_channel 保留非法名（让 send 明确报 unknown_channel 而不是假装成功）",
          channels.resolve_channel({"channel": "telegram"}, None) == "telegram")


# ---------------------------------------------------------------------------
# 6. hooks 服务端到端
# ---------------------------------------------------------------------------

HOOK_EVENTS = [
    # (query evt, body, 期望是否推送, 说明)
    ("PermissionRequest", {"hook_event_name": "PermissionRequest", "session_id": "abcd1234ef56", "cwd": "D:/anyi/billing", "message": "需要授权执行 npm run build"}, True, "授权请求"),
    ("Notification", {"hook_event_name": "Notification", "session_id": "abcd1234ef56", "cwd": "D:/anyi/billing", "notification_type": "result", "message": "任务已等待 3 分钟"}, True, "通知"),
    ("Stop", {"hook_event_name": "Stop", "session_id": "abcd1234ef56", "cwd": "D:/anyi/billing", "duration_s": 420, "last_assistant_message": "三个模块都改完了，测试通过"}, True, "长回合结束"),
    ("SubagentStop", {"hook_event_name": "SubagentStop", "session_id": "abcd1234ef56", "cwd": "D:/anyi/billing", "message": "子任务结束"}, False, "子任务结束默认静默"),
    ("PreToolUse", {"hook_event_name": "PreToolUse", "session_id": "abcd1234ef56", "cwd": "D:/anyi/billing"}, False, "未列入白名单"),
]


def read_mock(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """读 mock 落盘结果，容忍读到异步写入的半行。"""
    path = Path(cfg["log_dir"]) / "mock_sent.jsonl"
    return read_jsonl(path)


def read_raw(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    return read_jsonl(Path(cfg["log_dir"]) / "events.raw.jsonl")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            records.append(data)
    return records


def wait_mock_count(cfg: dict[str, Any], expected: int, timeout_s: float = 12.0) -> int:
    """异步推送要等一会儿才落盘；等到条数达标或超时。"""
    deadline = time.time() + timeout_s
    got = 0
    while time.time() < deadline:
        got = len(read_mock(cfg))
        if got >= expected:
            return got
        time.sleep(0.2)
    return got


def test_hooks(tmp: Path) -> None:
    section("hooks 服务端到端")
    cfg = make_cfg(tmp / "hooks", channel="mock", min_interval_s=0, max_per_10min=99, daily_limit=99, hook_port=free_port())
    # 让链接可控：写一个测试 rc.json，断言推送里带的是它
    Path(cfg["rc_json_path"]).write_text(json.dumps({"pid": os.getpid(), "url": "https://code-rc.kimi.com/devices/test/?rc=1"}), encoding="utf-8")
    config_path = write_tmp_config(tmp, cfg)

    env = dict(os.environ, WAITFORME_CONFIG=str(config_path), PYTHONIOENCODING="utf-8")
    mock_path = Path(cfg["log_dir"]) / "mock_sent.jsonl"
    port = int(cfg["hook_port"])

    serve_log = (Path(cfg["log_dir"]) / "hooks.child.out.log").open("ab")
    proc = subprocess.Popen([sys.executable, str(HERE / "hooks.py"), "serve", "--port", str(port)],
                            stdout=serve_log, stderr=serve_log, stdin=subprocess.DEVNULL, env=env, cwd=str(HERE))
    try:
        import requests

        health = None
        for _ in range(60):
            try:
                health = requests.get(f"http://127.0.0.1:{port}/health", timeout=2)
                break
            except requests.RequestException:
                time.sleep(0.25)
        check("服务起来并能访问 /health", health is not None and health.status_code == 200, str(health))
        if health is None:
            return
        check("/health 返回 ok:true + push_today", health.json().get("ok") is True and "push_today" in health.json(), health.text)

        base = f"http://127.0.0.1:{port}/hook"
        # === W7 回归：同一个 keep-alive 连接上，403/404 之后不能再出现 400 ===
        # 老代码在错误分支没把请求体读干净，残留字节被当成下一个请求的开头，
        # 实测同一个 Session 里「错 token POST → 正确 POST」第二条直接收到 400
        sess = requests.Session()
        wrong = sess.post(base, params={"k": "wrong-token"}, json={"hook_event_name": "Notification", "message": "错 token"}, timeout=5)
        check("token 不匹配 → 403", wrong.status_code == 403, wrong.text)
        wait_mock_count(cfg, 0, timeout_s=1.5)
        check("403 不产生推送", len(read_mock(cfg)) == 0, str(len(read_mock(cfg))))
        after_wrong = sess.post(base, params={"k": "test-token"}, json={"hook_event_name": "Notification", "session_id": "abcd1234ef56",
                                                "cwd": "D:/anyi/billing", "message": "错 token 之后的正常请求"}, timeout=5)
        check("403 之后同一连接仍正常应答（不 400）", after_wrong.status_code == 200, after_wrong.text[:200])
        sess.close()

        sess2 = requests.Session()
        check("未知路径 GET → 404", sess2.get(f"http://127.0.0.1:{port}/nope", timeout=5).status_code == 404)
        after_404 = sess2.get(f"http://127.0.0.1:{port}/health", timeout=5)
        check("404 之后同一连接 /health 仍 200（不 400）", after_404.status_code == 200 and after_404.json().get("ok") is True, after_404.text[:200])
        sess2.close()

        check("不带 token → 403", requests.post(base, json={"hook_event_name": "Notification"}, timeout=5).status_code == 403)
        check("只差一个字符的 token → 403", requests.post(base, params={"k": "test-toke"}, json={"message": "差一个字符"}, timeout=5).status_code == 403)

        # W7 那一批请求已经推过事件了，之后的计数一律以当前条数为基线
        wait_mock_count(cfg, 1, timeout_s=10)
        baseline = len(read_mock(cfg))
        check("W7 的正常请求确实推到了", baseline >= 1, str(baseline))
        expected = 0
        for evt, body, should_push, label in HOOK_EVENTS:
            resp = requests.post(base, params={"k": "test-token", "evt": evt}, json=body, timeout=5)
            ok = resp.status_code == 200
            check(f"POST {evt} → 200（{label}）", ok, resp.text)
            verdict = resp.json().get("push") if ok else None
            check(f"{evt} 判定 push={should_push}", verdict is should_push, resp.text)
            if verdict is True:
                expected += 1

        # 异步推送：等到条数齐了再断言（超时则按实际条数报错）
        wait_mock_count(cfg, baseline + expected)
        got = len(read_mock(cfg))
        check(f"新增 mock 条数 = 期望推送数 {expected}", got == baseline + expected, f"got={got} baseline={baseline} expected={expected}")
        records = read_mock(cfg)
        check("推送标题是「Kimi 需要你」", all("Kimi 需要你" in r["title"] for r in records), str(records[:1]))
        check("推送标题带事件中文名", all(("等你授权" in r["title"]) or ("有新通知" in r["title"]) or ("回合结束" in r["title"]) for r in records), str([r["title"] for r in records]))
        check("推送正文含项目目录名", any("billing" in r["content"] for r in records))
        check("推送正文含会话短 id", any("abcd1234" in r["content"] for r in records))
        check("推送正文含消息截断", any("npm run build" in r["content"] for r in records))
        check("推送带测试 rc 链接", all("code-rc.kimi.com/devices/test" in r["url"] for r in records))

        # 宽容解析与降级：这几个 case 都关心「判定对不对」，条数用 expected 累加跟踪
        def post_evt(label: str, expect_event: str | None = None, expect_push: bool | None = None,
                     extra_params: dict[str, str] | None = None, **kw) -> dict:
            params = {"k": "test-token"}
            if extra_params:
                params.update(extra_params)
            resp = requests.post(base, params=params, timeout=5, **kw)
            if resp.status_code == 200 and resp.headers.get("Content-Type", "").startswith("application/json"):
                data = resp.json()
            else:
                data = {"_status": resp.status_code, "_text": resp.text}
            if expect_event is not None:
                check(f"{label} 事件名={expect_event}", data.get("event") == expect_event, str(data)[:200])
            if expect_push is not None:
                check(f"{label} 判定 push={expect_push}", data.get("push") is expect_push, str(data)[:200])
                if data.get("push") is True:
                    nonlocal expected
                    expected += 1
            return data

        # evt 走 query，payload 里没有事件名
        post_evt("query 兜底事件名", expect_event="Notification", expect_push=True,
                 extra_params={"evt": "Notification"}, json={"message": "只有 query 里有事件名"})
        # 驼峰 / 别名键，藏在嵌套对象里也要抓到
        post_evt("嵌套驼峰字段", expect_event="Notification", expect_push=True,
                 json={"nested": {"deep": {"hook_event_name": "Notification", "sessionId": "xy12345678", "cwd": "D:/anyi/nested"}}})
        post_evt("事件名大小写归一", expect_event="Notification", expect_push=True,
                 json={"hook_event_name": "NOTIFICATION", "message": "大小写要归一"})
        post_evt("Stop 短问句命中", expect_push=True,
                 json={"hook_event_name": "Stop", "duration_s": 5, "last_assistant_message": "这个要不要改成 B？"})
        post_evt("Stop 短回合非问句不推", expect_push=False,
                 json={"hook_event_name": "Stop", "duration_s": 5, "cwd": "D:/anyi/billing", "session_id": "abcd1234ef56", "last_assistant_message": "顺手把 README 的笔误改了"})
        # 长消息必须截到 300 字，别把整坨日志推到手机上
        long_message = "开头标记" + "甲" * 800
        post_evt("长消息事件", expect_push=True,
                 json={"hook_event_name": "Notification", "session_id": "abcd1234ef56", "cwd": "D:/anyi/billing", "message": long_message})
        wait_mock_count(cfg, baseline + expected)
        long_records = [r for r in read_mock(cfg) if "开头标记" in r["content"]]
        check("长消息被截断（原文不全进来）", bool(long_records) and "甲" * 800 not in long_records[-1]["content"], str(len(long_records)))
        check("截断带省略号", bool(long_records) and "…" in long_records[-1]["content"])

        # GBK body：中文 Windows 上某些客户端（Git Bash 里的原生 curl）会把中文转成 GBK 发出来
        gbk_body = json.dumps({"hook_event_name": "Notification", "session_id": "abcd1234ef56",
                               "cwd": "D:/anyi/billing", "message": "需要授权删除旧订单"}, ensure_ascii=False).encode("gb18030")
        post_evt("GBK 编码 body", expect_push=True, data=gbk_body, headers={"Content-Type": "application/json"})
        wait_mock_count(cfg, baseline + expected)
        gbk_records = [r for r in read_mock(cfg) if "删除旧订单" in r["content"]]
        check("GBK body 也被正确解码", bool(gbk_records), "应能读到中文而不是乱码")
        check("推送里没有替换符", all("\ufffd" not in r["content"] for r in read_mock(cfg)), str([r["content"][:60] for r in read_mock(cfg) if "\ufffd" in r["content"]][:1]))
        resp = requests.post(base, params={"k": "test-token"}, data="这不是 JSON", headers={"Content-Type": "text/plain"}, timeout=5)
        check("非 JSON body → 仍 200", resp.status_code == 200, resp.text)
        resp = requests.post(base, params={"k": "test-token"}, data="", timeout=5)
        check("空 body → 仍 200", resp.status_code == 200, resp.text)
        resp = requests.post(base + "/elsewhere", params={"k": "test-token"}, json={}, timeout=5)
        check("错误路径 POST → 404（但仍不应崩）", resp.status_code == 404, resp.text)
        resp = requests.get(f"http://127.0.0.1:{port}/nope", timeout=5)
        check("未知路径 → 404", resp.status_code == 404)

        # 原始事件必须全部落盘，这是以后校准字段映射的唯一依据
        wait_mock_count(cfg, baseline + expected)
        raw_lines = read_raw(cfg)
        # 403/404 不进 handle_hook；本段合法 /hook 200 = after_wrong1 + HOOK_EVENTS5 + post_evt7 + 非JSON1 + 空body1 = 15
        check("原始事件全落盘（含被拦下的）", len(raw_lines) >= len(read_mock(cfg)) and len(raw_lines) >= 15, f"raw={len(raw_lines)} mock={len(read_mock(cfg))}")
        if raw_lines:
            check("落盘含 ts 与 raw 字段", "ts" in raw_lines[0] and "raw" in raw_lines[0], str(raw_lines[0])[:150])
        check("hooks.log 已生成", (Path(cfg["log_dir"]) / "hooks.log").exists())
        # === E2 回归：hook_token 不许进日志（真实日志里曾经明文躺着完整 token）===
        log_text = (Path(cfg["log_dir"]) / "hooks.log").read_text(encoding="utf-8", errors="replace")
        check("hooks.log 里没有明文 token", "test-token" not in log_text, [l for l in log_text.splitlines() if "test-token" in l][:1])
        check("hooks.log 里能看到打码后的痕迹", "?k=***" in log_text, [l for l in log_text.splitlines() if "/hook" in l][:1])
        child_out = (Path(cfg["log_dir"]) / "hooks.child.out.log").read_text(encoding="utf-8", errors="replace")
        check("子进程 stdout/stderr 也没有 token", "test-token" not in child_out, child_out[:200])
        check("redact_token 单元行为", hooks_redact("POST /hook?k=SECRET&evt=Stop HTTP/1.1") == "POST /hook?k=***&evt=Stop HTTP/1.1")
        check("redact_token 不动无 token 的 URL", hooks_redact("GET /health HTTP/1.1") == "GET /health HTTP/1.1")
        check("raw 事件落盘不写 query 里的 k", not any("test-token" in r.get("raw", "") + json.dumps(r.get("query", {})) for r in read_raw(cfg)))

        # 去重：必须等第一次的推送真的落 push_log，再发第二条，否则测的是竞态不是去重
        dup = {"hook_event_name": "PermissionRequest", "session_id": "abcd1234ef56", "cwd": "D:/anyi/billing", "message": "重复内容测试"}
        first = requests.post(base, params={"k": "test-token"}, json=dup, timeout=5).json()
        for _ in range(40):
            if len(read_mock(cfg)) > baseline + expected:
                break
            time.sleep(0.2)
        second = requests.post(base, params={"k": "test-token"}, json=dup, timeout=5).json()
        check("同内容 60s 内第二次不推", first.get("push") is True and second.get("push") is False, f"{first} {second}")
        # 同类最小间隔在 hooks 配置里放开了（min_interval_s=0），所以换内容仍应放行
        third = requests.post(base, params={"k": "test-token"}, json=dict(dup, message="换了内容"), timeout=5).json()
        check("换内容后可再推", third.get("push") is True, third)
        if third.get("push") is True:
            expected += 1

        # 并发：8 个 hook 同时打进来，一条都不能丢（Windows O_APPEND 不原子的回归）
        before = len(read_mock(cfg))
        base_count = before
        threads = []
        for i in range(8):
            payload = {"hook_event_name": "Notification", "session_id": f"conc-{i}", "cwd": "D:/anyi/conc", "message": f"并发 {i} 号 🚴"}
            threads.append(threading.Thread(target=lambda p=payload: requests.post(base, params={"k": "test-token"}, json=p, timeout=10)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wait_mock_count(cfg, base_count + 8, timeout_s=20)
        after = read_mock(cfg)
        check("并发 8 条一条不丢", len(after) >= base_count + 8, f"{len(after)} < {base_count + 8}")
        conc_texts = [r["content"] for r in after if "并发" in r["content"]]
        check("并发内容都完整（无残缺 JSON）", len(conc_texts) == 8, str(len(conc_texts)))
        check("emoji 走完整条链路", any("🚴" in r["content"] for r in after))
        raw_after = read_raw(cfg)
        check("并发事件的原始 body 也全落盘", len(raw_after) >= len(after), f"raw={len(raw_after)} mock={len(after)}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        try:
            serve_log.close()
        except OSError:
            pass
        check("服务已停止", proc.poll() is not None)


# ---------------------------------------------------------------------------
# 7. CLI（stdout 必须是合法的一行 JSON）
# ---------------------------------------------------------------------------


def ask_with_late_answer(cfg: dict[str, Any], env: dict[str, str], payload_file: Path,
                         answer: dict[str, Any], deadline: int = 25, delay: float = 1.5) -> tuple[int, str, Path]:
    """跑 `ask --wait`，起跑 `delay` 秒后把答案写进 inbox。

    顺序很重要：真实场景是推送先出去、手机上后回话。先写答案再跑会被 W9 的
    「推送前清同名旧答案」逻辑删掉，那样测的是清理而不是等待。
    """
    path = waitforme.answer_path(answer["id"], cfg)
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "waitforme.py"), "ask", "--payload", str(payload_file),
         "--wait", "--deadline-s", str(deadline)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=str(HERE),
        text=True, encoding="utf-8", errors="replace")
    time.sleep(delay)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(answer, ensure_ascii=False), encoding="utf-8")
    try:
        stdout, _ = proc.communicate(timeout=deadline + 30)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, _ = proc.communicate()
    return int(proc.returncode), (stdout or "").strip(), path


def run_cli(tmp: Path, args: list[str]) -> tuple[int, str, str]:
    # 参数里可能带绝对路径，直接拼目录名在 Windows 上是非法的（冒号），必须洗一遍
    slot = tmp / ("cli_" + re.sub(r"[^0-9A-Za-z._-]+", "_", "_".join(args))[:60])
    cfg = make_cfg(slot)
    config_path = write_tmp_config(slot, cfg)
    env = dict(os.environ, WAITFORME_CONFIG=str(config_path), PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, str(HERE / "waitforme.py"), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, cwd=str(HERE), timeout=60)
    return proc.returncode, proc.stdout.strip(), proc.stderr


def test_cli(tmp: Path) -> None:
    section("CLI 契约")
    code, out, err = run_cli(tmp, ["link"])
    check("link 退出码 0", code == 0, err[-300:])
    check("link stdout 是一行合法 JSON", out.count("\n") == 0 and isinstance(json.loads(out), dict), out[:200])
    check("link 含三个必需字段", set(["primary_url", "fallback_url", "source"]) <= set(json.loads(out)), out)
    check("link 读不到 rc.json 时用 fallback 且仍算成功", json.loads(out)["source"] == "fallback" and code == 0, f"code={code} {out[:200]}")

    # rc.json 在的时候必须优先用它
    rc_slot = tmp / "cli_rc"
    rc_cfg = make_cfg(rc_slot)
    Path(rc_cfg["rc_json_path"]).write_text(json.dumps({"pid": os.getpid(), "url": "https://code-rc.kimi.com/devices/rc-case/"}), encoding="utf-8")
    rc_env = dict(os.environ, WAITFORME_CONFIG=str(write_tmp_config(rc_slot, rc_cfg)), PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, str(HERE / "waitforme.py"), "link"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=rc_env, cwd=str(HERE), timeout=60)
    data = json.loads(proc.stdout.strip())
    check("link 优先用 rc.json", proc.returncode == 0 and data["source"] == "rc" and "rc-case" in data["primary_url"], proc.stdout[:200])

    code, out, err = run_cli(tmp, ["status"])
    check("status 退出码 0", code == 0, err[-300:])
    result = json.loads(out)
    check("status 字段齐", set(["pid_exists", "port", "enabled", "channel", "today_count", "last_push_ts"]) <= set(result), str(result))

    tmpdir = tmp / "ask_valid"
    payload_file = tmpdir / "q.json"
    payload_file.parent.mkdir(parents=True, exist_ok=True)
    payload_file.write_text(json.dumps(VALID_PAYLOAD, ensure_ascii=False), encoding="utf-8")

    code, out, err = run_cli(tmp, ["ask", "--payload", str(payload_file), "--dry-run"])
    check("ask --dry-run 退出 0", code == 0, err[-300:])
    result = json.loads(out)
    check("ask --dry-run 给 markdown 预览", "稳妥重构" in result.get("markdown", ""), str(result)[:200])

    code, out, err = run_cli(tmp, ["ask", "--payload", str(payload_file)])
    check("ask 不 wait → status=sent 退出 0", code == 0 and json.loads(out)["status"] == "sent", f"code={code} {out[:200]}")

    bad = tmpdir / "bad.json"
    bad.write_text(json.dumps({"id": "x", "title": "", "situation": "s", "options": []}), encoding="utf-8")
    code, out, err = run_cli(tmp, ["ask", "--payload", str(bad)])
    check("非法 payload → invalid 退出 2", code == 2 and json.loads(out)["status"] == "invalid", f"code={code} {out[:200]}")

    ghost = tmpdir / "ghost.json"
    code, out, err = run_cli(tmp, ["ask", "--payload", str(ghost)])
    check("payload 文件不存在 → invalid 退出 2 不崩", code == 2 and json.loads(out)["status"] == "invalid", f"code={code} {out[:200]}")

    # --wait：真实顺序是「先推送、手机后回话」。答案在进程起跑后才写进 inbox。
    wait_cfg_dir = tmp / "ask_wait"
    wait_cfg_dir.mkdir(parents=True, exist_ok=True)
    answered_payload = dict(VALID_PAYLOAD, id="ask-wait-ok")
    p_file = wait_cfg_dir / "q.json"
    p_file.write_text(json.dumps(answered_payload, ensure_ascii=False), encoding="utf-8")
    cfg = make_cfg(wait_cfg_dir)
    config_path = write_tmp_config(wait_cfg_dir, cfg)
    env = dict(os.environ, WAITFORME_CONFIG=str(config_path), PYTHONIOENCODING="utf-8")

    code, stdout, path = ask_with_late_answer(cfg, env, p_file, {"id": "ask-wait-ok", "choice": "B", "note": "先上线，历史数据我不管"}, deadline=25)
    out = json.loads(stdout) if stdout else {}
    check("ask --wait 读到答案 → answered 退出 0", code == 0 and out.get("status") == "answered", f"code={code} {stdout[:200]}")
    check("answered 带回 choice/note/answer_file", out.get("choice") == "B" and "先上线" in out.get("note", "") and out.get("answer_file") == str(path), str(out))
    check("answered 标出来源是回答", out.get("choice_source") == "reply", str(out))

    # 回了不在 options 里的字母：按默认项走，但把原值交出去
    bad_payload = dict(VALID_PAYLOAD, id="ask-wait-bad-choice")
    b_file = wait_cfg_dir / "b.json"
    b_file.write_text(json.dumps(bad_payload, ensure_ascii=False), encoding="utf-8")
    code, stdout, _ = ask_with_late_answer(cfg, env, b_file, {"id": "ask-wait-bad-choice", "choice": "Z", "note": "手滑"}, deadline=25)
    out = json.loads(stdout) if stdout else {}
    check("回答非法字母 → answered 回落默认项 退出 0", code == 0 and out.get("choice") == "A" and out.get("raw_choice") == "Z", f"code={code} {stdout[:200]}")
    check("非法回答的回落有标记", out.get("choice_source") == "invalid_reply_fallback", str(out))

    # === W9 回归：旧答案不许串到下一次请求 ===
    p_a = waitforme.answer_path("中文id甲", cfg)
    p_b = waitforme.answer_path("中文id乙", cfg)
    check("纯非 ASCII 的 id 不再撞同一个答案文件", p_a != p_b, f"{p_a.name} vs {p_b.name}")
    ascii_path = waitforme.answer_path("ask-20261031-01", cfg)
    check("ASCII id 保留可读前缀 + 哈希后缀", ascii_path.name.startswith("ask-20261031-01-") and ascii_path.name.endswith(".answer.json"), ascii_path.name)
    stale = wait_cfg_dir / "st.json"
    stale_payload = dict(VALID_PAYLOAD, id="ask-wait-stale")
    stale.write_text(json.dumps(stale_payload, ensure_ascii=False), encoding="utf-8")
    stale_path = waitforme.answer_path("ask-wait-stale", cfg)
    stale_path.parent.mkdir(parents=True, exist_ok=True)
    stale_path.write_text(json.dumps({"id": "上一次的历史请求", "choice": "A", "note": "旧答案"}, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(HERE / "waitforme.py"), "ask", "--payload", str(stale), "--wait", "--deadline-s", "3"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, cwd=str(HERE), timeout=90)
    out = json.loads(proc.stdout.strip())
    check("预置的旧答案不会立刻被当成回答（应 timeout）", proc.returncode == 2 and out["status"] == "timeout", f"code={proc.returncode} {proc.stdout[:200]}")
    check("ask 前同名旧答案文件已被清掉", not stale_path.exists() or json.loads(stale_path.read_text(encoding="utf-8")).get("id") != "上一次的历史请求", str(stale_path))
    # read_answer 单独验：id 必须对得上
    any_path = wait_cfg_dir / "any.answer.json"
    any_path.write_text(json.dumps({"id": "req-x", "choice": "B"}, ensure_ascii=False), encoding="utf-8")
    check("不带 id 也能读（兼容手工写的文件）", waitforme.read_answer(any_path) is not None)
    check("id 不匹配 → 忽略", waitforme.read_answer(any_path, "req-y") is None)
    check("id 匹配 → 认", (waitforme.read_answer(any_path, "req-x") or {}).get("choice") == "B")
    any_path.write_text(json.dumps({"choice": "B"}, ensure_ascii=False), encoding="utf-8")
    check("答案里没有 id → 忽略", waitforme.read_answer(any_path, "req-x") is None)
    any_path.write_text("半截 {", encoding="utf-8")
    check("半截 JSON 不算回答", waitforme.read_answer(any_path, "req-x") is None)

    timeout_payload = dict(VALID_PAYLOAD, id="ask-wait-timeout")
    t_file = wait_cfg_dir / "t.json"
    t_file.write_text(json.dumps(timeout_payload, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(HERE / "waitforme.py"), "ask", "--payload", str(t_file), "--wait", "--deadline-s", "3"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, cwd=str(HERE), timeout=90)
    out = json.loads(proc.stdout.strip())
    check("ask --wait 超时 → timeout 退出 2", proc.returncode == 2 and out["status"] == "timeout", f"code={proc.returncode} {proc.stdout[:200]}")
    check("timeout 带 default_applied", out.get("default_applied") == "A", str(out))

    code, out, err = run_cli(tmp, ["send", "--title", "x", "--channel", "feishu"])
    check("send 通道未配置 → 退出 2", code == 2 and json.loads(out)["status"] == "failed", f"code={code} {out[:200]}")
    code, out, err = run_cli(tmp, ["send", "--title", "x", "--channel", "mock"])
    check("send mock → 退出 0", code == 0 and json.loads(out)["status"] == "sent", f"code={code} {out[:200]}")
    code, out, err = run_cli(tmp, ["test", "--mock"])
    check("test --mock 退出 0 且 stdout 单行 JSON", code == 0 and out.count("\n") == 0, f"code={code}")
    check("test 的 stderr 里有完整 markdown", "### ⏳ Kimi 需要你：测试通道" in err, err[-300:])
    check("test 报告 link_source", json.loads(out).get("link_source") in ("rc", "fallback"), out[:200])

    # === W11 回归：用法错误也得守「stdout 一行 JSON」，且用退出码 3 与业务失败区分 ===
    usage_cases = [
        ("无参数裸跑", []),
        ("缺 --title", ["send", "--urgency", "normal"]),
        ("urgency 值非法", ["send", "--title", "x", "--urgency", "asap"]),
        ("channel 值非法", ["send", "--title", "x", "--channel", "telegram"]),
        ("未知子命令", ["nope"]),
    ]
    for label, argv in usage_cases:
        code, out, err = run_cli(tmp, argv)
        ok_json = False
        status = None
        if out and out.count("\n") == 0:
            try:
                data = json.loads(out)
                ok_json = isinstance(data, dict)
                status = data.get("status")
                reason = data.get("reason", "")
            except (json.JSONDecodeError, ValueError):
                reason = out[:80]
        else:
            reason = f"stdout={out!r}"
        check(f"用法错误 {label} → usage_error JSON", ok_json and status == "usage_error", reason)
        check(f"用法错误 {label} → 退出码 3", code == 3, f"code={code}")
    code, out, err = run_cli(tmp, ["--help"])
    check("--help 仍走 argparse 正常退出 0", code == 0, f"code={code}")

    # === W10 回归：显式 ask/send 也要过静默时段，除非 --force 或 high ===
    quiet_cfg = make_cfg(tmp / "quiet_cli")
    now = datetime.now()
    quiet_cfg["quiet_hours"] = {
        "start": (now - timedelta(hours=1)).strftime("%H:%M"),
        "end": (now + timedelta(hours=1)).strftime("%H:%M"),
    }
    qdir = tmp / "quiet_cli"
    qdir.mkdir(parents=True, exist_ok=True)
    config_path = write_tmp_config(qdir, quiet_cfg)
    qenv = dict(os.environ, WAITFORME_CONFIG=str(config_path), PYTHONIOENCODING="utf-8")
    qfile = qdir / "q.json"
    qfile.write_text(json.dumps(VALID_PAYLOAD, ensure_ascii=False), encoding="utf-8")
    mock_path = Path(quiet_cfg["log_dir"]) / "mock_sent.jsonl"

    def qrun(args: list[str]) -> tuple[int, dict]:
        proc = subprocess.run([sys.executable, str(HERE / "waitforme.py"), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=qenv, cwd=str(HERE), timeout=90)
        return proc.returncode, (json.loads(proc.stdout.strip()) if proc.stdout.strip() else {})

    check("测试用的静默窗口确实覆盖现在", policy.in_quiet_hours(quiet_cfg) is True, str(quiet_cfg["quiet_hours"]))
    code, data = qrun(["ask", "--payload", str(qfile)])
    check("静默时段 ask 被拦 → blocked 退出 2", code == 2 and data.get("status") == "blocked" and "quiet_hours" in data.get("reason", ""), str(data))
    check("被拦下时没有推送落盘", not mock_path.exists() or len(read_jsonl(mock_path)) == 0, str(read_jsonl(mock_path)))
    code, data = qrun(["ask", "--payload", str(qfile), "--force"])
    check("--force 可越静默时段 → sent 退出 0", code == 0 and data.get("status") == "sent", str(data))
    code, data = qrun(["send", "--title", "静默时段", "--channel", "mock"])
    check("静默时段 send 被拦", code == 2 and data.get("status") == "blocked", str(data))
    code, data = qrun(["send", "--title", "紧急", "--urgency", "high", "--channel", "mock"])
    check("send --urgency high 越静默时段", code == 0 and data.get("status") == "sent", str(data))
    high_payload = dict(VALID_PAYLOAD, id="ask-high-quiet", urgency="high")
    hfile = qdir / "high.json"
    hfile.write_text(json.dumps(high_payload, ensure_ascii=False), encoding="utf-8")
    code, data = qrun(["ask", "--payload", str(hfile)])
    check("ask payload 里 urgency=high 越静默时段", code == 0 and data.get("status") == "sent", str(data))
    code, data = qrun(["test", "--live", "--mock"])
    check("test --live 也受静默时段约束", code == 2 and data.get("status") == "blocked", str(data))
    check("--dry-run 不受静默时段影响（本来就不发）", qrun(["ask", "--payload", str(qfile), "--dry-run"])[0] == 0)

    # === info 回归：enabled 写成字符串 "false" 也算关 ===
    off_cfg = make_cfg(tmp / "off")
    off_cfg["enabled"] = "false"
    off_dir = tmp / "off"
    off_dir.mkdir(parents=True, exist_ok=True)
    off_env = dict(os.environ, WAITFORME_CONFIG=str(write_tmp_config(off_dir, off_cfg)), PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, str(HERE / "waitforme.py"), "send", "--title", "x", "--channel", "mock"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", env=off_env, cwd=str(HERE), timeout=60)
    data = json.loads(proc.stdout.strip())
    check('enabled="false" 不推送', data.get("status") == "failed" and data.get("error") == "disabled", str(data))
    proc = subprocess.run([sys.executable, str(HERE / "waitforme.py"), "status"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", env=off_env, cwd=str(HERE), timeout=60)
    check('status 里 enabled 反映为 false', json.loads(proc.stdout.strip())["enabled"] is False, proc.stdout[:200])
    check("truthy 解析多种写法", [waitforme.truthy(v) for v in ("false", "0", "no", "off", "", True, "TRUE", "yes")] == [False, False, False, False, False, True, True, True])


# ---------------------------------------------------------------------------
# 8. --live
# ---------------------------------------------------------------------------


def test_live() -> None:
    section("--live 真实推送")
    cfg = waitforme.load_config()
    channel = channels.resolve_channel(cfg)
    if channel == "mock":
        print("  SKIP  当前通道是 mock，没有真实推送（改 config.json 的 channel 后再 --live）")
        return
    key_fields = {"wecom_bot": ("wecom_bot", "key"), "feishu": ("feishu", "webhook"), "pushplus": ("pushplus", "token")}
    name, field = key_fields[channel]
    if not str((cfg.get("channels") or {}).get(name, {}).get(field) or "").strip():
        print(f"  SKIP  通道 {channel} 未配置 {field}，不真发")
        return
    import linker

    content_body = "> 这是 `--live` 自检消息，收到说明通道已打通。"
    result = waitforme.push_message(cfg, kind="live_test", title="⏳ waitforme 自检", body_md=content_body, urgency="normal")
    check("live 推送成功", result["ok"] is True, str(result))


def main() -> int:
    parser = argparse.ArgumentParser(prog="selftest.py", description="waitforme 自测")
    parser.add_argument("--dry-run", action="store_true", help="只做离线断言（默认行为）")
    parser.add_argument("--live", action="store_true", help="额外真发一条自检消息")
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="waitforme_selftest_"))
    print(f"waitforme selftest  临时目录={tmp}")
    started = time.time()
    try:
        test_payload_validation()
        test_markdown()
        test_linker(tmp / "linker")
        test_policy(tmp / "policy")
        test_channels(tmp / "channels")
        test_concurrent_append(tmp / "conc")
        test_hooks(tmp / "hooks")
        test_cli(tmp / "cli")
        if args.live:
            test_live()
    finally:
        print(f"\n耗时 {time.time() - started:.1f}s")
        print(f"PASS {PASS} / FAIL {FAIL}")
        if FAILED_NAMES:
            print("失败项：" + " | ".join(FAILED_NAMES[:12]))
        try:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)
        except OSError:
            pass
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
