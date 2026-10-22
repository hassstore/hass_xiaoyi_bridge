"""Standalone diagnostic for the 小艺 OpenClaw A2A WebSocket handshake.

The 小艺 gateway answers **every** refused upgrade with the same reply::

    HTTP 401  {"code":"badRequest","desc":"Resource is not found or param error"}

so "the credentials were rejected" is not something the status code alone can
tell you. This script prints what the server actually answered, together with a
control request that sends *no* credentials, so you can see whether your AK/SK
change anything at all.

Usage (needs ``pip install websockets``; the integration's own venv works too)::

    python tools/xiaoyi_probe.py --ak <AK> --sk <SK> --agent-id <AGENT_ID>

    # only one endpoint / a custom endpoint
    python tools/xiaoyi_probe.py --ak ... --sk ... --agent-id ... \
        --url wss://hag.cloud.huawei.com/openclaw/v1/ws/link

The script never prints the SK. Exit code 0 means at least one endpoint
accepted the handshake.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import ssl
import sys
import time

try:
    import websockets
except ImportError:  # pragma: no cover - guidance instead of a traceback
    sys.exit("需要 websockets：python -m pip install websockets")

DEFAULT_URLS = [
    "wss://hag.cloud.huawei.com/openclaw/v1/ws/link",
    "wss://116.63.174.231/openclaw/v1/ws/link",
]

HINT = (
    "排查顺序（以小艺开放平台的创建流程为准）：\n"
    "  1. Agent ID / AK / SK 与开放平台逐字一致，没有多余空格或换行；\n"
    "  2. 智能体已上架（发布真机测试），当前账号在测试白名单内；\n"
    "  3. 创建凭证时「服务平台」必须选“Web端（JS API）”；选错平台会一直 401；\n"
    "  4. 运行本脚本的机器时间偏差小于 5 分钟（签名里带毫秒时间戳）。"
)


def build_auth_headers(ak: str, sk: str, agent_id: str) -> dict[str, str]:
    """Return the four handshake headers the gateway expects."""
    timestamp = str(int(time.time() * 1000))
    signature = base64.b64encode(
        hmac.new(sk.encode("utf-8"), timestamp.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    return {
        "x-access-key": ak,
        "x-sign": signature,
        "x-ts": timestamp,
        "x-agent-id": agent_id,
    }


def describe(exc: BaseException) -> tuple[int | None, str, str]:
    """Return (status, detail, server) for a refused upgrade."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    body = getattr(response, "body", None)
    detail = ""
    if isinstance(body, (bytes, bytearray)):
        text = bytes(body).decode("utf-8", "replace").strip()
        try:
            parsed = json.loads(text)
        except ValueError:
            detail = text[:300]
        else:
            if isinstance(parsed, dict):
                detail = str(parsed.get("desc") or parsed.get("message") or parsed)
            else:
                detail = text[:300]
    server = ""
    if response is not None:
        try:
            server = str(response.headers.get("Server") or "")
        except Exception:  # noqa: BLE001 - headers are optional
            server = ""
    return status, detail, server


def ssl_context_for(url: str) -> ssl.SSLContext | None:
    """Return a TLS context for wss:// (None for plain ws://)."""
    if not url.startswith("wss"):
        return None
    context = ssl.create_default_context()
    host = url.split("://", 1)[1].split("/", 1)[0].split(":")[0]
    if host.replace(".", "").isdigit() or ":" in host:
        # IP endpoint: the certificate has no IP SAN, keep chain verification.
        context.check_hostname = False
    return context


async def attempt(
    label: str,
    url: str,
    headers: dict[str, str],
    timeout: float = 15.0,
) -> bool:
    """Try one handshake and print everything the server said."""
    started = time.monotonic()
    try:
        ws = await websockets.connect(
            url,
            additional_headers=headers,
            ssl=ssl_context_for(url),
            open_timeout=timeout,
            ping_interval=None,
        )
    except Exception as exc:  # noqa: BLE001 - this is a diagnostic tool
        elapsed = (time.monotonic() - started) * 1000
        status, detail, server = describe(exc)
        print(f"[{label}] ✗ {type(exc).__name__}  {elapsed:.0f} ms")
        print(f"          status : {status}")
        if server:
            print(f"          server : {server}")
        print(f"          detail : {detail}")
        return False

    elapsed = (time.monotonic() - started) * 1000
    print(f"[{label}] ✓ 握手成功（{elapsed:.0f} ms）")
    await ws.close()
    return True


async def main() -> int:
    """Run the probe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ak", required=True, help="Access Key")
    parser.add_argument("--sk", required=True, help="Secret Key（不会被打印）")
    parser.add_argument("--agent-id", required=True, help="小艺智能体 agentId")
    parser.add_argument(
        "--url",
        action="append",
        default=None,
        help="待测端点，可重复；默认测官方两个端点",
    )
    parser.add_argument(
        "--skip-control",
        action="store_true",
        help="跳过“不带凭证”的对照请求",
    )
    args = parser.parse_args()

    urls = args.url or DEFAULT_URLS
    headers = build_auth_headers(args.ak, args.sk, args.agent_id)

    print("=" * 74)
    print("小艺 A2A 握手诊断")
    print("=" * 74)
    print(f"本机时间   : {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"x-ts       : {headers['x-ts']}（毫秒，签名内容）")
    print(f"x-sign     : {headers['x-sign'][:12]}…（Base64(HMAC-SHA256(sk, ts))）")
    print(f"x-access-key: {args.ak[:4]}…{args.ak[-2:]}（长度 {len(args.ak)}）")
    print(f"x-agent-id : {args.agent_id}")
    print()

    ok = False
    for index, url in enumerate(urls, start=1):
        print(f"--- 端点 {index}: {url} ---")
        ok = await attempt("带凭证", url, headers) or ok
        if not args.skip_control:
            control_ok = await attempt("对照：不带凭证", url, {})
            if control_ok:
                print("          ⚠ 不带凭证竟然也能连上，说明该端点未启用鉴权")
        print()

    print("=" * 74)
    if ok:
        print("结论：至少一个端点接受了这组凭证，问题不在 AK/SK。")
        print("      如果在 Home Assistant 里仍失败，请检查 HA 主机的出网与时区。")
        return 0

    print("结论：两个端点都拒绝了握手。")
    print("      注意：网关对所有拒绝都返回同一个 401，所以“被拒绝”≠“AK/SK 一定错”。")
    print()
    print(HINT)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
