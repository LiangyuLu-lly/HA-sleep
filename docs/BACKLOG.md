# Backlog — Pending Work Items

Items here are explicitly *not* finished but have been thought through.
Pick one in a focused future session.

## R60ABD1 ESPHome native API direct path

**Goal:** drop the WS-relayed latency for the SleepRadar R60ABD1 mmWave
radar by talking to the ESPHome firmware over its native binary protocol.

**Status:** evaluated, not implemented.  Carries enough tradeoffs that
it should not be a drive-by change.

### Why it might help

* ESPHome's HA integration funnels every sensor change through the
  `state_changed` WebSocket.  At our 30-s inference cadence this is
  fine, but a future "ultra-low-latency" mode (e.g. < 1 s closed loop
  for a sleep apnea alarm) would benefit from the ~30 ms native path
  instead of the 200-500 ms HA round trip.
* Native API gives us the *raw* radar telemetry frames, not the
  rounded sensor values HA exposes.  We could re-derive richer signals
  (e.g. movement variance, tidal-volume estimate) ourselves.

### Why we have not done it yet

1. **Architectural cost.** The current model has *one* source of truth
   (HA's state machine).  Adding a parallel ingestion path means we
   own the failure mode "device value diverges between HA UI and the
   add-on".  That is a real support burden.
2. **Dependency cost.** `aioesphomeapi` is ~1 MB of pure-Python plus
   protobuf.  Cheap on a Pi 4B but doubles our runtime image surface.
3. **Configuration cost.** Users today already paste an entity_id into
   one box.  Native API needs the ESPHome host/port + password (or
   noise key), which we'd have to expose as new add-on Configuration
   fields and document carefully.
4. **Diminishing returns.** With our 30-s inference window, sensor
   latency under 200 ms is invisible.  The architecture upside only
   matters if we add a true real-time loop later.

### Implementation sketch (when we do it)

* New module `src/esphome_ingest.py` wrapping `aioesphomeapi.APIClient`.
* New Configuration block:
  ```yaml
  esphome_devices:
    - host: r60abd1.local
      port: 6053
      noise_key: "base64-encoded-secret"
  ```
* `SmartSleepService.__init__` would optionally build an
  `ESPHomeIngestor`; if present, `_route_state_change` skips entities
  served natively because they're already pushed into the inference
  buffers.
* Health check: cross-validate native vs HA values once per minute,
  log a divergence above 5 % (catches firmware mis-mapping).
* New unit tests with `aioesphomeapi`'s test client fixtures.

### Decision criteria

Do this work iff at least one of:

* A user reports a real-time use case (apnea alarm, snore detection)
  where 30-s cadence is insufficient.
* We add a sub-second physiological feature that demands < 50 ms
  latency end-to-end.
* The HA WebSocket starts dropping events under load (we have not
  observed this; the existing reconnect path handles transient drops).

Until then: stay on the HA WebSocket path.  It's simpler, cheaper, and
hits every measurable product-quality metric.

---

## Per-stage env learning (shipped in v1.5.0) ✅

Originally specified in this backlog as "v1.4" work and shipped in
v1.5.0 — the controller now prefers learned AWAKE/LIGHT/DEEP/REM
deltas (computed against the LIGHT-stage baseline) over the clinical
defaults, with a per-field merge and a Kish-effective-sample-size
guard.  See README "Per-stage learned deltas (v1.5.0)".

The implementation deviates from the original sketch in two
deliberate ways:

* The learner returns *deltas relative to LIGHT*, not absolute env
  per stage.  This dropped the dimensionality of the learning
  problem by 4× — a stage now needs ~4 nights of data to cross the
  ESS threshold instead of ~16, because the per-user midpoint is
  already nailed down by ``recommend_knn``.
* The merge with clinical defaults is **per field** rather than
  per stage.  A user with 30 nights of temperature data but only 2
  of brightness data therefore gets a personalised temperature
  delta while keeping the safe brightness clinical default,
  instead of waiting until the whole stage is learned.

### Future work atop v1.5.0

* **Per-stage exploration.**  Currently exploration noise is added
  to the absolute setpoint, not the per-stage delta.  A future pass
  could perturb the *delta* itself, e.g. trial a 0.5 °C narrower
  DEEP-vs-LIGHT gap, to discover whether a flatter night yields
  better quality scores than the textbook curve.
* **Cross-night auto-correlation in the delta.**  If a user's DEEP
  delta correlates with their AWAKE delta (e.g. both shrink in
  summer), we could regularise the learner via a small graphical
  model.  Useful only after we have ~50+ users and can study the
  empirical correlation matrix.

## v1.5.0 quality-audit hotspots

Findings from the v1.5.0 release audit (`pytest --cov=src`, 89 % overall):

* **`src/ha_api_client.py` at 57 % coverage.**  The lowest-covered
  module — but it's mostly the WebSocket reconnect / retry / token
  refresh loops, none of which can be exercised meaningfully against
  a unit-test mock.  The fix is **integration tests** with an
  in-process aiohttp server that speaks just enough of the HA
  WebSocket protocol to cover the `auth_required → auth_ok →
  subscribe_events → state_changed` happy path plus a
  drop-and-reconnect.  Estimated 1 fresh session.
* **`scripts/run_ha_smart_service.py` at 1065 lines.**  Big but
  coherent.  The cleanest split is to extract the four
  `publish_*` / `_publish_*` helpers (debt+bedtime, learning panel,
  per-stage deltas, soundscape) into a `LearningPanelPublisher`
  class so the orchestrator drops to ~700 lines and the panel
  becomes independently testable.  Not blocking; do it the next
  time a panel feature lands.
* **`src/_time_utils.py` at 75 %.**  Only 8 statements; the
  uncovered branches are the `try: ZoneInfo / except` fallback for
  a missing tzdata package — only triggered on a misconfigured
  system, so the gap is acceptable.

Coverage report kept in CI thinking; not auto-published yet because
the `pytest-cov` step adds ~0.7 s to the suite.  Add it once the
suite breaks 1 s budget.

## Sleep apnea detector — algorithm + wiring shipped (v1.7.0) ✅

**Status:** FULLY SHIPPED as of v1.7.0.

v1.6.0 shipped the **pure algorithm** in ``src/apnea_detector.py``
with dedicated tests.  v1.7.0 added the rest:

* ``src/apnea_wiring.py`` — orchestrator glue (consent state
  machine, baseline persistence, session bracket, live-sample
  routing).  17 dedicated tests.
* ``sensor.sleep_classifier_apnea_index`` (the 15th HA entity)
  with enum states ``pending_consent`` / ``calibrating`` /
  ``green`` / ``amber`` / ``red`` and a permanent ``disclaimer``
  attribute reminding users this is a trend indicator, not a
  diagnosis.
* 3 publisher tests lock in the medical-safety contract: clinical
  numbers in the status dict are filtered out before reaching HA,
  and the disclaimer attribute is always present.

The implementation deliberately deviates from the original
"report a numeric AHI" sketch: we publish only the coarse
red/amber/green bucket so users can't misread the sensor as a
diagnosis.  The whole numeric pipeline exists inside the detector
but is firewalled behind the publisher.

### Future refinements (not blockers)

* **Spousal signal separation** — currently the radar might pick
  up either sleeper in a shared bed.  Multi-zone radar modes
  (R60ABD1 supports 4 zones) could route two parallel
  ``ApneaWiring`` instances.
* **Validation cohort** — without polysomnography ground truth
  we can't tune the ``amber`` / ``red`` thresholds.  Partnering
  with a sleep clinician to compare the add-on output against
  annotated PSG recordings for a week is the path to tightening
  the bands.
  real users we can't tune the thresholds.  The right scope is a
  PoC that surfaces a *trend* (red/amber/green) rather than a
  numeric AHI, until we have a study partner.

### Implementation sketch (when we do it)

* New `src/apnea_detector.py` with a `BreathingWindow` dataclass and
  a `compute_ahi_proxy(events, recorded_seconds) -> float`.
* New HA sensor `sensor.sleep_classifier_apnea_index` (state =
  red/amber/green, attributes = events_per_hour, calibration_days,
  confidence).
* Orchestrator subscribes to the breathing source the same way it
  subscribes to the stage source, with a similar
  `apnea_breathing_source` config slot.
* Onboarding: first publish only `pending_consent` until the user
  toggles a new HA `input_boolean.sleep_classifier_apnea_consent`.

(The above implementation sketch was realised in v1.7.0 with one
deliberate deviation: the events-per-hour number is NOT surfaced
in sensor attributes.  Only the enum bucket reaches HA.)

## Other ideas worth a fresh session

* **Spousal disambiguation.**  Multi-person beds + a single radar can
  produce mixed signals.  Investigate whether the multi-zone radar
  modes (R60ABD1 supports 4 zones) can be auto-routed to two parallel
  subscriber instances.
* **Open the dataset.**  Release a redacted subset of preference
  histories (with explicit consent) so the community can compare
  recovery-plan algorithms.
* **HACS distribution.**  Detailed 4-stage migration plan in
  `docs/HACS_MIGRATION.md` covering the Lovelace plugin extraction,
  integration scaffold, logic migration, and add-on retirement.
  Decision criteria for kicking off v2.0 are documented there.
