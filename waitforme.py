"""waitforme.py —— AI 卡住要你拍板时，把决策请求推到微信，点开就是手机上的远程控制页。

子命令：`init` / `link` / `send` / `ask` / `test` / `status`。

输出纪律（照搬 telephonist 的约定）：stdout 永远只有最后一行结果 JSON（UTF-8、
`ensure_ascii=False`），所有过程诊断走 stderr。Windows 下不 reconfigure 编码的话中文
会乱码，上游 `tail -1 | jq` 直接失败。

退出码：0 成功 / 2 业务失败（invalid、timeout、通道未配置、被限流）/ 1 程序异常。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError) as _exc:  # pragma: no cover - 极老的终端
        print(f"[waitforme] stdout/stderr 编码重配置失败：{_exc}", file=sys.stderr)

HERE = Path(__file__).resolve().parent
# 环境变量可整体换掉配置文件位置：自测和「多套通道配置」都靠它，不必改代码
CONFIG_PATH = Path(os.environ.get("WAITFORME_CONFIG") or HERE / "config.json")
EXAMPLE_PATH = HERE / "config.example.json"
RUN_DIR = HERE / "run"
INBOX_DIR = HERE / "inbox"
LOGS_DIR = HERE / "logs"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BUSINESS = 2

URGENCIES = ("normal", "high")
# inbox 里的答案文件名要落到磁盘上，Windows 非法字符必须先洗掉
SANITIZE_RE = re.compile(r"[^0-9A-Za-z._\-]+")

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "channel": "mock",
    "channels": {
        "wecom_bot": {"key": ""},
        "serverchan": {"sckey": ""},
        "pushplus": {"token": ""},
    },
    "hook_token": "",
    "hook_host": "127.0.0.1",
    "hook_port": 8790,
    "rc_json_path": "",
    "fallback_url": "http://10.204.69.40:8791/",
    "stop_mode": "smart",
    "stop_min_duration_s": 120,
    "min_interval_s": 180,
    "max_per_10min": 2,
    "daily_limit": 20,
    "quiet_hours": {"start": "23:00", "end": "08:30"},
    "ask_default_deadline_s": 600,
}


def log(message: str) -> None:
    """过程输出：一律走 stderr，stdout 留给最后一行 JSON。"""
    print(message, file=sys.stderr, flush=True)


def emit(result: dict[str, Any], exit_code: int) -> int:
    """stdout 上唯一的一行 JSON。"""
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return exit_code


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """浅合并，但 `channels` 这种嵌套字典单独并一层，避免用户填的 key 被默认值吃掉。"""
    out = dict(base)
    for key, value in (extra or {}).items():
        if key == "channels" and isinstance(value, dict):
            merged = dict(base.get("channels") or {})
            for name, sub in value.items():
                slot = dict(merged.get(name) or {})
                if isinstance(sub, dict):
                    slot.update(sub)
                merged[name] = slot
            out["channels"] = merged
        else:
            out[key] = value
    return out


def load_config(path: Path | None = None) -> dict[str, Any]:
    """读 config.json；不存在/坏了都回落到内置默认值，绝不因为配置问题崩掉。"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝，别把默认模板改脏
    target = Path(path) if path else CONFIG_PATH
    try:
        user = json.loads(target.read_text(encoding="utf-8-sig"))
        if isinstance(user, dict):
            cfg = _merge(cfg, user)
    except FileNotFoundError:
        log(f"[config] {target} 不存在，用内置默认值（先跑 `python waitforme.py init`）")
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        log(f"[config] 读取 {target} 失败：{type(exc).__name__}: {exc}，用内置默认值")
    cfg.setdefault("log_dir", str(LOGS_DIR))
    cfg.setdefault("run_dir", str(RUN_DIR))
    cfg.setdefault("inbox_dir", str(INBOX_DIR))
    return cfg


# ---------------------------------------------------------------------------
# 推送编排（CLI 与 hooks 共用）
# ---------------------------------------------------------------------------


def push_message(
    cfg: dict[str, Any],
    *,
    kind: str,
    title: str,
    body_md: str,
    urgency: str = "normal",
    channel: str | None = None,
    fingerprint_value: str | None = None,
) -> dict[str, Any]:
    """组装链接 → 发送 → 记推送日志。返回 `{ok, channel, status_code, error, url, ...}`。

    `enabled:false` 时不发送，直接回 `disabled`；这不影响 hooks 的 HTTP 应答。
    """
    import channels
    import linker
    import policy

    links = linker.build_links(cfg)
    url = links["primary_url"]
    if not cfg.get("enabled", True):
        return {"ok": False, "channel": channels.resolve_channel(cfg, channel), "status_code": 0,
                "error": "disabled", "url": url, "link_source": links["source"], "skipped": True}

    result = channels.send(cfg, title, body_md, url, urgency, channel=channel)
    fp = fingerprint_value or policy.fingerprint(title, body_md)
    try:
        policy.record_push(cfg, kind, fp, result.get("channel", "?"), result.get("ok", False),
                           {"status_code": result.get("status_code"), "error": result.get("error"),
                            "title": title[:120], "link_source": links["source"]})
    except OSError as exc:
        log(f"[policy] 写 push_log 失败：{exc}")
    result["url"] = url
    result["link_source"] = links["source"]
    result["fingerprint"] = fp
    return result


# ---------------------------------------------------------------------------
# ask payload：校验 + markdown
# ---------------------------------------------------------------------------


def validate_payload(raw: Any) -> tuple[dict[str, Any] | None, str]:
    """结构化决策请求的校验。返回 `(规范化 payload 或 None, 失败原因)`。

    规则：id/title/situation 非空；options ≥2 且 key 唯一、label 非空；
    `default_if_no_answer` 必须是已有 key（留空取第一个）；urgency ∈ {high, normal}。
    """
    if not isinstance(raw, dict):
        return None, "payload 必须是 JSON 对象"
    text = {key: str(raw.get(key) or "").strip() for key in ("id", "title", "situation", "default_if_no_answer", "urgency")}
    for key in ("id", "title", "situation"):
        if not text[key]:
            return None, f"{key} 不能为空"

    options = raw.get("options")
    if not isinstance(options, list) or len(options) < 2:
        return None, "options 至少 2 项"
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, option in enumerate(options):
        if not isinstance(option, dict):
            return None, f"options[{index}] 必须是对象"
        key = str(option.get("key") or "").strip()
        label = str(option.get("label") or "").strip()
        if not key:
            return None, f"options[{index}].key 不能为空"
        if key in seen:
            return None, f"options key 重复：{key}"
        if not label:
            return None, f"options[{index}].label 不能为空"
        seen.add(key)
        normalized.append({"key": key, "label": label, "tradeoff": str(option.get("tradeoff") or "").strip()})

    urgency = (text["urgency"] or "normal").lower()
    if urgency not in URGENCIES:
        return None, f"urgency 只能是 {'/'.join(URGENCIES)}，收到 {text['urgency']}"

    default = text["default_if_no_answer"] or normalized[0]["key"]
    if default not in seen:
        return None, f"default_if_no_answer 不在 options 里：{default}"

    return {
        "id": text["id"],
        "title": text["title"],
        "situation": text["situation"],
        "options": normalized,
        "default_if_no_answer": default,
        "urgency": urgency,
        "context": str(raw.get("context") or "").strip(),
    }, ""


def ask_body_md(payload: dict[str, Any], deadline_s: int | None = None) -> str:
    """把决策请求排成企业微信能读的 markdown：不用 `**`，靠引用块和换行分层次。"""
    lines = [f"> {payload['situation']}", ""]
    for option in payload["options"]:
        lines.append(f"`{option['key']}` {option['label']}")
        if option["tradeoff"]:
            lines.append(f"　取舍：{option['tradeoff']}")
        lines.append("")
    default = payload["default_if_no_answer"]
    lines.append("> " + "｜".join([
        f"回一个字母即可（默认 {default}）",
        f"不回则自动按 {default} 执行",
    ]))
    if deadline_s:
        until = (datetime.now() + timedelta(seconds=deadline_s)).strftime("%H:%M")
        lines.append(f"> {deadline_s} 秒后截止（约 {until}）")
    if payload.get("context"):
        lines.append("")
        lines.append(f"补充：{payload['context'][:600]}")
    return "\n".join(lines).strip()


def answer_path(payload_id: str, cfg: dict[str, Any]) -> Path:
    """`--wait` 轮询的答案文件路径：`inbox/<清洗后的 id>.answer.json`。"""
    inbox = Path(str(cfg.get("inbox_dir") or INBOX_DIR))
    safe_id = SANITIZE_RE.sub("-", payload_id).strip("-") or "ask"
    return inbox / f"{safe_id}.answer.json"


def read_answer(path: Path) -> dict[str, Any] | None:
    """读用户回复的文件。半截 JSON / 空文件 / 顶层不是对象都当「还没回」。"""
    try:
        raw = path.read_text(encoding="utf-8-sig", errors="replace").strip()
    except (OSError, ValueError):
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    target = CONFIG_PATH
    if target.exists() and not args.force:
        cfg = load_config(target)
        token = str(cfg.get("hook_token") or "")
        if not token:
            token = secrets.token_urlsafe(24)
            cfg["hook_token"] = token
            target.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            log("[init] config.json 已有但缺 hook_token，已补齐")
        return emit({"status": "exists", "config": str(target), "hook_token": token, "regenerated": False}, EXIT_OK)

    try:
        base = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8-sig"))
        if not isinstance(base, dict):
            base = {}
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        log(f"[init] 读 config.example.json 失败：{type(exc).__name__}: {exc}")
        base = {}
    base["hook_token"] = secrets.token_urlsafe(24)
    target.write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")
    return emit({"status": "created", "config": str(target), "hook_token": base["hook_token"], "regenerated": True}, EXIT_OK)


def cmd_link(args: argparse.Namespace) -> int:
    import linker

    cfg = load_config()
    links = linker.build_links(cfg)
    result = {"primary_url": links["primary_url"], "fallback_url": links["fallback_url"], "source": links["source"]}
    # 两种 source 都是可用链接，所以都是成功；「是不是远程控制真链接」由 source 表达
    if result["source"] != "rc":
        log("[link] 没读到 rc.json 的远程控制链接，本次给的是局域网答题页 fallback_url")
    return emit(result, EXIT_OK)


def cmd_send(args: argparse.Namespace) -> int:
    cfg = load_config()
    urgency = args.urgency or "normal"
    body = args.body if args.body is not None else "> 手动测试：看一眼链接能不能打开手机上的会话页"
    title = f"⏳ Kimi 需要你：{args.title}"
    if args.dry_run:
        import channels
        import linker

        links = linker.build_links(cfg)
        preview = channels.build_content(title, body, links["primary_url"], urgency)
        log("\n" + preview + "\n")
        return emit({"status": "dry_run", "channel": channels.resolve_channel(cfg, args.channel),
                     "url": links["primary_url"], "link_source": links["source"], "markdown": preview}, EXIT_OK)

    result = push_message(cfg, kind="manual", title=title, body_md=body, urgency=urgency, channel=args.channel)
    ok = bool(result.get("ok"))
    if not ok:
        log(f"[send] 发送失败：{result.get('channel')} {result.get('error')}")
    return emit({"status": "sent" if ok else "failed", **result}, EXIT_OK if ok else EXIT_BUSINESS)


def cmd_ask(args: argparse.Namespace) -> int:
    cfg = load_config()
    payload_path = Path(args.payload)
    try:
        raw = json.loads(payload_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return emit({"status": "invalid", "reason": f"payload 文件不存在：{payload_path}"}, EXIT_BUSINESS)
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        return emit({"status": "invalid", "reason": f"payload 读取/解析失败：{type(exc).__name__}: {exc}"}, EXIT_BUSINESS)

    payload, reason = validate_payload(raw)
    if payload is None:
        log(f"[ask] payload 不合法：{reason}")
        return emit({"status": "invalid", "reason": reason}, EXIT_BUSINESS)

    deadline_s = args.deadline_s if args.deadline_s else int(cfg.get("ask_default_deadline_s") or 600)
    title = f"⏳ 要你拍板：{payload['title']}"
    body_md = ask_body_md(payload, deadline_s if args.wait else None)

    if args.dry_run:
        import channels
        import linker

        links = linker.build_links(cfg)
        preview = channels.build_content(title, body_md, links["primary_url"], payload["urgency"])
        log("\n" + preview + "\n")
        return emit({"status": "dry_run", "id": payload["id"], "url": links["primary_url"], "markdown": preview}, EXIT_OK)

    result = push_message(cfg, kind="ask", title=title, body_md=body_md,
                          urgency=payload["urgency"], channel=args.channel)
    if not result.get("ok"):
        return emit({"status": "failed", "id": payload["id"], **{k: result.get(k) for k in ("channel", "status_code", "error", "url")}},
                    EXIT_BUSINESS)
    if not args.wait:
        return emit({"status": "sent", "id": payload["id"], "url": result["url"], "channel": result["channel"]}, EXIT_OK)

    path = answer_path(payload["id"], cfg)
    log(f"[ask] 等回答：{path.name}，最多 {deadline_s}s（每 3s 查一次）")
    return wait_for_answer(payload, path, deadline_s, result["url"])


def wait_for_answer(payload: dict[str, Any], path: Path, deadline_s: int, url: str) -> int:
    """轮询答案文件直到出现或超时。手机回话的唯一通道就是这个文件。"""
    started = time.time()
    while True:
        answer = read_answer(path)
        if answer is not None:
            raw_choice = str(answer.get("choice") or "").strip().upper()
            note = str(answer.get("note") or "")
            valid_keys = {o["key"].upper() for o in payload["options"]}
            default = payload["default_if_no_answer"]
            if raw_choice and raw_choice not in valid_keys:
                # 回了个不存在的字母：按默认项走，但把原值交出去，别让上游猜
                log(f"[ask] 回答的选项 {raw_choice!r} 不在 options 里，按默认项 {default} 执行")
                return emit({"status": "answered", "choice": default, "note": note, "answer_file": str(path),
                             "id": payload["id"], "url": url, "raw_choice": raw_choice,
                             "choice_source": "invalid_reply_fallback"}, EXIT_OK)
            return emit({"status": "answered",
                         "choice": raw_choice or default,
                         "note": note,
                         "answer_file": str(path),
                         "id": payload["id"],
                         "url": url,
                         "choice_source": "reply" if raw_choice else "default_no_choice"}, EXIT_OK)
        if time.time() - started >= deadline_s:
            return emit({"status": "timeout", "default_applied": payload["default_if_no_answer"],
                         "id": payload["id"], "waited_s": round(time.time() - started, 1),
                         "answer_file": str(path), "url": url}, EXIT_BUSINESS)
        time.sleep(min(3.0, max(0.5, deadline_s - (time.time() - started))))


def cmd_test(args: argparse.Namespace) -> int:
    cfg = load_config()
    channel = "mock" if args.mock else None
    import channels
    import linker

    links = linker.build_links(cfg)
    demo_title = "⏳ Kimi 需要你：测试通道"
    demo_body = "\n".join([
        "> 这是一条自检消息，用来确认通道和跳转链接都通。",
        "",
        "`A` 一切正常",
        "`B` 链接打不开",
        "",
        "> 回一个字母即可（默认 A）",
    ])
    content = channels.build_content(demo_title, demo_body, links["primary_url"], "normal")
    log("\n----- 将要发送的完整 markdown -----\n")
    log(content)
    log("\n----- 结束 -----\n")

    result: dict[str, Any] = {"status": "preview", "channel": channels.resolve_channel(cfg, channel),
                              "link_source": links["source"], "url": links["primary_url"],
                              "markdown_bytes": len(content.encode("utf-8")), "markdown": content}
    if args.live:
        sent = push_message(cfg, kind="test", title=demo_title, body_md=demo_body, urgency="normal", channel=channel)
        result["live"] = {k: sent.get(k) for k in ("ok", "channel", "status_code", "error")}
        result["status"] = "sent" if sent.get("ok") else "failed"
        log(f"[test] live 结果：{result['live']}")
    return emit(result, EXIT_OK if (not args.live or result["status"] == "sent") else EXIT_BUSINESS)


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config()
    import linker
    import policy

    run_dir = Path(str(cfg.get("run_dir") or RUN_DIR))
    pid = None
    try:
        pid = int((run_dir / "hooks.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid = None
    result = {
        "pid_exists": bool(pid and linker.pid_alive(pid)),
        "pid": pid,
        "port": int(cfg.get("hook_port") or 8790),
        "enabled": bool(cfg.get("enabled", True)),
        "channel": str(cfg.get("channel") or "mock"),
        "today_count": policy.count_today(cfg),
        "last_push_ts": policy.last_push_ts(cfg),
    }
    return emit(result, EXIT_OK)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="waitforme.py",
        description="AI 需要你时推一条带跳转链接的微信",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="从 config.example.json 生成 config.json 并写入随机 hook_token")
    p_init.add_argument("--force", action="store_true", help="覆盖已有 config.json")
    p_init.set_defaults(func=cmd_init)

    p_link = sub.add_parser("link", help="输出当前该用的跳转链接（rc.json 优先，读不到用 fallback）")
    p_link.set_defaults(func=cmd_link)

    p_send = sub.add_parser("send", help="手动推一条消息")
    p_send.add_argument("--title", required=True)
    p_send.add_argument("--body", default=None, help="markdown 正文（不用 **，企微不认）")
    p_send.add_argument("--urgency", choices=URGENCIES, default="normal")
    p_send.add_argument("--dry-run", action="store_true")
    p_send.add_argument("--channel", choices=list(channels_choice_names()), default=None)
    p_send.set_defaults(func=cmd_send)

    p_ask = sub.add_parser("ask", help="结构化决策请求推送，可选等待手机回答")
    p_ask.add_argument("--payload", required=True, help="q.json 路径")
    p_ask.add_argument("--wait", action="store_true", help="轮询 inbox/<id>.answer.json 直到回答或超时")
    p_ask.add_argument("--deadline-s", type=int, default=None, dest="deadline_s")
    p_ask.add_argument("--dry-run", action="store_true")
    p_ask.add_argument("--channel", choices=list(channels_choice_names()), default=None)
    p_ask.set_defaults(func=cmd_ask)

    p_test = sub.add_parser("test", help="打印完整 markdown 消息体；--live 真发一条自检消息")
    p_test.add_argument("--mock", action="store_true", help="强制用 mock 通道")
    p_test.add_argument("--live", action="store_true")
    p_test.set_defaults(func=cmd_test)

    p_status = sub.add_parser("status", help="服务与通道状态")
    p_status.set_defaults(func=cmd_status)
    return parser


def channels_choice_names() -> tuple[str, ...]:
    import channels

    return channels.VALID_CHANNELS


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SystemExit:
        raise
    except KeyboardInterrupt:
        return emit({"status": "interrupted"}, EXIT_BUSINESS)
    except Exception as exc:
        log(traceback.format_exc())
        return emit({"status": "error", "reason": f"{type(exc).__name__}: {exc}"}, EXIT_ERROR)


if __name__ == "__main__":
    sys.exit(main())
