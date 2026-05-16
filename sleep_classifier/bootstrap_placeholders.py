# sleep_classifier/bootstrap_placeholders.py
"""在 smart service 启动前把 sensor.sleep_classifier_* 占位实体发出去。

独立于 SleepStatePublisher —— 不 import src/，不读 effective_config，
仅依赖 SUPERVISOR_TOKEN + aiohttp。即使 stage 未绑定、effective_config
损坏，这一步也能先让 Lovelace "有东西可看"。

与 SleepStatePublisher.publish_initial_placeholders 的区别：
- 后者依赖 HomeAssistantClient 与 SleepStatePublisher 实例，要先做
  ping + discovery。
- 本脚本只做 5 次 POST /api/states/<entity_id>，失败降级为
  "best-effort + 日志警告"。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import aiohttp

PLACEHOLDERS = [
    ("sensor.sleep_classifier_stage", "configuring", {"friendly_name": "Sleep stage"}),
    ("sensor.sleep_classifier_confidence", "0", {"friendly_name": "Sleep classifier confidence", "unit_of_measurement": "%"}),
    ("sensor.sleep_classifier_health", "configuring", {"friendly_name": "Sleep classifier health"}),
    ("sensor.sleep_classifier_last_action", "—", {"friendly_name": "Last sleep automation action"}),
    ("sensor.sleep_classifier_session_duration", "0", {"friendly_name": "Sleep session duration", "unit_of_measurement": "s"}),
]

# 所有占位实体共享的 attribute（用户/测试可 grep 这一条断言占位模式在生效）
_COMMON_ATTRS = {"reason": "awaiting_stage_binding", "source": "bootstrap"}


async def post_one(session: aiohttp.ClientSession, base: str, token: str, eid: str, state: str, attrs: dict) -> None:
    """POST a single placeholder entity to HA Core via Supervisor proxy."""
    body = {"state": state, "attributes": {**_COMMON_ATTRS, **attrs}}
    try:
        async with session.post(
            f"{base}/api/states/{eid}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=aiohttp.ClientTimeout(total=5),
        ) as r:
            if r.status not in (200, 201):
                print(f"[bootstrap] {eid} → HTTP {r.status}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001  最多 5 秒延迟，失败降级
        print(f"[bootstrap] {eid} → {type(exc).__name__}: {exc}", file=sys.stderr)


async def main() -> int:
    """Publish 5 placeholder sensors concurrently. Returns 0 always."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    base = os.environ.get("SUPERVISOR_HA_BASE", "http://supervisor/core")
    if not token:
        # Silently return 0 — not running inside HA add-on environment
        return 0
    async with aiohttp.ClientSession() as s:
        await asyncio.gather(*[post_one(s, base, token, *p) for p in PLACEHOLDERS])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
