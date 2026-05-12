# Sleep Classifier — Home Assistant Add-on

**Closed-loop smart-home sleep automation.**  Subscribe to any HA
sleep-stage sensor you already have (Apple Watch, Mi Band, Fitbit,
sleep_as_android, bedside radars, …), and the add-on learns your
preferred bedtime + bedroom environment from history, then writes it
back to your lights / AC / humidifier / fan automatically.

```text
sleep-stage sensor ──▶ Sleep Classifier add-on ──▶ light · climate · fan
                              │
                              ▼
                  learns bedtime + setpoints
                  per weekday / weekend
                  decayed by recency
```

## What's new in v1.3.0

The add-on no longer trains or runs its own CNN-BiLSTM sleep-stage
model.  Instead it subscribes to a stage entity you've **already**
built into HA — usually because a mass-market wearable (Mi Band, Apple
Watch, Garmin, Withings, Eight Sleep, sleep_as_android, …) publishes
one out of the box.

What you gain:

- **20 MB add-on image** (down from ~60 MB).  No PyWavelets / h5py /
  scipy / hdf5; the only runtime deps are `aiohttp` and `numpy`.
- **No model file shipping**.  Skip the whole "where do I put
  ``best_model.h5``" dance.
- **Four new "explainable" sensors** that mirror the
  preference-learner's reasoning to Lovelace:
  `sensor.sleep_classifier_learned_bedtime_workday`,
  `sensor.sleep_classifier_learned_bedtime_weekend`,
  `sensor.sleep_classifier_learned_environment`,
  `sensor.sleep_classifier_recommendation_explain` (with neighbour
  list + confidence + half-life as attributes).
- **Recency-aware learning**: a session's contribution to the
  recommendation decays exponentially with a 14-day half-life by
  default, so seasonal changes (winter → spring) ripple into
  setpoints within ~1 month without one bad night nuking the model.
- **Weekend ≠ workday bedtime**.  Sessions are bucketed by *wake
  day*, so a Friday-night-into-Saturday-morning sleep counts as
  weekend even though it starts on a Friday.
- **k-NN-style environment recommendation** conditioned on tonight's
  hour-of-bedtime and ambient temperature, so a winter recommendation
  doesn't blindly average in a July night.

## How it works (1 page)

1. **Subscribe**.  On startup the add-on uses HA's WebSocket
   `state_changed` event to follow the entity you set in
   `sleep_stage_source`.  Stage strings are normalised case-insensitively
   so "Deep" / "deep" / "DEEP" / "deep_sleep" all map to the same
   internal `SleepStage.DEEP`.
2. **Score the session**.  When the entity transitions from a sleep
   stage back to AWAKE (or the session times out), the add-on counts
   how much time you spent in each stage, computes a 0-100 quality
   score (DEEP/REM rewarded, fragmented AWAKE penalised), and records
   it together with the bedroom environment that was active.
3. **Learn**.  The `PreferenceLearner` keeps a rolling history of
   sessions, weights them by `quality × exp(-age/half_life)`, and
   surfaces three recommendations every inference tick:
   - tonight's bedtime (separate workday/weekend medians);
   - tonight's ideal env setpoints (weighted-median k-NN over the
     top sessions, conditioned on current hour + temperature);
   - a JSON "why" payload listing the neighbour sessions that drove
     the recommendation, exposed as a sensor attribute.
4. **Actuate**.  A bounded controller writes back to your
   `light_targets` / `climate_target` / `humidifier_target` / `fan_target`
   via the HA REST API, with deadbands and inter-action cool-downs so
   it never flaps.

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
