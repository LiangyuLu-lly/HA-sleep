# Changelog

All notable changes to the Sleep Classifier add-on are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The "headline" rows in `README.md` are the user-facing summary; this
file is the engineering log — what landed, in what order, and why.

## [Unreleased]

Tracked items live in `docs/BACKLOG.md`.

## [1.6.2] — 2026-05-12

### Added

- **Capability gating** — `SmartEnvironmentController` now consults
  `src/device_capabilities.capabilities_of()` for each bound entity at
  construction, and refuses to plan actions (`climate.set_temperature`,
  `light.turn_on(brightness_pct=...)`, `fan.set_percentage`,
  `humidifier.set_humidity`) against entities that don't advertise the
  matching `supported_features` bit.  Closes the single biggest
  "looks-correct-but-doesn't-actually-work" hazard where HA would
  return 200 OK for the service call but the device no-oped because
  the integration didn't implement the feature.
- **Preset-fan fallback** — fans that only expose `preset_modes`
  (Sonoff iFan04 pattern) now receive `fan.set_preset_mode` with a
  quantised `low/medium/high` value instead of being silently
  dropped.
- **On/off-only light degradation** — bulbs without a dimmer no
  longer have `brightness_pct` sent to them (which HA would accept
  but silently drop); the controller degrades to a plain
  `light.turn_on` so the user at least gets on/off behaviour.
- **`capability_stats()`** on the controller + `skipped_by_capability`
  attribute on `sensor.sleep_classifier_last_action`, so users can see
  on their Lovelace dashboard that e.g. their AC was skipped 12 times
  today for `set_temperature` — a strong hint the bound entity is the
  wrong one.

### Fixed

- **Orchestrator crash** — `scripts/run_ha_smart_service.py` was
  calling `.get()` on the `ControlAction` dataclass instances returned
  by `SmartEnvironmentController.apply()`, which raised
  `AttributeError` on every real device action.  Replaced with
  attribute access.  Regression test pins the formatted `last_action`
  string.
- **Silently-dropped config** — `sleep_classifier/run.sh` now pipes
  `wind_down_minutes` and `min_stage_dwell_seconds` from
  `/data/options.json` into `effective_config.json`.  Pre-fix, user
  edits to these v1.4 knobs in the Configuration form had no effect.
- **CI cache key** — `.github/workflows/test.yml` stops referencing
  the deleted `requirements-train.txt` and the zero-use `hypothesis`
  dev dependency.

### Changed

- **Runtime image** — `requirements-runtime.txt` drops `numpy`
  (`grep -R "numpy" src/ scripts/` confirmed zero usages).  Add-on
  image drops ~5 MB to ~15 MB.
- **Dead-code removal** — `src/data_structures.py` shrinks from 150
  to 47 lines, keeping only the `SleepStage` enum.  Eleven unused
  dataclasses removed (HeartRateData, MovementData, ModelWeights, …).
  `training_config/config_loader.py` + `config.json` drop the
  `model` / `mqtt` / `training` / `disaster_monitoring` sections;
  validation rewritten around the four rules that actually affect
  startup (deadband bounds, quality_quantile bounds,
  min_sessions ≥ 1).
- **Documentation alignment** — README / INSTALL / DOCS stop
  claiming numpy is a runtime dep; `repository.yaml` renamed from
  `CNN-BiLSTM Sleep Model Add-ons` to `Sleep Classifier Add-ons`;
  `setup_env.sh` / `.bat` banners updated; `setup.py` /
  `pyproject.toml` name → `sleep-classifier`, version → `1.6.2`.

## [1.6.0] — 2026-05-12

### Added

- **`CHANGELOG.md`** itself, in Keep-a-Changelog format, so HACS and
  GitHub Releases can render version history without scraping the
  README.
- **Lovelace dashboard template** at `examples/lovelace_dashboard.yaml`
  covering all 14 owned sensors (stage / quality / debt / bedtime /
  wake decision / soundscape / 4 learning entities / per-stage deltas /
  last action) — drop into Settings → Dashboards → Raw Editor.
- **HA WebSocket integration tests** in `tests/test_ha_api_client_ws.py`
  exercising `auth_required → auth_ok → subscribe_events → state_changed`
  plus drop-and-reconnect against an in-process aiohttp WS server.
  Lifts `src/ha_api_client.py` coverage from 57 % to ~85 %.
- **Apnea detector PoC** (`src/apnea_detector.py` +
  `sensor.sleep_classifier_apnea_index`) gated behind
  `input_boolean.sleep_classifier_apnea_consent`.  Publishes only a
  `red/amber/green/calibrating/pending_consent` trend, never a numeric
  AHI — the BACKLOG explains the medical-disclaimer rationale.
- **`_time_utils.now_local`** test for the tzdata-missing fallback
  branch, closing the last 2-line coverage gap in that module.

### Changed

- **Orchestrator split**: the four `_publish_*` panel methods on
  `SmartSleepService` (debt+bedtime, learning panel, per-stage deltas,
  soundscape) extracted into `src/learning_panel_publisher.py`.
  `scripts/run_ha_smart_service.py` drops from 1065 → ~700 lines and
  the panel becomes independently testable.
- **`recommend_bedtime` is now cached** for 60 seconds inside the
  orchestrator's `_effective_control_stage` path, eliminating ~120 K
  redundant weighted-median computations per night at scale.

### Fixed

- *Nothing user-visible.*  Refactor only.

## [1.5.0] — 2026-05-12

### Added

- **Per-stage learned env deltas** — `PreferenceLearner.recommend_per_stage_deltas()`
  computes weighted-median DEEP/REM/AWAKE offsets vs. the LIGHT
  baseline, with Kish effective-sample-size guard (≥ 4 sessions
  before promoting a learned value).  Each *field* is independently
  sourced — temperature can be personalised while brightness still
  uses the clinical default.
- **`SleepSession.env_by_stage`** field — stores per-session env
  snapshots taken on stage *entry*.  JSON round-trip is backwards
  compatible; pre-v1.5 sessions load with an empty dict.
- **`sensor.sleep_classifier_per_stage_deltas`** — new HA entity with
  state `clinical → learning → personalised` and flat per-stage
  attributes (`deep_temperature_c_delta`, `deep_ess`,
  `deep_n_sessions`, `ess_threshold`).
- 17 new tests covering schema round-trip, ESS guard, weighted-median
  outlier robustness, per-field independence, controller integration,
  cache amortisation, learner-crash safety, and publisher state
  transitions.

### Changed

- `SmartEnvironmentController._compose` is now an instance method that
  consults a 120 s-cached `_learned_deltas()` and merges per-field
  with the clinical `_STAGE_DELTAS` table.  Used to be `@staticmethod`.
- README adds a "Per-stage learned deltas (v1.5.0)" section explaining
  the heavy-duvet user payoff.

## [1.4.0] — 2026-05-12

### Added

- **Per-actuator latency anticipation** — each device's target is
  blended with the next stage's target proportional to its known
  response time.  Climate (~900 s) at typical 1800 s stage =
  α = 0.5, so the AC starts pre-cooling for DEEP while the user is
  still in LIGHT.  Lights and fans (0 s) keep crisp stage-boundary
  transitions.
- **Wind-down pre-cool** — when AWAKE within `wind_down_minutes`
  (default 30) of the learned bedtime, the controller treats the
  user as already in LIGHT for control purposes.  The HA stage
  sensor still reports the truthful AWAKE.
- **Stage debouncing** — `min_stage_dwell_seconds` (default 60)
  filters 30-second wearable blips (LIGHT → AWAKE → LIGHT) without
  delaying real transitions noticeably.
- 16 new tests for anticipation, wind-down windowing including
  midnight wrap-around, and stage debouncing.

### Changed

- `SmartControlConfig` exposes `wind_down_minutes` and
  `min_stage_dwell_seconds` on the add-on Configuration form.
- `ExternalStageSubscriber` maintains both `_raw_stage` and
  `_stable_stage`; only the stable one is fed to the controller.

## [1.3.1] — 2026-05-12

### Fixed

- Per-stage adaptation preserved when learning kicks in: AWAKE / LIGHT /
  DEEP / REM each apply a clinical delta on top of the learned
  baseline, instead of all stages collapsing onto one value.
- Safe-range clamps prevent runaway setpoints from a noisy learner.

## [1.3.0] — 2026-05-12

### Added

- **External sleep-stage subscriber** (`src/external_stage_subscriber.py`)
  — the add-on now subscribes to any HA sleep-stage entity (Apple
  Watch, Mi Band, Fitbit, sleep_as_android, …) instead of running
  a local CNN-BiLSTM.
- **Preference learner** with `recorded_at` + exponential decay,
  weekday / weekend bedtime split, current-context k-NN, and a JSON
  explainability panel.
- 4 new HA sensors (`learned_bedtime_workday`, `learned_bedtime_weekend`,
  `learned_environment`, `recommendation_explain`) mirror the learner's
  reasoning so users can see *why* the controller picked tonight's
  setpoints.

### Removed

- **Local CNN-BiLSTM model** and the entire training pipeline.  Image
  size drops from ~60 MB to ~20 MB.  Anyone wanting the old behaviour
  can pin to `v1.2.3`.

### Changed

- Slot binding configuration replaces three slots (HR / movement /
  breathing) with a single `sleep_stage_source`.

## Older

For pre-v1.3 history (the CNN-BiLSTM era), see `git log v1.0.0..v1.2.3`.
The `v1.2.3` tag is the last release that bundled the local model.

[Unreleased]: https://github.com/LiangyuLu-lly/HA-sleep/compare/v1.6.2...HEAD
[1.6.2]: https://github.com/LiangyuLu-lly/HA-sleep/compare/v1.6.0...v1.6.2
[1.6.0]: https://github.com/LiangyuLu-lly/HA-sleep/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/LiangyuLu-lly/HA-sleep/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/LiangyuLu-lly/HA-sleep/compare/v1.3.1...v1.4.0
[1.3.1]: https://github.com/LiangyuLu-lly/HA-sleep/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/LiangyuLu-lly/HA-sleep/compare/v1.2.3...v1.3.0
