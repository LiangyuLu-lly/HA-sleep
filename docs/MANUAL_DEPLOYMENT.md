# Manual deployment (without the HA OS Add-on)

If you run Home Assistant Core / Container / Supervised — i.e. *not*
HA OS — the add-on store isn't available and you have to run the
service yourself.  This guide is the systemd + venv recipe for a
Raspberry Pi or any 64-bit Linux box on the same LAN as HA.

For the HA OS one-click install path, use [`../INSTALL.md`](../INSTALL.md)
instead.

## 0. Architecture

```text
┌──────────────────── Linux host ────────────────────┐
│  ┌────────────────┐    REST    ┌─────────────────┐  │
│  │ Home Assistant │ ─────────► │ Sleep Smart      │  │
│  │  (port 8123)   │            │ Service (this    │  │
│  │                │ ◄────WS────│ project)         │  │
│  │  sensor.*_     │            │                  │  │
│  │   sleep_stage  │            │ - PreferenceLearner │
│  │  light.*       │            │ - Bounded controller │
│  │  climate.*     │            │ - SleepDebt tracker  │
│  │  humidifier.*  │            │                  │  │
│  └────────────────┘            └─────────────────┘  │
└─────────────────────────────────────────────────────┘
```

The service uses **two** HA APIs:

- **REST** (`/api/states`, `/api/services/...`) to read entity state
  snapshots and call services like `light.turn_on`.
- **WebSocket** (`/api/websocket`) to subscribe to `state_changed`
  events for the configured `sleep_stage_source` sensor.

You authenticate with a **Long-Lived Access Token** (LLAT) created
once in the HA Profile page.

## 1. Prerequisites

| Item | Recommended |
|---|---|
| OS | Raspberry Pi OS 64-bit Bookworm / Ubuntu 22.04+ / any glibc-based 64-bit Linux |
| Python | 3.10 or 3.11 |
| RAM | 1 GB+ (the v1.3.0 service stays under 80 MB resident) |
| Network | Same LAN as HA, or reachable on TCP 8123 |
| HA install | Core, Container or Supervised — anything that exposes the REST API |

> HA OS (locked-down Pi 4B image) doesn't let you `pip install` into
> the host.  In that case either use the Add-on path
> ([`INSTALL.md`](../INSTALL.md)) or run this service on a *different*
> machine and point it at HA's IP.

## 2. Generate a Long-Lived Access Token

1. HA Web UI → click your avatar (bottom-left).
2. **Security** tab → scroll to **Long-Lived Access Tokens**.
3. **Create Token**, name it `sleep_smart_service`, click **OK**.
4. Copy the JWT string immediately — HA only shows it once.

Test it from your shell:

```bash
export HA_TOKEN="eyJ..."
curl -s -H "Authorization: Bearer $HA_TOKEN" \
  http://homeassistant.local:8123/api/ | python3 -m json.tool
# Expected: {"message": "API running."}
```

## 3. Install the service

```bash
# 1. system deps
sudo apt update
sudo apt install -y python3 python3-venv git

# 2. clone + venv
cd ~
git clone https://github.com/<your-user>/<your-repo>.git sleep_smart
cd sleep_smart

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip wheel

# 3. runtime-only deps (aiohttp + numpy, ~10 MB)
pip install --extra-index-url https://www.piwheels.org/simple/ \
  -r requirements-runtime.txt
```

## 4. Sanity-check entity wiring

You need at least one sleep-stage entity for the service to do
anything interesting.  List your HA entities and confirm there's
one whose state is something like `AWAKE` / `LIGHT` / `DEEP` / `REM`:

```bash
curl -s -H "Authorization: Bearer $HA_TOKEN" \
  http://homeassistant.local:8123/api/states | \
  python3 -c '
import json, sys
for e in json.load(sys.stdin):
    if "sleep" in e["entity_id"]:
        print(e["entity_id"], "=", e["state"])
'
```

Common integrations that publish a usable stage sensor:

- **Apple Watch** via HAHealthBridge / homeassistant-healthkit
- **Mi Band 4/5/6/7/8/9** via Gadgetbridge + MQTT
- **sleep_as_android** via its built-in HA integration
- **Withings / Beddit / Eight Sleep** mattresses
- **mmWave radars** (R60ABD1 / LD2410B) via ESPHome with a sleep-state
  template sensor
- A separate sleep-staging add-on you've already deployed

The exact stage label vocabulary varies; the v1.3.0 stage subscriber
normalises common variants (case-insensitive, bilingual zh/en) but
edge cases can be added to the `sleep_stage_aliases` configuration.

## 5. Run the service

### 5.1 Dry-run (no HA writes)

```bash
source venv/bin/activate
python scripts/run_ha_smart_service.py --dry-run \
  --duration 30 --infer-interval 5
```

Output:

```text
smart_service | Dry-run without token — synthetic loop
smart_service | Running offline synthetic loop for 30s
smart_service | stage=AWAKE conf=1.00
smart_service | stage=LIGHT conf=0.91
```

### 5.2 Real HA loop

```bash
export HA_TOKEN="eyJ..."
python scripts/run_ha_smart_service.py \
  --base-url http://homeassistant.local:8123 \
  --area bedroom \
  --infer-interval 30 \
  --session-interval 1800 \
  --duration 600
```

Expected log lines:

```text
smart_service | HA exposes 187 entities
external_stage_subscriber | watching sensor.mi_band_8_pro_sleep_stage
external_stage_subscriber | stage transition: AWAKE → LIGHT (conf=0.92)
src.smart_environment_controller | Executed light.turn_on(light.bedroom_main, brightness_pct=8)
src.smart_environment_controller | Executed climate.set_temperature(climate.bedroom_ac, temperature=21.0)
```

In the HA UI you'll see 13 new entities under
`sensor.sleep_classifier_*` — see
[`../sleep_classifier/DOCS.md`](../sleep_classifier/DOCS.md) for the
full entity reference and Lovelace examples.

### 5.3 Default stage → control mapping

Until the preference learner has 3+ sessions, the controller uses
this fallback table:

| Stage | Lights | Climate | Humidifier | Fan |
|---|---|---|---|---|
| AWAKE | 40 % @ 4000 K | 23 °C | 50 % | 20 % |
| LIGHT | 8 % @ 2200 K | 21 °C | 55 % | 15 % |
| DEEP  | off | 19 °C | 55 % | 10 % |
| REM   | off | 19.5 °C | 55 % | 10 % |

Once enough history accumulates, the **top-quantile, decay-weighted
weighted-median** of past sessions takes over the env recommendation,
conditioned on tonight's hour-of-bedtime and current room temperature
via k-NN.  See `src/preference_learner.py` for the full algorithm.

## 6. Run as a systemd service

`/etc/systemd/system/sleep-smart.service`:

```ini
[Unit]
Description=Sleep Classifier — HA control loop (v1.3.0)
After=network-online.target home-assistant.service
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/sleep_smart
Environment="HA_TOKEN=eyJ..."
ExecStart=/home/pi/sleep_smart/venv/bin/python \
    /home/pi/sleep_smart/scripts/run_ha_smart_service.py \
    --base-url http://localhost:8123 \
    --area bedroom \
    --infer-interval 30 \
    --session-interval 1800
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sleep-smart
sudo journalctl -fu sleep-smart
```

> 🔐 Don't commit `HA_TOKEN` to git.  Put it in
> `/etc/default/sleep-smart` and reference via
> `EnvironmentFile=` in the unit file, or store it in
> `/etc/systemd/system/sleep-smart.service.d/token.conf` with mode
> `600`.

## 7. Run in Docker (alternative)

```bash
docker build -t sleep-smart .
docker run -d --name sleep-smart \
  --restart unless-stopped \
  -e HA_TOKEN="eyJ..." \
  -e HA_BASE_URL=http://homeassistant.local:8123 \
  -v sleep-smart-data:/data \
  sleep-smart
```

A minimal `Dockerfile` at the repo root would mirror what
`sleep_classifier/Dockerfile` does, but without the Supervisor-specific
ENV vars.  Easiest is to just point Docker at the existing add-on
context:

```bash
sleep_classifier/prepare.sh    # mirror src/ scripts/ ... into rootfs/
docker build -t sleep-smart sleep_classifier/
```

## 8. Persistence

The service writes three files under `/data` (or the directory
specified by `--data-dir`):

- **`user_preferences.json`** — session history; survives restarts
  and is the input to every recommendation.
- **`effective_config.json`** — the merged config the service
  actually loaded.  Useful when CLI flags + the JSON config disagree.
- **`user_profile.json`** — Bayesian sleep-hour posterior driven by
  the (optional) `birth_year` config field.

Back these up alongside your HA config; deleting them is the way to
"start over" without uninstalling.

## 9. Troubleshooting

### Service exits immediately with `HAAuthError: Empty HA access token`

`HA_TOKEN` isn't set in the environment.  Add it via `EnvironmentFile`
in systemd or `export HA_TOKEN=...` in your shell before
`python scripts/run_ha_smart_service.py`.

### Log: `Configured stage source 'sensor.foo' not in HA states`

The entity ID you passed doesn't exist in HA right now.  Open
**Developer Tools → States** in HA and confirm the spelling.

### Log: `external_stage_subscriber: ignoring state 'asleep'`

Your stage sensor emits a string the subscriber doesn't recognise.
Add the literal to `sleep_stage_aliases` in `training_config/config.json`
or in the CLI's JSON config override file.

### HA returns 401 / 403 mid-run

The LLAT was revoked (e.g. you re-issued it in the HA UI).  Generate
a fresh one and restart the service.

### Recommendation is always "unknown"

The preference learner has fewer than `min_sessions_for_personalisation`
(default 3) closed sessions in the relevant bucket.  Either sleep a
few more nights, or lower the threshold in the JSON config.

### Want to test the controller without touching real devices

Pass `--dry-run` to the service: stages, recommendations and planned
actions are still logged, but no HA service calls fire.

## Further reading

- [`../INSTALL.md`](../INSTALL.md) — HA OS Add-on Store install
- [`../sleep_classifier/DOCS.md`](../sleep_classifier/DOCS.md) — full
  entity + configuration reference (applies whether you run the
  add-on or the bare script).
- [`./BACKLOG.md`](./BACKLOG.md) — planned features and known
  trade-offs.
