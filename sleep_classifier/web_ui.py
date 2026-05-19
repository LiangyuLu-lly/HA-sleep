"""Tiny aiohttp app exposing an entity-picker UI for the Sleep Classifier add-on.

Why this file exists
====================
HA Add-on Configuration UI does NOT support an `EntitySelector` widget — that
luxury is reserved for in-process HA Core integrations.  The cheapest path to
"don't make the user type entity_ids by hand" is to embed a small web UI
inside the add-on container and let HA Supervisor reverse-proxy it via the
`Ingress` feature.

User flow
---------
1. User clicks ``Open Web UI`` on the add-on detail page.
2. Supervisor proxies the request, injecting an authenticated session — no
   extra login.
3. We pull the live entity list from HA Core (``/api/states``, auth via the
   ``SUPERVISOR_TOKEN`` env var).
4. User picks the right ``sensor.xxx`` for each slot, clicks Save.
5. We persist their picks into ``/data/web_ui_overrides.json``.
6. ``run_ha_smart_service.py`` merges this file on top of the
   Configuration form values at next add-on restart, so picks always win.

Things this server intentionally does NOT do
--------------------------------------------
* It does *not* mutate ``/data/options.json`` — that's owned by the
  supervisor and would be overwritten the next time the user opens the
  Configuration form.
* It does *not* trigger an add-on restart.  After Save, we tell the user
  to click ``Restart`` in the add-on detail page.  Auto-restart from a
  background process is fragile (signals, supervisor races) and the user
  loses control of *when* their bedroom wakes up reconfiguring itself.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
from aiohttp import web

# Ensure /app is on sys.path so ``from src._io_utils import ...`` works
# when this script is executed directly inside the container.
if "/app" not in sys.path:  # pragma: no cover
    sys.path.insert(0, "/app")

from src._io_utils import atomic_write_json, atomic_write_text
from src._overrides_schema import V2_1_0_DEFAULTS, apply_v2_1_0_defaults

logger = logging.getLogger("web_ui")
logging.basicConfig(
    level=os.environ.get("WEB_UI_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# /data is the persistent volume the supervisor mounts.  Anything we write
# here survives add-on upgrades.
_DATA_DIR = Path("/data")
_OVERRIDES_PATH = _DATA_DIR / "web_ui_overrides.json"
_OPTIONS_PATH = _DATA_DIR / "options.json"

# HA Core proxy URL provided by the supervisor when ``homeassistant_api: true``.
_HA_BASE = os.environ.get("SUPERVISOR_HA_BASE", "http://supervisor/core")
_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

# Domains we surface to the picker.  Anything else is hidden to keep the
# dropdowns short — there's no point listing weather entities for the
# heart-rate slot.
_SENSOR_DOMAINS = {"sensor", "binary_sensor"}
_LIGHT_DOMAINS = {"light"}
_SWITCH_DOMAINS = {"switch"}
_CLIMATE_DOMAINS = {"climate"}
_FAN_DOMAINS = {"fan"}
_HUMIDIFIER_DOMAINS = {"humidifier"}
_MEDIA_DOMAINS = {"media_player"}
_INPUT_NUMBER_DOMAINS = {"input_number"}

# Order matters — this is the order rendered in the form.
_SLOTS: List[Dict[str, Any]] = [
    # v1.3.0: a single sleep-stage entity replaces the three legacy
    # physiology slots.  ``input_select`` is allowed because some users
    # pipe their tracker's output through a helper before exposing it.
    {"key": "sleep_stage_source",  "label": "睡眠阶段实体 / Sleep-stage entity",
     "domains": _SENSOR_DOMAINS | {"input_select"}, "multi": False},
    {"key": "temperature_source",  "label": "温度传感器 / Temperature sensor",
     "domains": {"sensor"},         "multi": False},
    {"key": "humidity_source",     "label": "湿度传感器 / Humidity sensor",
     "domains": {"sensor"},         "multi": False},
    {"key": "illuminance_source",  "label": "光照传感器 / Illuminance sensor",
     "domains": {"sensor"},         "multi": False},
    {"key": "light_targets",       "label": "灯光设备(可多选) / Lights",
     "domains": _LIGHT_DOMAINS,    "multi": True},
    {"key": "switch_targets",      "label": "开关设备(可多选) / Switches",
     "domains": _SWITCH_DOMAINS,   "multi": True},
    {"key": "climate_target",      "label": "空调 / Climate",
     "domains": _CLIMATE_DOMAINS,  "multi": False},
    {"key": "fan_target",          "label": "风扇 / Fan",
     "domains": _FAN_DOMAINS,      "multi": False},
    {"key": "humidifier_target",   "label": "加湿器 / Humidifier",
     "domains": _HUMIDIFIER_DOMAINS, "multi": False},
    {"key": "wake_light_targets",  "label": "唤醒灯(可多选) / Wake lights",
     "domains": _LIGHT_DOMAINS,    "multi": True},
    {"key": "whitenoise_target",   "label": "白噪音播放器 / White-noise player",
     "domains": _MEDIA_DOMAINS,    "multi": False},
    {"key": "feedback_entity",     "label": "主观评分输入 / Subjective rating",
     "domains": _INPUT_NUMBER_DOMAINS, "multi": False},
]


# ---------------------------------------------------------------------------
# v3.0.0 user-profile enums (Task 7.1, R8.2)
# ---------------------------------------------------------------------------
#
# Allowed values for the ``v3_user_profile`` sub-dict written into
# ``/data/web_ui_overrides.json``.  Empty string is **always** allowed and
# means "user did not specify"; the consumers (BAO bucket lookup, etc.)
# treat missing/empty values as ``unspecified`` / ``neutral`` per design
# §4.3.  The enums must stay in lockstep with the ``Literal`` aliases in
# ``src/population_prior.py`` (``AgeBand`` / ``Sex`` / ``Chronotype``).

_V3_AGE_BANDS: tuple[str, ...] = ("18-25", "26-35", "36-50", "51-65", "65+")
_V3_SEXES: tuple[str, ...] = ("M", "F", "unspecified")
_V3_CHRONOTYPES: tuple[str, ...] = ("morning", "evening", "neutral")

# Subset of the ``sensor.*`` aggregate v3 health entity emitted by
# ``SleepStatePublisher`` (R11.6, design §3.5).  We surface its state +
# per-module attributes in the sticky banner that lives on every UI page.
_V3_HEALTH_SENSOR = "sensor.sleep_classifier_v3_health_summary"
_V3_HEALTH_MODULES: tuple[str, ...] = ("bao", "cae", "pp", "emst")



# ---------------------------------------------------------------------------
# HA REST helpers
# ---------------------------------------------------------------------------


async def _fetch_states() -> List[Dict[str, Any]]:
    """Pull the live ``/api/states`` snapshot from HA Core."""
    if not _TOKEN:
        # Running locally outside the supervisor — return an empty list so
        # the UI still renders for development inspection.
        logger.warning("SUPERVISOR_TOKEN not set; returning empty entity list")
        return []
    headers = {"Authorization": f"Bearer {_TOKEN}",
               "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=10)
    url = f"{_HA_BASE}/api/states"
    logger.info("Fetching HA states from %s", url)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as r:
                if r.status != 200:
                    body_preview = (await r.text())[:200]
                    logger.error(
                        "HA states returned HTTP %s from %s: %s",
                        r.status, url, body_preview,
                    )
                    raise aiohttp.ClientError(
                        f"HA Core returned HTTP {r.status}: {body_preview}"
                    )
                data = await r.json()
                logger.info("HA states fetched OK: %d entities", len(data))
                return data
    except asyncio.TimeoutError as exc:
        logger.error("HA states fetch timed out after 10s from %s", url)
        raise aiohttp.ClientError(f"Timeout fetching HA states: {exc}") from exc
    except aiohttp.ClientConnectorError as exc:
        # The most common failure: DNS / routing / firewall stopping us
        # from reaching the Supervisor-proxied HA Core URL.
        logger.error(
            "Cannot connect to HA Core at %s (%s). "
            "Check that homeassistant_api: true is set in config.yaml "
            "and that SUPERVISOR_TOKEN is present in the container env.",
            url, exc,
        )
        raise aiohttp.ClientError(
            f"Cannot reach HA Core at {url}: {exc}"
        ) from exc
    except Exception as exc:    # noqa: BLE001
        logger.error(
            "Unexpected error fetching HA states from %s: %s: %s",
            url, type(exc).__name__, exc,
        )
        raise aiohttp.ClientError(
            f"Unexpected error: {type(exc).__name__}: {exc}"
        ) from exc


def _filter_states(
    states: List[Dict[str, Any]], domains: set,
) -> List[Dict[str, str]]:
    """Project the HA state dump down to what the picker needs."""
    out: List[Dict[str, str]] = []
    for s in states:
        eid = s.get("entity_id", "")
        domain = eid.split(".", 1)[0]
        if domain not in domains:
            continue
        attrs = s.get("attributes", {}) or {}
        friendly = attrs.get("friendly_name") or eid
        out.append({"entity_id": eid, "friendly_name": str(friendly)})
    out.sort(key=lambda x: x["entity_id"])
    return out


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _load_existing() -> Dict[str, Any]:
    """Pre-fill the form with the user's current selections.

    Priority:
      1. ``/data/web_ui_overrides.json`` if present (user already used the
         picker once).
      2. ``/data/options.json`` (Configuration form values).

    The returned dict is always passed through
    :func:`src._overrides_schema.apply_v2_1_0_defaults` so the v2.1.0
    feature flags (``onboarding_skipped``, ``telemetry_enabled``,
    ``upgrade_notifications_enabled``) are present even when reading a
    legacy v2.0.3 file.  Missing fields fall back to the privacy-safest
    defaults (PR3.2 / PR6.1).
    """
    for path in (_OVERRIDES_PATH, _OPTIONS_PATH):
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Could not read %s: %s", path, exc)
                continue
            if not isinstance(raw, dict):
                logger.warning(
                    "Ignoring non-object content in %s (got %s)",
                    path, type(raw).__name__,
                )
                continue
            return apply_v2_1_0_defaults(raw)
    return apply_v2_1_0_defaults(None)


def _normalise(value: Any) -> Any:
    """Strip the ``""`` literal users sometimes paste into Configuration."""
    if isinstance(value, str):
        v = value.strip()
        return "" if v in ('""', "''") else v
    if isinstance(value, list):
        return [v for v in (_normalise(x) for x in value) if v != ""]
    return value


# ---------------------------------------------------------------------------
# v3.0.0 user-profile helpers (Task 7.1)
# ---------------------------------------------------------------------------


def _coerce_enum(value: Any, allowed: tuple[str, ...]) -> str:
    """Return *value* if it's a valid enum member, otherwise empty string.

    Empty / missing / wrong-type / illegal-value all collapse to ``""``;
    callers interpret ``""`` as "user did not specify" (R8.2).  We never
    raise — onboarding tolerates malformed picks rather than 500ing.
    """
    if not isinstance(value, str):
        return ""
    v = value.strip()
    return v if v in allowed else ""


def _build_v3_user_profile(body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Project the wizard POST body into the ``v3_user_profile`` sub-dict.

    Returns ``None`` when the body carries no v3 profile fields at all,
    so legacy clients (and the Skip button) don't grow an empty
    sub-dict in ``web_ui_overrides.json``.

    Schema (design §4.3)::

        {
          "age_band": "26-35" | "",
          "sex": "F" | "M" | "unspecified" | "",
          "chronotype": "evening" | ...,
          "set_at": "ISO-8601 UTC",
          "prior_weight_lock": null | 0.0,
        }

    ``prior_weight_lock`` is either ``None`` (no lock) or ``0.0`` (R8.5
    user-controlled hard kill of the population prior).  The flag is
    sourced from a checkbox (``prior_weight_lock_zero``) to keep the
    JSON shape forward-compatible with future intermediate values.
    """
    profile_keys = (
        "age_band", "sex", "chronotype", "prior_weight_lock_zero",
    )
    if not any(k in body for k in profile_keys):
        return None

    age_band = _coerce_enum(body.get("age_band", ""), _V3_AGE_BANDS)
    sex = _coerce_enum(body.get("sex", ""), _V3_SEXES)
    chronotype = _coerce_enum(body.get("chronotype", ""), _V3_CHRONOTYPES)
    lock_flag = bool(body.get("prior_weight_lock_zero", False))

    return {
        "age_band": age_band,
        "sex": sex,
        "chronotype": chronotype,
        "set_at": _dt.datetime.now(_dt.timezone.utc)
                  .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "prior_weight_lock": 0.0 if lock_flag else None,
    }


async def _fetch_v3_health_summary() -> Dict[str, Any]:
    """Return the ``sensor.sleep_classifier_v3_health_summary`` snapshot.

    Output shape::

        {
          "state": "green" | "amber" | "red" | "unknown",
          "modules": {"bao": "...", "cae": "...", "pp": "...", "emst": "..."},
        }

    Module values come from the sensor's per-module attributes (R11.6,
    design §3.5); each value is one of ``healthy`` / ``degraded`` /
    ``red`` / ``disabled`` / ``unknown``.  When HA Core is unreachable
    or the sensor doesn't exist yet, we degrade to ``unknown`` rather
    than 502 — the banner is informational, not load-bearing.
    """
    fallback: Dict[str, Any] = {
        "state": "unknown",
        "modules": {m: "unknown" for m in _V3_HEALTH_MODULES},
    }

    if not _TOKEN:
        return fallback

    headers = {"Authorization": f"Bearer {_TOKEN}",
               "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=5)
    url = f"{_HA_BASE}/api/states/{_V3_HEALTH_SENSOR}"
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as r:
                if r.status != 200:
                    logger.debug(
                        "v3 health summary HTTP %s from %s", r.status, url,
                    )
                    return fallback
                payload = await r.json()
    except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
        logger.debug("v3 health summary fetch failed: %s", exc)
        return fallback
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "v3 health summary unexpected %s: %s", type(exc).__name__, exc,
        )
        return fallback

    state = str(payload.get("state") or "unknown")
    attrs = payload.get("attributes") or {}
    modules: Dict[str, str] = {}
    for m in _V3_HEALTH_MODULES:
        v = attrs.get(m)
        modules[m] = str(v) if isinstance(v, str) and v else "unknown"
    return {"state": state, "modules": modules}


# ---------------------------------------------------------------------------
# Ingress IP allowlist middleware
# ---------------------------------------------------------------------------

# HA Supervisor 的 docker network (``hassio``) 在 HA OS 上是固定的：
# IPv4 172.30.32.2，见 HA Supervisor docs → Networking。
# 如果未来 HA 把 Supervisor 放到 IPv6 网段，我们通过 env 兜底：
# SUPERVISOR_IP_WHITELIST 可填 "172.30.32.2,fd00::2,::1"。
# 多值以逗号分隔；空白容错。
_DEFAULT_ALLOWED_IPS: set = {"172.30.32.2"}


def _parse_allowed_ips(raw: str) -> set:
    return {ip.strip() for ip in raw.split(",") if ip.strip()}


_ALLOWED_IPS = (
    _parse_allowed_ips(os.environ.get("SUPERVISOR_IP_WHITELIST", ""))
    or _DEFAULT_ALLOWED_IPS
)
_DISABLE_GUARD = os.environ.get("WEB_UI_DISABLE_INGRESS_GUARD", "") == "1"


@web.middleware
async def ingress_ip_guard(request: web.Request, handler):
    """仅允许 Supervisor ingress 源 IP 的请求通过。

    aiohttp 的 request.remote 在 run_app(host='0.0.0.0') 下是 TCP 对端
    IP (Docker 容器网络里就是 Supervisor IP)，不经 nginx 反代所以不需要
    去解析 X-Forwarded-For。若未来 HA 换网络栈，SUPERVISOR_IP_WHITELIST
    环境变量提供向前兼容。
    """
    if _DISABLE_GUARD:
        return await handler(request)
    remote = request.remote or ""
    # IPv6 的 ::ffff:172.30.32.2 映射地址也接受
    normalized = remote.removeprefix("::ffff:")
    if normalized not in _ALLOWED_IPS:
        logger.warning("Rejected Web UI request from non-Supervisor IP: %s", remote)
        return web.Response(status=403, text="Forbidden")
    return await handler(request)


# ---------------------------------------------------------------------------
# HTTP handlers
# --------------------------------------------------------------------
# IMPORTANT: 前端 fetch() 必须使用不以 '/' 开头的相对路径。Supervisor
# Ingress 会透明注入 `/api/hassio_ingress/<token>/` 前缀；绝对路径
# 脱离 Ingress 命名空间，会 404 (Supervisor 拦截) 或 500 (打到真 HA
# Core)。有回归测试在 tests/test_web_ui_ingress_paths.py 守护。
# ---------------------------------------------------------------------------


async def index(_: web.Request) -> web.Response:
    """Render the single-page picker UI."""
    return web.Response(text=_INDEX_HTML, content_type="text/html")


async def api_entities(_: web.Request) -> web.Response:
    """Return per-slot lists of candidate entities.

    Each slot only sees the domains that actually make sense for it —
    saves the browser from rendering a 500-row dropdown of weather and
    sun.* sensors when the user is picking a heart-rate input.
    """
    try:
        states = await _fetch_states()
    except aiohttp.ClientError as exc:
        logger.error("Failed to fetch HA states: %s", exc)
        return web.json_response(
            {"error": f"Could not reach HA Core: {exc}"}, status=502,
        )

    payload: Dict[str, Any] = {"slots": {}, "current": _load_existing()}
    for slot in _SLOTS:
        payload["slots"][slot["key"]] = {
            "label": slot["label"],
            "multi": slot["multi"],
            "candidates": _filter_states(states, slot["domains"]),
        }
    return web.json_response(payload)


async def api_save(request: web.Request) -> web.Response:
    """Persist user picks to ``web_ui_overrides.json``.

    We accept a JSON body shaped like the slot dict, validate that every
    string value matches a real entity_id in HA, then atomically replace
    the file on disk.  Atomicity matters because a partially-written
    file would crash the service on the next start.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    # Pull a fresh entity snapshot so we can sanity-check picks against
    # what's actually live in HA.  This guards against a stale browser
    # tab posting an entity that was just deleted.
    try:
        states = await _fetch_states()
    except aiohttp.ClientError as exc:
        return web.json_response(
            {"error": f"Could not reach HA Core: {exc}"}, status=502,
        )
    valid_ids = {s.get("entity_id") for s in states}

    cleaned: Dict[str, Any] = {}
    rejected: List[str] = []
    for slot in _SLOTS:
        key = slot["key"]
        if key not in body:
            continue
        value = _normalise(body[key])
        if slot["multi"]:
            if not isinstance(value, list):
                rejected.append(f"{key}: expected list")
                continue
            kept: List[str] = []
            for item in value:
                if item and item in valid_ids:
                    kept.append(item)
                elif item:
                    rejected.append(f"{key}: unknown entity '{item}'")
            cleaned[key] = kept
        else:
            if value and value not in valid_ids:
                rejected.append(f"{key}: unknown entity '{value}'")
                continue
            cleaned[key] = value

    # Atomic write — if Python dies mid-write the user keeps their old
    # config rather than a half-truncated JSON.  We also carry over any
    # v2.1.0 feature flags (``onboarding_skipped`` /
    # ``telemetry_enabled`` / ``upgrade_notifications_enabled``) that may
    # already be set on disk so saving the slot picker doesn't wipe an
    # opt-in toggled by a separate telemetry / upgrade route — task 6.6
    # in this spec.  ``_load_existing`` already runs
    # ``apply_v2_1_0_defaults`` so missing keys back-fill to the
    # privacy-safest defaults.
    existing = _load_existing()
    preserved_flags = {k: existing[k] for k in V2_1_0_DEFAULTS if k in existing}
    merged: Dict[str, Any] = {**preserved_flags, **cleaned}
    try:
        atomic_write_json(_OVERRIDES_PATH, merged)
    except OSError as exc:
        return web.json_response(
            {"error": f"Could not persist overrides: {exc}"}, status=500,
        )

    logger.info(
        "Saved %d slot overrides to %s; %d rejected",
        len(cleaned), _OVERRIDES_PATH, len(rejected),
    )
    return web.json_response({
        "saved": merged,
        "rejected": rejected,
        "message": (
            "Saved.  Click Restart in the add-on detail page to apply."
        ),
    })


# ---------------------------------------------------------------------------
# Onboarding wizard routes
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# HTML — kept inline so the add-on image stays single-file-ish
# ---------------------------------------------------------------------------


_INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sleep Classifier — Entity Picker</title>
<style>
  :root { color-scheme: light dark; }
  body {
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI",
          "PingFang SC", "Microsoft YaHei", Roboto, sans-serif;
    margin: 0; padding: 24px; max-width: 760px; margin-inline: auto;
    background: var(--bg, #fafafa); color: var(--fg, #222);
  }
  @media (prefers-color-scheme: dark) {
    body { --bg: #1d1d1d; --fg: #eee; }
    select, input, button { background: #2a2a2a; color: #eee;
                            border: 1px solid #444; }
  }
  h1 { font-size: 22px; margin: 0 0 4px; }
  p.lead { color: #888; margin: 0 0 24px; }
  .row { display: grid; grid-template-columns: 1fr 2fr; gap: 12px;
         align-items: center; padding: 10px 0;
         border-bottom: 1px solid rgba(127,127,127,.18); }
  .row label { font-weight: 500; }
  select, input[type=text] {
    width: 100%; padding: 7px 9px; border-radius: 6px;
    border: 1px solid #ccc; box-sizing: border-box; font: inherit;
  }
  select[multiple] { height: 96px; }
  .actions { margin-top: 22px; display: flex; gap: 12px; align-items: center; }
  button {
    padding: 9px 18px; border-radius: 6px; border: none; cursor: pointer;
    font: inherit; background: #1a73e8; color: white;
  }
  button:disabled { opacity: .55; cursor: progress; }
  button.secondary { background: transparent; color: inherit;
                     border: 1px solid #999; }
  #status { font-size: 14px; }
  #status.ok { color: #2e7d32; }
  #status.err { color: #c62828; }
  details { margin-top: 18px; font-size: 13px; color: #666; }
  .v3-health { position: sticky; top: 0; z-index: 100;
               background: var(--bg, #fafafa); border: 1px solid #ddd;
               border-radius: 4px; padding: 6px 12px; margin-bottom: 12px;
               font-size: 13px; display: flex; gap: 8px;
               align-items: center; flex-wrap: wrap; }
  .v3-health .badge { display: inline-flex; align-items: center;
                      gap: 4px; padding: 2px 8px; border-radius: 10px;
                      border: 1px solid #ccc; background: var(--bg, #fff); }
  .v3-health .dot { width: 8px; height: 8px; border-radius: 50%;
                    background: #bbb; display: inline-block; }
  .v3-health .dot.green { background: #2e7d32; }
  .v3-health .dot.amber { background: #ed6c02; }
  .v3-health .dot.red { background: #c62828; }
  .v3-health .dot.disabled { background: #9e9e9e; }
  .v3-health .dot.unknown { background: #bdbdbd; }
</style>
</head>
<body>

<div id="v3-health" class="v3-health" data-state="unknown">
  <strong>v3 modules:</strong>
  <span class="badge" data-module="bao"><span class="dot unknown"></span>BAO: <span class="m-state">unknown</span></span>
  <span class="badge" data-module="cae"><span class="dot unknown"></span>CAE: <span class="m-state">unknown</span></span>
  <span class="badge" data-module="pp"><span class="dot unknown"></span>PP: <span class="m-state">unknown</span></span>
  <span class="badge" data-module="emst"><span class="dot unknown"></span>EMST: <span class="m-state">unknown</span></span>
</div>

<h1>Sleep Classifier — Entity Picker</h1>
<p class="lead">从 Home Assistant 的实时实体里选,而不是手敲 entity_id。<br>
   留空 ⇒ add-on 启动时按 area + 关键词自动发现。</p>

<form id="form">
  <div id="rows">Loading…</div>

  <div class="actions">
    <button type="submit" id="save">保存 / Save</button>
    <button type="button" class="secondary" id="reload">重新加载实体列表</button>
    <span id="status"></span>
  </div>
</form>

<details>
  <summary>提示</summary>
  <ul>
    <li>保存后,需要去 add-on 详情页点 <strong>Restart</strong> 才生效。</li>
    <li>这里的选择会写到 <code>/data/web_ui_overrides.json</code>,
        <strong>覆盖</strong> Configuration 表单里的同名字段。</li>
    <li>多选项目按住 <kbd>Ctrl</kbd>(macOS: <kbd>⌘</kbd>)点击。</li>
  </ul>
</details>

<script>
async function loadEntities() {
  const status = document.getElementById('status');
  status.textContent = '加载实体列表…'; status.className = '';
  document.getElementById('rows').textContent = 'Loading…';
  try {
    const r = await fetch('api/entities');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    render(data);
    status.textContent = '已加载 ' +
      Object.values(data.slots)
            .reduce((s, x) => s + x.candidates.length, 0) +
      ' 个候选实体';
    status.className = 'ok';
  } catch (e) {
    status.textContent = '加载失败: ' + e.message;
    status.className = 'err';
    document.getElementById('rows').textContent = '';
  }
}

function render(data) {
  const wrap = document.getElementById('rows'); wrap.innerHTML = '';
  const current = data.current || {};
  for (const [key, slot] of Object.entries(data.slots)) {
    const row = document.createElement('div'); row.className = 'row';
    const label = document.createElement('label');
    label.textContent = slot.label; label.htmlFor = key; row.appendChild(label);

    const sel = document.createElement('select');
    sel.id = key; sel.name = key;
    if (slot.multi) sel.multiple = true;

    if (!slot.multi) {
      const blank = document.createElement('option');
      blank.value = ''; blank.textContent = '— 留空(自动发现) —';
      sel.appendChild(blank);
    }
    for (const c of slot.candidates) {
      const opt = document.createElement('option');
      opt.value = c.entity_id;
      opt.textContent = c.entity_id +
        (c.friendly_name && c.friendly_name !== c.entity_id
          ? '  ·  ' + c.friendly_name : '');
      sel.appendChild(opt);
    }

    // pre-select current value(s)
    const cur = current[key];
    if (Array.isArray(cur)) {
      for (const v of cur)
        for (const o of sel.options) if (o.value === v) o.selected = true;
    } else if (typeof cur === 'string') {
      for (const o of sel.options) if (o.value === cur) o.selected = true;
    }

    row.appendChild(sel); wrap.appendChild(row);
  }
}

document.getElementById('form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const status = document.getElementById('status');
  const btn = document.getElementById('save');
  btn.disabled = true; status.textContent = '保存中…'; status.className = '';
  const body = {};
  for (const sel of document.querySelectorAll('select')) {
    if (sel.multiple) {
      body[sel.name] = [...sel.selectedOptions].map(o => o.value);
    } else {
      body[sel.name] = sel.value;
    }
  }
  try {
    const r = await fetch('api/options', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
    status.textContent = data.message;
    status.className = 'ok';
    if (data.rejected && data.rejected.length) {
      status.textContent += '  忽略: ' + data.rejected.join(', ');
    }
  } catch (e) {
    status.textContent = '保存失败: ' + e.message;
    status.className = 'err';
  } finally {
    btn.disabled = false;
  }
});

document.getElementById('reload').addEventListener('click', loadEntities);
loadEntities();

// v3 health summary banner (Task 7.1, R11.6)
function _v3StateClass(s) {
  s = (s || '').toLowerCase();
  if (s === 'green' || s === 'healthy') return 'green';
  if (s === 'amber' || s === 'degraded') return 'amber';
  if (s === 'red') return 'red';
  if (s === 'disabled') return 'disabled';
  return 'unknown';
}
function _v3Refresh() {
  fetch('api/v3/health').then(r=>r.json()).then(d => {
    const banner = document.getElementById('v3-health');
    if (!banner) return;
    banner.dataset.state = _v3StateClass(d.state);
    const mods = d.modules || {};
    banner.querySelectorAll('.badge').forEach(b => {
      const m = b.dataset.module;
      const v = mods[m] || 'unknown';
      const dot = b.querySelector('.dot');
      const lbl = b.querySelector('.m-state');
      if (dot) dot.className = 'dot ' + _v3StateClass(v);
      if (lbl) lbl.textContent = v;
    });
  }).catch(()=>{});
}
_v3Refresh();
setInterval(_v3Refresh, 30000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Telemetry toggle routes (Task 6.6)
# ---------------------------------------------------------------------------

# Injected by ``make_app(telemetry_reporter=...)``; may be None when the
# module is loaded standalone (tests, dry-run) or before the task is wired.
_telemetry_reporter: Any = None

_LAST_UPGRADE_CHECK_PATH = _DATA_DIR / "last_upgrade_check.json"


async def api_telemetry_status(_: web.Request) -> web.Response:
    """GET /api/telemetry/status — read current telemetry_enabled flag."""
    data = _load_existing()
    return web.json_response({"enabled": data.get("telemetry_enabled", False)})


async def api_telemetry_toggle(request: web.Request) -> web.Response:
    """POST /api/telemetry/toggle — switch telemetry on/off.

    Body: ``{"enabled": true|false}``

    When switching to false, calls the injected TelemetryReporter.disable()
    to stop the background task and delete install_id within ≤ 30 s.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, Exception):
        return web.json_response({"error": "Invalid JSON"}, status=400)

    enabled = bool(body.get("enabled", False))
    data = _load_existing()
    data["telemetry_enabled"] = enabled
    try:
        atomic_write_json(_OVERRIDES_PATH, data)
    except OSError as exc:
        return web.json_response(
            {"error": f"Could not persist setting: {exc}"}, status=500,
        )

    # If switching to false, call disable on the reporter
    if not enabled and _telemetry_reporter is not None:
        try:
            await _telemetry_reporter.disable()
        except Exception as exc:  # noqa: BLE001
            logger.warning("TelemetryReporter.disable() failed: %s", exc)

    return web.json_response({"enabled": enabled})


async def api_upgrade_status(_: web.Request) -> web.Response:
    """GET /api/upgrade/status — read last upgrade check result."""
    if not _LAST_UPGRADE_CHECK_PATH.is_file():
        return web.json_response({"available": False})
    try:
        raw = json.loads(
            _LAST_UPGRADE_CHECK_PATH.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return web.json_response({"available": False})

    latest = raw.get("latest", "")
    # We consider upgrade available if the file has a "latest" field set
    available = bool(latest)
    return web.json_response({
        "available": available,
        "latest": latest,
        "url": raw.get("url", f"https://github.com/pzq123456/sleep-classifier/releases/tag/{latest}"),
    })


# ---------------------------------------------------------------------------
# v3 health summary route (Task 7.1, R11.6)
# ---------------------------------------------------------------------------


async def api_v3_health(_: web.Request) -> web.Response:
    """GET /api/v3/health — drive the sticky health banner.

    Always returns 200; degraded states surface in the JSON body rather
    than as HTTP errors so the banner JS can keep its layout.  See
    :func:`_fetch_v3_health_summary` for output shape.
    """
    return web.json_response(await _fetch_v3_health_summary())


# ---------------------------------------------------------------------------
# Onboarding wizard routes (Task 6.1)
# ---------------------------------------------------------------------------

# Cache for candidate scan (avoid hammering HA on every wizard step render)
_candidates_cache: dict[str, Any] = {"data": None, "ts": 0.0}
_CANDIDATE_CACHE_TTL = 60.0  # seconds


def _get_locale(request: web.Request) -> str:
    """Extract locale from Accept-Language header; default to 'en'."""
    accept = request.headers.get("Accept-Language", "")
    # Simple parse: look for zh-cn or zh first
    lower = accept.lower()
    if "zh-cn" in lower or "zh" in lower:
        return "zh-cn"
    return "en"


def _load_translations(locale: str) -> dict[str, Any]:
    """Load translation YAML for the given locale, fallback to en."""
    import importlib.resources
    translations_dir = Path(__file__).parent / "translations"
    target = translations_dir / f"{locale}.yaml"
    if not target.is_file():
        target = translations_dir / "en.yaml"
    if not target.is_file():
        return {}
    try:
        import yaml  # type: ignore[import-untyped]
        with open(target, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        # Fallback: no yaml available — return empty (tests may not have yaml)
        return {}
    except Exception:  # noqa: BLE001
        return {}


def _onboarding_html(locale: str) -> str:
    """Render the onboarding wizard HTML with i18n strings."""
    tr = _load_translations(locale)
    onb = tr.get("onboarding", {})
    step1_title = onb.get("step1_title", "Welcome to Sleep Classifier")
    step1_disclaimer = onb.get("step1_disclaimer",
        "This add-on is not a medical device. See the Medical Disclaimer.")
    step2_title = onb.get("step2_title", "Scanning for sleep-stage entities")
    no_hardware_cta = onb.get("no_hardware_cta",
        "I have no sleep-stage hardware — show me recommendations")
    step3_title = onb.get("step3_title", "Confirm environment sensors and actuators")
    step4_title = onb.get("step4_title", "Safety confirmation")
    step4_dry_run_warning = onb.get("step4_dry_run_warning",
        "Strongly recommended: keep dry_run=true for at least 7 days.")
    step4_finish = onb.get("step4_finish", "Finish setup")

    # v3.0.0 user-profile labels (Task 7.1).  Translation files may
    # override; fallbacks below keep the wizard usable when YAML is
    # missing.
    profile_title = onb.get("profile_title",
        "Optional user profile (improves cold-start setpoints)")
    profile_subtitle = onb.get("profile_subtitle",
        "All fields are optional. Empty values fall back to 'unspecified'.")
    profile_age_label = onb.get("profile_age_label", "Age band")
    profile_sex_label = onb.get("profile_sex_label", "Sex")
    profile_chrono_label = onb.get("profile_chrono_label", "Chronotype")
    profile_lock_label = onb.get("profile_lock_label",
        "Lock prior_weight to 0 (do not blend population data)")
    profile_lock_hint = onb.get("profile_lock_hint",
        "When checked, the population prior is fully ignored — even before "
        "you have 7 nights of personal history.")
    profile_privacy = onb.get("profile_privacy",
        "Stored locally in /data/web_ui_overrides.json. Never uploaded.")

    return f"""<!doctype html>
<html lang="{locale}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sleep Classifier — Onboarding</title>
<style>
  body {{ font: 15px/1.5 sans-serif; margin: 0; padding: 24px;
         max-width: 700px; margin-inline: auto; }}
  .step {{ display: none; }}
  .step.active {{ display: block; }}
  button {{ padding: 9px 18px; border-radius: 6px; border: none;
           cursor: pointer; font: inherit; background: #1a73e8; color: white; }}
  .skip-btn {{ background: #888; }}
  .privacy-link {{ font-size: 13px; color: #666; }}
  .upgrade-banner {{ background: #fffbe6; border: 1px solid #ffe58f;
                     padding: 8px 16px; border-radius: 4px;
                     position: sticky; top: 0; z-index: 100; }}
  .telemetry-toggle {{ position: sticky; top: 40px; z-index: 99;
                       background: #f5f5f5; padding: 6px 12px;
                       border-radius: 4px; font-size: 13px; }}
  .v3-health {{ position: sticky; top: 76px; z-index: 98;
                background: #fafafa; border: 1px solid #ddd;
                border-radius: 4px; padding: 6px 12px; font-size: 13px;
                display: flex; gap: 8px; align-items: center;
                flex-wrap: wrap; }}
  .v3-health .badge {{ display: inline-flex; align-items: center;
                       gap: 4px; padding: 2px 8px; border-radius: 10px;
                       border: 1px solid #ccc; background: #fff; }}
  .v3-health .dot {{ width: 8px; height: 8px; border-radius: 50%;
                     background: #bbb; display: inline-block; }}
  .v3-health .dot.green {{ background: #2e7d32; }}
  .v3-health .dot.amber {{ background: #ed6c02; }}
  .v3-health .dot.red {{ background: #c62828; }}
  .v3-health .dot.disabled {{ background: #9e9e9e; }}
  .v3-health .dot.unknown {{ background: #bdbdbd; }}
  .v3-profile {{ margin: 16px 0; padding: 12px;
                 border: 1px solid #ddd; border-radius: 6px; }}
  .v3-profile .group {{ margin-bottom: 10px; }}
  .v3-profile legend {{ font-weight: 500; padding: 0 6px; }}
  .v3-profile label {{ margin-right: 12px; font-weight: normal; }}
  .v3-profile .hint {{ color: #666; font-size: 12px; margin-top: 4px; }}
</style>
</head>
<body>
<div id="upgrade-banner" class="upgrade-banner" style="display:none;">
  <span id="upgrade-text"></span>
</div>
<div class="telemetry-toggle">
  <label><input type="checkbox" id="telemetry-cb"> Anonymous telemetry</label>
  <a class="privacy-link" href="../PRIVACY.md">Privacy policy</a>
</div>
<div id="v3-health" class="v3-health" data-state="unknown">
  <strong>v3 modules:</strong>
  <span class="badge" data-module="bao"><span class="dot unknown"></span>BAO: <span class="m-state">unknown</span></span>
  <span class="badge" data-module="cae"><span class="dot unknown"></span>CAE: <span class="m-state">unknown</span></span>
  <span class="badge" data-module="pp"><span class="dot unknown"></span>PP: <span class="m-state">unknown</span></span>
  <span class="badge" data-module="emst"><span class="dot unknown"></span>EMST: <span class="m-state">unknown</span></span>
</div>

<div id="step1" class="step active">
  <h2>{step1_title}</h2>
  <p>{step1_disclaimer}</p>
  <button onclick="showStep(2)">Next</button>
</div>

<div id="step2" class="step">
  <h2>{step2_title}</h2>
  <div id="candidates">Loading...</div>
  <p id="no-hw" style="display:none;">
    <a href="../docs/HARDWARE.md">{no_hardware_cta}</a>
  </p>
  <div id="ha-degraded" style="display:none;">
    <p>HA is not reachable. You can skip the wizard and configure manually.</p>
    <button class="skip-btn" onclick="skipWizard()">Skip</button>
  </div>
  <button onclick="showStep(3)">Next</button>
</div>

<div id="step3" class="step">
  <h2>{step3_title}</h2>
  <p>Select your environment sensors and actuators below.</p>

  <fieldset class="v3-profile">
    <legend>{profile_title}</legend>
    <p class="hint">{profile_subtitle}</p>

    <div class="group" id="ageband-group">
      <strong>{profile_age_label}:</strong>
      <label><input type="radio" name="age_band" value=""> —</label>
      <label><input type="radio" name="age_band" value="18-25"> 18-25</label>
      <label><input type="radio" name="age_band" value="26-35"> 26-35</label>
      <label><input type="radio" name="age_band" value="36-50"> 36-50</label>
      <label><input type="radio" name="age_band" value="51-65"> 51-65</label>
      <label><input type="radio" name="age_band" value="65+"> 65+</label>
    </div>

    <div class="group" id="sex-group">
      <strong>{profile_sex_label}:</strong>
      <label><input type="radio" name="sex" value=""> —</label>
      <label><input type="radio" name="sex" value="M"> M</label>
      <label><input type="radio" name="sex" value="F"> F</label>
      <label><input type="radio" name="sex" value="unspecified"> unspecified</label>
    </div>

    <div class="group" id="chronotype-group">
      <strong>{profile_chrono_label}:</strong>
      <label><input type="radio" name="chronotype" value=""> —</label>
      <label><input type="radio" name="chronotype" value="morning"> morning</label>
      <label><input type="radio" name="chronotype" value="evening"> evening</label>
      <label><input type="radio" name="chronotype" value="neutral"> neutral</label>
    </div>

    <div class="group">
      <label>
        <input type="checkbox" id="prior-lock-cb" name="prior_weight_lock_zero">
        {profile_lock_label}
      </label>
      <p class="hint">{profile_lock_hint}</p>
    </div>

    <p class="hint">{profile_privacy}</p>
  </fieldset>

  <button onclick="showStep(4)">Next</button>
</div>

<div id="step4" class="step">
  <h2>{step4_title}</h2>
  <p>{step4_dry_run_warning}</p>
  <button onclick="finishWizard(false)">{step4_finish}</button>
  <button onclick="finishWizard(true)" style="background:#c62828;">Disable dry_run</button>
</div>

<script>
function showStep(n) {{
  document.querySelectorAll('.step').forEach(s => s.classList.remove('active'));
  document.getElementById('step'+n).classList.add('active');
  if (n === 2) loadCandidates();
}}
async function loadCandidates() {{
  try {{
    const r = await fetch('api/onboarding/candidates');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    if (data.error) {{
      document.getElementById('ha-degraded').style.display = 'block';
      document.getElementById('candidates').textContent = '';
      return;
    }}
    if (!data.candidates || data.candidates.length === 0) {{
      document.getElementById('no-hw').style.display = 'block';
      document.getElementById('candidates').textContent = 'No candidates found.';
      return;
    }}
    let html = '<ul>';
    data.candidates.forEach(c => {{
      html += '<li>' + c.entity_id + ' (' + c.friendly_name + ', score: ' + c.score + ')</li>';
    }});
    html += '</ul>';
    document.getElementById('candidates').innerHTML = html;
  }} catch(e) {{
    document.getElementById('ha-degraded').style.display = 'block';
    document.getElementById('candidates').textContent = 'Error: ' + e.message;
  }}
}}
function skipWizard() {{
  fetch('api/onboarding/save', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{skip: true}})
  }}).then(() => window.location = '.');
}}
async function finishWizard(disableDryRun) {{
  const body = {{sleep_stage_source: 'user_selected'}};
  // v3 user profile (Task 7.1).  Empty string means "unspecified".
  const ageBand = document.querySelector('input[name="age_band"]:checked');
  const sex = document.querySelector('input[name="sex"]:checked');
  const chrono = document.querySelector('input[name="chronotype"]:checked');
  body.age_band = ageBand ? ageBand.value : '';
  body.sex = sex ? sex.value : '';
  body.chronotype = chrono ? chrono.value : '';
  const lockCb = document.getElementById('prior-lock-cb');
  body.prior_weight_lock_zero = !!(lockCb && lockCb.checked);
  if (disableDryRun) body.confirm_disable_dry_run = true;
  await fetch('api/onboarding/save', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(body)
  }});
  window.location = '.';
}}
// Upgrade banner
fetch('api/upgrade/status').then(r=>r.json()).then(d => {{
  if (d.available) {{
    document.getElementById('upgrade-banner').style.display = 'block';
    document.getElementById('upgrade-text').textContent =
      'v' + d.latest + ' is available. Release notes: ' + d.url;
  }}
}}).catch(()=>{{}});
// Telemetry toggle
fetch('api/telemetry/status').then(r=>r.json()).then(d => {{
  document.getElementById('telemetry-cb').checked = d.enabled;
}}).catch(()=>{{}});
document.getElementById('telemetry-cb').addEventListener('change', (ev) => {{
  fetch('api/telemetry/toggle', {{
    method:'POST',
    headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{enabled: ev.target.checked}})
  }});
}});
// v3 health summary banner (Task 7.1, R11.6)
function _v3StateClass(s) {{
  s = (s || '').toLowerCase();
  if (s === 'green' || s === 'healthy') return 'green';
  if (s === 'amber' || s === 'degraded') return 'amber';
  if (s === 'red') return 'red';
  if (s === 'disabled') return 'disabled';
  return 'unknown';
}}
function _v3Refresh() {{
  fetch('api/v3/health').then(r=>r.json()).then(d => {{
    const banner = document.getElementById('v3-health');
    if (!banner) return;
    banner.dataset.state = _v3StateClass(d.state);
    const mods = d.modules || {{}};
    banner.querySelectorAll('.badge').forEach(b => {{
      const m = b.dataset.module;
      const v = mods[m] || 'unknown';
      const dot = b.querySelector('.dot');
      const lbl = b.querySelector('.m-state');
      if (dot) dot.className = 'dot ' + _v3StateClass(v);
      if (lbl) lbl.textContent = v;
    }});
  }}).catch(()=>{{}});
}}
_v3Refresh();
setInterval(_v3Refresh, 30000);
</script>
</body>
</html>"""


async def onboarding(request: web.Request) -> web.Response:
    """GET /onboarding — render the wizard SPA."""
    locale = _get_locale(request)
    return web.Response(text=_onboarding_html(locale), content_type="text/html")


async def api_onboarding_candidates(request: web.Request) -> web.Response:
    """GET /api/onboarding/candidates — scan HA for sleep-stage entities."""
    import time
    now = time.time()
    if (
        _candidates_cache["data"] is not None
        and (now - _candidates_cache["ts"]) < _CANDIDATE_CACHE_TTL
    ):
        return web.json_response(_candidates_cache["data"])

    try:
        states = await _fetch_states()
    except Exception as exc:  # noqa: BLE001
        logger.warning("HA unreachable during onboarding scan: %s", exc)
        return web.json_response({"error": str(exc), "candidates": []})

    from src.onboarding_scanner import filter_candidates
    candidates = filter_candidates(states)
    result = {
        "candidates": [
            {"entity_id": c.entity_id, "friendly_name": c.friendly_name, "score": c.score}
            for c in candidates
        ]
    }
    _candidates_cache["data"] = result
    _candidates_cache["ts"] = now
    return web.json_response(result)


async def api_onboarding_save(request: web.Request) -> web.Response:
    """POST /api/onboarding/save — persist wizard results."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, Exception):
        return web.json_response({"error": "Invalid JSON"}, status=400)

    data = _load_existing()
    # Merge slot picks from body
    for key in ("sleep_stage_source", "temperature_source", "humidity_source",
                "illuminance_source", "light_targets", "climate_target",
                "fan_target", "humidifier_target"):
        if key in body:
            data[key] = body[key]

    # v3.0.0 user profile (Task 7.1, R8.2 / R8.5 / R8.7).  Empty/missing
    # fields collapse to "" and are interpreted downstream as
    # ``unspecified`` / ``neutral``; the prior_weight_lock checkbox
    # writes 0.0 (R8.5) or null.  We only mutate the sub-dict when at
    # least one v3 profile field is present in the body so that pure
    # v2.x clients (or the Skip path) leave existing v3 settings alone.
    v3_profile = _build_v3_user_profile(body)
    if v3_profile is not None:
        data["v3_user_profile"] = v3_profile

    data["onboarding_skipped"] = True

    # dry_run safety: only set to false if explicitly confirmed
    if body.get("confirm_disable_dry_run") is True:
        data["dry_run"] = False
    else:
        data.setdefault("dry_run", True)

    try:
        atomic_write_json(_OVERRIDES_PATH, data)
    except OSError as exc:
        return web.json_response(
            {"error": f"Could not persist: {exc}"}, status=500,
        )
    return web.json_response({"saved": True})


# ---------------------------------------------------------------------------
# Dashboard importer route (Task 6.4)
# ---------------------------------------------------------------------------

# Injected HAAPIClient instance; set by make_app() or tests
_ha_client: Any = None


async def api_dashboard_import(request: web.Request) -> web.Response:
    """POST /api/dashboard/import — one-click Lovelace dashboard creation."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, Exception):
        body = {}

    confirm_overwrite = body.get("confirm_overwrite", False)

    # Import here to avoid circular deps at module load time
    sys.path.insert(0, str(Path(__file__).parent))
    from lovelace_template import (
        DASHBOARD_URL_PATH, DASHBOARD_TITLE, DASHBOARD_ICON,
        build_dashboard_config,
    )

    if _ha_client is None:
        return web.json_response(
            {"error": "HA client not available"}, status=502,
        )

    try:
        existing = await _ha_client.lovelace_dashboards()
    except Exception as exc:  # noqa: BLE001
        return web.json_response(
            {"error": f"Could not list dashboards: {exc}"}, status=502,
        )

    already_exists = any(
        d.get("url_path") == DASHBOARD_URL_PATH for d in existing
    )

    if already_exists and not confirm_overwrite:
        return web.json_response({"existing": True}, status=409)

    # Create or overwrite
    created_new = False
    try:
        if not already_exists:
            await _ha_client.lovelace_create_dashboard(
                url_path=DASHBOARD_URL_PATH,
                title=DASHBOARD_TITLE,
                icon=DASHBOARD_ICON,
            )
            created_new = True
        await _ha_client.lovelace_save_config(
            url_path=DASHBOARD_URL_PATH,
            config=build_dashboard_config(),
        )
    except Exception as exc:  # noqa: BLE001
        # Rollback: if we created a new dashboard but save_config failed,
        # try to delete it
        if created_new:
            try:
                await _ha_client._ws_request(
                    {"type": "lovelace/dashboards/delete",
                     "dashboard_id": DASHBOARD_URL_PATH}
                )
            except Exception:  # noqa: BLE001
                pass
        return web.json_response(
            {"error": f"Dashboard import failed: {exc}",
             "hint": "You can manually copy the YAML from examples/lovelace_dashboard.yaml"},
            status=502,
        )

    return web.json_response(
        {"created": True,
         "link": f"lovelace-{DASHBOARD_URL_PATH}"},
        status=201,
    )


# ---------------------------------------------------------------------------
# Patched index handler with onboarding redirect
# ---------------------------------------------------------------------------

async def index_with_onboarding(request: web.Request) -> web.Response:
    """Render picker UI, but redirect to wizard if onboarding needed."""
    if not _OVERRIDES_PATH.exists():
        raise web.HTTPFound("onboarding")
    try:
        raw = json.loads(_OVERRIDES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise web.HTTPFound("onboarding")
    if not raw.get("sleep_stage_source"):
        raise web.HTTPFound("onboarding")
    return web.Response(text=_INDEX_HTML, content_type="text/html")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def make_app(
    *,
    telemetry_reporter: Any = None,
    ha_client: Any = None,
) -> web.Application:
    """Wire up the routes; exported separately so tests can drive a TestClient."""
    global _telemetry_reporter, _ha_client
    _telemetry_reporter = telemetry_reporter
    _ha_client = ha_client

    app = web.Application(middlewares=[ingress_ip_guard])
    app.router.add_get("/", index_with_onboarding)
    app.router.add_get("/onboarding", onboarding)
    app.router.add_get("/api/entities", api_entities)
    app.router.add_post("/api/options", api_save)
    app.router.add_get("/api/onboarding/candidates", api_onboarding_candidates)
    app.router.add_post("/api/onboarding/save", api_onboarding_save)
    app.router.add_get("/api/telemetry/status", api_telemetry_status)
    app.router.add_post("/api/telemetry/toggle", api_telemetry_toggle)
    app.router.add_get("/api/upgrade/status", api_upgrade_status)
    app.router.add_get("/api/v3/health", api_v3_health)
    app.router.add_post("/api/dashboard/import", api_dashboard_import)
    return app


def main() -> None:
    port = int(os.environ.get("WEB_UI_PORT", "8099"))
    app = make_app()
    logger.info("Starting Sleep Classifier Web UI on :%d", port)
    web.run_app(app, host="0.0.0.0", port=port, print=None)


if __name__ == "__main__":
    main()
