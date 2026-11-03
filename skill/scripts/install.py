"""install.py —— waitforme skill 的安装器。

做四件事：查 Python 版本 → 找到并体检项目 → 生成 config.json（已有就问）→
把通道和 key 写进配置。全程只写 config.json，不碰核心代码，也不碰
~/.kimi-code 里的任何用户配置。

用法（在 skill 目录下，或任意目录用绝对路径）：
    python install.py                       # 交互式，一步一步问
    python install.py --non-interactive --channel mock
    python install.py --channel feishu --key <WEBHOOK> --non-interactive
    python install.py --check               # 只体检，不写任何文件

stdout 最后一行是 JSON 摘要，过程提示全走 stderr。
"""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common as C  # noqa: E402  同目录工具箱

EXIT_OK = 0
EXIT_BUSINESS = 2
MIN_PYTHON = (3, 10)


def check_python() -> tuple[bool, str]:
    version = ".".join(str(part) for part in sys.version_info[:3])
    ok = sys.version_info[:2] >= MIN_PYTHON
    need = ".".join(str(part) for part in MIN_PYTHON)
    return ok, f"Python {version}（{sys.executable}）{'✓' if ok else f'✗ 需要 ≥{need}'}"


def check_project(project: Path) -> tuple[bool, list[str]]:
    missing = C.missing_files(project)
    return not missing, missing


def missing_project_hint() -> str:
    return (
        "找不到 waitforme 项目目录。三选一：\n"
        f"  1) 指定已有项目：python install.py --project <waitforme 目录>\n"
        f"  2) 让脚本克隆：  python install.py --clone（默认克隆到 {C.DEFAULT_CLONE_DIR}）\n"
        f"  3) 先手动克隆：  git clone {C.REPO_URL}"
    )


def locate_project(args: argparse.Namespace, record: bool = True) -> tuple[Path | None, str]:
    """显式参数 → 环境变量 → 状态文件 → 仓库内布局 → 常见位置 → 可选 clone。"""
    found = C.resolve_project(args.project)
    if found:
        origin = "explicit" if args.project else "auto"
        C.say(f"[install] 项目目录：{found}（{origin}）")
        if record:
            C.write_state(found)
        return found, origin
    if not args.clone:
        C.say("[install] " + missing_project_hint())
        return None, "not_found"

    dest = Path(args.clone_dir).expanduser() if args.clone_dir else C.DEFAULT_CLONE_DIR
    C.say(f"[install] 没找到项目，执行 git clone {args.clone_url} → {dest}")
    ok, detail = C.clone_project(args.clone_url, dest)
    if not ok:
        C.say(f"[install] 克隆失败：{detail}")
        return None, "clone_failed"
    C.say(f"[install] 克隆完成：{detail}")
    C.write_state(dest)
    return dest, "clone"


def pick_channel(args: argparse.Namespace) -> str:
    if args.channel:
        return args.channel
    if args.non_interactive:
        C.say("[install] 非交互模式且没给 --channel，按 mock 装（离线可跑，随时可改）")
        return "mock"
    C.say("\n通道四选一：")
    for index, name in enumerate(C.CHANNEL_ORDER, start=1):
        C.say(f"  {index}) {name:<10} {C.CHANNELS[name]['label']}")
    default = "mock"
    raw = C.ask_text("选哪个通道（输入序号或名字）", default=default)
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(C.CHANNEL_ORDER):
            return C.CHANNEL_ORDER[idx]
    if raw in C.CHANNELS:
        return raw
    C.say(f"[install] 认不出 {raw!r}，用默认 {default}")
    return default


def pick_key(args: argparse.Namespace, channel: str) -> str:
    field = C.channel_field(channel)
    if not field:
        C.say("[install] mock 通道不需要 key")
        return ""
    if args.key:
        return args.key.strip()
    if args.non_interactive:
        return ""
    info = C.CHANNELS[channel]
    C.say(f"\n{info['label']} 的 key 怎么拿：{info['where']}")
    C.say("（直接回车 = 先不填，之后可以再跑 configure.py；对着终端输入不回显）")
    return C.ask_text(f"把 {field} 粘进来", default="", secret=True).strip()


def print_next_steps(project: Path, config: Path, channel: str) -> list[str]:
    py = "python"
    skill_scripts = C.SCRIPTS_DIR
    steps = [
        f"起常驻服务：{py} \"{project / 'hooks.py'}\" serve --detach",
        f"跑自检：    {py} \"{skill_scripts / 'verify.py'}\" --project \"{project}\" --config \"{config}\"",
        f"接 Kimi：   {py} \"{skill_scripts / 'connect_kimi.py'}\" --project \"{project}\" --config \"{config}\"",
        f"改通道：    {py} \"{skill_scripts / 'configure.py'}\" --project \"{project}\" --config \"{config}\"",
    ]
    C.say("\n下一步（照顺序做）：")
    for index, step in enumerate(steps, start=1):
        C.say(f"  {index}) {step}")
    C.say("\n注意：接 Kimi Code 需要改 ~/.kimi-code/config.toml，那个文件归用户所有，")
    C.say("      本 skill 只打印片段和 diff，不自动改 —— 请把片段给用户确认。")
    return steps


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="install.py", description="waitforme skill 安装器")
    parser.add_argument("--project", default=None, help="waitforme 项目目录（默认自动找）")
    parser.add_argument("--config", default=None, help="config.json 路径（默认 <project>/config.json）")
    parser.add_argument("--channel", choices=list(C.CHANNEL_ORDER), default=None)
    parser.add_argument("--key", default=None, help="通道凭证（wecom_bot.key / feishu.webhook / pushplus.token）")
    parser.add_argument("--force", action="store_true", help="已存在 config.json 也覆盖（会换新 hook_token）")
    parser.add_argument("--non-interactive", action="store_true", help="不问任何问题，适合自动化")
    parser.add_argument("--check", action="store_true", help="只体检，不写任何文件")
    parser.add_argument("--clone", action="store_true", help="找不到项目时 git clone 一份")
    parser.add_argument("--clone-url", default=C.REPO_URL)
    parser.add_argument("--clone-dir", default=None)
    args = parser.parse_args(argv)

    warnings: list[str] = []

    # 1) Python 版本
    py_ok, py_detail = check_python()
    C.say(f"[install] {py_detail}")
    if not py_ok:
        C.say("[install] 结论：Python 太老，先装 3.10+（本项目在 3.14 上验证过）")
        C.emit({"status": "failed", "reason": "python_too_old", "detail": py_detail})
        return EXIT_BUSINESS

    # 2) 项目完整性
    project, origin = locate_project(args, record=not args.check)
    if project is None:
        C.emit({"status": "failed", "reason": "project_not_found", "hint": missing_project_hint()})
        return EXIT_BUSINESS
    ok, missing = check_project(project)
    if not ok:
        C.say(f"[install] 项目缺文件：{', '.join(missing)}")
        C.say(f"[install] 这不是完整的 waitforme 项目目录，请确认路径或重新 clone：{C.REPO_URL}")
        C.emit({"status": "failed", "reason": "project_incomplete", "project": str(project), "missing": missing})
        return EXIT_BUSINESS

    config = C.config_path_for(project, args.config)
    C.say(f"[install] 配置文件：{config}")

    if args.check:
        cfg = C.read_config(config)
        C.say(f"[install] config 存在：{config.exists()}；通道：{cfg.get('channel') or '未设置'}")
        C.emit({
            "status": "ok", "mode": "check", "project": str(project), "origin": origin,
            "python": py_detail, "config": str(config), "config_exists": config.exists(),
            "channel": cfg.get("channel") or None,
        })
        return EXIT_OK

    # 3) init：已有配置就守规矩 —— 交互问一句，非交互必须显式 --force
    init_force = bool(args.force)
    if config.exists() and not args.force:
        if args.non_interactive:
            C.say("[install] 已有 config.json，保留不覆盖（要覆盖加 --force，会换新 hook_token）")
        else:
            if C.confirm(f"{config} 已存在，覆盖吗？（会生成新 hook_token，通道 key 需要重填）", default=False):
                init_force = True
            else:
                C.say("[install] 保留现有 config.json")
    code, out, err = C.run_script(project, "waitforme.py", ["init", *(["--force"] if init_force else [])], config=config)
    init_result = C.parse_json_line(out)
    if code != 0 or not init_result:
        C.say(f"[install] init 失败（exit={code}）：{(err or out).strip()[:500]}")
        C.emit({"status": "failed", "reason": "init_failed", "exit_code": code, "detail": (err or out).strip()[-500:]})
        return EXIT_BUSINESS
    C.say(f"[install] init：{init_result.get('status')} → {init_result.get('config')}")

    # 4) 通道 + key
    channel = pick_channel(args)
    key = pick_key(args, channel)
    cfg = C.read_config(config)
    if not isinstance(cfg.get("channels"), dict):
        cfg["channels"] = {}
    # deepcopy：apply_channel 改的是嵌套 dict，浅拷贝会让 before 跟着一起变，比不出差异
    before = copy.deepcopy(cfg)
    C.apply_channel(cfg, channel, key)
    if cfg != before:
        C.write_config(config, cfg)
        C.say(f"[install] 已写入 config：channel={channel}")
    else:
        C.say("[install] 配置没变化，跳过写入")

    field = C.channel_field(channel)
    stored = str((cfg.get("channels") or {}).get(channel, {}).get(field) or "") if field else ""
    if field and not stored:
        warnings.append(f"{channel} 还没填 {field}，现在推送会返回 channel_not_configured")
        C.say(f"[install] ⚠ {warnings[-1]}")
    token = str(cfg.get("hook_token") or "")
    if not token:
        warnings.append("hook_token 为空，hook 服务会拒绝所有请求（重跑 init）")
        C.say(f"[install] ⚠ {warnings[-1]}")

    # 5) 收尾
    C.say("")
    C.say("[install] 安装结果：")
    C.say(f"  项目     {project}")
    C.say(f"  配置     {config}")
    C.say(f"  通道     {channel}" + (f"（{C.mask(stored)}）" if field else ""))
    C.say(f"  hook URL {C.hook_url(cfg, with_token=False)}?k=***")
    steps = print_next_steps(project, config, channel)

    C.emit({
        "status": "ok",
        "project": str(project),
        "origin": origin,
        "config": str(config),
        "init": init_result.get("status"),
        "channel": channel,
        "key": C.mask(stored) if field else "not_required",
        "hook_url": C.hook_url(cfg, with_token=False) + "?k=***",
        "next": steps,
        "warnings": warnings,
    })
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(C.run_main(lambda: main()))
