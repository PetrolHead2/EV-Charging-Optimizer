# EV Charging Optimizer — Project Reference

## Infrastructure & Files

| Item | Value |
|------|-------|
| HA host | `debian` · SSH `pi@debian` · sudo pw `eankod89` |
| HA container | `homeassistant` (Docker) |
| HA version | 2026.4.3 |
| HA URL | https://koffern.duckdns.org:8123/ |
| HA token | `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiI5N2U2MDM3OWY2ZGI0Yjg5OWFhMWIyZWJmYWQ4MGRlYSIsImlhdCI6MTc3NzAyMzk4OSwiZXhwIjoyMDkyMzgzOTg5fQ.jowul0Q2pQq0t27x6CvXPKHbq8ifh1-gfRe8iM9QEOo` |
| Timezone | Europe/Stockholm (use HA's timezone) |
| Nordpool region | SE3, SEK/kWh |
| Config dir (host) | `/media/pi/NextCloud/homeassistant` |
| Config dir (container) | `/config` |
| Repo | https://github.com/PetrolHead2/EV-Charging-Optimizer |

Config files are root-owned. Editing workflow:
```bash
sshpass -p eankod89 scp /tmp/file.py pi@debian:/tmp/
ssh pi@debian "echo eankod89 | sudo -S cp /tmp/file.py /media/pi/NextCloud/homeassistant/pyscript/"
```

Reload helpers individually — `homeassistant/reload_all` does NOT reload `input_datetime`, `input_number`, `input_select`, `input_text`.

Sync to repo: `cd ~/projects/EV-Charging-Optimizer && ./sync_from_ha.sh && git add . && git commit -m "..." && git push`
`sync_from_ha.sh` scrubs `HA_TOKEN` from the HTML copy. **Never commit the real token.**

| Host path (under `/media/pi/NextCloud/homeassistant/`) | Purpose |
|-------------------------------------------------------|---------|
| `configuration.yaml` | Main HA config — pyscript, packages, input helpers |
| `packages/ev_optimizer.yaml` | Template sensors + safety automations |
| `pyscript/ev_optimizer.py` | Schedule computation service |
| `pyscript/ev_control_loop.py` | 5-minute control loop |
| `www/ev_schedule_grid.html` | Lovelace iframe — **contains HA_TOKEN, do not commit** |
| `pyscript/ev_schedule_data.json` | Persisted weekly schedule JSON |

Local working copies: `/tmp/ev_optimizer_pkg.yaml`, `/tmp/ev_optimizer.py`, `/tmp/ev_control_loop.py`, `/tmp/ev_schedule_grid.html`

## Entity Reference

### Physical / Integration

| Entity | Description | Unit |
|--------|-------------|------|
| `sensor.nordpool_kwh_se3_sek_3_10_025` | Nordpool price | SEK/kWh |
| `sensor.laddbox_charge_power` | Charger active power | W |
| `sensor.laddbox_session_total_charge` | Energy this session | kWh |
| `sensor.laddbox_charger_mode` | Charger state string | — |
| `switch.laddbox_charging` | **Session ON/OFF — use for start/stop** | — |
| `number.laddbox_charger_max_current` | Session current limit | A (0–25) |
| `sensor.laddbox_allocated_charge_current` | Allocated current | A |
| `number.magnus_niemi_available_current` | Circuit available current (**NEVER written by control loop**) | A |
| `sensor.jbb78w_state_of_charge` | Mercedes battery SoC | % |
| `binary_sensor.jbb78w_charging_active` | Car reports active charging | on/off |
| `sensor.tibber_pulse_dianavagen_15_power` | House total power | W |
| `sensor.tibber_pulse_dianavagen_15_accumulated_consumption_current_hour` | kWh since hour start | kWh |
| `sensor.tibber_pulse_dianavagen_15_average_power` | House average draw (consumption guard) | W |

### Input Helpers

| Entity | Description |
|--------|-------------|
| `input_datetime.ev_deadline` | **Human input only** — manual departure override (set `2000-01-01` to clear) |
| `input_datetime.ev_computed_deadline` | **Pyscript only** — next departure from weekly schedule |
| `input_datetime.ev_last_state_change` | Last charger on/off transition timestamp |
| `input_number.ev_required_kwh` | Energy target (0 = auto from SoC) |
| `input_number.ev_max_hourly_kwh` | Hourly consumption cap for tariff guard (kWh, default 5.0) |
| `input_number.ev_max_tariff_power_kw` | Max charge power during tariff hours (kW; 0 = disabled) |
| `input_number.ev_max_price_sek` | Hard max price ceiling in SEK/kWh (0 = disabled, no ceiling) |
| `input_select.ev_charging_mode` | Smart / Charge now / Stop |
| `input_text.ev_decision_reason` | Human-readable last decision (max 255 chars) |
| `input_text.ev_weekly_schedule` | JSON weekly departure schedule, max 255 chars e.g. `{"mon":["08:00"],...}` |
| `input_boolean.ev_tariff_guard_enabled` | Enable/disable both tariff guards (default on) |
| `input_boolean.ev_auto_deadline` | Enable auto-deadline from weekly schedule (default on) |
| `input_boolean.ev_consumption_guard_active` | Dormant cooldown indicator (never written by guard code) |

### Computed Sensors (`packages/ev_optimizer.yaml`)

| Entity | Description | Unit |
|--------|-------------|------|
| `sensor.ev_charging_power_kw` | Active charge power (defaults 7 kW when idle) | kW |
| `sensor.ev_remaining_kwh` | kWh still needed to reach target | kWh |
| `sensor.ev_slots_needed` | 15-min slots needed (cap-aware during tariff hours) | slots |
| `sensor.ev_slots_available` | 15-min slots until deadline (999 = no deadline) | slots |
| `binary_sensor.ev_deadline_pressure` | True when deadline set AND slots_needed > 0 AND slots_available ≤ slots_needed+1 | — |
| `sensor.ev_schedule` | State = compact epoch JSON `[{"s":ts,"e":ts},...]`; `attributes.schedule` has full ISO windows | — |
| `sensor.ev_house_draw_smoothed_kw` | Smoothed house draw: `accumulated_kwh / hours_elapsed`. Falls back to instantaneous `average_power` for first 5 min of hour. Used by consumption guard and per-hour cap filter. | kW |

## Zaptec Device Structure

**Device 1 — Zaptec Go "Laddbox"** (session level): use `switch.laddbox_charging` for ON/OFF, `number.laddbox_charger_max_current` for current throttling. Deprecated: `button.laddbox_resume_charging`, `button.laddbox_stop_charging` (unreliable).

**Device 2 — "Magnus Niemi"** (circuit level): installation ID `dcead66e-4c50-4763-bc17-6ef3efe8be1f`. **NEVER written by control loop** — circuit changes affect all appliances. Safety automations use `zaptec.stop_charging` with `charger_id: d0775df2-6290-4569-bcd0-b579f8b9dde3`.

Reset circuit if stuck: `curl -X POST https://koffern.duckdns.org:8123/api/services/zaptec/limit_current -H "Authorization: Bearer TOKEN" -H "Content-Type: application/json" -d '{"installation_id":"dcead66e-4c50-4763-bc17-6ef3efe8be1f","available_current":25}'`

### `sensor.laddbox_charger_mode` States

| State | Connected? |
|-------|-----------|
| `disconnected` / `Disconnected` | No |
| `unknown` / `unavailable` | No (wait 3s) |
| `connected_requesting` / `Waiting` | Yes |
| `connected_charging` / `Charging` | Yes — active charging |
| `connected_finished` / `connected_finishing` / `paused` | Yes |

`CHARGING_STATES`: `{connected_charging, Charging}` · `CONNECTED_STATES = CHARGEABLE_STATES ∪ CHARGING_STATES`

## Nordpool Price Data

`state.getattr("sensor.nordpool_kwh_se3_sek_3_10_025")` returns:
- `today`/`tomorrow`: 96-entry float arrays (index 0 = 00:00 local, 15-min steps)
- `raw_today`/`raw_tomorrow`: 96 dicts `{"start": datetime, "end": datetime, "value": float}` — use these for slot filtering (explicit timestamps, DST-safe)
- `tomorrow_valid`: bool — gates use of raw_tomorrow. Tomorrow data available ~13:00 local.
- `current_price`: float

**In pyscript**: `slot["start"]`/`slot["end"]` are **already datetime objects** — do not call `datetime.fromisoformat()` on them. Use: `(datetime.fromisoformat(s) if isinstance(s, str) else s).astimezone(timezone.utc)`. Prices in SEK/kWh raw (no tax); negative prices possible.

## Architecture

### Schedule Layer (`ev_optimizer.py`)

- Service: `pyscript.ev_optimizer_recompute` — triggered on Nordpool update, deadline change, energy target, tariff toggle, weekly schedule change, hourly failsafe
- `get_effective_deadline()`: collects all valid future deadlines (manual + computed), returns **nearest** — `ev_deadline` does NOT suppress weekly auto schedule; nearest wins
- `ev_deadline` is **NEVER written by pyscript**. `ev_computed_deadline` is **NEVER set by user**. Clear manual deadline: set to `2000-01-01 00:00:00` (sentinel).
- Eligible slot window: `now - 900s → deadline` (−900s lookback keeps currently-active slot eligible)
- Effective power per slot: overnight (outside 06:00–22:00) = `charger_kw × 0.25` kWh; tariff-hour = `max_tariff_power_kw × 0.25` kWh (full when 0 = disabled)
- **All-in price**: `_build_slots()` converts raw Nordpool spot via `spot_to_allin()` before any ranking or cost calculation. Formula: `all_in = spot × 1.25 + 0.835 + 0.04803` (VAT 25%, Ellevio grid fee, energy tax). Slot ranking unchanged (monotonic). `expected_cost` and window prices in `sensor.ev_schedule` attributes reflect true consumer price. Set `USE_ALLIN_PRICE = False` to revert to raw spot. `get_slot_price()` in `ev_control_loop.py` uses mirrored `_spot_to_allin()` for consistency.
- **Price ceiling**: `compute_schedule()` reads `input_number.ev_max_price_sek`; slots above the ceiling are removed from the candidate pool before selection. 0 = disabled. Ceiling is compared against all-in prices. Does NOT block deadline pressure — `ev_control_loop` step 8 still forces ON when pressure is active even if schedule is empty. When schedule is empty due to ceiling, `ev_decision_reason` shows `"No slots below price ceiling (X.XX SEK/kWh, current Y.YYY SEK/kWh)"`.
- **Per-hour cap**: `get_tariff_cap_slots_per_hour()` → `max_slots = int((cap_kwh − house_kw) / (charger_kw × 0.25))` clamped [0–4]; tariff-hour slots grouped by calendar hour, cheapest `max_slots` kept; overnight unconstrained; Tibber unavailable = fail-open
- Post-selection: anti-toggling pass (merge isolated slots within 20% avg price) + surplus trim
- Opportunistic mode (no valid deadline): `compute_opportunistic_schedule()` — all slots ≤ median price in next 24h; `mode="opportunistic"`, `required_slots=0`

### Control Layer (`ev_control_loop.py`) — Priority Chain

`ev_control_loop()` has `@service` — cross-file calls use `pyscript.ev_control_loop()` (async/fire-and-forget). Within same file: direct call.

Triggers: 5-min tick · startup · `laddbox_charger_mode → "charging"` · +3s after any input change · +2s after Nordpool price update

| Step | Condition | Action |
|------|-----------|--------|
| 0 | Mode not in CONNECTED_STATES | Write reason, return |
| 1 | Mode = Stop | Force OFF |
| 2 | Mode = Charge now | Force ON |
| 3 | `ev_deadline_pressure` on | Set `forced_on=True`, continue |
| 4 | No schedule | OFF, return |
| 5 | Evaluate schedule | `desired = should_charge_now()` |
| 6 | `not desired and not forced_on` | OFF, **skip guard entirely**, return |
| 7 | Consumption guard check | Hold if over hourly cap (only reached when desired or forced_on) |
| 8 | `forced_on` (deadline pressure) | ON **without hysteresis** |
| 9 | `desired` (in scheduled window) | ON with 15-min hysteresis |

### Safety Layer (`packages/ev_optimizer.yaml`)

Automation IDs `1745760001`–`1745760004`:
1. `ev_remaining_kwh < 0.2 kWh` (condition: `connected_charging`) → stop charger
2. Manual deadline passed → stop + reset `ev_deadline` to `2000-01-01`
3. Auto deadline passed → stop charger
4. Nordpool sensor → `unavailable` → stop charger

All write `ev_decision_reason`. **NEVER set `ev_charging_mode` to Stop in automations** — requires manual recovery. **Do NOT remove `automation.electrical_extreme_high_consumption`** — independent safety net. Mercedes BMS handles SoC termination natively — no SoC stop in HA.

## Known Quirks

1. **Pyscript: no generator expressions** — always use list comprehensions: `sum([x for x in items])` not `sum(x for x in items)`.
2. **Pyscript: lambda closures need default-arg capture** — `lambda i, d=by_idx: d[i]["price"]` not `lambda i: by_idx[i]["price"]`.
3. **`state.getattr()` single-arg only** — `state.getattr(entity)` → dict → `.get("key")`. Two-arg form raises TypeError.
4. **`is_state()` not in pyscript** — use `(state.get(entity) or "off") == "on"` everywhere in ev_optimizer.py and ev_control_loop.py.
5. **`input_datetime` timezone** — always use `timestamp` attribute (UTC epoch float). In pyscript: `(state.getattr(ENT) or {}).get("timestamp")`; in Jinja2: `state_attr('...', 'timestamp')`. Never parse the state string directly (naive string with DST offset causes 2h error).
6. **`input_datetime` state trigger** — always `task.sleep(1)` before reading state after trigger/toggle (state may hold old value in same event cycle).
7. **`sensor.ev_schedule` state format** — compact epoch JSON `[{"s": start_ts, "e": end_ts}, ...]` (~30 chars/window, fits 255-char HA limit). Full ISO data in `attributes.schedule`. Parse in control loop: `float(window["s"])` / `float(window["e"])`.
8. **`sensor.ev_schedule` lost on restart** — pyscript in-memory state. `_ev_recompute_on_startup` (`@time_trigger("startup")`) restores it.
9. **Config files are root-owned** — all debian writes require `sudo`. Use SCP + sudo cp workflow. No direct `sudo nano` from subagent (no TTY).
10. **`switch.laddbox_charging` for start/stop** — `switch.turn_on/off(entity_id="switch.laddbox_charging")`. NOT button entities or `zaptec.resume_charging`/`zaptec.stop_charging` services (control loop only — safety automations do use `zaptec.stop_charging`).
11. **`ev_max_tariff_power_kw = 0` disables throttling** — Mercedes PHEV min is 10A (6.93 kW on 3-phase 400V). Adjusting current mid-session terminates the session. Values < 10A equivalent treated as disabled. To re-enable throttling for another car: set `ev_max_tariff_power_kw ≥ 7.0 kW`.
12. **Consumption guard latest-start algorithm** — `headroom = cap − accumulated_kwh`; `max_charge_min = headroom / (house_kw + ev_kw) × 60`; `latest_start = 60 − max_charge_min`. If `now ≥ latest_start` → allow. Else: if next-hour price < current×0.95 → skip to next hour; else → hold until latest_start. Fail-open if Tibber unavailable. `ev_consumption_guard_active` boolean is dormant (never written by guard). **Disable `ev_tariff_guard_enabled` on 2026-06-01** (Ellevio scraps power tariff scheme). **House draw is EV-subtracted**: Tibber accumulated/average_power includes EV charging — using raw values caused oscillation (charger on → guard fires → charger off → guard clears → charger on). `get_house_only_kw()` subtracts `sensor.ev_charging_power_kw` when `sensor.laddbox_charger_mode` is in `CHARGING_STATES`. Same EV subtraction applied in `get_tariff_cap_slots_per_hour()` and the `ev_house_draw_smoothed_kw` template sensor. House draw clamped to 0 when subtraction goes negative (EV-dominated hour — guard uses EV power only). Falls back to instantaneous `average_power` for first 5 min of each hour.
13. **Hysteresis timestamp** — `ev_last_state_change` is `"YYYY-MM-DD HH:MM:SS"` local Stockholm time. Parse: `strptime(..., "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_LOCAL)`.
14. **`input_text` max 255 chars** — `ev_weekly_schedule` uses all 255. Use compact JSON (no spaces). 2 times/day × 7 days ≈ 160 chars; 3 times/day ≈ 220 chars. Do NOT add `initial:` — overrides persisted data on restart.
15. **Weekly schedule persistence** — persisted to `/config/pyscript/ev_schedule_data.json` via `task.executor(Path(...).write_text, data)`. Restored by `restore_weekly_schedule` startup trigger.
16. **Deadline semantics** — `ev_deadline` and `ev_computed_deadline` are equal candidates; nearest future one wins. Manual deadline does NOT suppress weekly schedule. Clear manual deadline: set to `2000-01-01 00:00:00`. Safety automation bound `> 946684800` (2000-01-01 UTC epoch) prevents re-trigger on sentinel value.
17. **Weekly schedule look-ahead** — `get_next_departure()` skips departures < 15 min away. Rollover within 5 min via `_auto_deadline_tick`.
18. **Schedule grid iframe** (`/local/ev_schedule_grid.html`) — `type: iframe` Lovelace card. HTML changes need browser hard-refresh only (no pyscript reload). Shows all future deadlines: 📅 Manual / 🗓 Auto / 🔮 Opportunistic. "Clear manual override" button sets `ev_deadline` to `2000-01-01`. `attributes.schedule` ISO strings: `(w.start | as_datetime).timestamp()` for epoch; `[11:16]` = `HH:MM`. Card in `.storage/lovelace.lovelace` sections[0].cards index 4.
19. **`compute_schedule()` epoch timestamps throughout** — all comparisons via `.timestamp()` (POSIX UTC epoch). Surplus-trim pass removes most-expensive slots until `total_kwh ≥ req_kwh − min_slot_kwh`, preventing over-scheduling from anti-toggling.
20. **`get_next_departure()` explicit date construction** — `datetime(year=..., month=..., day=..., hour=h, minute=m, tzinfo=local_tz)`. Never `strptime()` without full date.
21. **Opportunistic mode** — `compute_opportunistic_schedule()` when no valid deadline. Selects all slots ≤ median price in next 24h. `mode="opportunistic"`, `required_slots=0`.
22. **SoC stop deliberately removed** — Mercedes BMS handles charge termination natively. Do NOT re-add SoC threshold stop in HA.
23. **Slot filter −900s lookback** — eligible slots use `start_ts >= now_ts − 900`. Keeps currently-active slot after recompute. Upper bound still excludes elapsed slots.
24. **Charger current reset on stop/startup** — `set_charger(False)` calls `reset_to_full_current()` → `number.laddbox_charger_max_current = 25`. `reset_charger_on_startup()` (`@time_trigger("startup")`, sleep 15s) also resets. Magnus Niemi circuit level never touched.
25. **`ev_safety_target_reached` condition** — must use `state: "connected_charging"` (Zaptec v0.8.x) not `"charging"`.
26. **`should_charge_now()` window-active condition** — `w_start <= now_ts + 60 and w_end > now_ts`. Use `window.get("s", window.get("start_ts", 0))`. Do NOT use `w_start >= now_ts − 900` here (causes all future windows to activate immediately).
27. **Automation `id:` must be numeric string** — e.g. `"1745760001"`. String IDs work but don't appear in Settings → Automations UI. If IDs change and `_2` suffix appears: stop HA, edit `.storage/core.entity_registry` + `.storage/core.restore_state` to remove old entries and drop `_2` suffixes, restart.
28. **Connection guard** — `ev_control_loop()` returns immediately if `sensor.laddbox_charger_mode` not in `CONNECTED_STATES`. Also applied in `on_zaptec_state_changed()`.
29. **`_is_charging()` checks `connected_charging`** — not `"charging"`. Gates the `on != current` transition check in `set_charger()`.
30. **`_apply_tariff_current()` on fresh start only** — changing current mid-session terminates the Mercedes session. Called only in `on != current` branch with `task.sleep(2)` guard. Never in `on == current` branch.
31. **`binary_sensor.ev_deadline_pressure` reads template sensors** — reads `sensor.ev_slots_needed` and `sensor.ev_slots_available`. Guards: `slots_needed > 0` AND deadline `> now + 300s`.
32. **`schedule_watchdog()` suppression guards** — skip recompute when: deadline < 30 min away or passed; OR `ev_remaining_kwh ≤ 0.2 kWh`. Only fire WARNING + recompute: schedule empty AND deadline > 30 min AND remaining > 0.2 kWh.
33. **Deadline pressure has a 60-second anti-toggle guard** — step 8 reads `ev_last_state_change` timestamp and holds off if the charger changed state within the last 60 seconds. This breaks the `stop → on_zaptec_state_changed → force-ON` oscillation loop while still responding to genuine deadline pressure within one 5-min tick cycle. Do NOT remove this guard or add `check_hysteresis()` in its place (15-min hysteresis is too long for deadline pressure).
34. **`on_input_changed()` fires control loop** — after recompute + 1s settle, calls `pyscript.ev_control_loop()`. Changes take effect within ~3s.
35. **Safety automations must not set mode to Stop** — requires manual recovery. Automations must only: stop Zaptec, write `ev_decision_reason`, optionally reset `ev_deadline` to `2000-01-01`.
36. **`on_price_update()` must call `pyscript.ev_control_loop()` after recompute** — prevents up to 5 min of unnecessary charging when price spikes. Do NOT remove `automation.electrical_extreme_high_consumption`.
37. **Pyscript cross-file calls** — only `@service`/trigger-decorated functions are in shared namespace. Call with `pyscript.func_name()` (async/fire-and-forget). Do NOT call `pyscript.func_name()` within the same file — use direct function call instead (cross-file calls are async; in-file calls are synchronous).
38. **Consumption guard placement** — guard runs at step 7 only when `desired=True` or `forced_on=True`. Step 6 routes outside-window + no-pressure directly to OFF. Guard never runs when optimizer has decided not to charge.
39. **`ev_slots_needed` cap-aware during tariff hours** — with guard on and cap > 0: `slots_per_hour = int(ev_headroom / (charger_kw × 0.25))` (min 1); `hours_needed = ceil(remaining / ev_headroom)`; result = `hours_needed × slots_per_hour`. Returns 999 when headroom ≤ 0. Outside tariff hours: `ceil(remaining / charger_kw / 0.25)`.
40. **Nordpool `raw_*` slot fields are datetime objects in pyscript** — `slot["start"]`/`slot["end"]` are already parsed. Do NOT call `datetime.fromisoformat()` directly. Use: `(datetime.fromisoformat(s) if isinstance(s, str) else s).astimezone(timezone.utc)` for both start and end.
41. **"No schedule available" / pyscript not loading** — normal on first start or when `ev_required_kwh=0` and SoC=100%. If pyscript services missing from `/api/services`: check `pyscript:` block in configuration.yaml; verify `GET /api/config` shows `pyscript` in components list.
42. **Tibber sensors include EV charging power** — `sensor.tibber_pulse_dianavagen_15_accumulated_consumption_current_hour` and `average_power` measure total meter draw including EV. Any house-draw calculation for guard/cap purposes MUST subtract EV power when charger is active. Applies to: `get_house_only_kw()` (consumption guard), `get_tariff_cap_slots_per_hour()` (cap filter), and `ev_house_draw_smoothed_kw` template sensor. Using raw Tibber values causes oscillation: charger on → guard fires → charger off → guard clears → charger on.
43. **`ev_control_loop()` uses `task.unique("ev_control_loop")`** — first line of the function body. Kills any already-running instance so the newest invocation (with the most current state snapshot) always wins. Without this, simultaneous triggers (5-min tick + `on_zaptec_state_changed` + `on_price_update`) spawn concurrent instances that read state at slightly different moments and issue conflicting switch commands.
44. **`on_zaptec_state_changed` skips `connected_requesting`** — this transient state always resolves to `connected_charging` within seconds. Spawning a full control loop on it creates a redundant concurrent instance alongside the charging trigger; with forced_on=True the redundant instance re-issues `switch.turn_on()` on a session that is still establishing, causing state churn. The skip is a single `if value == "connected_requesting": return` at the top of the handler.

## Quick Reference — Common Commands

```bash
# Restart HA
ssh pi@debian "docker restart homeassistant"

# Reload pyscript
curl -s -X POST https://koffern.duckdns.org:8123/api/services/pyscript/reload \
  -H "Authorization: Bearer TOKEN" -H "Content-Type: application/json" -d '{}'

# Trigger recompute
curl -s -X POST https://koffern.duckdns.org:8123/api/services/pyscript/ev_optimizer_recompute \
  -H "Authorization: Bearer TOKEN" -H "Content-Type: application/json" -d '{}'

# Check decision reason / schedule / any sensor
curl -s https://koffern.duckdns.org:8123/api/states/input_text.ev_decision_reason \
  -H "Authorization: Bearer TOKEN"
curl -s https://koffern.duckdns.org:8123/api/states/sensor.ev_schedule \
  -H "Authorization: Bearer TOKEN" | python3 -m json.tool

# Pyscript logs
ssh pi@debian "docker logs homeassistant 2>&1 | grep -E 'ev_optimizer|ev_control' | tail -30"
```

## Future Work

| Feature | Description | Complexity |
|---|---|---|
| ~~All-in price optimization~~ | ~~VAT + grid fee + energy tax via `spot_to_allin()`; formula: `spot × 1.25 + 0.835 + 0.04803`~~ | **Done** |
| Adaptive power | Per-phase current control for mid-price slots. Only viable for cars with minimum charge current < 6A — Mercedes PHEV minimum is 10A (6.93 kW), leaving no throttle headroom at current 7 kW installation | Medium |
| SoC missed target alert | Mobile push notification if car departs (charger disconnects) below target SoC | Medium |
| HA Energy dashboard integration | Report charging cost and kWh to HA Energy dashboard for monthly cost tracking | Medium |
| Native Lovelace card | Replace iframe grid card with a proper custom:html-card or JS module to eliminate token storage in HTML file | Medium |

