"""selftest.py —— waitforme 离线自测。

默认 `--dry-run`：全程本地断言，不联网（mock 通道 + 临时目录）。`--live` 额外真发一条
自检消息，通道没配 key 就 skip 并说明。

隔离方式：所有临时状态写在系统临时目录里，通过 `WAITFORME_CONFIG` 环境变量把整套配置
（含 log_dir / run_dir / inbox_dir）指过去。跑自测不会污染真实 `logs/`，也不会误停
正在服务的 hooks。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import channels  # noqa: E402
import linker  # noqa: E402
import policy  # noqa: E402
import waitforme  # noqa: E402

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
    cfg["fallback_url"] = "http://10.204.69.40:8791/"
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
    long_content = channels.build_content("t", "字" * 3000, "u", "normal")
    truncated = channels._truncate_utf8(long_content, channels.WECOM_MAX_BYTES)
    check("超 4096 字节安全截断", len(truncated.encode("utf-8")) <= channels.WECOM_MAX_BYTES, str(len(truncated.encode("utf-8"))))
    check("截断不留半个汉字（无替换符）", "\ufffd" not in truncated)
    check("截断带提示尾串", truncated.endswith("（内容过长已截断）"), truncated[-30:])
    mixed = channels._truncate_utf8(("中文abc" * 2000) + "尾", 100)
    check("中英混排截断仍是合法 UTF-8", mixed.encode("utf-8").decode("utf-8") == mixed)


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
    check("正常 rc.json 带 fallback_url", links["fallback_url"] == "http://10.204.69.40:8791/")

    missing = make_cfg(tmp / "missing", rc_json_path=str(tmp / "nope" / "rc.json"))
    links = linker.build_links(missing)
    check("文件缺失 → source=fallback", links["source"] == "fallback", str(links))
    check("文件缺失 → primary 落到 fallback", links["primary_url"] == "http://10.204.69.40:8791/")

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

    # --- 去重：同内容 60s 内第二次不推
    cfg = make_cfg(tmp / "dedup")
    event = {"hook_event_name": "PermissionRequest", "message": "允许跑 build?"}
    fp = policy.fingerprint("", "允许跑 build?")
    check("第一次放行", policy.should_push(event, cfg)[0] is True)
    policy.record_push(cfg, "PermissionRequest", fp, "mock", True)
    check("60s 内同指纹去重", policy.should_push(event, cfg)[0] is False, "同内容应被去重")
    aged = make_cfg(tmp / "dedup_aged")
    policy.record_push(aged, "PermissionRequest", fp, "mock", True)
    log_path = policy.push_log_path(aged)
    lines = [json.loads(x) for x in log_path.read_text(encoding="utf-8").splitlines()]
    lines[0]["ts"] = time.time() - 90
    log_path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
    check("90s 后同指纹可再推", policy.should_push(event, aged)[0] is True)
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
    for _ in range(5):
        if policy.throttle_ok("Notification", cfg):
            allowed += 1
            policy.record_push(cfg, "Notification", policy.fingerprint("t", str(allowed)), "mock", True)
    check("连发 5 次只放 2 次", allowed == 2, f"allowed={allowed}")

    cfg = make_cfg(tmp / "interval", min_interval_s=180, max_per_10min=99, daily_limit=99)
    check("同类间隔未到 → 不放行", policy.throttle_ok("Notification", cfg) is True)
    policy.record_push(cfg, "Notification", "fp1", "mock", True)
    check("同类间隔已到 → 拦截", policy.throttle_ok("Notification", cfg) is False)
    check("换个类别仍可放行", policy.throttle_ok("Stop", cfg) is True)
    now = time.time()
    aged_cfg = make_cfg(tmp / "interval_aged", min_interval_s=30, max_per_10min=99, daily_limit=99)
    policy.record_push(aged_cfg, "Notification", "fp", "mock", True)
    p = policy.push_log_path(aged_cfg)
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["ts"] = round(now - 45, 3)
    p.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    check("间隔过完后放行", policy.throttle_ok("Notification", aged_cfg, now) is True)

    cfg = make_cfg(tmp / "daily", min_interval_s=0, max_per_10min=99, daily_limit=3)
    day_allowed = 0
    for i in range(6):
        if policy.throttle_ok("Notification", cfg):
            day_allowed += 1
            policy.record_push(cfg, "Notification", f"fp-{i}", "mock", True)
    check("当日上限生效（3 次）", day_allowed == 3, f"allowed={day_allowed}")

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
    push_lines = [l for l in policy.push_log_path(cfg).read_text(encoding="utf-8").splitlines() if l.strip()]
    check("push_log 并发也不丢计数", len(push_lines) == total, f"{len(push_lines)}/{total}")


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
        self.calls.append({"url": url, **kwargs})
        if self.mode == "timeout":
            raise self.Timeout("simulated timeout")
        if self.mode == "remote_fail":
            return _FakeResponse(401, {"code": -1000, "message": "simulated bad key"})
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
    result = channels.send(make_cfg(tmp / "unconf2", channel="serverchan"), "t", "b", "u")
    check("serverchan 未配 → channel_not_configured", result["error"] == "channel_not_configured", str(result))
    result = channels.send(make_cfg(tmp / "unconf3", channel="pushplus"), "t", "b", "u")
    check("pushplus 未配 → channel_not_configured", result["error"] == "channel_not_configured", str(result))
    result = channels.send(make_cfg(tmp / "unknown", channel="telegram"), "t", "b", "u")
    check("未知通道 → ok:false 不抛", result["ok"] is False and result["error"] == "unknown_channel", str(result))
    _stub_net("remote_fail")
    result = channels.send(make_cfg(tmp / "netfail", channel="serverchan", channels={"serverchan": {"sckey": "test-sckey"}}), "t", "b", "u")
    check("远端非成功码 → ok:false，不抛", result["ok"] is False and result["channel"] == "serverchan" and "http_401" in (result["error"] or ""), str(result))
    _stub_net("timeout")
    result = channels.send(make_cfg(tmp / "neterr", channel="wecom_bot", channels={"wecom_bot": {"key": "test-key"}}), "t", "b", "u")
    check("网络超时折成 ok:false network_error", result["ok"] is False and "network_error" in (result["error"] or ""), str(result))
    _stub_net("ok")
    result = channels.send(make_cfg(tmp / "netok", channel="wecom_bot", channels={"wecom_bot": {"key": "test-key"}}), "t", "b", "u")
    check("远端 errcode=0 → ok:true", result["ok"] is True and result["status_code"] == 200, str(result))
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
        wrong = requests.post(base, params={"k": "wrong-token"}, json={"hook_event_name": "Notification"}, timeout=5)
        check("token 不匹配 → 403", wrong.status_code == 403, wrong.text)
        missing = requests.post(base, json={"hook_event_name": "Notification"}, timeout=5)
        check("不带 token → 403", missing.status_code == 403, missing.text)
        check("403 时不推送", not mock_path.exists())

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
        wait_mock_count(cfg, expected)
        got = len(read_mock(cfg))
        check(f"mock 收到 {expected} 条（等于期望推送数）", got == expected, f"got={got} expected={expected}")
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
        wait_mock_count(cfg, expected)
        long_records = [r for r in read_mock(cfg) if "开头标记" in r["content"]]
        check("长消息被截断（原文不全进来）", bool(long_records) and "甲" * 800 not in long_records[-1]["content"], str(len(long_records)))
        check("截断带省略号", bool(long_records) and "…" in long_records[-1]["content"])

        # GBK body：中文 Windows 上某些客户端（Git Bash 里的原生 curl）会把中文转成 GBK 发出来
        gbk_body = json.dumps({"hook_event_name": "Notification", "session_id": "abcd1234ef56",
                               "cwd": "D:/anyi/billing", "message": "需要授权删除旧订单"}, ensure_ascii=False).encode("gb18030")
        post_evt("GBK 编码 body", expect_push=True, data=gbk_body, headers={"Content-Type": "application/json"})
        wait_mock_count(cfg, expected)
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
        wait_mock_count(cfg, expected)
        raw_lines = read_raw(cfg)
        check("原始事件已落盘 events.raw.jsonl", len(raw_lines) >= 13, str(len(raw_lines)))
        if raw_lines:
            check("落盘含 ts 与 raw 字段", "ts" in raw_lines[0] and "raw" in raw_lines[0], str(raw_lines[0])[:150])
        check("hooks.log 已生成", (Path(cfg["log_dir"]) / "hooks.log").exists())

        # 去重：必须等第一次的推送真的落 push_log，再发第二条，否则测的是竞态不是去重
        dup = {"hook_event_name": "PermissionRequest", "session_id": "abcd1234ef56", "cwd": "D:/anyi/billing", "message": "重复内容测试"}
        first = requests.post(base, params={"k": "test-token"}, json=dup, timeout=5).json()
        for _ in range(40):
            if len(read_mock(cfg)) > expected:
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

    # --wait：先放好答案文件，应该立刻读到
    wait_cfg_dir = tmp / "ask_wait"
    wait_cfg_dir.mkdir(parents=True, exist_ok=True)
    answered_payload = dict(VALID_PAYLOAD, id="ask-wait-ok")
    p_file = wait_cfg_dir / "q.json"
    p_file.write_text(json.dumps(answered_payload, ensure_ascii=False), encoding="utf-8")
    cfg = make_cfg(wait_cfg_dir)
    config_path = write_tmp_config(wait_cfg_dir, cfg)
    env = dict(os.environ, WAITFORME_CONFIG=str(config_path), PYTHONIOENCODING="utf-8")
    answer = Path(cfg["inbox_dir"]) / "ask-wait-ok.answer.json"
    answer.parent.mkdir(parents=True, exist_ok=True)
    answer.write_text(json.dumps({"choice": "B", "note": "先上线，历史数据我不管"}, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(HERE / "waitforme.py"), "ask", "--payload", str(p_file), "--wait", "--deadline-s", "20"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, cwd=str(HERE), timeout=90)
    out = json.loads(proc.stdout.strip())
    check("ask --wait 读到答案 → answered 退出 0", proc.returncode == 0 and out["status"] == "answered", f"code={proc.returncode} {proc.stdout[:200]}")
    check("answered 带回 choice/note/answer_file", out.get("choice") == "B" and "先上线" in out.get("note", "") and out.get("answer_file"), str(out))

    # 回了不在 options 里的字母：按默认项走，但把原值交出去
    bad_payload = dict(VALID_PAYLOAD, id="ask-wait-bad-choice")
    b_file = wait_cfg_dir / "b.json"
    b_file.write_text(json.dumps(bad_payload, ensure_ascii=False), encoding="utf-8")
    (Path(cfg["inbox_dir"]) / "ask-wait-bad-choice.answer.json").write_text(
        json.dumps({"choice": "Z", "note": "手滑"}, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(HERE / "waitforme.py"), "ask", "--payload", str(b_file), "--wait", "--deadline-s", "20"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, cwd=str(HERE), timeout=90)
    out = json.loads(proc.stdout.strip())
    check("回答非法字母 → answered 回落默认项 退出 0", proc.returncode == 0 and out["choice"] == "A" and out.get("raw_choice") == "Z", f"code={proc.returncode} {proc.stdout[:200]}")

    timeout_payload = dict(VALID_PAYLOAD, id="ask-wait-timeout")
    t_file = wait_cfg_dir / "t.json"
    t_file.write_text(json.dumps(timeout_payload, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(HERE / "waitforme.py"), "ask", "--payload", str(t_file), "--wait", "--deadline-s", "3"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, cwd=str(HERE), timeout=90)
    out = json.loads(proc.stdout.strip())
    check("ask --wait 超时 → timeout 退出 2", proc.returncode == 2 and out["status"] == "timeout", f"code={proc.returncode} {proc.stdout[:200]}")
    check("timeout 带 default_applied", out.get("default_applied") == "A", str(out))

    code, out, err = run_cli(tmp, ["send", "--title", "x", "--channel", "serverchan"])
    check("send 通道未配置 → 退出 2", code == 2 and json.loads(out)["status"] == "failed", f"code={code} {out[:200]}")
    code, out, err = run_cli(tmp, ["send", "--title", "x", "--channel", "mock"])
    check("send mock → 退出 0", code == 0 and json.loads(out)["status"] == "sent", f"code={code} {out[:200]}")
    code, out, err = run_cli(tmp, ["test", "--mock"])
    check("test --mock 退出 0 且 stdout 单行 JSON", code == 0 and out.count("\n") == 0, f"code={code}")
    check("test 的 stderr 里有完整 markdown", "### ⏳ Kimi 需要你：测试通道" in err, err[-300:])
    check("test 报告 link_source", json.loads(out).get("link_source") in ("rc", "fallback"), out[:200])


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
    key_fields = {"wecom_bot": ("wecom_bot", "key"), "serverchan": ("serverchan", "sckey"), "pushplus": ("pushplus", "token")}
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
