"""connect_kimi.py —— 打印「把 Kimi Code 的 hook 事件转给 waitforme」需要的配置片段。

为什么不是自动改配置：`~/.kimi-code/config.toml` 是用户的文件，里面还有模型、provider、
权限这些要命的东西。这个脚本只做两件事 —— 生成片段、生成可直接 review 的 patch；
写不写、什么时候写，由用户决定。

Kimi Code 的 hook 是「跑一个命令 + 事件 JSON 从 stdin 进来」，不是「POST 一个 URL」，
所以真正干活的是同目录的 kimi_hook.py（读 stdin → POST /hook?k=<token>）。它自己在
运行时读 config.json 拿 token，因此片段里不出现任何密钥。

用法：
    python connect_kimi.py                     # 打印片段 + patch
    python connect_kimi.py --write-patch out.patch
    python connect_kimi.py --target claude     # 顺带打印 Claude Code 的 settings.json 片段
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common as C  # noqa: E402

DEFAULT_EVENTS = ("Stop", "PermissionRequest", "Notification")
KIMI_CONFIG = Path(os.environ.get("KIMI_CODE_HOME") or Path.home() / ".kimi-code") / "config.toml"
BRIDGE = C.SCRIPTS_DIR / "kimi_hook.py"          # 旧：POST 到常驻服务
DIRECT = C.SCRIPTS_DIR / "hook_direct.py"        # 新：短命进程直发，零常驻
EXIT_OK = 0
EXIT_BUSINESS = 2


def toml_str(value: str) -> str:
    """TOML 字符串：路径里有反斜杠，优先用字面量字符串（单引号），省得用户数转义。"""
    if "'" not in value and "\n" not in value:
        return "'" + value + "'"
    return json.dumps(value, ensure_ascii=False)


def build_snippet(python_exe: str, bridge: Path, events: tuple[str, ...], mode: str = "direct") -> str:
    command = f'"{python_exe}" "{bridge}"'
    if mode == "direct":
        header = [
            "# === waitforme：零常驻直发（事件来了起进程 → 发微信 → 退出）===",
            "# 不依赖 8790 常驻服务；Stop 等非推送事件只写 logs/hook_direct.log",
        ]
    else:
        header = [
            "# === waitforme：把 Kimi Code 的 hook 事件转给本地常驻服务 ===",
            "# 需要 hooks.py serve 一直在跑；否则事件会被静默丢弃",
        ]
    lines = header + [
        "# 追加到 ~/.kimi-code/config.toml 末尾；[[hooks]] 只认 event/matcher/command/timeout 四个字段",
    ]
    for event in events:
        lines += [
            "",
            "[[hooks]]",
            f"event = {toml_str(event)}",
            f"command = {toml_str(command)}",
            "timeout = 10",
        ]
    return "\n".join(lines) + "\n"


def build_patch(config_path: Path, existing: str, snippet: str) -> str:
    old = existing.splitlines(keepends=True)
    if old and not old[-1].endswith("\n"):
        old[-1] += "\n"
        prefix = ["\n"]
    else:
        prefix = []
    new = old + prefix + snippet.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            old, new,
            fromfile=str(config_path),
            tofile=f"{config_path}（建议追加）",
            n=2,
        )
    )


def claude_snippet(python_exe: str, bridge: Path, events: tuple[str, ...]) -> str:
    """Claude Code 的 hooks 写在 settings.json 里，结构不同，这里只给最小可读版本。"""
    command = f'"{python_exe}" "{bridge}"'
    hooks = {event: [{"hooks": [{"type": "command", "command": command}]}] for event in events}
    return json.dumps({"hooks": hooks}, ensure_ascii=False, indent=2)


def render_report(project: Path, config: Path, events: tuple[str, ...] = DEFAULT_EVENTS,
                  target: str = "kimi", mode: str = "direct") -> dict:
    """把「要打印什么」算清楚，打印和 JSON 摘要共用一份事实。"""
    cfg = C.read_config(config)
    token = str(cfg.get("hook_token") or "")
    python_exe = sys.executable
    bridge = DIRECT if mode == "direct" else BRIDGE
    bridge_ok = bridge.is_file()

    snippet = build_snippet(python_exe, bridge, events, mode) if target == "kimi" else claude_snippet(python_exe, bridge, events)
    existing, config_exists = "", False
    if target == "kimi":
        try:
            existing = KIMI_CONFIG.read_text(encoding="utf-8-sig")
            config_exists = True
        except (OSError, ValueError):
            existing = ""
    already = bridge.name in existing
    needs_replace = mode == "direct" and "kimi_hook.py" in existing
    if target != "kimi":
        patch = ""
    elif needs_replace:
        # 旧 service 模式已经占用同样的三个事件；追加会双推，所以出「替换」patch
        replaced = existing.replace("kimi_hook.py", "hook_direct.py")
        patch = "".join(difflib.unified_diff(
            existing.splitlines(keepends=True),
            replaced.splitlines(keepends=True),
            fromfile=str(KIMI_CONFIG),
            tofile=f"{KIMI_CONFIG}（替换旧桥，不追加）",
            n=2,
        ))
    else:
        patch = build_patch(KIMI_CONFIG, existing, snippet)

    health_ok, health_detail = C.health_check(cfg)

    return {
        "target": target,
        "mode": mode,
        "project": str(project),
        "config": str(config),
        "python": python_exe,
        "bridge": str(bridge),
        "bridge_exists": bridge_ok,
        "config_file": str(KIMI_CONFIG) if target == "kimi" else ".claude/settings.json",
        "config_file_exists": config_exists,
        "already_configured": already,
        "needs_replace": needs_replace,
        "hook_token_configured": bool(token),
        "hook_url": C.hook_url(cfg, with_token=False) + "?k=***",
        "health_url": f"{C.hook_base(cfg)}/health",
        "service": {"running": health_ok, "detail": health_detail},
        "snippet": snippet,
        "patch": patch,
    }


def print_report(project: Path, config: Path, events: tuple[str, ...] = DEFAULT_EVENTS,
                 target: str = "kimi", mode: str = "direct") -> dict:
    report = render_report(project, config, events, target, mode)
    say = C.say
    say("")
    say("=" * 72)
    if not report["hook_token_configured"] and mode != "direct":
        say("⚠ config.json 里没有 hook_token，服务会拒绝所有请求：先跑 python install.py")
    if not report["bridge_exists"]:
        say(f"⚠ 桥脚本不存在：{report['bridge']}（skill 包不完整？）")
    if mode == "direct":
        say("· 零常驻模式：事件来了起一个短命进程、直发微信、立刻退出")
        say("· 不需要 hooks.py serve 常驻；Stop 等非推送事件只写 logs/hook_direct.log")
    else:
        if report["service"]["running"]:
            say(f"✓ 本地服务在跑：{report['health_url']}")
        else:
            say(f"· 本地服务还没起（{report['service']['detail']}）—— 片段可以先配，服务记得起：")
            say(f"    python \"{project / 'hooks.py'}\" serve --detach")

    if target == "kimi":
        say("")
        if report["needs_replace"]:
            say("⚠ 检测到旧 service 模式的 kimi_hook.py：**不要追加新片段**，否则同一事件会双推。")
            say("  最省事：把 config.toml 里 3 处 command 的 kimi_hook.py 改成 hook_direct.py（见下面的替换 patch）。")
            say("  确认不再需要常驻服务后，再 `python hooks.py stop`。")
            say("")
            say("下面是完整片段，仅供参考结构（事件与 timeout 不变）：")
        else:
            say(f"把下面这段追加到 {report['config_file']} 末尾（这个文件归你，本脚本不自动改）：")
        say("-" * 72)
        say(report["snippet"].rstrip())
        say("-" * 72)
        if report["already_configured"] and not report["needs_replace"]:
            say(f"⚠ 这个 config.toml 里已经出现过 {Path(report['bridge']).name}，别重复追加，否则同一事件会跑两遍。")
        if report["config_file_exists"]:
            say("")
            if report["needs_replace"]:
                say("替换 patch（确认无误再自己动手）：")
            else:
                say("patch 预览（等价于上面那次追加；确认无误再自己动手）：")
            say(report["patch"].rstrip() or "（patch 为空：内容已存在）")
        else:
            say(f"· {report['config_file']} 还不存在，新会话里 Kimi Code 会自己创建它。")
    else:
        say("")
        say("Claude Code 的 hooks 写在 .claude/settings.json（项目或用户级都行）：")
        say("-" * 72)
        say(report["snippet"])
        say("-" * 72)
        say("· 各版本 schema 略有差异，以你本机 Claude Code 的 hooks 文档为准。")

    say("")
    say("改完先用一条手工事件验证（不会真打扰你，除非策略判定该推）：")
    sample = json.dumps({"hook_event_name": "Stop", "session_id": "manual-test", "cwd": str(Path.cwd())},
                        ensure_ascii=False)
    say(f"  echo '{sample}' | python \"{report['bridge']}\" --debug")
    say("=" * 72)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="connect_kimi.py", description="打印 Kimi Code hooks 接入片段（不改配置）")
    parser.add_argument("--project", default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--event", action="append", choices=list(DEFAULT_EVENTS) + ["SubagentStop"], default=None,
                        help="要接的事件，可重复；默认 Stop/PermissionRequest/Notification")
    parser.add_argument("--target", choices=("kimi", "claude"), default="kimi")
    parser.add_argument("--mode", choices=("direct", "service"), default="direct",
                        help="direct=零常驻直发（默认）；service=POST 到常驻服务")
    parser.add_argument("--write-patch", default=None, help="把 patch 写到这个文件，方便 git apply / 人工 review")
    args = parser.parse_args(argv)

    project = C.resolve_project(args.project)
    if project is None:
        C.say("[connect] 找不到 waitforme 项目，先跑 install.py（或加 --project）")
        C.emit({"status": "failed", "reason": "project_not_found"})
        return EXIT_BUSINESS
    C.write_state(project)
    config = C.config_path_for(project, args.config)
    events = tuple(args.event) if args.event else DEFAULT_EVENTS

    report = print_report(project, config, events, args.target, args.mode)

    if args.write_patch:
        path = Path(os.path.expanduser(args.write_patch))
        text = report["patch"] or report["snippet"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        C.say(f"[connect] patch 已写入 {path}（只是文本，没有应用）")

    C.emit({"status": "ok", **{k: report[k] for k in (
        "target", "mode", "project", "config", "bridge", "bridge_exists", "config_file",
        "config_file_exists", "already_configured", "hook_token_configured")},
        "service_running": report["service"]["running"],
        "patch_written": bool(args.write_patch)})
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(C.run_main(lambda: main()))
