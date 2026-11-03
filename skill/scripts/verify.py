"""verify.py —— 装完到底能不能用，一次跑完给结论。

四件事：status 能不能出（配置/端口/额度）→ mock 通道能不能组出正确消息 →
selftest 272 项离线断言 → 可选 --live 真发一条；加 --service 还会真起一次服务、
探活、再停掉，确认 pid 与 /health 都对得上。

只读为主：mock 模式的 `test` 会往 logs/mock_sent.jsonl 追加一行（这就是它的用途），
不会碰真实通道；`--service` 起的服务在测完会关掉（已经在跑的服务不碰）。

用法：
    python verify.py
    python verify.py --service
    python verify.py --live
    python verify.py --skip-selftest --json
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common as C  # noqa: E402

EXIT_OK = 0
EXIT_BUSINESS = 2
SELFTEST_RE = re.compile(r"PASS\s+(\d+)\s*/\s*FAIL\s+(\d+)")


def add(checks: list[dict[str, Any]], name: str, ok: bool, detail: str,
        hint: str = "", skip: bool = False) -> bool:
    checks.append({"name": name, "ok": bool(ok), "detail": detail, "hint": hint, "skip": skip})
    mark = "SKIP" if skip else ("PASS" if ok else "FAIL")
    C.say(f"  {mark}  {name:<12} {detail}")
    return ok


def check_python(checks: list[dict[str, Any]]) -> bool:
    version = ".".join(str(part) for part in sys.version_info[:3])
    ok = sys.version_info[:2] >= (3, 10)
    return add(checks, "python", ok, f"{version} @ {sys.executable}",
               "" if ok else "装 Python 3.10+（本项目在 3.14 上验证过）")


def run_status(project: Path, config: Path, checks: list[dict[str, Any]]) -> dict[str, Any] | None:
    code, out, err = C.run_script(project, "waitforme.py", ["status"], config=config, timeout=60)
    data = C.parse_json_line(out)
    if code != 0 or not data:
        add(checks, "status", False, f"exit={code} {(err or out).strip()[-160:]}",
            "先跑 python waitforme.py init；再看 stderr 里的原因")
        return None
    detail = (f"pid_exists={data.get('pid_exists')} port={data.get('port')} "
              f"channel={data.get('channel')} today={data.get('today_count')} enabled={data.get('enabled')}")
    add(checks, "status", True, detail)
    return data


def run_mock_test(project: Path, config: Path, checks: list[dict[str, Any]]) -> dict[str, Any] | None:
    code, out, err = C.run_script(project, "waitforme.py", ["test", "--mock"], config=config, timeout=60)
    data = C.parse_json_line(out)
    if code != 0 or not data:
        add(checks, "test_mock", False, f"exit={code} {(err or out).strip()[-160:]}",
            "看 stderr 里的 markdown 预览；配置坏了就重跑 init")
        return None
    kept = bool(data.get("link_kept"))
    ok = data.get("status") == "preview" and kept
    detail = f"status={data.get('status')} link_kept={kept} bytes={data.get('markdown_bytes')}"
    add(checks, "test_mock", ok, detail,
        "" if ok else "消息里没保住跳转链接：检查 fallback_url 长度 / 通道字节上限")
    return data


def run_selftest(project: Path, checks: list[dict[str, Any]], timeout: int = 300) -> dict[str, int]:
    C.say("  ...   selftest 运行中（约 20 秒，离线、不联网）")
    code, out, err = C.run_script(project, "selftest.py", [], timeout=timeout)
    match = SELFTEST_RE.search(out or "")
    if not match:
        add(checks, "selftest", False, f"exit={code} 没找到 PASS/FAIL 汇总",
            f"手动跑 `python {project / 'selftest.py'}` 看完整输出")
        return {"pass": 0, "fail": -1}
    passed, failed = int(match.group(1)), int(match.group(2))
    ok = failed == 0 and code == 0
    last = [line for line in (out or "").strip().splitlines() if line.strip()][-6:]
    add(checks, "selftest", ok, f"PASS {passed} / FAIL {failed}（exit={code}）",
        "" if ok else "失败项：" + " | ".join(last))
    return {"pass": passed, "fail": failed}


def run_live(project: Path, config: Path, cfg: dict[str, Any], checks: list[dict[str, Any]]) -> None:
    channel = str(cfg.get("channel") or "mock")
    field = C.channel_field(channel)
    stored = str((cfg.get("channels") or {}).get(channel, {}).get(field) or "") if field else ""
    if channel == "mock" or (field and not stored):
        reason = "当前是 mock 通道" if channel == "mock" else f"{channel} 的 {field} 还没填"
        add(checks, "test_live", False, f"跳过：{reason}", "换成真实通道并填 key，再跑 --live", skip=True)
        return
    code, out, err = C.run_script(project, "waitforme.py", ["test", "--live", "--force"], config=config, timeout=90)
    data = C.parse_json_line(out)
    ok = code == 0 and bool(data) and data.get("status") == "sent"
    detail = f"status={data.get('status') if data else '?'} code={code}"
    if data and data.get("live"):
        detail += f" live={data['live']}"
    add(checks, "test_live", ok, detail,
        "" if ok else "看 stderr 的 live 结果：401/token 错 = key 不对；network_error = 网络/代理")


def run_service(project: Path, config: Path, cfg: dict[str, Any], checks: list[dict[str, Any]],
                keep: bool = False) -> None:
    """真起一次服务、探活、再停掉。已经在跑的服务只探活，不动它。"""
    status = C.parse_json_line(C.run_script(project, "waitforme.py", ["status"], config=config)[1]) or {}
    started_here = False
    if not status.get("pid_exists"):
        code, out, err = C.run_script(project, "hooks.py", ["serve", "--detach"], config=config, timeout=60)
        detached = C.parse_json_line(out) or {}
        if code != 0 or detached.get("status") not in ("detached", "already_running"):
            add(checks, "service", False, f"起服务失败 exit={code} {(err or out).strip()[-160:]}",
                "端口被占就改 config 的 hook_port；日志看 logs/hooks.stdout.log")
            return
        started_here = True

    healthy, detail = False, ""
    for _ in range(20):
        healthy, detail = C.health_check(cfg, timeout=1.0)
        if healthy:
            break
        time.sleep(0.3)
    if not healthy:
        add(checks, "service", False, f"/health 不通：{detail}",
            f"看 {project / 'logs' / 'hooks.stdout.log'}；端口 {cfg.get('hook_port')} 是否被占")
        if started_here:
            # 自己起的服务自己收尾，别把失败现场留成后台进程
            C.run_script(project, "hooks.py", ["stop"], config=config, timeout=30)
        return

    after = C.parse_json_line(C.run_script(project, "waitforme.py", ["status"], config=config)[1]) or {}
    ok = bool(after.get("pid_exists"))
    add(checks, "service", ok, f"health ok，pid={after.get('pid')} port={after.get('port')}",
        "" if ok else "status 里 pid_exists 仍是 False：pid 文件位置和服务的 run_dir 不一致？")

    if not started_here:
        C.say("  ·     服务是本来就在跑的，保持原样")
        return
    if keep:
        C.say("  ·     按 --keep-service 保留这次起的服务")
        return
    code, out, err = C.run_script(project, "hooks.py", ["stop"], config=config, timeout=30)
    stopped = C.parse_json_line(out) or {}
    time.sleep(0.3)
    gone, _ = C.health_check(cfg, timeout=0.8)
    ok = code == 0 and stopped.get("status") == "stopped" and not gone
    add(checks, "service_stop", ok, f"stop={stopped.get('status')} health_after={gone}",
        "" if ok else "停不掉就手动结束进程；pid 文件在 run/hooks.pid")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verify.py", description="waitforme 安装后验证")
    parser.add_argument("--project", default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--live", action="store_true", help="额外真发一条（会到你手机）")
    parser.add_argument("--service", action="store_true", help="真起一次服务、探活、再停掉")
    parser.add_argument("--keep-service", action="store_true", help="--service 起的服务测完别停")
    parser.add_argument("--skip-selftest", action="store_true", help="跳过 272 项离线自测（快，但少一半证据）")
    args = parser.parse_args(argv)

    checks: list[dict[str, Any]] = []
    warnings: list[str] = []
    stats: dict[str, int] = {}

    C.say("[verify] 开始检查")

    project = C.resolve_project(args.project)
    if project is None:
        C.say("[verify] 找不到 waitforme 项目：先跑 install.py --project <目录>")
        C.emit({"status": "failed", "reason": "project_not_found", "checks": []})
        return EXIT_BUSINESS
    C.write_state(project)
    config = C.config_path_for(project, args.config)
    cfg = C.read_config(config)
    C.say(f"[verify] 项目 {project}")
    C.say(f"[verify] 配置 {config}")

    if not check_python(checks):
        return finish(project, config, cfg, checks, stats, warnings)

    missing = C.missing_files(project)
    add(checks, "project", not missing, "文件齐全" if not missing else f"缺 {', '.join(missing)}",
        "" if not missing else f"重新 clone：{C.REPO_URL}")
    if missing:
        return finish(project, config, cfg, checks, stats, warnings)

    add(checks, "config", config.exists(), str(config) if config.exists() else "config.json 不存在",
        "" if config.exists() else "先跑 python install.py")
    if not config.exists():
        return finish(project, config, cfg, checks, stats, warnings)

    if not cfg.get("hook_token"):
        warnings.append("hook_token 为空：hook 服务会 403，所有事件都进不来")

    status = run_status(project, config, checks)
    run_mock_test(project, config, checks)
    if args.skip_selftest:
        add(checks, "selftest", True, "按 --skip-selftest 跳过", "", skip=True)
    else:
        stats = run_selftest(project, checks)
    if args.service:
        run_service(project, config, cfg, checks, keep=args.keep_service)
    if args.live:
        run_live(project, config, cfg, checks)

    if status is not None and not status.get("pid_exists"):
        warnings.append("服务没在跑：hook 不会被处理（python hooks.py serve --detach）")
    if status is not None and not status.get("enabled", True):
        warnings.append("config 里 enabled=false，所有推送都会被跳过")
    if cfg.get("channel") == "mock":
        warnings.append("当前是 mock 通道：消息只写 logs/mock_sent.jsonl，不会到微信")

    return finish(project, config, cfg, checks, stats, warnings)


def finish(project: Path, config: Path, cfg: dict[str, Any], checks: list[dict[str, Any]],
           stats: dict[str, int], warnings: list[str]) -> int:
    failed = [item for item in checks if not item["ok"] and not item["skip"]]
    hints = [item["hint"] for item in failed if item.get("hint")]
    ok = not failed

    C.say("")
    C.say("[verify] 摘要")
    for item in checks:
        mark = "SKIP" if item["skip"] else ("PASS" if item["ok"] else "FAIL")
        C.say(f"  {mark}  {item['name']:<12} {item['detail']}")
    if stats:
        C.say(f"  自测通过 {stats.get('pass')} 项，失败 {stats.get('fail')} 项")
    for warning in warnings:
        C.say(f"  ⚠ {warning}")
    if not ok:
        C.say("\n[verify] 修复建议：")
        for index, hint in enumerate(hints, start=1):
            C.say(f"  {index}) {hint}")
    C.say("")
    C.say("[verify] " + ("全部通过 ✓" if ok else f"{len(failed)} 项没通过 ✗"))

    C.emit({
        "status": "ok" if ok else "failed",
        "project": str(project),
        "config": str(config),
        "channel": cfg.get("channel"),
        "checks": checks,
        "selftest": stats or None,
        "warnings": warnings,
        "hints": hints,
    })
    return EXIT_OK if ok else EXIT_BUSINESS


if __name__ == "__main__":
    sys.exit(C.run_main(lambda: main()))
