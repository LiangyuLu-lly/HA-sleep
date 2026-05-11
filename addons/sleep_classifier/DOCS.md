# Sleep Classifier — Home Assistant Add-on

A deep-learning sleep stage classifier with **closed-loop smart-home
control**.  When you sleep, the add-on:

1. **Auto-discovers** physiological sensors (heart-rate, motion) and tunable
   devices (lights, climate, humidifier, fan) you already exposed in Home
   Assistant.
2. **Classifies** your sleep stage every 30 s with a CNN-BiLSTM model.
3. **Adjusts the bedroom** by calling HA services directly — no automation
   YAML required.
4. **Learns** your preferences: after a few nights, the add-on picks the
   exact temperature / humidity / brightness combo you historically sleep
   *best* under.

## Configuration

Open the **Configuration** tab and tune the following options.

| Option | Default | Description |
|--------|---------|-------------|
| `area` | `bedroom` | Discovery filter; only entities matching this area name are touched. |
| `infer_interval` | `30` | Seconds between inferences + control decisions. |
| `session_interval` | `1800` | How often to checkpoint the preference learner. |
| `dry_run` | `false` | If true, plan actions but never call HA services. |
| `exploration_rate` | `0.1` | Gaussian noise scale when probing new setpoints. |
| `min_seconds_between_actions` | `120` | Cool-down between consecutive service calls (anti-flapping). |
| `deadband_temperature_c` | `0.5` | Skip climate update when |current-target| ≤ this. |
| `deadband_humidity_pct` | `5` | Skip humidifier update when within this. |
| `deadband_brightness_pct` | `10` | Skip light update when within this. |
| `log_level` | `info` | One of `debug`, `info`, `warning`, `error`. |
| `heart_rate_keywords` | `[heart_rate, hr, pulse]` | Substrings used to detect HR sensors. |
| `movement_keywords` | `[movement, motion, activity]` | Substrings used to detect motion/activity sensors. |
| `controllable_domains` | `[light, climate, fan, humidifier]` | Domains the add-on is *allowed* to issue services on. |
| `explicit_includes` | `[]` | Entity IDs to *always* include even if filters skip them. |
| `explicit_excludes` | `[]` | Entity IDs to *never* touch (overrides everything). |

Click **Save**, then **Start** on the Info tab.

## What happens after Start

* The add-on calls `GET /api/states` through the Supervisor proxy (using the
  injected `SUPERVISOR_TOKEN`), classifies your entities into sensors and
  actionables, and prints a summary in the **Log** tab.
* It subscribes to the WebSocket so every `state_changed` event for a
  matched HR / motion entity is fed into the inference engine.
* Once the rolling window fills (~10 minutes of samples) the model starts
  producing real sleep-stage predictions instead of the bootstrap "LIGHT".
* When the predicted stage changes, the add-on issues the matching service
  calls (e.g. `light.turn_off`, `climate.set_temperature`, `humidifier.set_humidity`).

## What you should see in HA

* **Log tab** — fresh `infer stage=… conf=…` line every `infer_interval`
  seconds, plus `Executed <service>` lines whenever something is changed.
* **Devices** — your existing lights / climates respond just like a human
  flicked the switch.  No new HA device is created; the add-on operates on
  *your* devices.

## Persistence

The preference history lives at `/data/user_preferences.json`.  This path is
on the supervisor's persistent volume, so the file survives add-on restarts,
upgrades, and even a full re-install.

To inspect it: **Settings → Add-ons → File editor** (if installed) or via
the HA OS SSH add-on, look for `/usr/share/hassio/addons/data/<slug>/`.

## Troubleshooting

### "No heart-rate or movement sensors found"

The discovery filter found nothing.  Try the following:

1. Check the **Log** tab — it lists every domain bucket; you might be in the
   wrong area filter.
2. Add a custom substring to `heart_rate_keywords` if your sensor is named
   like `sensor.bedside_pulsometer_pulse`.
3. As a last resort, add the entity ID explicitly to `explicit_includes`.

### Services failing with HTTP 401

The supervisor token is missing or expired.  Restart the add-on; the
supervisor refreshes the token automatically.

### Add-on stuck on "Building"

The first install on a Raspberry Pi 4B downloads ~500 MB of wheels
(TensorFlow + scipy + h5py) and can take **15–25 minutes**.  Subsequent
upgrades reuse the layer cache.

### Add-on builds fail on aarch64

If `pip install tensorflow` fails, the piwheels fallback in the Dockerfile
might have been offline.  Re-run **Rebuild** from the add-on UI; transient
failures are common.

### Want to test without controlling anything

Set `dry_run: true` in Configuration.  The add-on still reads sensors and
classifies, but never issues service calls — exactly what you want when
deciding if your sensors are wired correctly.

## Uninstall

Just click **Uninstall** in the add-on info page.  The supervisor removes
the container and image; if you also want to wipe the learned preferences,
remove the file at `/data/user_preferences.json` first.

## More information

* Project repository: https://github.com/yourname/sleep-classifier-ha
* Deep deployment guide: see `docs/HA_SMART_DEPLOYMENT.md` in the repo.
* Architecture write-up: see `docs/PROJECT_COMPLETION_REPORT.md`.
