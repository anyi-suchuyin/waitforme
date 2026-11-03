"""_common.py —— waitforme skill 各脚本共用的小工具箱。

只做四件事：找项目目录、跑子进程并解析它那一行 JSON、读写 config.json、把
人看的诊断写 stderr、把机器看的一行 JSON 写 stdout。不依赖第三方库。

Windows 上不 `reconfigure(encoding="utf-8")` 的话中文会变成乱码（项目核心脚本
也这么做），所以这里统一处理，脚本本身不用再操心。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
# install/configure 把解析到的项目路径写在这里，好让 hook 桥（没有 cwd 线索）也能找到项目
STATE_FILE = SKILL_DIR / ".waitforme_home"

REPO_URL = "https://github.com/anyi-suchuyin/waitforme.git"
DEFAULT_CLONE_DIR = Path.home() / "waitforme"

# 项目完整性判据：少一个就跑不起来，宁可安装前报清楚，也别让用户跑到一半才炸
REQUIRED_FILES = (
    "waitforme.py",
    "hooks.py",
    "channels.py",
    "linker.py",
    "policy.py",
    "selftest.py",
    "config.example.json",
)

# field 是 key 落在 channels.<name> 里的哪个键，和 channels.py 一一对应
CHANNELS: dict[str, dict[str, str]] = {
    "mock": {
        "field": "",
        "label": "mock（不联网，写 logs/mock_sent.jsonl，用来看排版和跑自测）",
        "where": "不需要 key",
    },
    "wecom_bot": {
        "field": "key",
        "label": "企业微信群机器人",
        "where": "企业微信群 → 群机器人 → 添加，复制 Webhook 地址里 key= 后面那串",
    },
    "serverchan": {
        "field": "sckey",
        "label": "Server酱（sct.ee）",
        "where": "sct.ee 微信登录 → 复制 Server Key（Turbo 版才送微信）",
    },
    "pushplus": {
        "field": "token",
        "label": "PushPlus",
        "where": "www.pushplus.plus 微信扫码 → 用户中心 → 复制用户令牌",
    },
}
CHANNEL_ORDER = ("mock", "wecom_bot", "serverchan", "pushplus")


# ---------------------------------------------------------------------------
# 输出：人看的走 stderr，机器看的走 stdout 最后一行 JSON
# ---------------------------------------------------------------------------


def setup_stdio() -> None:
    """Windows 控制台默认 GBK，中文会乱码；两个流都强制 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


# 导入即生效：脚本自己不必记得调；子进程的 waitforme.py 也各自 reconfigure
setup_stdio()


def say(message: str = "") -> None:
    """诊断/给人看的进度，一律 stderr。"""
    print(message, file=sys.stderr, flush=True)


def emit(payload: dict[str, Any]) -> None:
    """stdout 唯一的最后一行 JSON。"""
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def run_main(func) -> int:
    """统一的入口包装：异常折成一行 JSON + 退出码，绝不把 traceback 当唯一输出。"""
    try:
        return int(func())
    except KeyboardInterrupt:
        say("\n已中断")
        emit({"status": "interrupted"})
        return 2
    except Exception as exc:  # noqa: BLE001 - 顶层兜底
        say(traceback.format_exc())
        emit({"status": "error", "reason": f"{type(exc).__name__}: {exc}"})
        return 1


# ---------------------------------------------------------------------------
# 交互输入：EOF/管道输入一律退到默认值，别让自动化卡死
# ---------------------------------------------------------------------------


def _interactive_tty() -> bool:
    """真的有终端才值得做「不回显」；管道输入时 getpass 会退回明文读并告警。"""
    try:
        return bool(sys.stdin.isatty() and sys.stderr.isatty())
    except (ValueError, OSError):
        return False


def ask_text(prompt: str, default: str = "", secret: bool = False) -> str:
    """在 stderr 上问一句，从 stdin 读一行。EOF（非交互）返回默认值。

    `secret=True` 且对着真终端时走 getpass（key 不回显）；管道输入时照常读，
    免得自动化脚本因为 getpass 的告警少一行输出。
    """
    suffix = f"（回车用 {default}）" if default and not secret else ""
    if secret and _interactive_tty():
        import getpass

        try:
            return (getpass.getpass(f"{prompt}\n> ", stream=sys.stderr) or "").strip() or default
        except (EOFError, KeyboardInterrupt, OSError):
            return default
    sys.stderr.write(f"{prompt}{suffix}\n> ")
    sys.stderr.flush()
    line = sys.stdin.readline()
    if not line:  # EOF
        sys.stderr.write("\n")
        return default
    value = line.strip()
    return value if value else default


def confirm(prompt: str, default: bool = False) -> bool:
    """是非题。回车取默认；非交互（EOF）也取默认。"""
    hint = "Y/n" if default else "y/N"
    sys.stderr.write(f"{prompt} [{hint}]\n> ")
    sys.stderr.flush()
    line = sys.stdin.readline()
    if not line:
        sys.stderr.write("\n")
        return default
    value = line.strip().lower()
    if not value:
        return default
    return value in ("y", "yes", "是", "1", "true")


# ---------------------------------------------------------------------------
# 项目定位
# ---------------------------------------------------------------------------


def missing_files(path: Path) -> list[str]:
    return [name for name in REQUIRED_FILES if not (path / name).is_file()]


def looks_like_project(path: Path | None) -> bool:
    return bool(path) and Path(path).is_dir() and not missing_files(Path(path))


def read_state() -> Path | None:
    try:
        text = STATE_FILE.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    return Path(text) if text else None


def write_state(project: Path) -> None:
    """记下项目路径，供 kimi_hook.py 在任意 cwd 下使用；写不了不算错。"""
    try:
        STATE_FILE.write_text(str(project), encoding="utf-8")
    except OSError:
        pass


def project_candidates(explicit: str | Path | None = None) -> list[Path]:
    """按「越可信越靠前」排的候选目录；不做存在性判断。"""
    raw: list[Path] = []
    if explicit:
        raw.append(Path(os.path.expanduser(str(explicit))))
    env = os.environ.get("WAITFORME_HOME")
    if env:
        raw.append(Path(os.path.expanduser(env)))
    state = read_state()
    if state:
        raw.append(state)
    # 仓库内布局：<repo>/skill/scripts/_common.py
    raw.append(SKILL_DIR.parent)
    raw.append(SKILL_DIR / "vendor")
    raw.append(Path.cwd())
    for base in (Path.home(), Path.home() / "projects", Path.home() / "code", Path.home() / "Desktop"):
        raw.append(base / "waitforme")

    out: list[Path] = []
    seen: set[str] = set()
    for path in raw:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(resolved)
    return out


def resolve_project(explicit: str | Path | None = None) -> Path | None:
    """找到真正像 waitforme 项目的目录；找不到返回 None，由调用方给可复制的修法。"""
    for path in project_candidates(explicit):
        if looks_like_project(path):
            return path
    return None


def clone_project(url: str, dest: Path) -> tuple[bool, str]:
    """git clone 一份项目。返回 (成功, 说明)；git 不在或目录已存在都不抛异常。"""
    if dest.exists():
        return False, f"目标目录已存在：{dest}"
    if not shutil.which("git"):
        return False, "找不到 git。请手动 `git clone " + url + "` 后加 --project 指定目录"
    try:
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", url, str(dest)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"git clone 失败：{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return False, f"git clone 失败（exit={proc.returncode}）：{(proc.stderr or '').strip()[:300]}"
    return True, str(dest)


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------


def config_path_for(project: Path, override: str | Path | None = None) -> Path:
    """--config > WAITFORME_CONFIG > <project>/config.json（和 waitforme.py 的顺序一致）。"""
    if override:
        return Path(os.path.expanduser(str(override)))
    env = os.environ.get("WAITFORME_CONFIG")
    if env:
        return Path(os.path.expanduser(env))
    return project / "config.json"


def read_config(path: Path) -> dict[str, Any]:
    """读配置；文件不存在/坏了回空字典，由调用方决定怎么补。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_config(path: Path, cfg: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def channel_field(channel: str) -> str:
    return CHANNELS.get(channel, {}).get("field", "")


def apply_channel(cfg: dict[str, Any], channel: str, key: str | None = None) -> dict[str, Any]:
    """写 channel 与对应 key；key 为空时保留原值（别把用户已有配置抹掉）。"""
    cfg["channel"] = channel
    field = channel_field(channel)
    if field and key:
        channels = cfg.setdefault("channels", {})
        if not isinstance(channels, dict):
            channels = cfg["channels"] = {}
        slot = channels.setdefault(channel, {})
        if not isinstance(slot, dict):
            slot = channels[channel] = {}
        slot[field] = key.strip()
    return cfg


def mask(secret: Any, keep: int = 4) -> str:
    """打印用：只留前几位，其余打码。空值说「未配置」。"""
    text = str(secret or "")
    if not text:
        return "未配置"
    if len(text) <= keep + 2:
        return "*" * len(text)
    return text[:keep] + "…" + "*" * 4


def hook_base(cfg: dict[str, Any]) -> str:
    host = str(cfg.get("hook_host") or "127.0.0.1")
    port = int(cfg.get("hook_port") or 8790)
    return f"http://{host}:{port}"


def hook_url(cfg: dict[str, Any], with_token: bool = True) -> str:
    token = str(cfg.get("hook_token") or "")
    if with_token and token:
        return f"{hook_base(cfg)}/hook?k={token}"
    return f"{hook_base(cfg)}/hook"


def health_check(cfg: dict[str, Any], timeout: float = 1.5) -> tuple[bool, dict[str, Any] | str]:
    """探 /health。服务没起/被防火墙拦都只是 (False, 原因)，不抛异常。"""
    url = f"{hook_base(cfg)}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
        data = json.loads(body)
        return (True, data if isinstance(data, dict) else {"raw": body})
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# 跑项目脚本
# ---------------------------------------------------------------------------


def child_env(project: Path, config: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["WAITFORME_HOME"] = str(project)
    if config is not None:
        env["WAITFORME_CONFIG"] = str(config)
    return env


def run_script(
    project: Path,
    script: str,
    args: list[str] | None = None,
    config: Path | None = None,
    timeout: int = 180,
) -> tuple[int, str, str]:
    """用当前解释器跑项目脚本，返回 (退出码, stdout, stderr)。

    `stdout` 里可能有不止一行（selftest 就是），解析交给 parse_json_line / 正则。
    """
    cmd = [sys.executable, str(project / script), *(args or [])]
    try:
        proc = subprocess.run(
            cmd, cwd=str(project), env=child_env(project, config),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"{script} 超时（{timeout}s）"
    except OSError as exc:
        return 1, "", f"{script} 启动失败：{type(exc).__name__}: {exc}"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def parse_json_line(stdout: str) -> dict[str, Any] | None:
    """取 stdout 里最后一处可解析的 JSON 对象（项目契约：唯一一行，但容错）。"""
    for line in reversed((stdout or "").strip().splitlines()):
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
