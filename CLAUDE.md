# EV Charging Optimizer — Project Reference

## Infrastructure

| Item | Value |
|------|-------|
| HA host | `debian` (SSH user: `pi`, password: `eankod89`) |
| HA container | `homeassistant` (Docker) |
| HA version | 2026.4.3 |
| HA URL | https://koffern.duckdns.org:8123/ |
| Config dir (host) | `/media/pi/NextCloud/homeassistant` |
| Config dir (container) | `/config` |
| Timezone | Europe/Stockholm | Use home assistants time zone instead
| Nordpool region | SE3, SEK/kWh |

All HA config files are owned by root. Use `echo eankod89 | sudo -S` for writes on debian.

To restart HA: `ssh pi@debian "docker restart homeassistant"`

To reload pyscript without restart: call service `pyscript.reload` via HA API or Developer Tools.

To reload input helpers: call `input_datetime/reload`, `input_number/reload`, `input_select/reload`, `input_text/reload` individually — `homeassistant/reload_all` does NOT reload these domains.

Connect to HA at https:///koffern.duckdns.org:8123/ using the long-lived token eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiI5N2U2MDM3OWY2ZGI0Yjg5OWFhMWIyZWJmYWQ4MGRlYSIsImlhdCI6MTc3NzAyMzk4OSwiZXhwIjoyMDkyMzgzOTg5fQ.jowul0Q2pQq0t27x6CvXPKHbq8ifh1-gfRe8iM9QEOo

the home assistant is a docker installation on the machine "debian" wich is ssh accessable with user "pi" and password "eankod89" where every a password is needed start with trying "eankod89"
[YOUR_SSH_PASSWORD]=eankod89
[YOUR_HA_HOST]=debian
[YOUR_HA_HOSTNAME]=koffern.duckdns.org
HA_TOKEN=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiI5N2U2MDM3OWY2ZGI0Yjg5OWFhMWIyZWJmYWQ4MGRlYSIsImlhdCI6MTc3NzAyMzk4OSwiZXhwIjoyMDkyMzgzOTg5fQ.jowul0Q2pQq0t27x6CvXPKHbq8ifh1-gfRe8iM9QEOo


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
| `input_datetime.ev_deadline` | **Human input only** — manual departure override; leave at past/sentinel for fully automatic operation. Pyscript never writes this entity. |
| `input_datetime.ev_computed_deadline` | **Pyscript only** — next departure computed from weekly schedule by `auto_set_deadline()`; never shown as an editable field. `get_effective_deadline()` reads this as the fallback when `ev_deadline` is not in the future. |

### Computed Template Sensors (packages/ev_optimizer.yaml)

| Entity | Description | Unit |
|--------|-------------|------|
| `sensor.ev_charging_power_kw` | Active charge power (defaults to 7 kW when idle) | kW |
| `sensor.ev_remaining_kwh` | kWh still needed to reach target | kWh |
| `sensor.ev_slots_needed` | 15-min slots needed to deliver remaining kWh | slots |
| `sensor.ev_slots_available` | 15-min slots until deadline (999 if no deadline) | slots |
| `binary_sensor.ev_deadline_pressure` | True when deadline set AND slots_needed > 0 AND slots_available <= slots_needed + 1 | — |

### Optimizer Output

| Entity | Description |
|--------|-------------|
| `sensor.ev_schedule` | State = JSON list of charging windows; rich attributes |

## Nordpool Price Data Structure

`state.getattr("sensor.nordpool_kwh_se3_sek_3_10_025")` returns:

```python
{
  "today":          [float, ...],   # 96 values, index 0 = 00:00 local, step 15 min (same data as raw_today values)
  "tomorrow":       [float, ...],   # 96 values (only valid when tomorrow_valid=True)
  "tomorrow_valid": bool,
  "raw_today":      [{"start": "2026-04-24T00:00:00+02:00", "end": "2026-04-24T00:15:00+02:00", "value": 0.521}, ...],
  "raw_tomorrow":   [...],          # 96 dicts, 15-min slots (same prices as tomorrow[], with explicit timestamps)
  "current_price":  float,          # current slot price
  ...
}
```

Key facts:
- `today`/`tomorrow` flat arrays and `raw_today`/`raw_tomorrow` contain **identical price data** — both have 96 15-minute entries per day.
- `_build_slots()` uses `raw_today`/`raw_tomorrow` (explicit UTC timestamps) rather than the flat arrays — this is more robust against indexing bugs and DST edge cases.
- **Cheap afternoon slots outside the deadline are correctly excluded** — if tomorrow's departure is 09:00, slots at 15:00-16:00 (even at 0.08 SEK/kWh) are after the deadline and will not appear in the schedule. This is expected behavior, not a bug.
- `compute_schedule()` logs the 5 cheapest eligible slots on every recompute at INFO level — check `home-assistant.log` with pyscript at DEBUG/INFO to verify pool contents.

Key facts:
- Prices are in SEK/kWh (raw, no tax)
- Negative prices are possible (overnight solar surplus)
- `today[0]` = midnight local Stockholm time
- Tomorrow data typically becomes available around 13:00 local time

## Zaptec Device Structure

Two separate Zaptec devices — each has distinct entities and distinct responsibilities:

### Device 1: Zaptec Go "Laddbox" (charger / session level)

| Entity | Description |
|--------|-------------|
| `switch.laddbox_charging` | Session ON/OFF — **use this for start/stop** |
| `number.laddbox_charger_max_current` | Per-session current limit (A); throttle here for tariff hours |
| `number.laddbox_charger_min_current` | Per-session minimum current (A) |
| `sensor.laddbox_charger_mode` | Charger state string (see states table below) |
| `sensor.laddbox_charge_power` | Active session power (W) |
| `sensor.laddbox_session_total_charge` | Energy delivered this session (kWh) |
| `sensor.laddbox_allocated_charge_current` | Currently allocated current (A) |
| `button.laddbox_resume_charging` | **Deprecated in control loop** — was unreliable; use switch instead |
| `button.laddbox_stop_charging` | **Deprecated in control loop** — was unreliable; use switch instead |

### Device 2: Zaptec Installation "Magnus Niemi" (circuit level)

| Entity | Description |
|--------|-------------|
| `number.magnus_niemi_available_current` | Circuit available current (A) — set via `zaptec.limit_current` |
| `sensor.magnus_niemi_available_current_phase_1/2/3` | Per-phase read-back sensors |
| `sensor.magnus_niemi_max_current` | Circuit maximum (25A) |
| `sensor.magnus_niemi_network_type` | `tn_3_phase` |

Installation ID: `dcead66e-4c50-4763-bc17-6ef3efe8be1f` (Zaptec API UUID, used only by `zaptec.limit_current` if called from automations/Developer Tools — **not called from ev_control_loop.py**).

**RULE: The control loop NEVER writes to magnus_niemi entities.** Circuit-level changes affect all appliances on the circuit. Tariff throttling is done session-level via `number.laddbox_charger_max_current` only.

To reset the installation from the command line (if stuck): `curl -X POST [HA_URL]/api/services/zaptec/limit_current -H "Authorization: Bearer [TOKEN]" -H "Content-Type: application/json" -d '{"installation_id":"dcead66e-4c50-4763-bc17-6ef3efe8be1f","available_current":25}'`

### `sensor.laddbox_charger_mode` state enum

| State | Meaning | CONNECTED? |
|-------|---------|------------|
| `disconnected` | No car present | No |
| `Disconnected` | Firmware alternate label | No |
| `unknown` / `unavailable` | Transitioning | No (wait 3 s) |
| `connected_requesting` | Car connected, awaiting start | Yes |
| `Waiting` | Firmware alternate label for requesting | Yes |
| `connected_charging` | Session active, delivering energy | Yes (charging) |
| `Charging` | Firmware alternate label | Yes (charging) |
| `connected_finished` | Session ended (BMS, stop cmd, or optimizer) | Yes |
| `connected_finishing` | Session winding down | Yes |
| `paused` | Session paused | Yes |

Active charging states (`CHARGING_STATES`): `connected_charging`, `Charging`
Chargeable states (`CHARGEABLE_STATES`): all "Yes" rows that are not charging
`CONNECTED_STATES = CHARGEABLE_STATES | CHARGING_STATES`

## Architecture Summary

The optimizer has three cooperating layers. The **schedule layer** (`ev_optimizer.py`) runs as a pyscript service (`pyscript.ev_optimizer_recompute`) and is triggered on every Nordpool price update, deadline change, energy target change, tariff guard toggle/change, weekly schedule change, and once per hour as a failsafe. When `input_boolean.ev_auto_deadline` is on, the schedule layer reads `input_text.ev_weekly_schedule` JSON every 5 minutes via `_auto_deadline_tick` and writes the next upcoming departure to `input_datetime.ev_computed_deadline` (≥ 15 minutes away). Days with empty time lists write the 2099 sentinel, triggering opportunistic mode. `get_effective_deadline()` collects ALL valid future deadlines (manual + auto computed) and returns the **nearest** one — whichever departure is soonest wins, regardless of source. This means a manual `ev_deadline` set for a distant trip does NOT suppress the auto weekly schedule; the optimizer charges for the weekly departures first and automatically switches to the manual deadline once the weekly departures have passed. Multiple trips can be planned simultaneously — the system chains from one deadline to the next as each passes. **`input_datetime.ev_deadline` is NEVER written by pyscript** — it belongs to the user as a manual override. When `ev_auto_deadline` is off, `ev_computed_deadline` is not updated and the optimizer relies on `ev_deadline` if set. It reads the 15-minute Nordpool slot prices for today (and tomorrow if valid), filters to slots within the eligible window (now → deadline, or 48-hour horizon in opportunistic mode), and selects the cheapest N slots needed to deliver the required kWh. Effective charging power is **time-aware**: overnight slots (outside 06:00–22:00 local) deliver `sensor.ev_charging_power_kw × 0.25` kWh each (typically 1.75 kWh at 7 kW); daytime tariff-hour slots deliver `input_number.ev_max_tariff_power_kw × 0.25` kWh each (typically 0.75 kWh at 3 kW) when `input_boolean.ev_tariff_guard_enabled` is on. N is computed by walking the cheapest-first eligible slots, accumulating effective kWh until the target is met — overnight slots cover the target in fewer slots and are therefore preferred both on price and energy-per-slot. `expected_cost` and `total_kwh` in the schedule attributes are calculated per slot at the correct effective power. The optimizer applies an anti-toggling pass to merge isolated single slots with cheap neighbours (within 20% of average price), and writes the resulting windows as a JSON list to `sensor.ev_schedule`.

The **control layer** (`ev_control_loop.py`) runs every 5 minutes, on pyscript startup, and immediately whenever `sensor.laddbox_charger_mode` transitions to `"charging"`. The reactive charge-start trigger (`ev_control_on_charge_start`) closes the gap between car connection (when Zaptec may auto-start) and the next scheduled tick, stopping unauthorised charging within seconds. All three entry points share the same decision tree: manual override modes (Charge now / Stop) take absolute priority; then it checks whether the current time falls inside any scheduled window; deadline pressure (`binary_sensor.ev_deadline_pressure`) can force charging on even outside a window; a 15-minute hysteresis guard prevents rapid toggling; and finally a **consumption guard** blocks charging if the projected hourly kWh would exceed `input_number.ev_max_hourly_kwh`. When the guard fires it also sets `input_boolean.ev_consumption_guard_active = on` (cooldown) and sleeps until the next hour boundary — subsequent ticks see the cooldown flag and hold charging OFF without re-running the projection, preventing a toggle loop. The guard only applies during Ellevio tariff hours (06:00–22:00 local time) and can be disabled via `input_boolean.ev_tariff_guard_enabled`. All state changes write a human-readable reason to `input_text.ev_decision_reason`.

The **safety layer** (automations in `packages/ev_optimizer.yaml`) runs independently of pyscript as a hard-stop backstop. Three automations cover: energy target reached (ev_remaining_kwh < 0.2 kWh), departure deadline passed (resets deadline to 2000-01-01 to prevent re-trigger), and Nordpool data unavailable. The Mercedes Me BMS manages battery charge termination natively — HA does not interfere with SoC. The SoC safety stop was deliberately removed on 2026-04-26. These automations do not conflict with the existing HA automations because they use different triggers and conditions.

## Known Quirks

31. **Automation `id:` must be a numeric quoted string for HA UI editability (2026-04-27)**: HA only exposes automations as editable in **Settings → Automations** when the `id:` field is a numeric string (e.g. `"1745760001"`). String IDs like `ev_safety_target_reached` load and function correctly but do not appear in the UI editor. All four safety automations were changed to epoch-based numeric IDs. **`_2` suffix problem**: when IDs change, HA's entity registry still holds the old string-ID entries; new numeric-ID automations then get `_2` appended to their entity_id because the alias-derived object_id is already claimed. Fix requires stopping HA and editing `.storage/core.entity_registry` and `.storage/core.restore_state` while the container is down: (1) remove old string-ID entries, (2) rename `_2` entries to drop the suffix, (3) restart. Ghost automation entities (`automation.ev_safety_soc_target_reached`, `automation.ev_safety_deadline_passed`) persist in the registry until explicitly removed — they do NOT disappear on their own after a restart.

32. **Safety automation condition uses `connected_charging` (2026-04-27)**: The `ev_safety_target_reached` automation had `state: "charging"` in its condition, which never matched because Zaptec v0.8.x reports the active state as `"connected_charging"`. The condition was corrected to `state: "connected_charging"`. Without this fix the safety stop for energy-target-reached would silently never fire.

33. **`should_charge_now()` uses `.get()` with fallbacks for window keys (2026-04-27)**: The original code used `window["s"]` (hard key access). If `"s"` is missing (stale schedule from before compact-format migration), a `KeyError` is raised, caught by the `except` block, and the window is silently skipped — causing `should_charge_now()` to return `False` for a window that is active. Fixed to `window.get("s", window.get("start_ts", 0))` so a missing key is always tolerated. The active-window comparison also uses a -900s lookback (`w_start >= now_ts - 900`) to mirror the optimizer's slot filter and ensure a window that opened seconds before the tick is never missed due to timing skew.

34. **`button.laddbox_resume_charging` stays `unavailable` briefly after plug-in (2026-04-27)**: When the car is first plugged in, Zaptec transitions `disconnected → connected_requesting`, which makes `CONNECTED_STATES` check pass and lets the control loop proceed. But the button entity may still be `unavailable` for 1–5 s after the charger mode settles, producing the HA warning `"Referenced entities button.laddbox_resume_charging are missing or not currently available"` and silently failing to start charging. Fix: `set_charger(True, ...)` now checks `state.get(RESUME_BTN_ENT) == "unavailable"` before pressing; if unavailable, it waits 5 s and re-checks once. If still unavailable it writes `[btn unavailable]` to `ev_decision_reason` and returns — the next 5-minute tick will retry.

1. **Pyscript AST restrictions**: Generator expressions are not supported. Always use list comprehensions: `sum([x for x in items])` not `sum(x for x in items)`. Affects avg_px, sorted_px, total_kwh, total_cost calculations.

2. **Pyscript lambda closure bug**: Lambdas cannot close over local variables from an enclosing scope — raises `NameError` at runtime. Always capture enclosing variables via a default argument: `lambda i, d=by_idx: d[i]["price"]` instead of `lambda i: by_idx[i]["price"]`. Note: lambdas using only their own parameters (e.g. `lambda s: s["price"]`) are fine.

3. **state.getattr() single-argument only**: `state.getattr(entity, "attr")` raises TypeError. Always call `state.getattr(entity)` to get the full attributes dict, then use `.get("key")`.

4. **ev_schedule state is the JSON string**: `state.get("sensor.ev_schedule")` returns the JSON directly. In the control loop, parse it with `json.loads(sched_state)`. Do not try to read it from attributes. The state contains compact epoch windows `[{"s": start_epoch, "e": end_epoch}, ...]` (~30 chars each) to stay within HA's 255-char state limit. Full window data (ISO times, price, slots, kwh, cost) is in `attributes.schedule` for Lovelace display. The `cost` field is per-window accurate cost (sum of `price × effective_power_kw × 0.25` per slot) — not flat `price × kwh`.

5. **input_datetime defaults**: `ev_deadline` defaults to midnight today (past timestamp), so `ev_deadline_pressure` will be `True` and `ev_slots_available` will be 0 until a real future deadline is set. This is expected and harmless — the control loop falls through to the schedule check.

6. **ev_decision_reason shows "No schedule available"**: This is normal on first start before `pyscript.ev_optimizer_recompute` has run or before Nordpool data is available. It also appears when ev_required_kwh=0 and SoC=100% (nothing to charge).

7. **Pyscript sensor state lost on restart**: `state.set()` values (like `sensor.ev_schedule`) are held in pyscript's in-memory state and are not persisted across HA restarts. After a restart, `sensor.ev_schedule` returns to `unknown` until a trigger fires. A `@time_trigger("startup")` function `_ev_recompute_on_startup` was added to `ev_optimizer.py` to recompute immediately on startup and restore the sensor.

8. **Pyscript not loading**: If pyscript services are absent from `/api/services`, the `pyscript:` block is missing from configuration.yaml, or HA needs a full restart (not just reload). Verify with `GET /api/config` → check `components` list contains `pyscript`.

9. **configuration.yaml is root-owned**: All edits on debian require `sudo`. Use the SCP + sudo cp workflow. Avoid editing directly with `sudo nano` over SSH from a subagent (no TTY).

10. **Zaptec start/stop use `switch.laddbox_charging`, NOT button entities (2026-04-27)**: `ev_control_loop.py` uses `switch.turn_on/off(entity_id="switch.laddbox_charging")` for session control. Earlier iterations used the deprecated `zaptec.resume_charging` / `zaptec.stop_charging` services (raised `ValueError`), then tried `button.laddbox_resume_charging` / `button.laddbox_stop_charging` (remained `unavailable` 1-5 s after plug-in). The switch entity is the correct approach. Current throttling for tariff hours uses `number.laddbox_charger_max_current` (session-level) — never the installation-level `zaptec.limit_current`. The safety automations in `ev_optimizer.yaml` still use `zaptec.stop_charging` directly and continue to work.

29. **Connection guard skips all Zaptec calls when no car connected (2026-04-27)**: `ev_control_loop()` checks `sensor.laddbox_charger_mode` against `CONNECTED_STATES = {connected_requesting, connected_charging, connected_finished}` before any other logic. If the state is `disconnected` or `unknown`, the function writes `"No car connected ({state})"` to `ev_decision_reason` and returns immediately — no Zaptec button or service is called. `on_zaptec_state_changed()` applies the same check: if the new state is not in `CONNECTED_STATES`, it updates the reason and returns without running the control loop. This prevents `button.press` calls against `unavailable` entities and eliminates spurious log noise on every 5-minute tick when the car is unplugged.

30. **`_is_charging()` checks `connected_charging` not `charging` (2026-04-27)**: The Zaptec integration v0.8.x reports the active charging state as `"connected_charging"`, not plain `"charging"`. The old check `str(mode).lower() == "charging"` always returned False, causing `set_charger()` to see `on != current` on every tick even when the charger was already running — triggering spurious `resume_charging` calls that failed with `ValueError`. Fixed to `== "connected_charging"`. The `_is_charging()` return value gates the `on != current` transition check in `set_charger()`, so this fix also eliminates all redundant start/stop service calls during steady-state operation.

11. **`ev_max_tariff_power_kw = 0` disables current throttling (2026-04-27)**: Setting `input_number.ev_max_tariff_power_kw` to 0 disables per-session current throttling entirely. This is the default for this installation because the Mercedes PHEV minimum is 10 A (6.93 kW on 3-phase 400V), which makes sub-7 kW throttling impossible. Protection during tariff hours comes from: (1) the optimizer scheduling only the cheapest price slots, and (2) the Tibber consumption guard stopping charging when the projected hourly kWh would exceed `ev_max_hourly_kwh`. Both `_apply_tariff_current()` (control loop) and `effective_power_kw()` (optimizer) treat 0 kW as "throttling disabled" and use full charging power. Values between 0 and 6.93 kW are also treated as disabled (below car minimum 10 A). If current throttling is ever re-enabled for another car, set `ev_max_tariff_power_kw ≥ 7.0` kW. The infrastructure (`number.laddbox_charger_max_current`) remains in place for portability.

12. **Price-aware consumption guard with latest-start algorithm (2026-04-27)**: `check_consumption_guard()` in `ev_control_loop.py` replaces the old projection-based guard. Algorithm: (1) `headroom = cap - accumulated_kwh`; if ≤ 0, hold until next hour. (2) `max_charge_minutes = headroom / (house_kw + ev_kw) × 60` — how long the EV can run before hitting the cap. (3) `latest_start_minute = 60 - max_charge_minutes` — start this late and finish exactly at the hour. (4) If `now ≥ latest_start`, allow charging (optimal window). (5) If `now < latest_start`, compare current vs next-hour Nordpool price via `get_slot_price()`: next ≥ current×0.95 → hold until latest_start (charge cheaper this hour); next < current×0.95 → skip to next hour. Both Tibber sensors must be available — fail-open if not. Guard runs at priority step 4, ALWAYS (even during deadline pressure) — if guard holds + deadline pressure active, writes combined reason. `input_boolean.ev_consumption_guard_active` remains in YAML but is no longer set/cleared by this function (it exists as a dormant Lovelace indicator). **Disable `input_boolean.ev_tariff_guard_enabled` on 2026-06-01** when Ellevio scraps the power tariff scheme.

13. **Hysteresis timestamp format**: `ev_last_state_change` state is "YYYY-MM-DD HH:MM:SS" in local Stockholm time (no UTC offset). Parse with `strptime(..., "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_LOCAL)`.

15. **`input_text` YAML max is 255**: HA's YAML-defined `input_text` helpers enforce `max ≤ 255`. The `ev_weekly_schedule` entity uses `max: 255`. The default schedule is ~100 chars; 2 departure times per day across all days is ~160 chars; 3 per day is ~220 chars. Compact JSON (no extra spaces) is required for complex schedules.

16. **Two deadline entities: human input vs pyscript computed**: `input_datetime.ev_deadline` is the human-only manual override — pyscript never writes it. `input_datetime.ev_computed_deadline` is written only by `auto_set_deadline()` and never shown as editable in the UI. `get_effective_deadline()` collects both entities and returns the nearest valid future deadline (nearest-wins, not manual-priority). This means setting a manual deadline for a long trip does not suppress the weekly auto schedule — the auto deadlines are used first, and the manual deadline is used once they have passed. To plan a trip beyond the weekly schedule, set `ev_deadline` to that trip's departure time; the system will handle weekly departures automatically and then switch to the manual deadline. To clear the manual deadline and rely solely on the weekly schedule, set `ev_deadline` to a past date (e.g. 2000-01-01). The optimizer will then use `ev_computed_deadline` from the weekly schedule automatically.

17. **Weekly schedule 15-minute minimum look-ahead**: `get_next_departure()` skips any departure time that is fewer than 15 minutes in the future (`< now + 15 min`). This prevents scheduling for an imminent departure that leaves no time to charge. Rollover after a departure passes happens within 5 minutes via the `_auto_deadline_tick`.

14. **`is_state()` not available in pyscript**: `is_state(entity_id, state)` is a Jinja2 template helper and does NOT exist in pyscript. Use `(state.get(entity_id) or "default") == "value"` instead. Example: `(state.get("input_boolean.ev_tariff_guard_enabled") or "off") == "on"`. This applies everywhere in ev_optimizer.py and ev_control_loop.py.

19. **`input_datetime` timezone bug — always use the `timestamp` attribute**: Pyscript's `state.get("input_datetime.ev_deadline")` may return a UTC string in some HA versions even though the entity logically holds local time. Parsing that string with `.replace(tzinfo=TZ_LOCAL)` then yields a timestamp 2 hours early (CEST = UTC+2). Similarly, Jinja2's `as_timestamp(states('input_datetime.ev_deadline'))` can misinterpret the naive string. **Always use the pre-computed UTC epoch instead**: in pyscript: `(state.getattr(DEADLINE_ENT) or {}).get("timestamp")`; in Jinja2 templates: `state_attr('input_datetime.ev_deadline', 'timestamp')`. Both return a float UTC epoch that is correct regardless of DST.

20. **Weekly schedule persistence via file**: `input_text.ev_weekly_schedule` has no `initial:` value — HA would reset to `initial:` on every restart if one were set. Instead, ev_optimizer.py persists the schedule to `/config/pyscript/ev_schedule_data.json` on every change (`persist_weekly_schedule` state trigger) and restores it from that file on startup (`restore_weekly_schedule` startup trigger). File I/O uses `task.executor(Path(...).read_text)` / `task.executor(Path(...).write_text, data)` — the raw `open()` built-in and `pathlib.Path.read_text()` are not directly callable in pyscript (blocked as blocking I/O on the event loop). **Do not add an `initial:` value** to the YAML definition — it would override the persisted data on every restart.

21. **`input_datetime` state trigger requires `task.sleep(1)`**: When pyscript triggers on `input_datetime.ev_deadline`, `state.getattr(DEADLINE_ENT)` may still return the old value within the same event cycle. The dedicated `on_deadline_changed` function calls `task.sleep(1)` before invoking `ev_optimizer_recompute()` to allow HA state to fully settle. The same applies to `_on_auto_deadline_toggle` — always `task.sleep(1)` before reading state after a toggle.

22. **`sensor.ev_schedule` state uses compact epoch format**: The state string stores windows as `[{"s": start_epoch, "e": end_epoch}, ...]` using compact JSON separators (`separators=(',', ':')`) — approximately 30 chars per window. This replaced the ISO timestamp format (`{"start": "...", "end": "..."}` at ~75 chars/window) which overflowed the 255-char HA state limit at 4+ windows. Full ISO times are still available in `attributes.schedule` for Lovelace display. The control loop (`ev_control_loop.py`) parses windows using `float(window["s"])` and `float(window["e"])` as UTC epoch timestamps.

23. **`compute_schedule()` uses epoch timestamps throughout for slot filtering and trim**: Slot eligibility filtering and the surplus-trim pass both use `.timestamp()` comparisons (`slot["start"].timestamp() >= now_ts`, `slot["end"].timestamp() <= deadline_ts`) rather than datetime object comparisons. This avoids a potential CEST (UTC+2) 2-hour offset error that could include slots beyond the deadline when datetime-aware objects from different construction paths are compared. `now_ts = datetime.now(tz=ZoneInfo(hass.config.time_zone)).timestamp()` — `.timestamp()` always returns a POSIX UTC epoch regardless of the timezone argument, so this is correct and consistent with `get_effective_deadline()`. The trim pass after anti-toggling removes the most expensive surplus slots (those whose removal keeps total kWh ≥ req_kwh) until the schedule is within one minimum-power slot of the target, preventing over-scheduling caused by the clustering heuristic.

24. **`get_next_departure()` always uses explicit date components**: Departure datetimes are constructed as `datetime(year=target_date.year, month=target_date.month, day=target_date.day, hour=h, minute=m, second=0, tzinfo=local_tz)` — never via `datetime.strptime()` without a full date (Python defaults missing date components to 1900 or 2000 depending on version). The 15-minute look-ahead check uses epoch comparison (`candidate.timestamp() > now_ts + 900`) rather than a direct datetime comparison, consistent with the BUG A fix pattern throughout the codebase.

25. **Opportunistic mode selects cheap slots without a completion guarantee**: When `get_effective_deadline()` returns `(None, "opportunistic")` the recompute path calls `compute_opportunistic_schedule()` instead of `compute_schedule()`. Opportunistic mode selects all slots at or below the median price in the next 24 hours — no slot-count limit and no guarantee that a specific kWh target is met. The schedule `mode` attribute will be `"opportunistic"` and `required_slots` will be 0. `compute_schedule()` is only called when a valid deadline exists. To verify opportunistic mode: disable `input_boolean.ev_auto_deadline`, set both deadline entities to the 2099 sentinel, trigger recompute, and check `sensor.ev_schedule` attributes for `"mode": "opportunistic"`.

26. **SoC safety stop deliberately removed (2026-04-26)**: The `ev_safety_soc_reached` automation (which hard-stopped charging at 95% SoC) was removed. The Mercedes Me BMS manages its own battery charge termination natively and will stop at the correct level automatically. HA only stops charging based on kWh target, departure deadline, and price data availability. Do not re-add a SoC threshold stop.

28. **Slot filter uses -900s lookback to include the currently-active slot (2026-04-27)**: `compute_schedule()` and `compute_opportunistic_schedule()` filter eligible slots with `start_ts >= now_ts - 900` (one full 15-minute slot duration) rather than `>= now_ts`. Without this, a recompute triggered 1 second after a Nordpool slot boundary (the `task.sleep(1)` in `on_price_update`) sets `now_ts = slot_start + 1` — the `>= now_ts` test then excludes the just-started slot, advancing the schedule window forward by 15 minutes on every price update. With the -900s lookback, the currently-active slot remains eligible for the entire 15 minutes it is running. Slots that have already fully elapsed are still excluded by the upper bound (`end_ts <= deadline_ts` / `end_ts <= horizon_ts`). The same lookback is applied in `compute_opportunistic_schedule()` using `s["start"].timestamp() >= now_ts - 900`.

27. **Charger current reset to full on stop and startup (2026-04-27)**: `set_charger(False, ...)` always calls `reset_to_full_current()` (which sets `number.laddbox_charger_max_current = 25`) after a real ON→OFF transition. A separate `@time_trigger("startup")` function `reset_charger_on_startup()` (with `task.sleep(15)`) resets to 25A on every HA reload/restart. This ensures manual charging starts at full speed if HA was restarted while the charger was throttled. The ev_decision_reason shows `| current reset to 25A` appended when a real stop transition fires. The reset uses the charger-level entity only — the installation (magnus_niemi) is never touched by the control loop.

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

35. **Mercedes PHEV minimum charge current is 10A (2026-04-27)**: The car terminates the charging session if the current limit falls below approximately 10A (6.93 kW on 3-phase 400V). `CHARGER_MIN_AMPS = 10` (was 6). `_apply_tariff_current()` computes `raw_amps = int(max_kw × 1000 / (400 × 1.732))` and checks `raw_amps < CHARGER_MIN_AMPS` before calling `set_charge_current()`: if the configured tariff power would produce fewer than 10A, it logs a warning, calls `reset_to_full_current()`, and returns — leaving ON/OFF control (via the consumption guard) to handle the cap. `set_charge_current()` itself also clamps to `[CHARGER_MIN_AMPS, CIRCUIT_MAX_AMPS]` as a second line of defence, but the pre-check is required to avoid calling `set_charge_current(4)` which would silently clamp to 10A instead of triggering the ON/OFF fallback path.

37. **Consumption guard uses stateless latest-start algorithm — no cooldown boolean needed (2026-04-27)**: The old guard used `input_boolean.ev_consumption_guard_active` as a cooldown flag + `task.sleep` until next hour to prevent a toggle loop. Replaced by the price-aware latest-start algorithm (see quirk 12) which is inherently non-toggling: once `now >= latest_start_minute`, the guard returns `(False, "", None)` on every tick for the rest of the hour. The boolean still exists in the YAML and Lovelace but is never set/cleared by the guard code — it's a dormant indicator. `reset_consumption_guard_hourly` still runs hourly and clears the boolean as a dormant safety net.

36. **`_apply_tariff_current()` is called on fresh start only, never mid-session (2026-04-27)**: Adjusting the current limit on an already-charging session causes the Mercedes PHEV to terminate the session (Zaptec firmware re-negotiates, car sees the new lower limit and ends the session). `set_charger()` calls `_apply_tariff_current()` only in the `on != current` branch (fresh start), with a `task.sleep(2)` guard before the call to give Zaptec time to transition from `connected_requesting` → `connected_charging` before the current write is sent. The `else` branch (`on == current`, charger already ON) no longer calls `_apply_tariff_current()` at all — tariff-hour boundary transitions are handled by the next fresh start cycle.

38. **`binary_sensor.ev_deadline_pressure` reads from template sensors, not raw entities (2026-04-27)**: The binary sensor reads `sensor.ev_slots_needed` and `sensor.ev_slots_available` rather than recalculating from `ev_remaining_kwh` directly. This means any fix to `sensor.ev_slots_needed` (e.g. the tariff-power-aware calculation) automatically flows through to deadline pressure. The sensor also guards `slots_needed > 0` (no pressure when battery is already full or no target) and checks that a future deadline actually exists via `ev_computed_deadline` or `ev_deadline` timestamp > now + 300s. Root cause of the 172-slots bug: the old `ev_slots_needed` template used `ev_max_tariff_power_kw` directly as the charging power during tariff hours; when that entity was transiently set to a low value (e.g. 0.057 kW) the slots calculation exploded. Fixed by the tariff-throttling guard in the template and by the 0 = disabled logic.

39. **Nordpool `raw_today`/`raw_tomorrow` slot fields are datetime objects in pyscript (2026-04-27)**: `state.getattr("sensor.nordpool_kwh_se3_sek_3_10_025")["raw_today"]` returns a list of dicts where `slot["start"]` and `slot["end"]` are already-parsed `datetime` objects, not ISO format strings. Calling `datetime.fromisoformat(slot["start"])` raises `TypeError: fromisoformat: argument must be str`. Fix in `get_slot_price()`: `start_raw = slot["start"]; slot_start = (datetime.fromisoformat(start_raw) if isinstance(start_raw, str) else start_raw).astimezone(timezone.utc)`. The same two-path handling is needed for `slot["end"]`. This affects `ev_control_loop.py` only — `ev_optimizer.py` uses `raw_today`/`raw_tomorrow` differently (iterates the top-level price lists, not the raw slot dicts with start/end fields).

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
    - type: conditional
      conditions:
        - condition: template
          value_template: >
            {{ state_attr('input_datetime.ev_computed_deadline', 'timestamp') | float(0) > now().timestamp() }}
      card:
        type: entities
        entities:
          - entity: input_datetime.ev_computed_deadline
            name: Next auto departure
    - type: markdown
      content: |
        {% set schedule = state_attr('sensor.ev_schedule', 'schedule') %}
        {% set now_ts = now().timestamp() %}
        **⚡ Charging Schedule**
        {% if not schedule or schedule | length == 0 %}
        *No charging windows scheduled*
        {% else %}
        | # | Time | Price | Energy |
        |---|------|-------|--------|
        {% for w in schedule %}
        {%   set start_ts = (w.start | as_datetime).timestamp() %}
        {%   set end_ts   = (w.end   | as_datetime).timestamp() %}
        {%   set start_str = w.start | as_datetime | as_local | string %}
        {%   set end_str   = w.end   | as_datetime | as_local | string %}
        {%   set h_s = start_str[11:16] %}
        {%   set h_e = end_str[11:16]   %}
        {%   if start_ts > now_ts %}
        {%     set icon = "⏳" %}
        {%   elif end_ts > now_ts %}
        {%     set icon = "🔋" %}
        {%   else %}
        {%     set icon = "✅" %}
        {%   endif %}
        | {{ icon }} {{ loop.index }} | {{ h_s }}–{{ h_e }} | {{ w.price | round(3) }} SEK | {{ w.kwh | round(2) }} kWh |
        {% endfor %}
        ---
        **Total:** {{ state_attr('sensor.ev_schedule', 'total_kwh') | round(2) }} kWh ·
        **Est. cost:** {{ state_attr('sensor.ev_schedule', 'expected_cost') | round(2) }} SEK ·
        **Mode:** {{ state_attr('sensor.ev_schedule', 'mode') }}
        {% set next_start = namespace(ts=0) %}
        {% for w in schedule %}
        {%   set s = (w.start | as_datetime).timestamp() %}
        {%   if s > now_ts and next_start.ts == 0 %}
        {%     set next_start.ts = s %}
        {%   endif %}
        {% endfor %}
        {% if next_start.ts > 0 %}
        {% set diff = (next_start.ts - now_ts) | int %}
        **Next window in:** {{ diff // 3600 }}h {{ (diff % 3600) // 60 }}min
        {% endif %}
        {% endif %}
  ```
  The `attributes.schedule` list uses ISO strings (`"start"`, `"end"` with `+02:00` offset). Parse with `(w.start | as_datetime).timestamp()` for epoch comparisons. `w.start | as_datetime | as_local | string` gives a string where `[11:16]` extracts `HH:MM`.
  Icons: ⏳ upcoming, 🔋 currently charging, ✅ completed.
  The card is added to the "car" view in Lovelace storage (`.storage/lovelace.lovelace`) — position index 4 in sections[0].cards (between Status entities and Tariff protection heading).
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
