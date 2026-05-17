"""通过 HA Core WebSocket 转发 supervisor/api 命令的最小客户端。

Usage:
    python scripts/ha_supervisor_ws.py <command> [<addon_slug>]
        commands: info | stop | rebuild | start | logs | restart_chain

普通 Long-Lived Access Token 不允许直接 ``POST /api/hassio/...``（401），
但可以通过 ``/api/websocket`` 的 ``supervisor/api`` 命令把请求转发到
Supervisor。这就是 HA Web UI Add-on 详情页执行 STOP / REBUILD / START
按钮的实际通路。

环境变量：
    HA_HOST   默认 192.168.31.71
    HA_TOKEN  必须，HA Long-Lived Access Token
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import aiohttp


HOST = os.environ.get("HA_HOST", "192.168.31.71")
TOKEN = os.environ.get("HA_TOKEN", "")
ADDON_DEFAULT = "local_sleep_classifier"


async def supervisor_call(
    ws: aiohttp.ClientWebSocketResponse,
    msg_id: int,
    *,
    endpoint: str,
    method: str = "get",
    timeout: float = 600.0,
) -> dict[str, Any]:
    payload = {
        "id": msg_id,
        "type": "supervisor/api",
        "endpoint": endpoint,
        "method": method,
    }
    await ws.send_json(payload)
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise TimeoutError(f"supervisor call {endpoint} timed out")
        msg = await asyncio.wait_for(ws.receive_json(), timeout=remaining)
        if msg.get("id") == msg_id:
            return msg


async def authenticate(ws: aiohttp.ClientWebSocketResponse) -> None:
    hello = await ws.receive_json()
    if hello.get("type") != "auth_required":
        raise RuntimeError(f"unexpected hello: {hello}")
    await ws.send_json({"type": "auth", "access_token": TOKEN})
    auth = await ws.receive_json()
    if auth.get("type") != "auth_ok":
        raise RuntimeError(f"auth failed: {auth}")


async def main() -> int:
    if not TOKEN:
        print("HA_TOKEN env var required", file=sys.stderr)
        return 2

    args = sys.argv[1:]
    if not args:
        print("usage: ha_supervisor_ws.py <info|stop|rebuild|start|logs|restart_chain> [addon_slug]")
        return 2

    cmd = args[0]
    addon = args[1] if len(args) > 1 else ADDON_DEFAULT

    base = f"/addons/{addon}"
    method_map = {
        "info":    ("get",  f"{base}/info"),
        "stop":    ("post", f"{base}/stop"),
        "rebuild": ("post", f"{base}/rebuild"),
        "start":   ("post", f"{base}/start"),
        "logs":    ("get",  f"{base}/logs"),
    }

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            f"http://{HOST}:8123/api/websocket",
            heartbeat=30,
        ) as ws:
            await authenticate(ws)

            if cmd == "restart_chain":
                # stop -> rebuild -> 等到 rebuild job 真的释放 -> start
                steps = [
                    ("stop",    "post", f"{base}/stop"),
                    ("rebuild", "post", f"{base}/rebuild"),
                ]
                msg_id = 0
                for label, m, ep in steps:
                    msg_id += 1
                    print(f"[{msg_id}] {label}...", flush=True)
                    res = await supervisor_call(
                        ws, msg_id=msg_id, endpoint=ep, method=m,
                        timeout=60.0,  # rebuild 命令立即返回（真正构建后台跑）
                    )
                    ok = res.get("success", False)
                    print(f"    {label} -> success={ok}", flush=True)
                    if not ok:
                        print(json.dumps(res, ensure_ascii=False))
                        return 1

                # 轮询 rebuild job 是否完成：尝试发个 noop（再发 rebuild），
                # 拿 "Another job is running" 即仍在跑；空 success=true 即闲。
                # 这是当前 supervisor API 没有显式 "wait" 的最稳兜底。
                print("[poll] waiting for rebuild job to finish...", flush=True)
                for poll in range(40):  # 最多 40 * 30s = 20 分钟
                    await asyncio.sleep(30.0)
                    msg_id += 1
                    probe = await supervisor_call(
                        ws, msg_id=msg_id, endpoint=f"{base}/rebuild",
                        method="post", timeout=60.0,
                    )
                    err = (probe.get("error") or {}).get("message", "")
                    if "Another job is running" in err:
                        print(f"    [{poll+1}/40] still rebuilding ...", flush=True)
                        continue
                    # 释放了；这次的 probe 又触发了一次 rebuild，等其完成。
                    if probe.get("success"):
                        print("    rebuild job done; second rebuild ack'd", flush=True)
                    else:
                        print(f"    rebuild probe -> {probe}", flush=True)
                    break
                else:
                    print("[poll] rebuild still running after 20 minutes", flush=True)
                    return 1

                # 等到 rebuild 完成后再发 start
                msg_id += 1
                print(f"[{msg_id}] start...", flush=True)
                res = await supervisor_call(
                    ws, msg_id=msg_id, endpoint=f"{base}/start", method="post",
                    timeout=120.0,
                )
                ok = res.get("success", False)
                print(f"    start -> success={ok}", flush=True)
                return 0 if ok else 1

            if cmd not in method_map:
                print(f"unknown command: {cmd}", file=sys.stderr)
                return 2

            method, endpoint = method_map[cmd]
            res = await supervisor_call(
                ws, msg_id=1, endpoint=endpoint, method=method, timeout=900.0,
            )
            ok = res.get("success", False)
            if cmd == "logs" and ok:
                # logs 走 result.data 是字符串
                tail = res.get("result", "")[-3000:]
                print(tail)
                return 0
            print(json.dumps(res, ensure_ascii=False, indent=2)[:4000])
            return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
