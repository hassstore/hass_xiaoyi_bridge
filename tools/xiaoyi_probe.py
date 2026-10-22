"""Standalone diagnostic for the 小艺 OpenClaw A2A WebSocket connection.

Two different failures look similar in Home Assistant:

1. **Handshake refused** — the gateway answers HTTP 401 for every rejected
   upgrade, with the same body (``{"code":"badRequest","desc":"Resource is not
   found or param error"}``), whether the AK/SK pair is wrong, the agent is not
   published, or the credential was created for the wrong platform.
2. **Handshake accepted, then closed** — the socket opens and the server closes
   it right away with close code 1000. That is *not* a credential problem: the
   agent is unpublished, the same agent is connected somewhere else, or the
   platform is rate limiting.

This script prints exactly what happens, per endpoint and per "traffic mode",
so the two can be told apart::

    # no traffic at all (does the bare connection stay open?)
    python tools/xiaoyi_probe.py --ak <AK> --sk <SK> --agent-id <AGENT_ID> --mode hold

    # handshake + clawd_bot_init
    ... --mode init

    # what Home Assistant does (init + heartbeat at +1s, then every 20s)
    ... --mode full

    # hold both endpoints at the same time (duplicate-session check)
    ... --mode hold --dual

It needs ``websockets`` (``python -m pip install websockets``) and never prints
the SK. Exit code 0 means at least one connection stayed open for the requested
duration.
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
    "  4. 同一智能体只能有一个客户端在线：确认没有第二个 HA / OpenClaw 插件在连；\n"
    "  5. 运行本脚本的机器时间偏差小于 5 分钟（签名里带毫秒时间戳）。"
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


def heartbeat_frame(agent_id: str) -> str:
    """Return the application-level heartbeat frame."""
    return json.dumps(
        {
            "msgType": "heartbeat",
            "agentId": agent_id,
            "timestamp": int(time.time() * 1000),
        }
    )


def init_frame(agent_id: str) -> str:
    """Return the handshake frame Home Assistant sends after connecting."""
    return json.dumps({"msgType": "clawd_bot_init", "agentId": agent_id})


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


def describe_rejection(exc: BaseException) -> str:
    """Return a readable description of a refused upgrade."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    body = getattr(response, "body", None)
    server = ""
    if response is not None:
        try:
            server = str(response.headers.get("Server") or "")
        except Exception:  # noqa: BLE001 - headers are optional
            server = ""
    detail = ""
    if isinstance(body, (bytes, bytearray)):
        text = bytes(body).decode("utf-8", "replace").strip()
        try:
            parsed = json.loads(text)
        except ValueError:
            detail = text[:300]
        else:
            if isinstance(parsed, dict):
                detail = str(
                    parsed.get("desc") or parsed.get("message") or parsed
                )[:300]
            else:
                detail = text[:300]
    bits = [f"HTTP {status}"]
    if server:
        bits.append(f"Server: {server}")
    if detail:
        bits.append(detail)
    return "  ".join(bits).strip()


async def run_one(
    label: str,
    url: str,
    headers: dict[str, str],
    mode: str,
    hold: float,
    agent_id: str,
) -> bool:
    """Connect once, optionally send traffic, and report what happened."""
    started = time.monotonic()
    try:
        ws = await websockets.connect(
            url,
            additional_headers=headers,
            ssl=ssl_context_for(url),
            open_timeout=15,
            ping_interval=None,
        )
    except Exception as exc:  # noqa: BLE001 - this is a diagnostic tool
        elapsed = (time.monotonic() - started) * 1000
        print(f"[{label}] ✗ 握手被拒（{elapsed:.0f} ms）：{describe_rejection(exc)}")
        return False

    print(f"[{label}] ✓ 握手成功（{(time.monotonic() - started) * 1000:.0f} ms）")
    frames = 0

    async def reader() -> str:
        """Print inbound frames and return a description of the close."""
        nonlocal frames
        try:
            async for raw in ws:
                frames += 1
                text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
                print(
                    f"          ← 帧 {frames}"
                    f"（+{time.monotonic() - started:.1f}s）：{text[:240]}"
                )
        except Exception as exc:  # noqa: BLE001 - report whatever ended the loop
            close = getattr(exc, "rcvd", None)
            code = getattr(close, "code", "?")
            reason = getattr(close, "reason", "") or "-"
            return f"code={code} reason={reason}"
        return "流结束但没收到关闭帧"

    reader_task = asyncio.create_task(reader())

    async def sender() -> None:
        """Send the frames Home Assistant would send."""
        if mode in ("init", "full"):
            await asyncio.sleep(0.2)
            await ws.send(init_frame(agent_id))
            print(f"          → clawd_bot_init（+{time.monotonic() - started:.1f}s）")
        if mode == "full":
            await asyncio.sleep(1.0)
            await ws.send(heartbeat_frame(agent_id))
            print(f"          → heartbeat（+{time.monotonic() - started:.1f}s）")
            while True:
                await asyncio.sleep(20)
                await ws.send(heartbeat_frame(agent_id))
                print(f"          → heartbeat（+{time.monotonic() - started:.1f}s）")

    sender_task = asyncio.create_task(sender())
    try:
        close_reason = await asyncio.wait_for(reader_task, timeout=hold)
    except TimeoutError:
        close_reason = ""
        print(f"          … {hold:.0f}s 内仍保持在线（收到 {frames} 帧）")
    except Exception as exc:  # noqa: BLE001 - keep the report readable
        close_reason = str(exc)

    sender_task.cancel()
    try:
        await sender_task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass

    lifetime = time.monotonic() - started
    if close_reason:
        print(f"[{label}] ✗ 连接在 {lifetime:.2f}s 后被关闭：{close_reason}")
        try:
            await ws.close()
        except Exception:  # noqa: BLE001 - already closed
            pass
        return False

    await ws.close()
    return True


async def run_dual(
    urls: list[str], headers: dict[str, str], hold: float, agent_id: str
) -> bool:
    """Hold both endpoints open at the same time (duplicate-session check)."""
    print("--- 双连接同时保持（检查同一智能体是否允许多连接）---")
    tasks = [
        asyncio.create_task(
            run_one(f"双连接-{index}", url, headers, "hold", hold, agent_id)
        )
        for index, url in enumerate(urls, start=1)
    ]
    results = await asyncio.gather(*tasks)
    return all(results)


async def main() -> int:
    """Run the probe."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ak", required=True, help="Access Key")
    parser.add_argument("--sk", required=True, help="Secret Key（不会被打印）")
    parser.add_argument("--agent-id", required=True, help="小艺智能体 agentId")
    parser.add_argument("--url", action="append", default=None, help="自定义端点，可重复")
    parser.add_argument(
        "--mode",
        choices=("hold", "init", "full"),
        default="hold",
        help="hold=只连接；init=连接后发 clawd_bot_init；full=再加心跳（等同 HA）",
    )
    parser.add_argument("--hold", type=float, default=20.0, help="每个端点观察秒数")
    parser.add_argument("--dual", action="store_true", help="额外测试两个端点同时在线")
    args = parser.parse_args()

    urls = args.url or DEFAULT_URLS
    headers = build_auth_headers(args.ak, args.sk, args.agent_id)

    print("=" * 74)
    print("小艺 A2A 连接诊断")
    print("=" * 74)
    print(f"本机时间    : {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"模式        : {args.mode}（观察 {args.hold:.0f}s）")
    print(f"x-ts        : {headers['x-ts']}")
    print(f"x-sign      : {headers['x-sign'][:12]}…")
    print(f"x-access-key: {args.ak[:4]}…{args.ak[-2:]}（长度 {len(args.ak)}）")
    print(f"x-agent-id  : {args.agent_id}")
    print()

    ok = False
    for index, url in enumerate(urls, start=1):
        print(f"--- 端点 {index}: {url} ---")
        ok = (
            await run_one(
                f"端点{index}", url, headers, args.mode, args.hold, args.agent_id
            )
            or ok
        )
        print()

    if args.dual and len(urls) > 1:
        ok = await run_dual(urls[:2], headers, args.hold, args.agent_id) or ok
        print()

    print("=" * 74)
    if ok:
        print("结论：至少一个连接在观察期内保持在线。")
        if args.mode == "hold":
            print("      建议再用 --mode full 复测，确认心跳不会触发关闭。")
        return 0

    print("结论：没有连接能保持在线。")
    print()
    print("如果输出里是「握手被拒 HTTP 401」→ 凭证/上架/服务平台问题：")
    print(HINT)
    print()
    print("如果输出里是「握手成功，随后被关闭 code=1000」→ 与凭证无关：")
    print("  a) 智能体未上架或账号不在测试白名单；")
    print("  b) 同一智能体已被另一个客户端连接（第二个 HA、OpenClaw 插件等）；")
    print("  c) 平台限流；")
    print(
        "  d) 用 --mode hold 对比：若 hold 能保持、full 才被关闭，"
        "说明是初始化帧/心跳的问题，请把完整输出反馈。"
    )
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
