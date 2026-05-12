# Sleep Classifier — Home Assistant Add-on

**Learns your ideal sleep environment and adapts the bedroom across
the night.**  The add-on figures out — from your own history — which
combination of temperature, humidity, light and fan speed correlates
with the nights you sleep best, then tunes those values continuously:
across each sleep stage, between weekday and weekend, and as the
seasons shift.  Any HA-resident sleep-stage sensor (Apple Watch, Mi
Band, Fitbit, sleep_as_android, mmWave radars, …) is enough input —
the add-on doesn't need a dedicated wearable or its own sleep-staging
model.

```text
                  ┌── analyse ────┐
sleep-stage  ──▶  │ which env did │  ──▶  warm + dim light  pre-sleep
quality          │ you sleep    │  ──▶  cool + dark        DEEP / REM
score            │ best in?     │  ──▶  gentle ramp        wake window
                  └───────────────┘
                          │
                          ▼
              learns the *midpoint*
              you sleep best at;
              clinical deltas shape
              the curve across stages
```

## What it does for you, in one paragraph

Every night, the add-on watches your sleep-stage sensor and records
what your bedroom was actually like — temperature, humidity, light,
fan speed — together with a 0-100 quality score that rewards DEEP /
REM time and punishes fragmented wakefulness.  Over a week or two of
this data, the `PreferenceLearner` figures out the env combination
that consistently shows up under your *best* nights, weighted by how
recent each session is.  When the next bedtime approaches it hands
that "personal baseline" to the controller, which then walks the room
through a stage-aware curve: warmer + brighter while you wind down,
cool + dark while you're in DEEP, a gentle ramp back to daylight
inside your configured wake window.  Everything that's learned is
exposed as plain HA sensors with attribute panels so you can see
exactly *why* tonight's setpoints look the way they do.

## Phases of regulation

The product personalises along **four time scales**, each implemented
by a different piece of the codebase:

1. **Within the night — per sleep stage.**
   `src/smart_environment_controller.py` keeps the clinical
   stage→setpoint table (`_STAGE_DELTAS`): AWAKE = baseline + 2 °C,
   +32 % brightness, +5 % fan; DEEP = baseline − 2 °C, dark, slow
   fan; REM ≈ DEEP.  The deltas are anchored on the user's *learned*
   midpoint rather than the clinical default, so you sleep within
   your own comfort zone but with the stage variation modern sleep
   medicine recommends.
2. **Within the week — workday vs weekend.**
   `PreferenceLearner.recommend_bedtime()` buckets sessions by
   *wake-day* (so a Friday-night-into-Saturday-morning sleep counts
   as weekend) and surfaces both bedtimes as separate HA sensors.
3. **Within the month — recency decay.**
   Every session contributes
   ``weight = quality × 2^(-age_days / half_life)`` to the
   recommendation, with a 14-day half-life by default.  Seasonal
   shifts (cool summer setpoints fading as autumn drops the room
   temp on their own) ripple into the model within ~1 month, but a
   single rough night never nukes a few weeks of stable data.
4. **Within tonight — current-context k-NN.**
   `PreferenceLearner.recommend_knn()` picks the `k` past sessions
   that are most similar to *this evening* on bedtime hour + ambient
   temperature, then weighted-median-averages their env params.  A
   winter recommendation therefore stops blindly averaging in a
   July night.

All four are explainable: the
`sensor.sleep_classifier_recommendation_explain` entity carries the
neighbour list, weights, effective sample size, decay half-life, and
confidence as attributes that Lovelace renders in the More-Info
dialog.

## Per-stage learned deltas (v1.5.0)

Until v1.4 the controller applied **clinical** AWAKE/LIGHT/DEEP/REM
offsets on top of the learner's per-user midpoint:
*"DEEP must be 2 °C cooler than LIGHT"* etc.  These deltas come from
the population literature and are right for most people most of the
time — but **wrong** for the heavy-duvet user who actually sleeps
best at a flat 19 °C across the whole night, or the silk-sheet user
whose REM tolerates +0.5 °C.

v1.5.0 makes those offsets *learned* too:

- The orchestrator snapshots the room env at every stage *entry*
  (e.g. the 19.4 °C reading the moment DEEP started) and saves the
  trace per session in a new `env_by_stage` field.
- `PreferenceLearner.recommend_per_stage_deltas()` computes the
  weighted-median DEEP-minus-LIGHT (and AWAKE/REM) delta across all
  sessions, with the same exponential decay used by k-NN so old
  preferences fade.
- An **effective sample size guard** (Kish's ESS ≥ 4) holds the
  learned delta back until there's enough evidence — so a noisy
  first week doesn't push the bedroom into uncomfortable territory.
- The merge is **per-field**, so a user with 30 nights of
  temperature data but only 2 nights of brightness data still gets
  a personalised temperature delta while keeping the safe brightness
  clinical default.
- A new sensor `sensor.sleep_classifier_per_stage_deltas` flips
  through the states `clinical → learning → personalised` so users
  see the controller "graduate" to a per-user policy as evidence
  accumulates.

Why this matters for sleep: a setpoint that's 2 °C too cold for *you*
during DEEP can wake you with shivering or push you out of DEEP into
LIGHT — undoing all the controller's other work.  Personalised
deltas close that gap.

## Real-world robustness (v1.4.0)

Three things in the real world break the textbook
"stage-change → setpoint → done" loop:

1. **Actuators have latency.** An AC takes ~15 min to drop the room a
   couple of degrees, a humidifier ~5 min.  By the time the user
   actually enters DEEP, the room hasn't caught up yet.
2. **Bedtime varies night to night.**  22:00 on Monday, 02:00 on
   Friday — the controller can't wait for the stage signal to flip
   before starting to cool.
3. **Wearables produce stage noise.**  A 30-second AWAKE blip during
   DEEP (just a stir) shouldn't switch the bedroom lights on.

v1.4.0 addresses all three:

- **Per-actuator anticipation** — each device's target is blended with
  the *next* stage's target proportional to that device's known
  response time.  Climate uses `α = min(0.6, 900s / 1800s) = 0.5`, so
  while the user is in LIGHT the AC is already aimed at the midpoint
  between LIGHT and DEEP setpoints; by the time DEEP arrives the room
  is at temperature.  Lights and fans (zero latency) keep crisp
  stage-boundary transitions.
- **Wind-down pre-cool** — when the user is still AWAKE but within
  the configurable `wind_down_minutes` (default 30) of their
  *learned* bedtime, the controller treats them as already in LIGHT
  for control purposes.  The HA stage sensor still reports the
  truthful AWAKE — only the control path substitutes.
- **Stage debouncing** — the subscriber requires a new candidate
  stage to hold for `min_stage_dwell_seconds` (default 60) before
  promoting it to "stable", filtering wearable blips without
  delaying actual transitions noticeably.

## What's new — for the impatient

| Version | Headline |
|---|---|
| **v1.6.0** | Engineering polish + apnea PoC. `CHANGELOG.md` + `examples/lovelace_dashboard.yaml` for HACS-friendliness; `LearningPanelPublisher` extracted out of the 1215-line orchestrator; HA WebSocket integration tests with an in-process aiohttp fake server (covers reconnect path); 60 s bedtime cache eliminates ~120 K redundant computes/night; pure-functional apnea/hypopnea detector in `src/apnea_detector.py` (algorithm only, consent-gated wiring is v1.7 work); `docs/HACS_MIGRATION.md` for the v2.0 plan. **+45 tests, 419 total, 92 % coverage.** |
| **v1.5.0** | Per-stage env *deltas* are now learned too — not just the midpoint. New `env_by_stage` field per session, decay-weighted weighted-median delta per field, ESS guard against noisy starts, per-field merge with clinical fallback, new `sensor.sleep_classifier_per_stage_deltas` exposing the controller's `clinical → learning → personalised` graduation. |
| **v1.4.0** | Real-world robustness pass: per-actuator anticipation lets the AC lead the user by ~15 min; wind-down pre-cool starts dimming + cooling before the learned bedtime; stage debouncing filters 30-second wearable blips. |
| **v1.3.1** | Per-stage adaptation preserved when learning kicks in: AWAKE / LIGHT / DEEP / REM each apply a clinical delta on top of the learned baseline. Safe-range clamps prevent runaway setpoints. |
| **v1.3.0** | Local CNN-BiLSTM dropped — the add-on now subscribes to any HA sleep-stage sensor.  Image ~20 MB. Preference learner gains recorded_at + exponential decay, weekday/weekend bedtime split, current-context k-NN, and a JSON explainability panel; 4 new HA sensors mirror the reasoning. |

Older release notes live in the git tag history (e.g. `git show v1.2.3`
for the last release that bundled the local CNN-BiLSTM model).

## Installation

The fast path is the HA Add-on Store (~5 min, no SSH, no pip):

1. Run `sleep_classifier/prepare.sh` (or `prepare.bat`) once to
   mirror `src/`, `scripts/`, `training_config/` and the runtime
   requirements file into `sleep_classifier/rootfs/`.
2. Push this repo to GitHub.
3. In HA: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**,
   paste the repo URL.
4. Install the **Sleep Classifier** card that appears, wait for the
   build (~3 min on a Pi 4B).
5. Open the **Configuration** tab and set `sleep_stage_source` to
   the entity id of your stage sensor.  Start.

Detailed walk-through, including how to bind targets through the
embedded Web UI and how to interpret the v1.3 sensors in Lovelace,
is in [`INSTALL.md`](INSTALL.md).  Manual (non-Add-on) deployment
is documented in [`docs/MANUAL_DEPLOYMENT.md`](docs/MANUAL_DEPLOYMENT.md).

## Repository layout

```text
.
├── src/                        # 15 runtime modules (subscriber, learner, controller, …)
├── scripts/
│   └── run_ha_smart_service.py # add-on entrypoint
├── training_config/            # config_loader.py + defaults (no model training any more)
├── tests/                      # ~340 tests covering every src module
├── sleep_classifier/           # HA add-on (Dockerfile, config.yaml, web UI, prepare.{sh,bat})
├── docs/
│   ├── BACKLOG.md
│   └── MANUAL_DEPLOYMENT.md    # systemd / Docker on non-HA-OS hosts
├── INSTALL.md                  # add-on installation walk-through
└── README.md
```

## Configuration

The bare minimum is a single field:

```yaml
sleep_stage_source: sensor.<your_stage_entity>
```

Everything else (target lights / AC / humidifier / fan, dry-run, log
level, age-cohort for sleep-debt, weekend/workday bedtime overrides,
the k-NN tunables, …) has sane defaults; full reference is in
[`sleep_classifier/DOCS.md`](sleep_classifier/DOCS.md).

## Versioning & licence

- **v1.3.0** (current) — external sleep-stage source, decay + k-NN
  preference learner, 4 new explainability sensors.
- **v1.2.x** — bundled CNN-BiLSTM model + natural-sleep suite.
- **v1.0.x – v1.1.x** — training-only research code.

The pre-v1.3 release with the bundled model is preserved under the
GitHub tag `v1.2.3` for anyone who needs to reproduce the academic
results.

Licensed under MIT.
