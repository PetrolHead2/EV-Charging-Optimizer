# EV Charging Optimizer — Quick Start Guide

## Contents

- [What you will end up with](#what-you-will-end-up-with)
- [Prerequisites checklist](#prerequisites-checklist)
- [Step 1 — Install pyscript](#step-1--install-pyscript)
- [Step 2 — Copy the files](#step-2--copy-the-files)
- [Step 3 — Configure entity IDs](#step-3--configure-entity-ids)
- [Step 4 — Add token to grid card](#step-4--add-token-to-grid-card)
- [Step 5 — Update configuration.yaml](#step-5--update-configurationyaml)
- [Step 6 — Reload Home Assistant](#step-6--reload-home-assistant)
- [Step 7 — Add the Lovelace dashboard](#step-7--add-the-lovelace-dashboard)
- [Step 8 — Set your weekly schedule](#step-8--set-your-weekly-schedule)
- [Verify it is working](#verify-it-is-working)
- [Daily usage](#daily-usage)
- [If something is wrong](#if-something-is-wrong)
- [Adjusting for your setup](#adjusting-for-your-setup)

---

## What you will end up with

Plug in your car, set a weekly departure schedule once, and the system charges your Mercedes PHEV at the cheapest available Nordpool electricity prices, always finishing before you leave. On peak-tariff days it automatically throttles to stay under your grid operator's hourly consumption cap. After initial setup the only daily action required is plugging in the car.

---

## Prerequisites checklist

- [ ] Home Assistant running in Docker
- [ ] HACS installed
- [ ] Nordpool integration installed and showing 15-minute prices (`sensor.nordpool_kwh_*`)
- [ ] Zaptec integration (v0.8.x) installed and showing charger status
- [ ] Mercedes Me integration installed and showing battery SoC
- [ ] Tibber integration with Pulse showing live house consumption
- [ ] Long-lived access token ready — **Profile → Security → Long-Lived Access Tokens → Create Token**

---

## Step 1 — Install pyscript

1. In HACS → Integrations, search **pyscript** and install it.
2. Add to `configuration.yaml`:

```yaml
pyscript:
  allow_all_imports: true
  hass_is_global: true
```

3. Restart HA:

```bash
docker restart homeassistant
```

4. Verify pyscript is running:

```bash
curl -s https://[HA_HOST]:8123/api/services \
  -H "Authorization: Bearer [TOKEN]" | python3 -m json.tool | grep pyscript
# Must return pyscript service entries — if empty, check configuration.yaml syntax
```

---

## Step 2 — Copy the files

Find your HA config path (the host directory mounted to `/config` inside the container):

```bash
docker inspect homeassistant | grep -A3 '"Mounts"'
# The "Source" field under the /config bind mount is [HA_CONFIG_PATH]
```

Create required directories if they do not exist:

```bash
mkdir -p [HA_CONFIG_PATH]/pyscript
mkdir -p [HA_CONFIG_PATH]/packages
mkdir -p [HA_CONFIG_PATH]/www
```

Copy from the repo root:

```bash
cp pyscript/ev_optimizer.py    [HA_CONFIG_PATH]/pyscript/
cp pyscript/ev_control_loop.py [HA_CONFIG_PATH]/pyscript/
cp packages/ev_optimizer.yaml  [HA_CONFIG_PATH]/packages/
cp www/ev_schedule_grid.html   [HA_CONFIG_PATH]/www/
```

> **WARNING**: If `cp` fails with permission denied, prefix with `sudo`.

---

## Step 3 — Configure entity IDs

Entity IDs differ between installations. Find yours:

```bash
HA="https://[HA_HOST]:8123"
TOK="Authorization: Bearer [TOKEN]"

curl -s $HA/api/states -H "$TOK" | python3 -m json.tool | grep '"entity_id"' | grep nordpool
curl -s $HA/api/states -H "$TOK" | python3 -m json.tool | grep '"entity_id"' | grep -i laddbox
curl -s $HA/api/states -H "$TOK" | python3 -m json.tool | grep '"entity_id"' | grep -i jbb78w
curl -s $HA/api/states -H "$TOK" | python3 -m json.tool | grep '"entity_id"' | grep -i tibber
```

Edit the constants at the top of each pyscript file to match your entities:

| What to replace | File | Constant name |
|---|---|---|
| Nordpool price sensor | `ev_optimizer.py` | `PRICE_ENT` |
| Mercedes SoC entity prefix | `ev_optimizer.py` | `jbb78w` (replace all) |
| Zaptec charger API UUID | `ev_control_loop.py` | `ZAPTEC_DEVICE_ID` |
| Zaptec installation API UUID | `ev_control_loop.py` | `ZAPTEC_INSTALL_ID` |
| Zaptec entity prefix | `ev_control_loop.py` | `laddbox` (replace all) |
| Tibber accumulated sensor | `ev_control_loop.py` | `TIBBER_ACCUM_ENT` |
| Tibber average power sensor | `ev_control_loop.py` | `TIBBER_AVG_W_ENT` |

> **WARNING**: `ZAPTEC_DEVICE_ID` and `ZAPTEC_INSTALL_ID` are Zaptec API UUIDs, not HA device IDs. Find them in the Zaptec integration settings or your Zaptec account portal.

---

## Step 4 — Add token to grid card

```bash
# Find the placeholder
grep -n "PASTE_TOKEN_HERE" [HA_CONFIG_PATH]/www/ev_schedule_grid.html

# Replace it
sed -i 's/PASTE_TOKEN_HERE/[YOUR_TOKEN]/g' \
  [HA_CONFIG_PATH]/www/ev_schedule_grid.html

# Verify — must print 0
grep -c "PASTE_TOKEN_HERE" [HA_CONFIG_PATH]/www/ev_schedule_grid.html
```

---

## Step 5 — Update configuration.yaml

Add these blocks — merge into existing top-level keys, never duplicate them:

```yaml
homeassistant:
  packages: !include_dir_named packages
input_datetime:
  ev_deadline:
    has_date: true
    has_time: true
  ev_last_state_change:
    has_date: true
    has_time: true
input_number:
  ev_required_kwh:
    min: 0
    max: 40
    step: 0.5
    unit_of_measurement: kWh
input_select:
  ev_charging_mode:
    options: [Smart, "Charge now", Stop]
    initial: Smart
input_text:
  ev_decision_reason:
    max: 255
```

## Step 6 — Reload Home Assistant

Run in order — wait for the restart to finish before the next commands:

```bash
HA="https://[HA_HOST]:8123"
TOK="-H 'Authorization: Bearer [TOKEN]' -H 'Content-Type: application/json'"

# Full restart to load configuration.yaml changes + package file
docker restart homeassistant
sleep 30

# Reload pyscript (loads the two .py files)
curl -s -X POST $HA/api/services/pyscript/reload \
  -H "Authorization: Bearer [TOKEN]" \
  -H "Content-Type: application/json" -d '{}'

# Trigger first schedule computation
curl -s -X POST $HA/api/services/pyscript/ev_optimizer_recompute \
  -H "Authorization: Bearer [TOKEN]" \
  -H "Content-Type: application/json" -d '{}'
```

---

## Step 7 — Add the Lovelace dashboard

1. **Settings → Dashboards → Add Dashboard** — create a view named **Car**.
2. Enter edit mode → **Add Card → Manual card**.
3. Paste this YAML — replace `jbb78w_state_of_charge` with your Mercedes SoC entity:

```yaml
type: vertical-stack
title: EV Charging Optimizer
cards:
  - type: entities
    title: Controls
    entities:
      - entity: input_select.ev_charging_mode
      - entity: input_number.ev_required_kwh
      - entity: input_datetime.ev_deadline
      - entity: input_text.ev_decision_reason
  - type: entities
    title: Status
    entities:
      - entity: sensor.ev_remaining_kwh
      - entity: sensor.ev_slots_needed
      - entity: sensor.ev_slots_available
      - entity: binary_sensor.ev_deadline_pressure
      - entity: sensor.jbb78w_state_of_charge
  - type: iframe
    url: /local/ev_schedule_grid.html
```

## Step 8 — Set your weekly schedule

Open in your browser:

```
https://[HA_HOST]:8123/local/ev_schedule_grid.html
```

Enter typical departure times for each day of the week (up to 3 per day). Click **Save**. The system now automatically charges before each departure using the cheapest available Nordpool slots.

---

## Verify it is working

Check these within the first 24 hours:

- [ ] `sensor.ev_schedule` is not empty — **Developer Tools → States**, search `ev_schedule`
- [ ] `input_datetime.ev_computed_deadline` shows a real near-future date (not year 2000)
- [ ] `input_text.ev_decision_reason` updates every 5 minutes
- [ ] Plug in car: decision reason updates within 30 seconds
- [ ] At the first scheduled window: charging starts automatically

---

## Daily usage

1. **Normal day**: plug in the car — the system does everything else.
2. **Different departure time today**: set `input_datetime.ev_deadline` in the dashboard to the new time. To clear it afterwards, set it to any past date (e.g. 2000-01-01).
3. **Charge immediately**: set `input_select.ev_charging_mode` to **Charge now**. Set it back to **Smart** when done.

---

## If something is wrong

1. Read `input_text.ev_decision_reason` — it states exactly why charging is on or off.
2. Check `sensor.ev_schedule` is not empty in Developer Tools.
3. Check logs:

```bash
docker logs homeassistant 2>&1 | \
  grep -i "ev_optimizer\|ev_control" | tail -20
```

4. Bypass the optimizer entirely: set `input_select.ev_charging_mode` to **Charge now**.

For deeper diagnostics see **MANUAL.md section 12**.

---

## Adjusting for your setup

| What to change | Entity / Constant | Default |
|---|---|---|
| Hourly consumption cap (tariff hours) | `input_number.ev_max_hourly_kwh` | 5.0 kWh |
| Max charge power during tariff hours | `input_number.ev_max_tariff_power_kw` | 3.0 kW |
| Disable tariff guard entirely | `input_boolean.ev_tariff_guard_enabled` | on |
| Nordpool region or tax tier | `PRICE_ENT` in `ev_optimizer.py` | SE3, 3% VAT, 2.5 öre |
| Full-power fallback (if sensor missing) | `CHARGER_KW` in `ev_optimizer.py` | 7.0 kW |
