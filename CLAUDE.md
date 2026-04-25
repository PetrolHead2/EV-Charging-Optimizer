# EV Charging Optimizer — Project Reference

## Infrastructure

| Item | Value |
|------|-------|
| HA host | `[YOUR_HA_HOST]` (SSH user: `pi`, password: `[YOUR_SSH_PASSWORD]`) |
| HA container | `homeassistant` (Docker) |
| HA version | 2026.4.3 |
| HA URL | https://[YOUR_HA_HOSTNAME]:8123/ |
| Config dir (host) | `/media/pi/NextCloud/homeassistant` |
| Config dir (container) | `/config` |
| Timezone | Europe/Stockholm |
| Nordpool region | SE3, SEK/kWh |

All HA config files are owned by root. Use `echo [YOUR_SSH_PASSWORD] | sudo -S` for writes on debian.

To restart HA: `ssh pi@[YOUR_HA_HOST] "docker restart homeassistant"`

To reload pyscript without restart: call service `pyscript.reload` via HA API or Developer Tools.

To reload input helpers: call `input_datetime/reload`, `input_number/reload`, `input_select/reload`, `input_text/reload` individually — `homeassistant/reload_all` does NOT reload these domains.

Connect to HA at https://[YOUR_HA_HOSTNAME]:8123/ using the long-lived token [HA_TOKEN]

the home assistant is a docker installation on the machine "debian" wich is ssh accessable with user "pi" and password "[YOUR_SSH_PASSWORD]" where every a password is needed start with trying "[YOUR_SSH_PASSWORD]"

## Project Files

| File (host path) | Purpose |
|-----------------|---------|
| `/media/pi/NextCloud/homeassistant/configuration.yaml` | Main HA config — enables pyscript, packages, and input helpers |
| `/media/pi/NextCloud/homeassistant/packages/ev_optimizer.yaml` | Template sensors + safety automations (HA package) |
| `/media/pi/NextCloud/homeassistant/pyscript/ev_optimizer.py` | Schedule computation service |
| `/media/pi/NextCloud/homeassistant/pyscript/ev_control_loop.py` | 5-minute control loop |
| `/media/pi/NextCloud/homeassistant/www/ev_schedule_grid.html` | Lovelace iframe card for weekly departure schedule grid. **Contains `HA_TOKEN` — do not commit to version control.** |
| `/media/pi/NextCloud/homeassistant/pyscript/ev_schedule_data.json` | Persisted weekly departure schedule (written on every change, read on startup to restore `input_text.ev_weekly_schedule`). |

Local working copies (for editing before SCP transfer):
- `/tmp/ev_optimizer_pkg.yaml`
- `/tmp/ev_optimizer.py`
- `/tmp/ev_control_loop.py`
- `/tmp/ev_schedule_grid.html`

**Editing workflow** (files are root-owned on debian):
```bash
# Edit locally, then:
sshpass -p [YOUR_SSH_PASSWORD] scp /tmp/myfile.py pi@[YOUR_HA_HOST]:/tmp/
ssh pi@[YOUR_HA_HOST] "echo [YOUR_SSH_PASSWORD] | sudo -S cp /tmp/myfile.py /media/pi/NextCloud/homeassistant/pyscript/"
```

### configuration.yaml additions
```yaml
pyscript:
  allow_all_imports: true
  hass_is_global: true

homeassistant:
  packages: !include_dir_named packages

input_datetime:
  ev_deadline:
    name: EV Departure Deadline
    has_date: true
    has_time: true
  ev_last_state_change:
    name: EV Last Charger State Change
    has_date: true
    has_time: true

input_number:
  ev_required_kwh:
    name: EV Required kWh
    min: 0
    max: 40
    step: 0.5
    unit_of_measurement: kWh

input_select:
  ev_charging_mode:
    name: EV Charging Mode
    options:
      - Smart
      - Charge now
      - Stop
    initial: Smart

input_text:
  ev_decision_reason:
    name: EV Decision Reason
    max: 255
```

## Entity Reference

### Physical / Integration Entities

| Entity | Description | Unit |
|--------|-------------|------|
| `sensor.nordpool_kwh_se3_sek_3_10_025` | Nordpool electricity price | SEK/kWh |
| `sensor.laddbox_charge_power` | Zaptec charger active power | W |
| `sensor.laddbox_session_total_charge` | Energy delivered this session | kWh |
| `sensor.laddbox_charger_mode` | Charger state string | — |
| `switch.laddbox_charging` | Charging on/off switch | — |
| `number.laddbox_charger_max_current` | Current limit | A (0–25) |
| `sensor.jbb78w_state_of_charge` | Mercedes battery SoC | % |
| `binary_sensor.jbb78w_charging_active` | Car reports active charging | on/off |
| `sensor.jbb78w_charging_status` | Car charging status string | — |
| `sensor.tibber_pulse_dianavagen_15_power` | House total power | W |
| `sensor.tibber_pulse_dianavagen_15_accumulated_consumption_current_hour` | kWh consumed since start of current hour | kWh |
| `sensor.tibber_pulse_dianavagen_15_average_power` | Whole-house average draw (W) — used by consumption guard | W |

### Input Helpers (created by this project)

| Entity | Description |
|--------|-------------|
| `input_datetime.ev_deadline` | Departure deadline for charge target |
| `input_datetime.ev_last_state_change` | Timestamp of last charger on/off transition |
| `input_number.ev_required_kwh` | Energy needed for next trip (0 = auto from SoC) |
| `input_select.ev_charging_mode` | Smart / Charge now / Stop |
| `input_text.ev_decision_reason` | Human-readable last decision (255 chars) |
| `input_number.ev_max_hourly_kwh` | Hourly consumption cap for tariff guard (kWh, default 5.0) |
| `input_number.ev_max_tariff_power_kw` | Max charge power during tariff hours (kW, default 3.0) |
| `input_boolean.ev_tariff_guard_enabled` | Enable/disable both tariff guards (default on) |
| `input_boolean.ev_auto_deadline` | Enable auto-deadline from weekly schedule (default on) |
| `input_text.ev_weekly_schedule` | JSON weekly departure schedule, max 255 chars (e.g. `{"mon":["08:00","18:00"],"sat":[],...}`) |

### Computed Template Sensors (packages/ev_optimizer.yaml)

| Entity | Description | Unit |
|--------|-------------|------|
| `sensor.ev_charging_power_kw` | Active charge power (defaults to 7 kW when idle) | kW |
| `sensor.ev_remaining_kwh` | kWh still needed to reach target | kWh |
| `sensor.ev_slots_needed` | 15-min slots needed to deliver remaining kWh | slots |
| `sensor.ev_slots_available` | 15-min slots until deadline (999 if no deadline) | slots |
| `binary_sensor.ev_deadline_pressure` | True when slots_available <= slots_needed + 1 | — |

### Optimizer Output

| Entity | Description |
|--------|-------------|
| `sensor.ev_schedule` | State = JSON list of charging windows; rich attributes |

## Nordpool Price Data Structure

`state.getattr("sensor.nordpool_kwh_se3_sek_3_10_025")` returns:

```python
{
  "today":          [float, ...],   # 96 values, index 0 = 00:00 local, step 15 min
  "tomorrow":       [float, ...],   # 96 values (only valid when tomorrow_valid=True)
  "tomorrow_valid": bool,
  "raw_today":      [{"start": "2026-04-24T00:00:00+02:00", "end": "...", "value": 0.521}, ...],
  "raw_tomorrow":   [...],
  "current_price":  float,          # current slot price
  ...
}
```

Key facts:
- Prices are in SEK/kWh (raw, no tax)
- Negative prices are possible (overnight solar surplus)
- `today[0]` = midnight local Stockholm time
- Tomorrow data typically becomes available around 13:00 local time

## Zaptec Service Calls

```yaml
# Start charging  (charger_id = Zaptec API UUID; NOT the HA device registry ID)
service: zaptec.resume_charging
data:
  charger_id: d0775df2-6290-4569-bcd0-b579f8b9dde3

# Stop charging
service: zaptec.stop_charging
data:
  charger_id: d0775df2-6290-4569-bcd0-b579f8b9dde3

# Throttle current  (installation-level; use installation_id, NOT device_id)
service: zaptec.limit_current
data:
  installation_id: dcead66e-4c50-4763-bc17-6ef3efe8be1f
  available_current: 10   # amps per phase, 0–25 (circuit max)
```

Zaptec integration: v0.8.6, entity prefix `laddbox`, Installation ID: `dcead66e-4c50-4763-bc17-6ef3efe8be1f`.

**IMPORTANT — parameter names**: `resume_charging` / `stop_charging` take `charger_id` (Zaptec API UUID), NOT `device_id` (HA device registry). Using `device_id` causes "Unable to find device" errors. `limit_current` takes `installation_id` (Zaptec API UUID), NOT `device_id`. These are all Zaptec API identifiers — the HA internal device registry ID is different (`5a4975267833e4f5f8589c76831d4526`).

**Deprecation**: `resume_charging` / `stop_charging` are deprecated as of current Zaptec integration version. HA warns to use `button.laddbox_resume_charging` / `button.laddbox_stop_charging` instead. However these button entities show as `unavailable` when no car is connected, making them unreliable for programmatic use. Service calls still work.

`sensor.laddbox_charger_mode` values observed: `charging`, `connected_finished`, `disconnected`.
`switch.laddbox_charging` is unavailable when charger_mode = `connected_finished` — use the service calls instead.

## Architecture Summary

The optimizer has three cooperating layers. The **schedule layer** (`ev_optimizer.py`) runs as a pyscript service (`pyscript.ev_optimizer_recompute`) and is triggered on every Nordpool price update, deadline change, energy target change, tariff guard toggle/change, weekly schedule change, and once per hour as a failsafe. When `input_boolean.ev_auto_deadline` is on, the schedule layer reads `input_text.ev_weekly_schedule` JSON every 5 minutes via `_auto_deadline_tick` and advances `input_datetime.ev_deadline` to the next upcoming departure time (≥ 15 minutes away) automatically. Days with empty time lists trigger opportunistic mode (no deadline, charges at cheapest available slots in the next 48 h). The auto-deadline is also refreshed at the start of every `ev_optimizer_recompute()` call (startup, Nordpool update, etc.) to ensure the deadline is always current. When `ev_auto_deadline` is off, `ev_deadline` is fully manual. It reads the 15-minute Nordpool slot prices for today (and tomorrow if valid), filters to slots within the eligible window (now → deadline, or 48-hour horizon in opportunistic mode), and selects the cheapest N slots needed to deliver the required kWh. Effective charging power is **time-aware**: overnight slots (outside 06:00–22:00 local) deliver `sensor.ev_charging_power_kw × 0.25` kWh each (typically 1.75 kWh at 7 kW); daytime tariff-hour slots deliver `input_number.ev_max_tariff_power_kw × 0.25` kWh each (typically 0.75 kWh at 3 kW) when `input_boolean.ev_tariff_guard_enabled` is on. N is computed by walking the cheapest-first eligible slots, accumulating effective kWh until the target is met — overnight slots cover the target in fewer slots and are therefore preferred both on price and energy-per-slot. `expected_cost` and `total_kwh` in the schedule attributes are calculated per slot at the correct effective power. The optimizer applies an anti-toggling pass to merge isolated single slots with cheap neighbours (within 20% of average price), and writes the resulting windows as a JSON list to `sensor.ev_schedule`.

The **control layer** (`ev_control_loop.py`) runs every 5 minutes, on pyscript startup, and immediately whenever `sensor.laddbox_charger_mode` transitions to `"charging"`. The reactive charge-start trigger (`ev_control_on_charge_start`) closes the gap between car connection (when Zaptec may auto-start) and the next scheduled tick, stopping unauthorised charging within seconds. All three entry points share the same decision tree: manual override modes (Charge now / Stop) take absolute priority; then it checks whether the current time falls inside any scheduled window; deadline pressure (`binary_sensor.ev_deadline_pressure`) can force charging on even outside a window; a 15-minute hysteresis guard prevents rapid toggling; and finally a **consumption guard** blocks charging if the projected hourly kWh would exceed `input_number.ev_max_hourly_kwh`. The guard only applies during Ellevio tariff hours (06:00–22:00 local time) and can be disabled via `input_boolean.ev_tariff_guard_enabled`. All state changes write a human-readable reason to `input_text.ev_decision_reason`.

The **safety layer** (automations in `packages/ev_optimizer.yaml`) runs independently of pyscript as a hard-stop backstop. Four automations cover: energy target reached (ev_remaining_kwh < 0.2 kWh), SoC target reached (jbb78w_state_of_charge > 95%), departure deadline passed (resets deadline to 2099 to prevent re-trigger), and Nordpool data unavailable. These automations do not conflict with the existing 8 HA automations because they use different triggers and conditions.

## Known Quirks

1. **Pyscript AST restrictions**: Generator expressions are not supported. Always use list comprehensions: `sum([x for x in items])` not `sum(x for x in items)`. Affects avg_px, sorted_px, total_kwh, total_cost calculations.

2. **Pyscript lambda closure bug**: Lambdas cannot close over local variables from an enclosing scope — raises `NameError` at runtime. Always capture enclosing variables via a default argument: `lambda i, d=by_idx: d[i]["price"]` instead of `lambda i: by_idx[i]["price"]`. Note: lambdas using only their own parameters (e.g. `lambda s: s["price"]`) are fine.

3. **state.getattr() single-argument only**: `state.getattr(entity, "attr")` raises TypeError. Always call `state.getattr(entity)` to get the full attributes dict, then use `.get("key")`.

4. **ev_schedule state is the JSON string**: `state.get("sensor.ev_schedule")` returns the JSON directly. In the control loop, parse it with `json.loads(sched_state)`. Do not try to read it from attributes. The state contains compact epoch windows `[{"s": start_epoch, "e": end_epoch}, ...]` (~30 chars each) to stay within HA's 255-char state limit. Full window data (ISO times, price, slots, kwh, cost) is in `attributes.schedule` for Lovelace display. The `cost` field is per-window accurate cost (sum of `price × effective_power_kw × 0.25` per slot) — not flat `price × kwh`.

5. **input_datetime defaults**: `ev_deadline` defaults to midnight today (past timestamp), so `ev_deadline_pressure` will be `True` and `ev_slots_available` will be 0 until a real future deadline is set. This is expected and harmless — the control loop falls through to the schedule check.

6. **ev_decision_reason shows "No schedule available"**: This is normal on first start before `pyscript.ev_optimizer_recompute` has run or before Nordpool data is available. It also appears when ev_required_kwh=0 and SoC=100% (nothing to charge).

7. **Pyscript sensor state lost on restart**: `state.set()` values (like `sensor.ev_schedule`) are held in pyscript's in-memory state and are not persisted across HA restarts. After a restart, `sensor.ev_schedule` returns to `unknown` until a trigger fires. A `@time_trigger("startup")` function `_ev_recompute_on_startup` was added to `ev_optimizer.py` to recompute immediately on startup and restore the sensor.

8. **Pyscript not loading**: If pyscript services are absent from `/api/services`, the `pyscript:` block is missing from configuration.yaml, or HA needs a full restart (not just reload). Verify with `GET /api/config` → check `components` list contains `pyscript`.

9. **configuration.yaml is root-owned**: All edits on debian require `sudo`. Use the SCP + sudo cp workflow. Avoid editing directly with `sudo nano` over SSH from a subagent (no TTY).

10. **Zaptec service parameter names**: `resume_charging` and `stop_charging` require `charger_id` (Zaptec UUID `d0775df2-...`), NOT `device_id`. Using `device_id` with the Zaptec UUID causes "Unable to find device" because HA's service registry expects the HA internal device registry ID. `limit_current` requires `installation_id` (Zaptec UUID `dcead66e-...`). The pyscript code and safety automations all use `charger_id`/`installation_id`.

11. **Tariff hour current limiting (Option A)**: `set_charger(True)` always calls `_apply_current_limit()` after starting the charger. Inside tariff hours (06:00–22:00) with guard enabled: throttles to `ev_max_tariff_power_kw` kW, converted via `int(kW * 1000 / (400 × 1.732))` for 3-phase 400V. Result clamped to min 6A (Zaptec won't charge below), max 25A (circuit max). At 3.0 kW = 4.3A → clamped to 6A = 4.16 kW effective minimum. Outside tariff hours or guard disabled: restores to 25A (full power). Uses `installation_id` with `zaptec.limit_current`. **Disable `input_boolean.ev_tariff_guard_enabled` on 2026-06-01** when Ellevio scraps the power tariff scheme.

12. **Consumption guard projection**: projects kWh for the remainder of the current hour as `accumulated_kwh + (house_kw + ev_kw) × (remaining_minutes / 60)`. Both Tibber sensors (`accumulated_consumption_current_hour` and `average_power`) must be available — if either is `unavailable`/`unknown` the guard is skipped (fail-open). The `average_power` sensor reflects whole-house draw; since the EV is OFF at evaluation time, adding `ev_charging_power_kw` gives the correct projected total. Guard only runs when `desired == True` (i.e. the schedule says charge) — it is never applied to stops. **Disable `input_boolean.ev_tariff_guard_enabled` on 2026-06-01** when Ellevio scraps the power tariff scheme.

13. **Hysteresis timestamp format**: `ev_last_state_change` state is "YYYY-MM-DD HH:MM:SS" in local Stockholm time (no UTC offset). Parse with `strptime(..., "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_LOCAL)`.

15. **`input_text` YAML max is 255**: HA's YAML-defined `input_text` helpers enforce `max ≤ 255`. The `ev_weekly_schedule` entity uses `max: 255`. The default schedule is ~100 chars; 2 departure times per day across all days is ~160 chars; 3 per day is ~220 chars. Compact JSON (no extra spaces) is required for complex schedules.

16. **Auto-deadline overrides manual ev_deadline changes**: When `input_boolean.ev_auto_deadline` is on, `ev_optimizer_recompute()` calls `auto_set_deadline()` before reading the deadline. Any manual write to `input_datetime.ev_deadline` triggers `_ev_recompute_on_change`, which immediately calls `ev_optimizer_recompute()`, which overwrites the deadline with the auto-computed next departure. To manually override the deadline, first turn `ev_auto_deadline` off.

17. **Weekly schedule 15-minute minimum look-ahead**: `get_next_departure()` skips any departure time that is fewer than 15 minutes in the future (`< now + 15 min`). This prevents scheduling for an imminent departure that leaves no time to charge. Rollover after a departure passes happens within 5 minutes via the `_auto_deadline_tick`.

14. **`is_state()` not available in pyscript**: `is_state(entity_id, state)` is a Jinja2 template helper and does NOT exist in pyscript. Use `(state.get(entity_id) or "default") == "value"` instead. Example: `(state.get("input_boolean.ev_tariff_guard_enabled") or "off") == "on"`. This applies everywhere in ev_optimizer.py and ev_control_loop.py.

19. **`input_datetime` timezone bug — always use the `timestamp` attribute**: Pyscript's `state.get("input_datetime.ev_deadline")` may return a UTC string in some HA versions even though the entity logically holds local time. Parsing that string with `.replace(tzinfo=TZ_LOCAL)` then yields a timestamp 2 hours early (CEST = UTC+2). Similarly, Jinja2's `as_timestamp(states('input_datetime.ev_deadline'))` can misinterpret the naive string. **Always use the pre-computed UTC epoch instead**: in pyscript: `(state.getattr(DEADLINE_ENT) or {}).get("timestamp")`; in Jinja2 templates: `state_attr('input_datetime.ev_deadline', 'timestamp')`. Both return a float UTC epoch that is correct regardless of DST.

20. **Weekly schedule persistence via file**: `input_text.ev_weekly_schedule` has no `initial:` value — HA would reset to `initial:` on every restart if one were set. Instead, ev_optimizer.py persists the schedule to `/config/pyscript/ev_schedule_data.json` on every change (`persist_weekly_schedule` state trigger) and restores it from that file on startup (`restore_weekly_schedule` startup trigger). File I/O uses `task.executor(Path(...).read_text)` / `task.executor(Path(...).write_text, data)` — the raw `open()` built-in and `pathlib.Path.read_text()` are not directly callable in pyscript (blocked as blocking I/O on the event loop). **Do not add an `initial:` value** to the YAML definition — it would override the persisted data on every restart.

21. **`input_datetime` state trigger requires `task.sleep(1)`**: When pyscript triggers on `input_datetime.ev_deadline`, `state.getattr(DEADLINE_ENT)` may still return the old value within the same event cycle. The dedicated `on_deadline_changed` function calls `task.sleep(1)` before invoking `ev_optimizer_recompute()` to allow HA state to fully settle. The same applies to `_on_auto_deadline_toggle` — always `task.sleep(1)` before reading state after a toggle.

22. **`sensor.ev_schedule` state uses compact epoch format**: The state string stores windows as `[{"s": start_epoch, "e": end_epoch}, ...]` using compact JSON separators (`separators=(',', ':')`) — approximately 30 chars per window. This replaced the ISO timestamp format (`{"start": "...", "end": "..."}` at ~75 chars/window) which overflowed the 255-char HA state limit at 4+ windows. Full ISO times are still available in `attributes.schedule` for Lovelace display. The control loop (`ev_control_loop.py`) parses windows using `float(window["s"])` and `float(window["e"])` as UTC epoch timestamps.

18. **Schedule grid iframe card** (`/local/ev_schedule_grid.html`): The weekly departure grid is embedded as a `type: iframe` Lovelace card pointing to `/local/ev_schedule_grid.html`. In Docker HA, `/config/www/` maps to `/local/` for browser access. If the card shows blank, check: (a) the file exists at `/media/pi/NextCloud/homeassistant/www/ev_schedule_grid.html` on the host, (b) HA has served it at least once (try opening `https://[YOUR_HA_HOSTNAME]:8123/local/ev_schedule_grid.html` directly), (c) the `HA_TOKEN` constant in the file has been replaced with a valid long-lived access token.

## How to Test

**Verify template sensors are computed:**
```bash
curl -s -H "Authorization: Bearer TOKEN" \
  https://[YOUR_HA_HOSTNAME]:8123/api/states/sensor.ev_remaining_kwh | python3 -m json.tool
```

**Manually trigger schedule recompute:**
```bash
curl -s -X POST -H "Authorization: Bearer TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}' \
  https://[YOUR_HA_HOSTNAME]:8123/api/services/pyscript/ev_optimizer_recompute
```

**Check schedule output:**
```bash
curl -s -H "Authorization: Bearer TOKEN" \
  https://[YOUR_HA_HOSTNAME]:8123/api/states/sensor.ev_schedule | python3 -m json.tool
```

**Check control loop decision:**
```bash
curl -s -H "Authorization: Bearer TOKEN" \
  https://[YOUR_HA_HOSTNAME]:8123/api/states/input_text.ev_decision_reason
```

**Check pyscript logs (inside container):**
```bash
ssh pi@[YOUR_HA_HOST] "docker logs homeassistant 2>&1 | grep ev_optimizer | tail -30"
ssh pi@[YOUR_HA_HOST] "docker logs homeassistant 2>&1 | grep ev_control | tail -30"
```

**Reload pyscript after file changes:**
```bash
curl -s -X POST -H "Authorization: Bearer TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}' \
  https://[YOUR_HA_HOSTNAME]:8123/api/services/pyscript/reload
```

**Full HA restart:**
```bash
ssh pi@[YOUR_HA_HOST] "docker restart homeassistant"
```

**Set a test deadline 2 hours from now and 5 kWh target:**
Developer Tools → States → set `input_datetime.ev_deadline` to a time 2h from now, set `input_number.ev_required_kwh` to 5.0, then trigger recompute. Check `sensor.ev_schedule` for windows.

## Future Work

- **Load balancing**: Read `sensor.tibber_pulse_dianavagen_15_power` (house power) and reduce charging current via `zaptec.limit_current` when house load is high, to stay within fuse limits.
- **Multi-rate tariffs**: The Nordpool price does not include grid tariffs which vary by hour. Incorporate `sensor.electric_nordpool_current_price` (all-in price ×~100 SEK/kWh) for true cost optimization.
- **Dashboard card improvements**: A basic card was added via the HA GUI (storage/UI mode — not a file). HA is in storage mode so card config lives in `/media/pi/NextCloud/homeassistant/.storage/lovelace.lovelace` and must be edited through the GUI or the Lovelace REST API. To recreate the card, add a new Manual card with this YAML:
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
          name: Decision reason
    - type: entities
      title: Status
      entities:
        - entity: sensor.ev_remaining_kwh
        - entity: sensor.ev_slots_needed
        - entity: sensor.ev_slots_available
        - entity: binary_sensor.ev_deadline_pressure
        - entity: sensor.jbb78w_state_of_charge
          name: Car SoC
        - entity: sensor.ev_charging_power_kw
    - type: attribute
      entity: sensor.ev_schedule
      attribute: schedule
      name: Charging schedule
  ```
  Window start/end times are stored in local Stockholm time with `+02:00` / `+01:00` offset. To show only HH:MM in a template: `{{ window.start[11:16] }}`.
- **Adaptive power**: Zaptec GO supports per-phase current control. Could charge at reduced rate during moderately-priced slots rather than full on/off.
- **Notification on missed target**: If EV departures without reaching target SoC, send a mobile notification.
- **Solar integration**: If a solar inverter sensor is added, bias slot selection toward midday when solar production is high.
- **Price spike guard**: Add a hard ceiling price (e.g., 2.0 SEK/kWh) above which charging never starts, regardless of schedule or deadline pressure.

## Version Control

| Item | Value |
|------|-------|
| Repo | https://github.com/PetrolHead2/EV-Charging-Optimizer |
| Local | `~/projects/EV-Charging-Optimizer/` |

To sync changes from HA to repo:
```bash
cd ~/projects/EV-Charging-Optimizer
./sync_from_ha.sh
git add .
git commit -m "describe your change"
git push
```

**WARNING**: Never commit the real `HA_TOKEN`.
`sync_from_ha.sh` scrubs it automatically.
Always sync via the script, never copy manually.
