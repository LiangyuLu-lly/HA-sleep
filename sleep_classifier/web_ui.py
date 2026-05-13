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
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
from aiohttp import web

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
    """
    for path in (_OVERRIDES_PATH, _OPTIONS_PATH):
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Could not read %s: %s", path, exc)
    return {}


def _normalise(value: Any) -> Any:
    """Strip the ``""`` literal users sometimes paste into Configuration."""
    if isinstance(value, str):
        v = value.strip()
        return "" if v in ('""', "''") else v
    if isinstance(value, list):
        return [v for v in (_normalise(x) for x in value) if v != ""]
    return value


# ---------------------------------------------------------------------------
# HTTP handlers
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
    # config rather than a half-truncated JSON.
    tmp = _OVERRIDES_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(
            json.dumps(cleaned, indent=2, ensure_ascii=False), encoding="utf-8",
        )
        tmp.replace(_OVERRIDES_PATH)
    except OSError as exc:
        return web.json_response(
            {"error": f"Could not persist overrides: {exc}"}, status=500,
        )

    logger.info(
        "Saved %d slot overrides to %s; %d rejected",
        len(cleaned), _OVERRIDES_PATH, len(rejected),
    )
    return web.json_response({
        "saved": cleaned,
        "rejected": rejected,
        "message": (
            "Saved.  Click Restart in the add-on detail page to apply."
        ),
    })


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
</style>
</head>
<body>

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
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def make_app() -> web.Application:
    """Wire up the routes; exported separately so tests can drive a TestClient."""
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/entities", api_entities)
    app.router.add_post("/api/options", api_save)
    return app


def main() -> None:
    port = int(os.environ.get("WEB_UI_PORT", "8099"))
    app = make_app()
    logger.info("Starting Sleep Classifier Web UI on :%d", port)
    web.run_app(app, host="0.0.0.0", port=port, print=None)


if __name__ == "__main__":
    main()
