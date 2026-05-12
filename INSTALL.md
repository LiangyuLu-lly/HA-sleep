# Installing on Home Assistant OS (Raspberry Pi 4B or amd64)

The fastest path is the **HA Add-on Store**: same UX as HACS, no SSH,
no pip, no Long-Lived Access Token to generate.

```text
1. prepare.bat / prepare.sh     ── mirror src/ scripts/ training_config/ into rootfs/
2. git push                     ── publish your fork
3. HA Web UI → Add Repository   ── paste the GitHub URL
4. Install "Sleep Classifier"   ── HA Supervisor builds the image (~3 min)
5. Configuration → sleep_stage_source = sensor.<your_stage>
6. Start
```

> v1.6.0 lean image: no TensorFlow, no PyWavelets, no h5py, no
> numpy either — the only Python wheel we pull is `aiohttp`, so the
> Pi 4B build finishes in ~1 min and the image ends up around 15 MB
> on disk.  The add-on does **not** ship a model file any more;
> stages come from an HA entity you already own.

---

## 1. Run prepare ⚠️ required before every push

HA Supervisor builds the add-on image with the *add-on* directory as
the Docker context, which means it can't see anything outside
`sleep_classifier/`.  The prepare script mirrors the project source
into `sleep_classifier/rootfs/` so the Dockerfile's `COPY rootfs/`
has the right files at hand.

**Windows**:

```cmd
sleep_classifier\prepare.bat
```

**Linux / macOS**:

```bash
chmod +x sleep_classifier/prepare.sh
sleep_classifier/prepare.sh
```

Expected output:

```text
[prepare] mirrored src\
[prepare] mirrored scripts\
[prepare] mirrored training_config\
[prepare] copied requirements-runtime.txt
[prepare] copied requirements.txt
[prepare] done
```

Re-run prepare every time you change `src/`, `scripts/`,
`training_config/`, or `requirements-runtime.txt`, then commit + push.

## 2. Publish the repo on GitHub

If your fork isn't online yet:

```bash
git init
git add .
git commit -m "Sleep Classifier v1.3.0"
git remote add origin https://github.com/<your-user>/<your-repo>.git
git branch -M main
git push -u origin main
```

> Private repos won't work — HA Supervisor doesn't authenticate against
> GitHub.  Either make the repo public, or embed a token in the URL:
> `https://<token>@github.com/<your-user>/<your-repo>`.

## 3. Add the repository in HA

1. Open HA in a browser: `http://homeassistant.local:8123` (or your
   Pi's IP).
2. **Settings → Add-ons → ADD-ON STORE** (bottom-right ⊕).
3. Top-right **⋮ → Repositories**.
4. Paste the repo URL, click **Add**, then **Close**.
5. Back on the store page, hard-refresh (Ctrl-F5) and scroll to the
   bottom — a new **Sleep Classifier** card should appear.

## 4. Install the add-on

1. Click the **Sleep Classifier** card → **INSTALL**.
2. Wait.  First-time build on a Pi 4B takes ~3 minutes; the **Log**
   tab shows progress (apk install → pip wheels → cleanup).
3. When START / STOP / RESTART buttons appear, the build is done.

## 5. Configure

The **only required** field in v1.3.0 is `sleep_stage_source`.  Everything
else is optional and falls back to either auto-discovery or sensible
defaults.

### 5.1 Recommended: the embedded Web UI

The add-on bundles an aiohttp Web UI that lets you pick entity IDs from
**live HA dropdowns** instead of typing them by hand.

1. Start the add-on once (it's `dry_run: true` by default, so it won't
   touch your devices).
2. Add-on detail page → **OPEN WEB UI**.
3. For each slot, pick the right entity from the dropdown.  The
   "Sleep stage source" dropdown filters down to sensors whose state
   matches one of the recognised stage strings (AWAKE / LIGHT / DEEP /
   REM and their case-insensitive / Chinese variants).
4. Click **Save**, then **RESTART** the add-on.

The Web UI writes to `/data/web_ui_overrides.json`, which takes
priority over the Configuration form.  To clear a slot back to
auto-discovery, pick the first dropdown entry (`— leave empty —`).

### 5.2 Or: edit the Configuration form directly

```yaml
# ── Required ───────────────────────────────────────────────────────────
sleep_stage_source: sensor.mi_band_8_pro_sleep_stage    # the only must-have

# ── Optional: bedroom environment sources (drive deadbands + k-NN) ────
temperature_source: sensor.bedroom_temperature
humidity_source: sensor.bedroom_humidity
illuminance_source: sensor.bedroom_illuminance

# ── Optional: actuator targets ───────────────────────────────────────
light_targets:
  - light.bedroom_main
  - light.bedroom_bedside
climate_target: climate.bedroom_ac
humidifier_target: humidifier.bedroom_humidifier
fan_target: fan.bedroom_fan

# ── Safety: keep this on the first night ─────────────────────────────
dry_run: true
```

In **dry-run mode** the add-on still reads your sleep-stage entity,
still publishes every `sensor.sleep_classifier_*` entity, and still
*plans* device actions — it just never POSTs them.  Read the log to
confirm what it would have done.

## 6. Start + verify

1. **Info → START**.
2. After ~30 seconds the **Log** tab should show:

   ```text
   [run.sh] slot bindings active: 4 role(s)
   smart_service | HA exposes 187 entities
   external_stage_subscriber | watching sensor.mi_band_8_pro_sleep_stage
   external_stage_subscriber | stage transition: AWAKE → LIGHT (conf=0.90)
   smart_service | infer stage=LIGHT  env(T=22.5 H=48.0 lux=2.0)
   smart_service |   → 2 HA action(s) planned (light.bedroom_main, climate.bedroom_ac)
   ```

3. Open **Developer Tools → States** in HA and search
   `sleep_classifier`.  You should see **13 entities**:

   ```text
   # Stage + session diagnostics
   sensor.sleep_classifier_stage
   sensor.sleep_classifier_confidence
   sensor.sleep_classifier_quality_score
   sensor.sleep_classifier_session_duration
   sensor.sleep_classifier_last_action

   # Natural-sleep suite (v1.2.0)
   sensor.sleep_classifier_debt_hours
   sensor.sleep_classifier_recommended_bedtime
   sensor.sleep_classifier_wake_decision
   sensor.sleep_classifier_soundscape

   # Preference-learning panel (v1.3.0)
   sensor.sleep_classifier_learned_bedtime_workday
   sensor.sleep_classifier_learned_bedtime_weekend
   sensor.sleep_classifier_learned_environment
   sensor.sleep_classifier_recommendation_explain
   ```

4. Once you're confident, flip `dry_run: false`, **RESTART**.

---

## Lovelace dashboard (drop-in YAML)

Use the dashboard's **Edit → ⋮ → Edit in YAML** mode and paste:

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
        name: Last score
      - entity: sensor.sleep_classifier_session_duration
        name: Session
  - type: glance
    title: What the model learned about *you*
    entities:
      - entity: sensor.sleep_classifier_learned_bedtime_workday
        name: Workday bedtime
      - entity: sensor.sleep_classifier_learned_bedtime_weekend
        name: Weekend bedtime
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

The **"Why?"** card is the new v1.3.0 attribute panel: click it
open and Lovelace will show the JSON attributes — neighbour
sessions, weights, confidence, decay half-life — that drove tonight's
recommendation.

## Maintenance

- **Auto-restart**: turn on **Watchdog** and **Auto-update** in the
  add-on Info tab.
- **Reconnect**: the WebSocket subscriber backs off exponentially
  (1 s → 2 s → … → 5 min) on network blips; no manual action needed.
- **Upgrade**: bump `version:` in `sleep_classifier/config.yaml`,
  `git push`, then **UPDATE** in the add-on Info tab.

## Uninstall

- HA UI → **Settings → Add-ons → Sleep Classifier → UNINSTALL**.
- Learned preferences (`/data/user_preferences.json`) survive an
  uninstall on purpose so a reinstall picks up where you left off.
  To wipe them, SSH in and `rm /data/user_preferences.json`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "Not a valid repository" when adding the URL | URL typo or private repo | Fix URL, make the repo public, or embed a token |
| Add-on doesn't appear in the store | Stale browser cache | Hard-refresh (Ctrl-F5); restart Supervisor |
| Build stuck on "installing numpy" | piwheels rate-limit / Pi network | Wait; check Pi network |
| Log: `No sleep_stage_source configured` | The required slot is empty | Open Web UI, pick the stage sensor, RESTART |
| Log: `external_stage_subscriber: ignoring state 'asleep'` | Your sensor uses a stage string we don't recognise | Add the literal to `sleep_stage_aliases` in Configuration, or open an issue |
| `sensor.sleep_classifier_*` entities missing | Stage source hasn't emitted any state since boot | Wait for the first transition, or force one in Developer Tools |
| Light / AC not reacting | `dry_run: true`, or deadband | Confirm `dry_run: false`; the Logbook shows real service calls |
| Recommended bedtime says "unknown" | Need ≥ 3 sessions in that bucket | Sleep a few more nights; both buckets populate independently |

## Manual deployment (without HA OS)

If you run Home Assistant Core on Raspberry Pi OS, Ubuntu, or in a
container, the add-on UI isn't available.  See
[`docs/MANUAL_DEPLOYMENT.md`](docs/MANUAL_DEPLOYMENT.md) for the
systemd / Docker walk-through.
