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

## Sleep apnea detector — algorithm shipped, wiring deferred

**Status:** v1.6.0 shipped the **pure algorithm** in
``src/apnea_detector.py`` with 20 tests + 94 % coverage.  The
remaining work is the *orchestrator wiring* + consent flow, which
is gated on an explicit user opt-in for medical-disclaimer reasons.

### Why slot it in as a separate module

The stage subscriber is a discrete-state machine; apnea detection is
a continuous, sliding-window signal-processing pipeline.  Coupling
them would force the controller to wait on FFTs every 30 s.  A
separate module pushes its results into HA on its own cadence and
the orchestrator only listens.

### Algorithm sketch

1. **Subscribe** to a breathing-rate sensor (e.g. R60ABD1's
   `sensor.r60abd1_breathing_rate`) at 1 Hz and a chest-wall-motion
   sensor at 10 Hz if available.
2. **Sliding window** of length 60 s, hop 10 s.  For each window:
   a. Detect *apneic events* — a contiguous interval of ≥ 10 s
      where breathing rate < 4 bpm OR chest-wall variance is below
      the noise floor.
   b. Detect *hypopneic events* — a ≥ 10 s interval where
      breathing rate is 50 % below the user's baseline AND
      chest-wall amplitude is < 70 % of baseline.
3. **Per-night roll-up** at session checkpoint: count events per
   hour of recorded sleep, that's the AHI proxy.
4. **Confidence** comes from how much of the night the radar had
   signal vs. dropouts (e.g. sleeping on stomach often blocks the
   chest signal).

### Why we have not done it yet

* **Calibration.** The "user's baseline breathing rate" needs ~1
  week of nightly data to settle.  Until then the hypopnea
  detector throws false positives.
* **Medical disclaimer.**  AHI is a clinical metric; surfacing a
  number > 5 (the medical threshold) without a consent dialog and
  a "this is not a diagnosis" banner is irresponsible.  We need a
  one-time onboarding step before this sensor publishes a value.
* **Validation data.**  Without polysomnography ground truth on
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
