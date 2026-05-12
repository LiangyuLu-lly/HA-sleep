# Sleep Classifier — Home Assistant Add-on (v1.3.0)

Closed-loop smart-home sleep automation: subscribe to an existing HA
sleep-stage sensor, score each night, learn which environment and
bedtime correlate with your best sleep, then write those setpoints back
to your lights / climate / humidifier / fan.

The add-on is built around three pillars:

1. **External stage source** — instead of running its own CNN-BiLSTM
   model, the add-on follows any HA entity whose state cycles between
   AWAKE / LIGHT / DEEP / REM.  Most mass-market wearables (Apple
   Watch, Mi Band, Fitbit, Withings, Garmin, Eight Sleep,
   sleep_as_android, …) and bedside radars already publish one out
   of the box; the matcher is case-insensitive and bilingual so
   "Deep" / "DEEP" / "deep" / "deep_sleep" / "深睡" all normalise to
   the same internal stage.
2. **Decay + k-NN preference learner** — every completed session is
   recorded with the env params and a 0-100 quality score.  Past
   sessions decay with a 14-day half-life (configurable) and the
   bedtime recommender splits weekday vs weekend buckets by *wake
   day*.  The env recommender does a weighted-median k-NN over the
   top sessions, conditioned on tonight's hour-of-bedtime and ambient
   temperature.
3. **Bounded HA controller** — every actuation goes through deadbands
   (e.g. don't update climate within ±0.5 °C of the current target)
   and a 120-second inter-action cool-down, so the add-on never
   flaps your devices.

## Configuration

Open the **Configuration** tab.  The form is split into four groups.
The bare-minimum field is `sleep_stage_source`; everything else is
optional and either auto-discovered or defaults to a safe value.

### General behaviour

| Option | Default | Description |
|---|---|---|
| `area` | (empty) | Discovery filter; leave empty to scan all rooms. |
| `infer_interval` | `30` | Seconds between control decisions. |
| `session_interval` | `1800` | How often the preference learner checkpoints to disk. |
| `dry_run` | `true` | If true, plan actions but never call HA services. **Keep this on for the first night.** |
| `exploration_rate` | `0.1` | Gaussian noise scale when probing setpoints around the historical optimum. |
| `min_seconds_between_actions` | `120` | Cool-down between consecutive service calls (anti-flapping). |
| `deadband_temperature_c` | `0.5` | Skip a climate update if the current target is within this many °C. |
| `deadband_humidity_pct` | `5` | Skip a humidifier update if within this much %RH. |
| `deadband_brightness_pct` | `10` | Skip a light update if within this much brightness. |
| `log_level` | `info` | One of `debug`, `info`, `warning`, `error`. |

### Slot bindings — pin a specific entity to a role

In v1.3.0 the only required slot is `sleep_stage_source`.  Everything
else can stay empty and fall back to keyword auto-discovery.

| Option | Example value | Notes |
|---|---|---|
| `sleep_stage_source` | `sensor.mi_band_8_pro_sleep_stage` | **Required.** The HA entity whose state we follow. |
| `temperature_source` | `sensor.bedroom_temperature` | Drives the "current T" reading + k-NN conditioning. |
| `humidity_source` | `sensor.bedroom_humidity` | Drives "current RH" + humidity deadband. |
| `illuminance_source` | `sensor.bedroom_illuminance` | Optional — used for the brightness deadband. |
| `light_targets` | `[light.bedroom_main, light.bedroom_bedside]` | List — all listed lights are dimmed together. |
| `climate_target` | `climate.bedroom_ac` | Single AC / heat-pump entity. |
| `humidifier_target` | `humidifier.bedroom_humidifier` | Optional. |
| `fan_target` | `fan.bedroom_fan` | Optional. |
| `switch_targets` | `[switch.bedside_outlet]` | List — generic switches the add-on may toggle. |

> 💡 **Don't know the entity_id?** Open **Developer Tools → States**
> in HA, type a fragment of the device name in the *Entity* search,
> and copy the resulting entity ID straight into the Configuration
> tab — or, easier, click **OPEN WEB UI** on the add-on detail page
> and pick from a live dropdown.

### Auto-discovery tunables

| Option | Default | Description |
|---|---|---|
| `sleep_stage_keywords` | `[sleep_stage, sleep, hypnogram, 睡眠, 睡眠阶段]` | Substrings used to find a stage sensor when `sleep_stage_source` is empty. |
| `controllable_domains` | `[light, climate, fan, humidifier]` | HA domains the add-on is allowed to invoke services on. |
| `explicit_includes` | `[]` | Entity IDs to *always* include, regardless of filters. |
| `explicit_excludes` | `[]` | Entity IDs to *never* touch, regardless of filters. |

### Natural-sleep suite (v1.2.0, still optional)

Each feature below is enabled by filling the relevant fields; leaving
any of them empty disables just that feature.

| Option | Example value | What it does |
|---|---|---|
| `birth_year` | `1995` | Drives the NSF/AAP sleep-hour recommendation + debt calculation. `0` = unknown → defaults to "adult". |
| `chronotype` | `evening` | Informational; reserved for a future scheduler. |
| `wake_window_start` / `wake_window_end` | `"07:00"` / `"07:30"` | Smart-wake fires inside this interval, preferring a LIGHT / post-REM boundary. |
| `wake_light_targets` | `[light.bedroom_main]` | Lights to ramp up during the 30 min leading up to the wake window. |
| `whitenoise_target` | `media_player.bedroom_speaker` | Single speaker that receives stage-appropriate audio. |
| `whitenoise_volume_scale` | `0.8` | Global multiplier on the per-stage default volume. |
| `feedback_entity` | `input_number.sleep_rating` | A 1-5 helper you nudge after waking; feeds into the quality score. |
| `feedback_scale` | `5` | Range of the helper (1..scale). |

### Preference-learning tunables (v1.3.0)

These four all have sane defaults; leave them alone unless you know
why you want different values.

| Option | Default | Description |
|---|---|---|
| `decay_half_life_days` | `14.0` | How quickly past sessions lose weight in the learner. A 14-day half-life means a session 14 days ago counts half as much as today's. |
| `knn_k` | `5` | How many neighbour sessions feed the env recommendation. |
| `knn_hour_sigma` | `1.5` | σ of the Gaussian kernel on bedtime hour (hours). Lower = stricter "same time of night" matching. |
| `knn_temp_sigma` | `1.5` | σ of the Gaussian kernel on ambient temperature (°C). Lower = stricter "same room condition" matching. |

## Entities published

The add-on owns **13 entities**, all prefixed with
`sensor.sleep_classifier_*`.  They cluster into three families.

**Stage + session diagnostics**

```text
sensor.sleep_classifier_stage              # AWAKE / LIGHT / DEEP / REM
sensor.sleep_classifier_confidence         # 0..100 %
sensor.sleep_classifier_quality_score      # latest session quality (0-100)
sensor.sleep_classifier_session_duration   # seconds since session start
sensor.sleep_classifier_last_action        # human-readable last device change
```

**Natural-sleep suite**

```text
sensor.sleep_classifier_debt_hours         # signed hours; + = behind on sleep
sensor.sleep_classifier_recommended_bedtime  # ISO timestamp for tonight
sensor.sleep_classifier_wake_decision      # hold / pre_ramp / open_window / fire_now
sensor.sleep_classifier_soundscape         # pink_noise / rain / off / dawn_chorus
```

**Preference-learning panel (v1.3.0)**

```text
sensor.sleep_classifier_learned_bedtime_workday   # "HH:MM" or "unknown"
sensor.sleep_classifier_learned_bedtime_weekend   # "HH:MM" or "unknown"
sensor.sleep_classifier_learned_environment       # "19.5 °C / 50 % / 5 %"
sensor.sleep_classifier_recommendation_explain    # "ready" / "not_ready"
```

The four learning entities expose the recommendation reasoning as
attributes — open the More-Info dialog on
`sensor.sleep_classifier_recommendation_explain` and Lovelace shows:

- `method`: `"knn+decay"`
- `n_total`: total sessions in history
- `avg_age_days`: how stale the history is
- `decay_half_life_days`: the active decay setting
- `effective_sample_size`: ≈ Σ weights; tells you how much real
  signal the recommendation rests on
- `recommendation`: the env dict picked tonight
- `neighbors`: top-5 neighbour sessions with their weights, quality,
  and start time — the actual "why this T / RH / lux?"

## What happens after Start

1. The add-on resolves the configured `sleep_stage_source` (or runs
   keyword auto-discovery against your HA entity list), then
   subscribes to its `state_changed` events over WebSocket.  Stage
   strings are normalised through a case-insensitive bilingual matcher.
2. Every time the stage transitions to a *non-AWAKE* state and stays
   there for the configured debounce window, a new session is opened
   and the current environment snapshot is captured.
3. On the next AWAKE transition (or after a session timeout), the
   session is closed: stage counts → quality score, the snapshot
   becomes the env params, and the whole `(env, stages, quality)`
   triple is appended to `/data/user_preferences.json`.
4. Once history reaches `min_sessions_for_personalisation` (default
   `3`), the four v1.3.0 sensors above start publishing real
   recommendations.  Before that the `_explain` entity sits at
   `not_ready` with the reason in attributes.
5. Every `infer_interval` seconds the controller compares the
   learner's recommendation against the live ambient reading,
   applies deadbands + cool-downs, and writes back to your targets.

The WebSocket subscriber reconnects with exponential backoff
(1 s → 2 s → … → 5 min cap) so transient network blips don't take
the service down.

## Lovelace dashboard example

```yaml
type: vertical-stack
cards:
  - type: glance
    title: Sleep monitor
    entities:
      - entity: sensor.sleep_classifier_stage
        name: Stage
      - entity: sensor.sleep_classifier_confidence
        name: Conf
      - entity: sensor.sleep_classifier_quality_score
        name: Last score
      - entity: sensor.sleep_classifier_session_duration
        name: Session
  - type: glance
    title: What the learner picked for tonight
    entities:
      - entity: sensor.sleep_classifier_learned_bedtime_workday
        name: Workday bed
      - entity: sensor.sleep_classifier_learned_bedtime_weekend
        name: Weekend bed
      - entity: sensor.sleep_classifier_learned_environment
        name: Best env
      - entity: sensor.sleep_classifier_recommendation_explain
        name: Why?
  - type: history-graph
    title: Sleep stages tonight
    hours_to_show: 12
    entities:
      - sensor.sleep_classifier_stage
      - sensor.sleep_classifier_quality_score
```

You can also drive automations off stage transitions, e.g. mute the
TV the moment the model thinks you've fallen into deep sleep:

```yaml
- alias: "Mute TV during deep sleep"
  trigger:
    - platform: state
      entity_id: sensor.sleep_classifier_stage
      to: "DEEP"
  action:
    - service: media_player.volume_mute
      target:
        entity_id: media_player.bedroom_tv
      data:
        is_volume_muted: true
```

## Persistence

All add-on state lives under `/data`, which is on the supervisor's
persistent volume.  These survive add-on restarts, upgrades, and a
full re-install:

- **`/data/user_preferences.json`** — session history + learned
  recommendations.  Don't delete unless you want to start over.
- **`/data/web_ui_overrides.json`** — slot bindings set via the
  embedded Web UI; takes priority over the Configuration form.
- **`/data/effective_config.json`** — the merged config the service
  actually loaded.  Handy when the Configuration UI seems to say one
  thing and the log says another.

## Troubleshooting

### "No sleep stage source found / Configured stage source not in HA states"

The `sleep_stage_source` slot is empty *and* auto-discovery didn't
match anything to the `sleep_stage_keywords`.  Either:

- Click **OPEN WEB UI** and pick your stage entity from the dropdown;
  the dropdown only lists entities whose recent state was one of the
  recognised stage strings, so finding the right one is fast.
- Or open **Developer Tools → States**, find your wearable's
  entity, copy the ID into `sleep_stage_source`, click Save → Restart.

### Log: `external_stage_subscriber: ignoring state '<…>'`

Your sensor uses a stage label we don't recognise (e.g. `"asleep"`
instead of `"DEEP"`).  Add the literal to `sleep_stage_aliases` in
the Configuration form, or open an issue with the exact string.

### Recommended bedtime shows "unknown"

The relevant bucket (workday or weekend) doesn't have enough
sessions yet.  `recommend_bedtime` needs at least 3 sessions in a
bucket before publishing a value; check the attribute panel on the
sensor to see `n_workday` / `n_weekend`.

### Services failing with HTTP 401

The supervisor token expired or wasn't injected.  Restart the add-on
to refresh; the Supervisor re-issues the token on every container
start.

### Add-on stuck on "Building"

First install on a Pi 4B pulls ~10 MB of arm64 wheels (numpy,
aiohttp) from piwheels — takes **2-3 minutes** on a decent
connection.  Subsequent rebuilds reuse the layer cache and finish in
under a minute.

### Want to test without controlling anything

Leave `dry_run: true`.  The add-on still:

- subscribes to the stage entity and creates sessions,
- publishes every `sensor.sleep_classifier_*` entity, and
- *plans* device actions and logs them,

but never sends a service call.  This is the recommended state for
the first night so you can verify everything in the Logbook before
turning it loose.

## Uninstall

Click **Uninstall** in the add-on info page.  The Supervisor removes
the container and image.  Learned preferences in `/data/` survive on
purpose so a reinstall picks up where you left off; to wipe them,
delete `/data/user_preferences.json` first via the SSH add-on.

## Scientific references

The natural-sleep suite (v1.2.0) and the preference-learner methodology
(v1.3.0) draw on these peer-reviewed sources:

- Hirshkowitz M et al. **National Sleep Foundation's sleep time
  duration recommendations**, *Sleep Health* 1 (2015) 40-43.
- Paruthi S et al. **Recommended Amount of Sleep for Pediatric
  Populations: A Consensus Statement of the AASM**, *J Clin Sleep
  Med* 12 (2016) 785-786.
- Van Dongen HPA et al. **The cumulative cost of additional
  wakefulness**, *Sleep* 26 (2003) 117-126.
- Belenky G et al. **Patterns of performance degradation and
  restoration during sleep restriction and subsequent recovery**,
  *J Sleep Res* 12 (2003) 1-12.
- Banks S et al. **Neurobehavioral dynamics following chronic sleep
  restriction**, *Sleep* 33 (2010) 1013-1026.
- Hilditch CJ, McHill AW. **Sleep inertia: current insights**, *Nat
  Sci Sleep* 11 (2019) 155-165.
- Phipps-Nelson J et al. **Daytime exposure to bright light…
  decreases sleepiness and improves psychomotor vigilance
  performance**, *Sleep* 26 (2003) 695-700.
- Papalambros NA et al. **Acoustic Enhancement of Sleep Slow
  Oscillations and Concomitant Memory Improvement in Older Adults**,
  *Front Hum Neurosci* 11 (2017) 109.
- Ohayon MM et al. **National Sleep Foundation's sleep quality
  recommendations: first report**, *Sleep Health* 3 (2017) 6-19.
- Berry RB et al. *AASM Manual for the Scoring of Sleep* (v2.6).

## More information

- Project repository: <https://github.com/LiangyuLu-lly/HA-sleep>
- Installation walk-through: see `INSTALL.md` in the repo.
- Manual (non-Add-on) deployment: see `docs/MANUAL_DEPLOYMENT.md`.
- Roadmap: see `docs/BACKLOG.md`.
