"""configure.py —— 三个问题改配置：用哪个通道、key 是什么、现在要不要接 hook。

和 install.py 的分工：install 负责「从零开始能跑」，configure 负责「改主意了」。
只写 config.json；接 hook 那一步只打印片段（改 ~/.kimi-code/config.toml 是用户的决定）。

用法：
    python configure.py
    python configure.py --channel wecom_bot --key <KEY> --non-interactive
    python configure.py --connect-now        # 顺手打印 Kimi hooks 片段
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common as C  # noqa: E402

EXIT_OK = 0
EXIT_BUSINESS = 2


def choose_channel(args: argparse.Namespace) -> str:
    if args.channel:
        return args.channel
    if args.non_interactive:
        raise SystemExit("非交互模式必须给 --channel")
    current = args.current_channel or "mock"
    C.say("\n① 用哪个通道？")
    for index, name in enumerate(C.CHANNEL_ORDER, start=1):
        mark = "（当前）" if name == current else ""
        C.say(f"  {index}) {name:<10} {C.CHANNELS[name]['label']}{mark}")
    raw = C.ask_text("输入序号或名字", default=current)
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(C.CHANNEL_ORDER):
            return C.CHANNEL_ORDER[idx]
    return raw if raw in C.CHANNELS else current


def choose_key(args: argparse.Namespace, channel: str) -> str:
    field = C.channel_field(channel)
    if not field:
        return ""
    if args.key:
        return args.key.strip()
    if args.non_interactive:
        return ""
    C.say(f"\n② {C.CHANNELS[channel]['label']} 的 {field}：{C.CHANNELS[channel]['where']}")
    C.say("   （回车 = 不修改现有值）")
    return C.ask_text(f"{field}", default="", secret=True).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="configure.py", description="waitforme 配置向导")
    parser.add_argument("--project", default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--channel", choices=list(C.CHANNEL_ORDER), default=None)
    parser.add_argument("--key", default=None)
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--connect-now", action="store_true", help="顺手打印 Kimi Code hooks 片段")
    args = parser.parse_args(argv)

    project = C.resolve_project(args.project)
    if project is None:
        C.say("[configure] 找不到 waitforme 项目，先跑 install.py --project <目录>（或用 --clone）")
        C.emit({"status": "failed", "reason": "project_not_found"})
        return EXIT_BUSINESS
    C.write_state(project)
    config = C.config_path_for(project, args.config)
    C.say(f"[configure] 项目 {project}")
    C.say(f"[configure] 配置 {config}")

    cfg = C.read_config(config)
    if not config.exists() or not cfg.get("hook_token"):
        # 没有配置就先按模板生成一份，别让用户在两支脚本之间来回跑
        code, out, err = C.run_script(project, "waitforme.py", ["init"], config=config)
        if code != 0:
            C.say(f"[configure] init 失败（exit={code}）：{(err or out).strip()[:400]}")
            C.emit({"status": "failed", "reason": "init_failed", "detail": (err or out).strip()[-400:]})
            return EXIT_BUSINESS
        cfg = C.read_config(config)
        C.say(f"[configure] 已生成 {config}")

    args.current_channel = str(cfg.get("channel") or "mock")

    try:
        channel = choose_channel(args)
        key = choose_key(args, channel)
    except SystemExit as exc:  # 非交互缺参数：当作业务失败，别抛栈
        C.say(f"[configure] {exc}")
        C.emit({"status": "failed", "reason": "missing_argument", "detail": str(exc)})
        return EXIT_BUSINESS

    field = C.channel_field(channel)
    before = str((cfg.get("channels") or {}).get(channel, {}).get(field) or "") if field else ""
    C.apply_channel(cfg, channel, key)
    C.write_config(config, cfg)
    stored = str((cfg.get("channels") or {}).get(channel, {}).get(field) or "") if field else ""

    connect = args.connect_now
    if not args.non_interactive and not args.connect_now:
        C.say("\n③ 现在就把 hook 接到 Kimi Code 吗？")
        connect = C.confirm("生成 Kimi Code hooks 片段（只打印，不改配置）", default=False)
    connect_done = False
    if connect:
        if not args.non_interactive:
            C.say("")
        import connect_kimi  # 延迟导入：不接 hook 的人不用付这份代码的注意力

        connect_kimi.print_report(project, config, events=connect_kimi.DEFAULT_EVENTS, target="kimi")
        connect_done = True
    else:
        C.say("   跳过。之后可以跑：python connect_kimi.py")

    warnings: list[str] = []
    if field and not stored:
        warnings.append(f"{channel} 的 {field} 还是空的，推送会失败")
    C.say(f"[configure] 通道已设为 {channel}")
    if key:
        C.say(f"[configure] {field} 已更新：{C.mask(stored)}")
    elif field and before:
        C.say(f"[configure] 保留原有 {field}：{C.mask(before)}")

    C.say("\n下一步：")
    C.say(f"  起服务：python \"{project / 'hooks.py'}\" serve --detach")
    C.say(f"  验证：  python \"{C.SCRIPTS_DIR / 'verify.py'}\" --project \"{project}\" --config \"{config}\"")
    C.say("  （服务每次请求都重读配置，改通道/key 不用重启）")

    C.emit({
        "status": "ok",
        "project": str(project),
        "config": str(config),
        "channel": channel,
        "key": C.mask(stored) if field else "not_required",
        "key_updated": bool(key),
        "connect_snippet_printed": connect_done,
        "warnings": warnings,
    })
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(C.run_main(lambda: main()))
