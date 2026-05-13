# Changelog

All notable changes to the Sleep Classifier add-on are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The "headline" rows in `README.md` are the user-facing summary; this
file is the engineering log — what landed, in what order, and why.

## [Unreleased]

Tracked items live in `docs/BACKLOG.md`.

## [1.7.1] — 2026-05-13

The **真·落地** release — the minimum set of fixes that separate
"works in our test harness" from "survives a real user's first
week".  All three of these scenarios were confirmed present in
v1.7.0 by walking the code paths against a real HA setup:

### Fixed

- **Off-state AC was a no-op.**  Firing `climate.set_temperature=19`
  against a climate entity whose state is `"off"` returns HTTP 200
  but the AC stays off.  User wakes up in a 26 °C bedroom and
  concludes the add-on is broken.  Fix: a new `LiveStateCache`
  tracks each bound entity's state; when the controller plans a
  setpoint against an off-state climate it now first injects a
  `set_hvac_mode` (`cool` / `heat` / `auto` picked by the sign of
  target-minus-ambient).  Same logic for `humidifier` → `turn_on`
  before `set_humidity`.
- **Unavailable entities got hammered silently.**  If a bulb
  dropped off the Zigbee mesh its state became `"unavailable"`;
  HA still accepted `light.turn_on` with a 200 but nothing changed.
  Fix: `LiveStateCache.is_available()` gates every dispatch via
  the new `_liveness_guard()` method.  Unreachable entities are
  skipped and the count surfaces on
  `sensor.sleep_classifier_last_action` under
  `skipped_unavailable`.
- **Manual user override got fought.**  At 03:30 the user got up
  for the bathroom and turned the light on; 30 s later the
  controller's next tick decided "stage=DEEP, brightness=0%" and
  forced the light back off in the user's face.  Fix: the cache
  classifies each state_changed event as self-echo (within 5 s of
  our last dispatch) vs external; external changes open a
  `user_override_grace_seconds` window (default 10 min) during
  which the controller holds off on that entity.  Count surfaces
  as `skipped_user_override`.

### Added

- **`src/live_state_cache.py`** — per-entity live state tracker
  shared between the orchestrator (which pushes state_changed
  events) and the controller (which reads availability / on-off /
  override status before planning).  Seeded from the HA registry
  snapshot at boot so the very first plan tick has accurate data.
- `home_assistant.live_state.user_override_grace_seconds` config
  (default 600 s) so power users can tune the manual-override
  grace window.
- `SmartEnvironmentController._climate_mode_for_target()` picks the
  HVAC mode when waking an off-state climate entity.
- 22 new tests: 15 in `test_live_state_cache.py` covering
  availability / off-detection / user-override classification /
  self-echo / grace expiry / stats counters; 7 in
  `test_smart_environment_controller.py::TestOffStateAutoTurnOn /
  TestUnavailableSkip / TestUserOverrideRespect` locking the
  controller integration.

### Changed

- `SmartEnvironmentController.__init__` takes an optional
  `live_state: LiveStateCache` parameter.  When omitted the
  controller constructs an empty cache (pre-v1.7.1 behaviour);
  the orchestrator wires a fully-seeded instance for production.
- `publish_last_action` gains a `live_state_stats` parameter,
  exposing the new counts on the existing diagnostic sensor under
  three new attribute keys.  Empty sub-dicts are omitted so
  healthy installs don't clutter the panel.

### Medical / safety impact: none

v1.7.1 touches only the device-control path.  Apnea (v1.7.0) and
preference learning (v1.3.0 onwards) are unchanged.

## [1.7.0] — 2026-05-13

Apnea / hypopnea trend monitoring lands in the main flow.  The
detection algorithm itself was shipped as a pure-function PoC in
v1.6.0 (`src/apnea_detector.py`); v1.7.0 is the **consent + wiring
+ publisher** work that was deliberately held back until it could
be delivered as one coherent story.

### Added

- **`src/apnea_wiring.py`** — orchestrator-facing glue layer that:
  * subscribes to a breathing-rate entity (e.g. R60ABD1 radar) and
    an optional chest-wall-amplitude entity,
  * tracks consent via an `input_boolean` toggle,
  * persists a `UserBaseline` across restarts
    (`/data/apnea_baseline.json`),
  * brackets sample buffering with `begin_session()` /
    `end_session()`,
  * publishes a coarse `ApneaTrend` bucket per completed session.
- **`sensor.sleep_classifier_apnea_index`** — new 15th HA entity.
  States: `pending_consent` / `calibrating` / `green` / `amber` /
  `red`.  Carries a permanent `disclaimer` attribute reminding the
  user that this is a trend indicator, not a medical diagnosis.
- **`home_assistant.apnea.*` config block** exposing
  `breathing_rate_source`, `chest_amplitude_source`, `consent_entity`,
  `calibration_nights`.  All fields opt-in; leaving
  `breathing_rate_source` empty disables the feature entirely.
- **37 new tests**:
  * `tests/test_apnea_wiring.py` × 17 covering consent gating,
    baseline persistence, session lifecycle, end-of-session trend
    projection, status-dict medical-safety invariant.
  * `tests/test_sleep_state_publisher.py` × 3 locking in the
    publisher's safety contract (disclaimer always present, clinical
    numbers in `status=` dropped before reaching HA).

### Changed

- `SleepStatePublisher.publish_initial_placeholders()` now seeds 15
  entities (was 14); the apnea sensor defaults to `pending_consent`.
- `_maybe_advance_session_lifecycle` hooks `apnea.begin_session()` on
  onset and `apnea.end_session()` on wake-up, so breathing samples
  are grouped with the right sleep session.

### Medical-safety contract

This release explicitly promises that the sensor surface:

1. Never publishes a numeric AHI or events/hour value.
2. Never leaves `pending_consent` without the user having toggled
   the consent `input_boolean` on.
3. Never leaves `calibrating` until at least
   `apnea_calibration_nights` (default 7) of baseline data has been
   accumulated.
4. Wipes persisted baseline from disk on consent revocation.
5. Carries a `disclaimer` attribute visible on every Lovelace view
   of the entity.

These are locked in by `tests/test_apnea_wiring.py::TestStatus::
test_status_never_exposes_events` and
`tests/test_sleep_state_publisher.py::TestPublishApneaIndex::
test_status_filters_to_safe_keys_only`.  Changing the contract
requires breaking a test on purpose.

## [1.6.4] — 2026-05-13

Continues the "落地" thread from v1.6.3 — two more real-world failure
modes where a technically-correct control loop produces practically
wrong behaviour.

### Fixed

- **Stale environment readings haunted the deadband.** Previously,
  when a temperature / humidity / illuminance sensor dropped off the
  HA mesh, `_route_state_change` silently dropped the update but
  `self.last_env` retained the last known value *forever*.  Hours
  later the controller's deadband would still compare to that old
  value: either refusing to act (thinks we're at setpoint) or acting
  wrongly (fighting phantom drift).  Now each env field carries its
  own last-update timestamp in `_env_ts`, and the inference loop
  reads a freshness-masked copy via `_safe_last_env()`.  Fields older
  than `env_freshness_window_seconds` (default 900 s = 15 min) come
  through as `None`, which the existing deadband already treats as
  "unknown, fall back to stage default" — strictly safer than "stale
  reading, act as if current".
- **Hammering saturated devices wasted network + wore the HA state
  write path.** If an AC was already at max cooling but couldn't
  fight a 35 °C outdoor temperature, the controller would keep firing
  the same `set_temperature=19` at every deadband trigger.  New
  `SmartEnvironmentController._is_entity_saturated()` tracks, for
  each controllable entity, a rolling window of (target, observed_env,
  ts) tuples.  After `_FUTILE_STREAK_THRESHOLD` (3) consecutive same-
  setpoint attempts at least `_FUTILE_MIN_SETTLE_SECONDS` (15 min)
  apart, if the observed environment hasn't moved by at least the
  per-field minimum (0.3 °C / 1.5 %RH / 2 %bright) the entity is
  marked saturated.  Further same-setpoint pushes are suppressed
  until the next stage transition clears the flag.

### Added

- `home_assistant.env_freshness_window_seconds` config (default 900 s)
  so installations with slow-updating sensors can widen the window.
- `SmartEnvironmentController.futility_stats()` exposes saturation
  bookkeeping; the orchestrator can surface it on the diagnostic
  `last_action` sensor in a future pass.
- 9 new tests (`TestEnvFreshness` × 4, `TestFutileRetrySuppression` × 5)
  covering: fresh reading passes through, stale reading masked to
  None, never-observed field stays None but not flagged stale, mixed
  freshness fields, no saturation before streak, saturation after
  futile streak, env movement resets the streak, stage change clears
  saturation, settle-time required before suppression triggers.

### Changed

- `_track_per_stage_env` still snapshots the RAW `last_env`, not the
  freshness-masked copy, so the learner has faithful evidence of what
  the sensors *said* they saw during a stage (even if stale).
  Freshness-masking is a control-path concern, not a learning-path
  concern.

## [1.6.3] — 2026-05-13

The "落地" pass — three real-world failure modes the v1.6.2 feature
list glossed over, each of which would silently make the add-on
misbehave on its first deployed night.

### Fixed

- **Session never reset.** `session_id`, `session_started_at`,
  `stage_counts`, `stage_sequence`, and `env_by_stage` were initialised
  once in `__init__` and never cleared, so an add-on running for a
  month produced one 30-day-long "session".  Every night's quality
  score was the cumulative average since boot, SE/WASO/SOL were
  meaningless, and the learner got the same mishmash of evidence
  every time.  Replaced with a proper onset/wake state machine:
  sessions start after `session_onset_dwell_seconds` of continuous
  non-AWAKE (default 300 s = 5 min, the AASM PSG criterion) and end
  after `session_wake_dwell_seconds` of continuous AWAKE (default
  600 s = 10 min, long enough that a brief stir doesn't close it).
  On session end `_persist_session(partial=False)` runs followed by
  `_reset_session_state()`, rotating to a fresh session id + zeroing
  all per-session accumulators.
- **Dead stage source locked the bedroom.** If the bound wearable /
  radar stopped reporting (dead battery, user took the watch off),
  `ExternalStageSubscriber.current()` kept returning the last stage
  forever.  The controller would then hold whatever setpoint was
  last inferred — e.g. lock the AC at DEEP's 18 °C for the whole
  day.  The inference loop now checks `engine.is_stale()` each
  tick: while stale it publishes the diagnostic stage sensor (so
  Lovelace shows "not reporting") but skips stage counting, skips
  wind-down substitution, and skips `controller.apply()` entirely.
  Recovery transitions are logged once per edge (one WARN on going
  stale, one INFO on coming back live).

### Added

- `home_assistant.session_lifecycle` config block with
  `onset_dwell_seconds` and `wake_dwell_seconds` so power users can
  tighten or loosen the thresholds without editing code.
- 6 new runtime tests in `tests/test_smart_sleep_service_runtime.py`:
  `TestSessionLifecycle` (4 cases covering reset, onset threshold,
  brief-stir tolerance, sustained-AWAKE wake-up) and
  `TestStaleStageSourceGuard` (2 cases covering `apply()` skip and
  log deduplication on recovery).

### Changed

- Inference log line now appends `[pre-onset]` while outside a
  session, so users can see in the log why their first minutes of
  being in bed aren't being recorded.

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

[Unreleased]: https://github.com/LiangyuLu-lly/HA-sleep/compare/v1.7.1...HEAD
[1.7.1]: https://github.com/LiangyuLu-lly/HA-sleep/compare/v1.7.0...v1.7.1
[1.7.0]: https://github.com/LiangyuLu-lly/HA-sleep/compare/v1.6.4...v1.7.0
[1.6.4]: https://github.com/LiangyuLu-lly/HA-sleep/compare/v1.6.3...v1.6.4
[1.6.3]: https://github.com/LiangyuLu-lly/HA-sleep/compare/v1.6.2...v1.6.3
[1.6.2]: https://github.com/LiangyuLu-lly/HA-sleep/compare/v1.6.0...v1.6.2
[1.6.0]: https://github.com/LiangyuLu-lly/HA-sleep/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/LiangyuLu-lly/HA-sleep/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/LiangyuLu-lly/HA-sleep/compare/v1.3.1...v1.4.0
[1.3.1]: https://github.com/LiangyuLu-lly/HA-sleep/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/LiangyuLu-lly/HA-sleep/compare/v1.2.3...v1.3.0
