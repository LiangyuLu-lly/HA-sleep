# Sleep Classifier — Home Assistant Add-on (v1.1.0)

A deep-learning sleep stage classifier with **closed-loop smart-home
control**.  When you sleep, the add-on:

1. **Auto-discovers** physiological sensors (heart-rate, motion, breathing
   rate) and tunable devices (lights, climate, humidifier, fan) you
   already exposed in Home Assistant — bilingual matcher works on both
   English and Chinese / pinyin friendly names.
2. **Classifies** your sleep stage every 30 s with a CNN-BiLSTM model.
3. **Publishes** the result back as four HA entities you can drop on a
   Lovelace dashboard:

   ```text
   sensor.sleep_classifier_stage              # AWAKE / LIGHT / DEEP / REM
   sensor.sleep_classifier_confidence         # 0..100 %
   sensor.sleep_classifier_quality_score      # latest sleep-quality grade
   sensor.sleep_classifier_session_duration   # seconds since session start
   sensor.sleep_classifier_last_action        # most recent device change
   ```

4. **Adjusts the bedroom** by calling HA services directly — no automation
   YAML required.
5. **Learns** your preferences: after a few nights, the add-on picks the
   exact temperature / humidity / brightness combo you historically sleep
   *best* under.

## Configuration

Open the **Configuration** tab.  The form is split into three groups:

### General behaviour

| Option | Default | Description |
|---|---|---|
| `area` | (empty) | Discovery filter; leave empty to scan all rooms. |
| `infer_interval` | `30` | Seconds between inferences + control decisions. |
| `session_interval` | `1800` | How often to checkpoint the preference learner. |
| `dry_run` | `true` | If true, plan actions but never call HA services. **Keep this on for the first night.** |
| `exploration_rate` | `0.1` | Gaussian noise scale when probing new setpoints. |
| `min_seconds_between_actions` | `120` | Cool-down between consecutive service calls (anti-flapping). |
| `deadband_temperature_c` | `0.5` | Skip climate update when within this. |
| `deadband_humidity_pct` | `5` | Skip humidifier update when within this. |
| `deadband_brightness_pct` | `10` | Skip light update when within this. |
| `log_level` | `info` | One of `debug`, `info`, `warning`, `error`. |

### Slot bindings — pin a specific entity to a role

Auto-discovery is good but not perfect.  If you have multiple HR sources
(e.g. a Mi Band on your wrist *and* a SleepRadar R60ABD1 next to the bed)
you'll want to tell the add-on which one to trust.  Each `*_source` /
`*_target` field below accepts **exactly one** `entity_id`; lists accept
multiple.  Leave any field empty to fall back to keyword auto-discovery.

| Option | Example value | Notes |
|---|---|---|
| `heart_rate_source` | `sensor.xiaomi_smart_band_9_pro_heart_rate` | Single HR sensor. |
| `movement_source` | `sensor.sleepradar_r60abd1_ts5_yundong_zhuangtai` | Movement / motion / presence. |
| `breathing_source` | `sensor.sleepradar_r60abd1_ts6_huxi_xinxi` | Used as HR proxy if no HR sensor is bound. |
| `temperature_source` | `sensor.bedroom_temperature` | Drives "current T" reading. |
| `humidity_source` | `sensor.bedroom_humidity` | Drives "current RH" reading. |
| `illuminance_source` | `sensor.bedroom_illuminance` | Optional — only used for brightness deadband. |
| `light_targets` | `[light.bedroom_main, light.bedroom_bedside]` | List — all listed lights are dimmed together. |
| `climate_target` | `climate.bedroom_ac` | Single AC / heat-pump entity. |
| `humidifier_target` | `humidifier.bedroom_humidifier` | Optional. |
| `fan_target` | `fan.bedroom_fan` | Optional. |
| `switch_targets` | `[switch.bedside_outlet]` | List — generic switches the add-on may toggle. |

> 💡 **Don't know the entity_id?**  Open **Developer Tools → States**
> in HA, type a fragment of the device name into the *Entity* search,
> then copy the resulting `entity_id` straight into Configuration.

### Auto-discovery tunables

| Option | Default | Description |
|---|---|---|
| `heart_rate_keywords` | `[heart_rate, hr, pulse, bpm, 心率, 脉搏]` | Substrings used to detect HR sensors. |
| `movement_keywords` | `[motion, movement, activity, presence, occupancy, 运动, 体动, 人体]` | Movement detector keywords. |
| `breathing_keywords` | `[breath, breathing, respiration, 呼吸]` | Breathing-rate keywords (used as HR fallback). |
| `controllable_domains` | `[light, climate, fan, humidifier]` | Domains the add-on may invoke services on. |
| `explicit_includes` | `[]` | Entity IDs to *always* include even if filters skip them. |
| `explicit_excludes` | `[]` | Entity IDs to *never* touch (overrides everything). |

Click **Save**, then **Start** on the Info tab.

## What happens after Start

* The add-on calls `GET /api/states` through the Supervisor proxy (using
  the injected `SUPERVISOR_TOKEN`), classifies your entities into sensors
  and actionables, and prints a summary in the **Log** tab.
* It restores the inference buffer from `/data/inference_buffer.npz` if
  one exists and is < 6 h old, so a restart doesn't force a fresh 10-min
  warm-up.
* It subscribes to the WebSocket so every `state_changed` event for a
  matched HR / motion / breathing entity is fed into the inference engine.
  The WebSocket reconnects automatically with exponential backoff on
  network blips.
* Once the rolling window fills (~10 minutes of samples cold-start, or
  instantly after a restart with the saved buffer) the model starts
  producing real sleep-stage predictions instead of the bootstrap "LIGHT".
* When the predicted stage changes, the add-on issues the matching service
  calls (e.g. `light.turn_off`, `climate.set_temperature`,
  `humidifier.set_humidity`).
* Every inference tick also publishes the stage + confidence + session
  duration to the `sensor.sleep_classifier_*` entities.

## Lovelace dashboard example

Drop this into your dashboard's raw YAML editor for an instant overview
card.  The entities are populated live by the add-on:

```yaml
type: vertical-stack
cards:
  - type: glance
    title: Sleep monitor
    entities:
      - entity: sensor.sleep_classifier_stage
        name: Stage
      - entity: sensor.sleep_classifier_confidence
        name: Confidence
      - entity: sensor.sleep_classifier_quality_score
        name: Last quality
      - entity: sensor.sleep_classifier_session_duration
        name: Session
  - type: history-graph
    title: Sleep stages over the night
    hours_to_show: 12
    entities:
      - sensor.sleep_classifier_stage
      - sensor.sleep_classifier_confidence
  - type: entities
    title: Last automation action
    entities:
      - entity: sensor.sleep_classifier_last_action
```

You can also drive automations off stage transitions, e.g. mute the TV
the moment the model thinks you're in DEEP sleep:

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

* **`/data/user_preferences.json`** — preference history (learner state).
* **`/data/inference_buffer.npz`** — last seen physiology samples, lets
  the add-on resume warm.
* **`/data/effective_config.json`** — merged config that the Python
  service actually loaded (handy for debugging Configuration UI issues).

All three live on the supervisor's persistent volume, so they survive
add-on restarts, upgrades, and even a full re-install.

## Troubleshooting

### "No heart-rate / movement / breathing sensor found"

The discovery filter found nothing.  The add-on now logs a list of
**candidate entity_ids** automatically — copy whichever of those matches
your hardware into the matching `*_source` field in Configuration, click
Save, then Restart.

If even the candidate list is empty, your sensors aren't yet integrated
into HA.  Add them under **Settings → Devices & Services** first.

### Services failing with HTTP 401

The supervisor token is missing or expired.  Restart the add-on; the
supervisor refreshes the token automatically.

### Add-on stuck on "Building"

The first install on a Raspberry Pi 4B downloads ~30 MB of arm64
piwheels (numpy, scipy, h5py, PyWavelets, aiohttp) and takes **3–5
minutes**.  Subsequent upgrades reuse the layer cache and are usually
< 1 minute.

### "TENSORFLOW not available — using numpy-based..." in the log

This is **expected and correct**.  The add-on image deliberately ships
without TensorFlow to stay small.  Numerical equivalence between the
Keras path (used at training time) and the numpy fallback (used at
runtime) is enforced by `tests/test_numpy_keras_equivalence.py` (max
abs diff < 1e-3).

### Want to test without controlling anything

Set `dry_run: true` in Configuration.  The add-on still reads sensors
and classifies, and still publishes `sensor.sleep_classifier_*` entities,
but never issues device service calls — exactly what you want for the
first night to validate sensor wiring.

### WebSocket / network instability

Transient errors are logged once at WARNING and the add-on reconnects
with exponential backoff (1 s → 2 s → 4 s → … → 5 min cap).  Auth
failures (HTTP 401/403) on the WS still stop the service — re-check your
SUPERVISOR_TOKEN by restarting the supervisor.

## Uninstall

Just click **Uninstall** in the add-on info page.  The supervisor removes
the container and image; if you also want to wipe learned preferences,
remove `/data/user_preferences.json` first via the SSH add-on.

## More information

* Project repository: <https://github.com/LiangyuLu-lly/HA-sleep>
* Manual deployment guide: see `docs/HA_SMART_DEPLOYMENT.md` in the repo.
* Architecture write-up: see `docs/PROJECT_COMPLETION_REPORT.md`.
