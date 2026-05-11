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

## Other ideas worth a fresh session

* **Self-supervised pretraining on consumer-grade physiology.**  The
  current Sleep-EDF training is clean PSG; adding a self-supervised
  stage on noisy Mi-Band / Apple Watch streams should improve out-of-
  distribution robustness.
* **Spousal disambiguation.**  Multi-person beds + a single radar can
  produce mixed signals.  Investigate whether the multi-zone radar
  modes (R60ABD1 supports 4 zones) can be auto-routed to two parallel
  inference engines.
* **Sleep apnea detector.**  Breathing-rate variability + chest-wall
  motion → apnea-hypopnea index proxy.  Out of scope for the current
  CNN-BiLSTM but would be a natural sibling module.
* **Open the dataset.**  Release a redacted subset of preference
  histories (with explicit consent) so the community can compare
  recovery-plan algorithms.
