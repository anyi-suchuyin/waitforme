"""kimi_hook.py —— Kimi Code hook 与 waitforme 服务之间的桥。

Kimi Code 的 hook 是「跑一条命令，事件 JSON 从 stdin 进来」；waitforme 的服务是
「收 POST /hook?k=<token>」。这个脚本把前者搬给后者，只做搬运：

* **永远 exit 0**。Stop 是「可阻断」事件，hook 退出码 2 会让当前回合被拦下；
  搬运失败最多是少一条微信，绝不能反过来卡住 AI。
* **不打印 stdout**。hook 的 stdout 可能被塞进上下文，这里一句都不输出。
* **token 运行时从 config.json 读**，所以 Kimi 的 config.toml 里没有密钥，
  换 token 也不用改 Kimi 配置。
* 只依赖标准库，3 秒超时，服务没起就安静地失败。

手工验证：
    echo '{"hook_event_name":"Stop","session_id":"manual-test","cwd":"."}' | python kimi_hook.py --debug
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common as C  # noqa: E402


def post_event(cfg: dict, raw: bytes, timeout: float) -> tuple[bool, str]:
    url = C.hook_url(cfg)
    if "k=" not in url:
        return False, "config.json 里没有 hook_token"
    request = urllib.request.Request(
        url,
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "waitforme-hook-bridge/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return True, f"HTTP {resp.status} {body[:300]}"
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300] if exc.fp else ""
        return False, f"HTTP {exc.code} {detail}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kimi_hook.py", description="Kimi Code hook → waitforme 服务")
    parser.add_argument("--timeout", type=float, default=3.0, help="POST 超时秒数（默认 3）")
    parser.add_argument("--debug", action="store_true", help="把请求结果打到 stderr，手工排查用")
    args = parser.parse_args(argv)

    try:
        raw = sys.stdin.buffer.read()
    except (OSError, ValueError):
        raw = b""
    if not raw.strip():
        raw = b"{}"

    project = C.resolve_project()
    if project is None:
        if args.debug:
            C.say("[hook-bridge] 找不到 waitforme 项目：install.py 没跑过？")
        return 0
    config = C.config_path_for(project)
    cfg = C.read_config(config)
    if not cfg:
        if args.debug:
            C.say(f"[hook-bridge] 配置读不到：{config}")
        return 0

    ok, detail = post_event(cfg, raw, args.timeout)
    if args.debug:
        C.say(f"[hook-bridge] {'ok' if ok else 'failed'}: {detail}")
        if ok:
            try:
                C.say("[hook-bridge] 事件：" + json.dumps(json.loads(raw), ensure_ascii=False)[:400])
            except ValueError:
                pass
    return 0


if __name__ == "__main__":
    # 故意不用 C.run_main：它出错时会往 stdout 打一行 JSON，而 hook 的 stdout 会被
    # 塞进模型上下文；这里任何意外都只留 stderr，退出码恒为 0（fail-open）。
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 桥脚本绝不把异常抛给 hook 调用方
        print(f"[hook-bridge] 未预期异常：{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(0)
