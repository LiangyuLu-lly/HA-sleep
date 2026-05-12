# HACS migration — design + roadmap (v2.0)

**Status:** designed, not implemented.  Tracked here rather than in
``BACKLOG.md`` because the design has enough sub-parts that a one-liner
is misleading.

## Why this isn't done in v1.6.0

[HACS](https://hacs.xyz/) is the community store for Home Assistant.
It distributes:

* **Integrations** (`custom_components/<name>/` — Python code that
  runs *inside* HA's event loop)
* **Lovelace plugins** (JS/HTML cards)
* **Themes**, **AppDaemon**, **NetDaemon**

It does **not** distribute Home Assistant *Add-ons* (the Docker-based
Supervisor extensions, which is what `sleep_classifier/` currently
ships as).

The Sleep Classifier's core logic — long-running inference loop,
exponential-decay learner, smart-wake planner, ~20 MB image with
numpy + aiohttp — is structured as an **add-on** because:

1. The inference loop runs forever and needs its own process.
2. The preference history (sessions.json) is persistent state owned
   by the add-on, not by HA.
3. The Pi 4B perf budget is met because we're a separate container.

Re-shaping this into a HACS-installable integration means moving:

* The inference loop → an `async_track_time_interval` task on HA's
  event loop.  Performance impact is real — every tick now contends
  with HA's own task scheduler.
* The session history → HA's `.storage/sleep_classifier_sessions`
  JSON, served by `homeassistant.helpers.storage.Store`.
* The 14 sensor entities → `SensorEntity` subclasses registered via
  the integration's `async_setup_entry`, with state changes pushed
  via `async_write_ha_state` rather than the current REST call.
* The 4 service-call APIs (`call_service`, `update_state`, etc.) →
  direct HA service-registry calls, dropping `ha_api_client.py`
  entirely.

That's a multi-week refactor and a **major version bump** (v2.0)
because it breaks the on-disk session-history location.

## v2.0 implementation plan

### Stage 1 — Frontend split (1 fresh session)

Extract a HACS *plugin* (Lovelace card) without touching the add-on.

**Deliverable:** a separate repo `HA-sleep-card` with:

* `dist/sleep-classifier-card.js` — a Lit-based card that renders
  the four-view layout from `examples/lovelace_dashboard.yaml`.
* `hacs.json` declaring `category=plugin`.
* `info.md` for HACS-rendered "About" page.

**Migration:** users on v1.x already have the YAML dashboard.  The
plugin offers a one-click upgrade path that doesn't change any of
their entities.

### Stage 2 — Integration scaffold (1 fresh session)

Add `custom_components/sleep_classifier/` *to this repo* but
non-functional, behind a `dummy: true` flag.

```text
custom_components/sleep_classifier/
├── __init__.py             # async_setup_entry entry point
├── manifest.json           # name, version, dependencies
├── const.py                # entity-id constants (mirror of
│                           #   src/sleep_state_publisher.py)
├── sensor.py               # 14 SensorEntity subclasses
├── coordinator.py          # DataUpdateCoordinator subscribing to
│                           #   the add-on's sensors via state_get
├── config_flow.py          # Add-on detection + opt-in
└── translations/en.json    # Friendly names per entity
```

In this stage the integration is a **read-only mirror**: it polls
the add-on's existing sensor entities and republishes them under
new entity_ids.  This proves the HACS pipeline end-to-end without
risking the add-on's data path.

### Stage 3 — Logic migration (2 fresh sessions)

Move the algorithm modules into the integration:

* `src/preference_learner.py`            → `custom_components/sleep_classifier/learner.py`
* `src/smart_environment_controller.py`  → `custom_components/sleep_classifier/controller.py`
* `src/sleep_debt.py`                    → `custom_components/sleep_classifier/sleep_debt.py`
* `src/external_stage_subscriber.py`     → `custom_components/sleep_classifier/stage_source.py`
* `src/apnea_detector.py`                → `custom_components/sleep_classifier/apnea.py`

The orchestrator (`scripts/run_ha_smart_service.py`) collapses into
the coordinator's `_async_update_data` callback.

**Backwards compat:** preserve `sessions.json` schema bit-for-bit
(`SleepSession.from_dict` already tolerates missing fields).
Migration script `migrate_v1_to_v2.py` reads the add-on's
`/data/user_preferences.json` and writes
`.storage/sleep_classifier_sessions` once on first start.

### Stage 4 — Add-on retirement (1 fresh session)

Once Stage 3 is in HACS and stable for 2+ weeks of feedback:

* Tag final add-on release as `v1.99.0` (point release for any
  bugfix, no new features).
* Add a deprecation banner to the add-on's DOCS.md pointing at
  HACS.
* Keep the add-on repo alive for at least 6 months for users on
  HA OS who can't / don't want HACS.

## Decision criteria for starting v2.0

Do this work when **at least two** of the following are true:

1. Three or more users explicitly request HACS distribution.
2. The HA Supervisor adds breaking changes that force the add-on
   to rev anyway (a "while we're here" trigger).
3. We add a feature that requires direct HA service-registry
   access (e.g. registering a custom service the user can call from
   automations) and the REST round-trip becomes a measurable bottleneck.

Until then, the add-on path is the right choice — simpler, lower
support burden, no perf concerns.

## What v1.6.0 *does* ship toward this future

Three things in v1.6.0 are deliberately migration-friendly:

* **`LearningPanelPublisher`** is a small, pure class with no add-on
  globals — it can be lifted into the integration unchanged.
* **`apnea_detector.py`** is also pure-functional; same.
* **`tests/test_ha_api_client_ws.py`** uses an in-process HA shape;
  the same fixture pattern will test the integration's coordinator
  against an in-memory HA in v2.

Everything else still has add-on-shaped seams (file paths, env-var
config, etc.) that v2 will untangle.
