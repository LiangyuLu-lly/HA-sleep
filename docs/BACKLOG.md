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

## Per-stage env learning (v1.4)

**Goal:** replace the hard-coded ``_STAGE_DELTAS`` table in
``src/smart_environment_controller.py`` with a learned per-stage
recommendation, so the directional offsets between AWAKE / LIGHT /
DEEP / REM also become user-specific.

**Status:** specified, not implemented.  Currently the *midpoint* is
learned (`PreferenceLearner.recommend_knn`) but the deltas are baked
in from clinical defaults.

### Per-stage — why it would help

* The clinical deltas are population averages.  A user who reliably
  sleeps best at a flat 19 °C across all stages (e.g. a heavy duvet
  user) is mis-served by the current "DEEP must be 2 °C cooler than
  LIGHT" assumption.
* Once we collect per-stage telemetry we can also surface a richer
  Lovelace card: "your AWAKE delta is +1.5 °C — narrower than the
  +2.0 °C clinical default, suggesting you wind down quickly".

### Per-stage — why we have not done it yet

1. **Storage shape.** Each session today stores one
   ``EnvironmentParams`` snapshot, not a per-stage trace.  We'd need
   to either:
   - bump the JSON schema to ``{stage: env_params}`` per session, or
   - sample env at every stage transition (and reconcile when stages
     flap quickly).
2. **Sparsity.** A normal night has only a few minutes of REM at the
   end; over 60 sessions that's ~3-4 hours of REM samples.  Decay
   weighting on top thins it further.  Need to verify the effective
   sample size is still > 10 before promoting a learned delta.
3. **Safety.** A noisy learner output for the brightness delta could
   keep the bedroom lights at 40 % during DEEP for an unfortunate
   user.  The current safe-range clamp helps but doesn't fully
   substitute for a sanity check against the clinical envelope.

### Per-stage — implementation sketch (when we do it)

1. Extend ``SleepSession`` with ``env_by_stage: Dict[str, EnvironmentParams]``,
   populated by the orchestrator at each stage transition.
2. Add ``PreferenceLearner.recommend_per_stage()`` returning
   ``Dict[SleepStage, EnvironmentParams]``; reuse the existing decay
   + k-NN machinery on a per-stage subset.
3. Refactor ``SmartEnvironmentController.target_for()`` to prefer the
   learned per-stage env when ``len(per_stage_history) >= 10`` and
   fall back to the current baseline + delta otherwise.
4. Add a 5th HA sensor exposing the *learned* deltas for transparency.

## R60ABD1 ESPHome native API direct path

(see top of file)

## Other ideas worth a fresh session

* **Spousal disambiguation.**  Multi-person beds + a single radar can
  produce mixed signals.  Investigate whether the multi-zone radar
  modes (R60ABD1 supports 4 zones) can be auto-routed to two parallel
  subscriber instances.
* **Sleep apnea detector.**  Breathing-rate variability + chest-wall
  motion → apnea-hypopnea index proxy.  Would slot in as a separate
  module beside the stage subscriber.
* **Open the dataset.**  Release a redacted subset of preference
  histories (with explicit consent) so the community can compare
  recovery-plan algorithms.
