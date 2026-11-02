"""channels.py —— 推送通道适配器。

统一入口 `send(cfg, title, body_md, url, urgency) -> dict`，返回值形如
`{"ok":bool,"channel":str,"status_code":int,"error":str|None}`。

三条硬规矩：

1. **这里绝不抛异常**。网络失败、通道没配 key、返回体看不懂，统统折成 `ok:false`
   + 一个能读的 `error` 串。推送是锦上添花，把主流程炸掉是不可接受的。
2. **链接必须活着**。整条消息的存在理由就是那个可点开的远程控制链接，正文再长也不能
   把它挤掉 —— 截断只作用在正文上，发出去前还会再校验一次。
3. **出口一律脱敏**。正文里的 `sk-xxx`、32 位以上随机串、`key=`/`token=` 参数值，以及
   异常串里的凭证，都不许原样离开本机（它们会进 push_log、也会进 Agent 的对话记录）。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import requests

_append_lock = threading.Lock()

HERE = Path(__file__).resolve().parent
NET_TIMEOUT_S = 10
CLOSER = "[👉 点击打开会话继续对话]({url})"
NO_LINK_NOTE = "> 本次没有可用跳转链接，请回到电脑前操作"
VALID_CHANNELS = ("mock", "wecom_bot", "serverchan", "pushplus")

# 分通道上限：企微 markdown 正文 4096 字节（留余量），ServerChan 标题实测超 32 字直接拒收
CHANNEL_LIMITS: dict[str, dict[str, int]] = {
    "wecom_bot": {"max_bytes": 4000, "title_chars": 40},
    "serverchan": {"max_bytes": 30000, "title_chars": 32},
    "pushplus": {"max_bytes": 30000, "title_chars": 100},
    # mock 与企微对齐，`test` 里看到的排版就是真发出去的样子
    "mock": {"max_bytes": 4000, "title_chars": 40},
}

# 内容脱敏：会离开本机的正文里不该有这些
_CONTENT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"), "sk-***"),
    (re.compile(r"(?i)([?&](?:key|token|sckey|secret|password|passwd|api[_-]?key)=)[^&\s\"']+"), r"\1***"),
    (re.compile(r"(?i)(://[^/\s:@]+:)[^/\s@]+(@)"), r"\1***\2"),
    (re.compile(r"\b[A-Za-z0-9][A-Za-z0-9_\-]{30,}\b"), "***"),
]


def redact_secrets(text: str) -> str:
    """把正文里的密钥/长随机串/凭证型参数值打码。

    只该用在标题与正文上：跳转链接本身要原样保留，否则用户点开就是 404。
    """
    if not isinstance(text, str) or not text:
        return text or ""
    out = text
    for pattern, repl in _CONTENT_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def _redact_error(text: str) -> str:
    """异常/响应串脱敏。requests 会把带 `?key=SECRET` 的完整 URL 写进异常消息里。"""
    cleaned = redact_secrets(str(text or ""))
    return re.sub(r"\s+", " ", cleaned).strip()[:240]


def _host_of(text: str) -> str:
    match = re.search(r"https?://([^/\s:]+)", str(text or ""))
    return match.group(1) if match else ""


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


def _result(ok: bool, channel: str, status_code: int = 0, error: str | None = None) -> dict[str, Any]:
    return {"ok": bool(ok), "channel": channel, "status_code": int(status_code), "error": error}


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """按 UTF-8 字节数截断正文。尾部的残缺多字节序列直接丢弃，不留半个汉字给企微报错。"""
    if max_bytes <= 0:
        return ""
    raw = (text or "").encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text or ""
    suffix = "\n\n…（内容过长已截断）"
    limit = max(0, max_bytes - len(suffix.encode("utf-8")))
    return raw[:limit].decode("utf-8", errors="ignore") + suffix


def clip_title(title: str, max_chars: int) -> str:
    """按通道上限截标题（ServerChan 超 32 字会整条被拒）。"""
    text = (title or "").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if max_chars == 1:
        return text[:1]
    return text[: max_chars - 1] + "…"


def build_content(title: str, body_md: str, url: str, urgency: str = "normal", max_bytes: int | None = None) -> str:
    """组一条通道通用的 markdown 正文：`###` 标题 + 正文 + 末尾链接。

    排版只用企业微信 markdown 认的东西（`###`、`>`、换行、`[]()`）；实测 `**加粗**`
    在企微里原样显示成星号，所以一律不用星号，重要信息靠引用块和独立成行突出。

    `max_bytes` 是整条消息的预算。**截断只吃正文**：先扣掉标题行和链接行的固定开销，
    再按剩余空间截正文，最后校验链接还在不在 —— 正文再长也不许把链接挤掉，这是这条
    消息存在的意义。正文超到连标题+链接都放不下时，缩标题、保链接。
    """
    mark = "🔴 " if str(urgency).lower() == "high" else ""
    safe_title = redact_secrets(title) or "Kimi 需要你"
    body = redact_secrets((body_md or "").strip())
    closer = CLOSER.format(url=url) if url else NO_LINK_NOTE

    def head_of(t: str) -> str:
        return f"### {mark}{t}"

    def join(head: str, b: str) -> str:
        parts = [head]
        if b:
            parts.append(b)
        parts.append(closer)
        return "\n\n".join(parts)

    content = join(head_of(safe_title), body)
    if max_bytes is None:
        return content
    if _fits(content, max_bytes):
        return content

    # 固定开销 = 标题行 + 链接行 + 中间的空行分隔
    overhead = len(join(head_of(safe_title), "").encode("utf-8")) + 2
    budget = max_bytes - overhead
    if budget < 0:
        # 连标题+链接都超预算：砍标题长度，链接一个字不动
        for keep in (20, 12, 8, 4, 1):
            short_head = head_of(clip_title(safe_title, keep))
            overhead = len(join(short_head, "").encode("utf-8")) + 2
            budget = max_bytes - overhead
            if budget >= 0:
                content = join(short_head, _truncate_utf8(body, budget))
                break
        else:
            content = f"### {mark}\n\n{closer}"
    else:
        content = join(head_of(safe_title), _truncate_utf8(body, budget))

    if url and url not in content:
        # 最后一道保险：链接没了就把正文整段丢掉，只留标题+链接（宁可少信息不可没链接）
        content = join(head_of(clip_title(safe_title, 20)), "")
    return content


def _fits(text: str, max_bytes: int) -> bool:
    return len(text.encode("utf-8")) <= max_bytes


def _channel_cfg(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    raw = (cfg or {}).get("channels")
    if isinstance(raw, dict) and isinstance(raw.get(name), dict):
        return raw[name]
    return {}


def channel_limits(channel: str) -> dict[str, int]:
    return dict(CHANNEL_LIMITS.get(channel) or {"max_bytes": 30000, "title_chars": 100})


def resolve_channel(cfg: dict[str, Any], override: str | None = None) -> str:
    """本次用哪个通道：CLI 覆盖 > config.channel > 没配当 mock。

    非法名字原样返回，由 `send` 明确报 `unknown_channel` —— 写错配置不该被静默当成成功。
    """
    name = str(override or (cfg or {}).get("channel") or "").strip().lower()
    return name if name in VALID_CHANNELS else (name or "mock")


def send(cfg: dict[str, Any], title: str, body_md: str, url: str, urgency: str = "normal", channel: str | None = None) -> dict[str, Any]:
    """发送。`channel` 为 None 时取 `cfg["channel"]`。任何失败都返回 `ok:false`。"""
    cfg = cfg or {}
    name = resolve_channel(cfg, channel)
    limits = channel_limits(name)
    t_title = clip_title(title or "Kimi 需要你", limits["title_chars"])
    content = build_content(t_title, body_md, url, urgency, max_bytes=limits["max_bytes"])
    try:
        if name == "mock":
            return _send_mock(cfg, t_title, content, url, urgency)
        if name == "wecom_bot":
            return _send_wecom(cfg, content)
        if name == "serverchan":
            return _send_serverchan(cfg, t_title, content)
        if name == "pushplus":
            return _send_pushplus(cfg, t_title, content)
        return _result(False, name, 0, "unknown_channel")
    except requests.RequestException as exc:
        # 异常消息里可能带完整 webhook URL，只留得住主机名 + 异常类型
        host = _host_of(str(exc)) or _host_of(getattr(getattr(exc, "request", None), "url", ""))
        detail = _redact_error(exc)
        return _result(False, name, 0, f"network_error:{type(exc).__name__}@{host}:{detail}"[:240])
    except Exception as exc:  # 通道实现的兜底，绝不让异常穿出去
        return _result(False, name, 0, f"{type(exc).__name__}:{_redact_error(exc)}"[:240])


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
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send",
        params={"key": key},
        json=body,
        timeout=NET_TIMEOUT_S,
    )
    return _judge(resp, "wecom_bot")


def _send_serverchan(cfg: dict[str, Any], title: str, content: str) -> dict[str, Any]:
    sckey = str(_channel_cfg(cfg, "serverchan").get("sckey") or "").strip()
    if not sckey:
        return _result(False, "serverchan", 0, "channel_not_configured")
    resp = requests.post(
        f"https://sctapi.ftqq.com/{sckey}.send",
        data={"title": title, "desp": content},
        timeout=NET_TIMEOUT_S,
    )
    return _judge(resp, "serverchan")


def _send_pushplus(cfg: dict[str, Any], title: str, content: str) -> dict[str, Any]:
    token = str(_channel_cfg(cfg, "pushplus").get("token") or "").strip()
    if not token:
        return _result(False, "pushplus", 0, "channel_not_configured")
    resp = requests.post(
        "https://www.pushplus.plus/send",
        json={"token": token, "title": title, "content": content, "template": "markdown"},
        timeout=NET_TIMEOUT_S,
    )
    return _judge(resp, "pushplus")


def _judge(resp: "requests.Response", channel: str) -> dict[str, Any]:
    code = resp.status_code
    ok = code == 200 and _biz_ok(resp)
    return _result(ok, channel, code, None if ok else _biz_error(resp, code))


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
        return f"http_{code}:{_redact_error(msg)}"
    return f"http_{code}:{_redact_error(resp.text)}"


def _safe_json(resp: "requests.Response") -> dict[str, Any] | None:
    try:
        data = resp.json()
    except (ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
