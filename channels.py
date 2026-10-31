"""channels.py —— 推送通道适配器。

统一入口 `send(cfg, title, body_md, url, urgency) -> dict`，返回值形如
`{"ok":bool,"channel":str,"status_code":int,"error":str|None}`。

约定：**这里绝不抛异常**。网络失败、通道没配 key、返回体看不懂，统统折成
`ok:false` + 一个能读的 `error` 串，由调用方决定记日志还是降级。推送是锦上添花，
把主流程炸掉是不可接受的。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import requests


_append_lock = threading.Lock()


def append_line(path: Path, line: str) -> None:
    """整行追加，进程内串行。

    Windows 的 `O_APPEND` 不是原子追加：UCRT 用「每次写之前 seek 到末尾」模拟它，
    20 个线程并发写同一个日志时会互相覆盖 —— 实测丢过 3 条 mock 记录和 6 条计数记录。
    限流计数丢一条就意味着多推一条骚扰消息，所以这里必须上锁（量很小，串行无成本）。
    """
    payload = (line + "\n").encode("utf-8", errors="replace")
    with _append_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o666)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)

HERE = Path(__file__).resolve().parent
NET_TIMEOUT_S = 10
# 企业微信 markdown 正文上限 4096 字节；留足余量，超限会被整条丢弃
WECOM_MAX_BYTES = 4000

CLOSER = "[👉 点击打开会话继续对话]({url})"
VALID_CHANNELS = ("mock", "wecom_bot", "serverchan", "pushplus")


def _result(ok: bool, channel: str, status_code: int = 0, error: str | None = None) -> dict[str, Any]:
    return {"ok": bool(ok), "channel": channel, "status_code": int(status_code), "error": error}


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """按 UTF-8 字节数截断。尾部的残缺多字节序列直接丢弃，不留半个汉字给企微报错。"""
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text
    suffix = "\n\n…（内容过长已截断）"
    limit = max(0, max_bytes - len(suffix.encode("utf-8")))
    return raw[:limit].decode("utf-8", errors="ignore") + suffix


def build_content(title: str, body_md: str, url: str, urgency: str = "normal") -> str:
    """组一条通道通用的 markdown 正文。

    排版只用企业微信 markdown 认的东西：`###` 标题、`>` 引用、换行、`[]()` 链接。
    实测 `**加粗**` 在企微里原样显示成星号，所以这里一律不用星号强调，重要信息靠
    行首引用块和独立成行来突出。
    """
    mark = "🔴 " if str(urgency).lower() == "high" else ""
    head = f"### {mark}{title or 'Kimi 需要你'}"
    body = (body_md or "").strip()
    parts = [head]
    if body:
        parts.append(body)
    if url:
        parts.append(CLOSER.format(url=url))
    else:
        parts.append("> 本次没有可用跳转链接，请回到电脑前操作")
    return "\n\n".join(parts)


def _channel_cfg(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    raw = (cfg or {}).get("channels")
    if isinstance(raw, dict) and isinstance(raw.get(name), dict):
        return raw[name]
    return {}


def resolve_channel(cfg: dict[str, Any], override: str | None = None) -> str:
    """本次用哪个通道：CLI 覆盖 > config.channel > 都没有就当 mock 没配。"""
    name = str(override or (cfg or {}).get("channel") or "").strip().lower()
    return name if name in VALID_CHANNELS else (name or "mock")


def send(cfg: dict[str, Any], title: str, body_md: str, url: str, urgency: str = "normal", channel: str | None = None) -> dict[str, Any]:
    """发送。`channel` 为 None 时取 `cfg["channel"]`。任何失败都返回 `ok:false`。"""
    cfg = cfg or {}
    name = resolve_channel(cfg, channel)
    content = build_content(title, body_md, url, urgency)
    if name == "wecom_bot":
        content = _truncate_utf8(content, WECOM_MAX_BYTES)
    try:
        if name == "mock":
            return _send_mock(cfg, title, content, url, urgency)
        if name == "wecom_bot":
            return _send_wecom(cfg, content)
        if name == "serverchan":
            return _send_serverchan(cfg, title, content)
        if name == "pushplus":
            return _send_pushplus(cfg, title, content)
        return _result(False, name, 0, "unknown_channel")
    except requests.RequestException as exc:
        return _result(False, name, 0, f"network_error:{type(exc).__name__}:{exc}"[:300])
    except Exception as exc:  # 通道实现的兜底，绝不让异常穿出去
        return _result(False, name, 0, f"{type(exc).__name__}:{exc}"[:300])


def _send_mock(cfg: dict[str, Any], title: str, content: str, url: str, urgency: str) -> dict[str, Any]:
    """把最终 payload 追加进 logs/mock_sent.jsonl，用于本地验证与离线自测。"""
    payload = {
        "ts": round(time.time(), 3),
        "channel": "mock",
        "title": title,
        "urgency": urgency,
        "url": url,
        "content": content,
    }
    log_dir = Path(str(cfg.get("log_dir") or HERE / "logs"))
    append_line(log_dir / "mock_sent.jsonl", json.dumps(payload, ensure_ascii=False))
    return _result(True, "mock", 200, None)


def _send_wecom(cfg: dict[str, Any], content: str) -> dict[str, Any]:
    key = str(_channel_cfg(cfg, "wecom_bot").get("key") or "").strip()
    if not key:
        return _result(False, "wecom_bot", 0, "channel_not_configured")
    body = {"msgtype": "markdown", "markdown": {"content": content}}
    resp = requests.post(
        f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}",
        json=body,
        timeout=NET_TIMEOUT_S,
    )
    code = resp.status_code
    ok = code == 200 and _biz_ok(resp)
    return _result(ok, "wecom_bot", code, None if ok else _biz_error(resp, code))


def _send_serverchan(cfg: dict[str, Any], title: str, content: str) -> dict[str, Any]:
    sckey = str(_channel_cfg(cfg, "serverchan").get("sckey") or "").strip()
    if not sckey:
        return _result(False, "serverchan", 0, "channel_not_configured")
    resp = requests.post(
        f"https://sctapi.ftqq.com/{sckey}.send",
        data={"title": (title or "Kimi 需要你")[:100], "desp": content},
        timeout=NET_TIMEOUT_S,
    )
    code = resp.status_code
    ok = code == 200 and _biz_ok(resp)
    return _result(ok, "serverchan", code, None if ok else _biz_error(resp, code))


def _send_pushplus(cfg: dict[str, Any], title: str, content: str) -> dict[str, Any]:
    token = str(_channel_cfg(cfg, "pushplus").get("token") or "").strip()
    if not token:
        return _result(False, "pushplus", 0, "channel_not_configured")
    resp = requests.post(
        "https://www.pushplus.plus/send",
        json={"token": token, "title": title or "Kimi 需要你", "content": content, "template": "markdown"},
        timeout=NET_TIMEOUT_S,
    )
    code = resp.status_code
    ok = code == 200 and _biz_ok(resp)
    return _result(ok, "pushplus", code, None if ok else _biz_error(resp, code))


def _biz_ok(resp: "requests.Response") -> bool:
    """三家都返回 JSON，成功码分别是 errcode=0 / code=0 / code=200。"""
    data = _safe_json(resp)
    if not data:
        return False
    for field, good in (("errcode", 0), ("code", 0), ("code", 200)):
        if field in data and data.get(field) == good:
            return True
    return False


def _biz_error(resp: "requests.Response", code: int) -> str:
    data = _safe_json(resp)
    if data:
        msg = data.get("message") or data.get("errmsg") or data.get("error") or ""
        return f"http_{code}:{msg}"[:300]
    return f"http_{code}:{(resp.text or '')[:120]}"


def _safe_json(resp: "requests.Response") -> dict[str, Any] | None:
    try:
        data = resp.json()
    except (ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
