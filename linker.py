"""linker.py —— 组装「点开就是手机远程控制页」的跳转链接。

唯一的真相来源是 Kimi Code 自己写的 `~/.kimi-code/server/rc.json`（每次远程控制会话
启动时被重写），我们只读它的 `url` 字段。这个文件随时可能不存在、半写、被清空，或者
正被 kimi 会话占用 —— 所以本模块的任何函数都**不许抛异常**，读不到就退回 fallback。
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from pathlib import Path
from typing import Any

# rc.json 的默认位置：优先用户目录，便于换机器/换用户时仍然正确
DEFAULT_RC_PATH = (Path.home() / ".kimi-code" / "server" / "rc.json").as_posix()
DEFAULT_FALLBACK_URL = "http://192.168.1.10:8791/"

# OpenProcess 只要查询权限，不申请 PROCESS_TERMINATE —— 误判的代价是不能是杀掉别人的会话
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


def _cfg_get(cfg: dict[str, Any] | None, key: str, default: Any) -> Any:
    """宽容取配置：cfg 为 None、缺字段、字段为空串都算「没配」，用 default 兜。"""
    if not isinstance(cfg, dict):
        return default
    value = cfg.get(key)
    if value is None:
        return default
    if isinstance(value, str) and not value.strip():
        return default
    return value


def rc_path(cfg: dict[str, Any] | None = None) -> str:
    """rc.json 的实际读取路径（cfg 可覆盖）。仅展开 ~，不做存在性检查。"""
    raw = str(_cfg_get(cfg, "rc_json_path", DEFAULT_RC_PATH))
    try:
        return os.path.expanduser(os.path.expandvars(raw))
    except Exception:
        return raw


def read_rc(cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """读 rc.json。任何失败（无文件 / 权限 / 半截 JSON / 顶层不是对象）都返回 None。"""
    path = rc_path(cfg)
    try:
        text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except (OSError, ValueError):
        return None
    if not text.strip():
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def build_links(cfg: dict[str, Any] | None = None) -> dict[str, str]:
    """给出推送里要用的链接。

    返回 `{primary_url, fallback_url, source}`，`source` 是 `rc`（拿到了远程控制真实
    链接）或 `fallback`（退回局域网答题页）。绝不抛异常：kimi 会话可能正在忙这个文件。
    """
    fallback = str(_cfg_get(cfg, "fallback_url", DEFAULT_FALLBACK_URL))
    result = {"primary_url": fallback, "fallback_url": fallback, "source": "fallback"}
    try:
        data = read_rc(cfg)
        if not data:
            return result
        url = data.get("url")
        # 先 strip 再判协议：带前导空格的合法 URL 不该被当成非法而回落 fallback
        if isinstance(url, str):
            url = url.strip()
            if url.lower().startswith(("http://", "https://")):
                result["primary_url"] = url
                result["source"] = "rc"
                device_id = data.get("device_id")
                if isinstance(device_id, str) and device_id.strip():
                    result["device_id"] = device_id.strip()
    except Exception:
        # 兜底：这个模块的失败模式必须是「用 fallback」，不是「炸调用方」
        pass
    return result


def pid_alive(pid: Any) -> bool:
    """进程是否还活着。只查询、绝不终止。

    Windows 上不能用 `os.kill(pid, 0)` 判活：实测（Python 3.14）它对刚退出但句柄未回收的
    pid 同样不抛异常，会把死进程判成活的；而且 os.kill 在 Windows 语义上是「终止进程」。
    所以这里走 kernel32 只读查询，`os.kill` 只作为非 Windows 的兜底路径。
    """
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False

    if sys.platform == "win32":
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid_int)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            finally:
                kernel32.CloseHandle(handle)
            return bool(ok) and code.value == _STILL_ACTIVE
        except Exception:
            return False

    try:
        os.kill(pid_int, 0)
        return True
    except (OSError, ProcessLookupError, PermissionError):
        # PermissionError 说明进程在、只是没权限碰它 —— 当作活着
        return isinstance(sys.exc_info()[1], PermissionError)
    except Exception:
        return False


def rc_is_alive(cfg: dict[str, Any] | None = None) -> bool:
    """rc.json 里记录的远程控制进程是否还活着。

    只用于提示（链接可能已经失效），不做阻断：判活错了也不该拦住推送。
    """
    data = read_rc(cfg)
    if not data:
        return False
    return pid_alive(data.get("pid"))
