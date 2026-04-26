# EV Charging Optimizer — User Manual

> **Audience:** Developers and home-lab administrators who want to understand,
> maintain, debug, and extend the system. This document is a complete reference,
> not a quick-start guide.

---

## Table of Contents

1. [System Overview](#1-system-overview)
   - [1.1 What it does](#11-what-it-does)
   - [1.2 What it does NOT do](#12-what-it-does-not-do)
   - [1.3 Hardware requirements](#13-hardware-requirements)
   - [1.4 Software requirements](#14-software-requirements)
   - [1.5 Architecture diagram](#15-architecture-diagram)
2. [File Reference](#2-file-reference)
3. [Entity Reference](#3-entity-reference)
   - [3.1 User inputs](#31-user-inputs)
   - [3.2 Computed sensors](#32-computed-sensors)
   - [3.3 Internal state](#33-internal-state)
   - [3.4 External dependencies](#34-external-dependencies)
4. [User Interface Guide](#4-user-interface-guide)
   - [4.1 Main Dashboard](#41-main-dashboard-car-view)
   - [4.2 Charging Mode selector](#42-ev-charging-mode-selector)
   - [4.3 Departure time (manual override)](#43-departure-time-manual-override)
   - [4.4 Weekly Departure Schedule grid](#44-weekly-departure-schedule-grid)
   - [4.5 Tariff protection controls](#45-tariff-protection-controls)
   - [4.6 Charging Schedule card](#46-charging-schedule-card)
5. [Optimizer Engine — Deep Dive](#5-optimizer-engine--deep-dive)
   - [5.1 Nordpool price data](#51-nordpool-price-data)
   - [5.2 Deadline resolution](#52-deadline-resolution-get_effective_deadline)
   - [5.3 Slot computation](#53-slot-computation-compute_schedule)
   - [5.4 Opportunistic mode](#54-opportunistic-mode-compute_opportunistic_schedule)
   - [5.5 Weekly schedule and auto-deadline](#55-weekly-schedule-and-auto-deadline)
   - [5.6 Recalculation triggers](#56-recalculation-triggers)
6. [Control Loop — Deep Dive](#6-control-loop--deep-dive)
   - [6.1 Execution cycle](#61-execution-cycle)
   - [6.2 Decision priority chain](#62-decision-priority-chain)
   - [6.3 Hysteresis](#63-hysteresis)
   - [6.4 Consumption guard](#64-consumption-guard)
   - [6.5 Tariff hour current limiting](#65-tariff-hour-current-limiting)
   - [6.6 Decision reason](#66-decision-reason)
7. [Safety Layer](#7-safety-layer)
8. [Zaptec Integration](#8-zaptec-integration)
9. [Mercedes Me Integration](#9-mercedes-me-integration)
10. [Tibber Pulse Integration](#10-tibber-pulse-integration)
11. [Known Limitations and Edge Cases](#11-known-limitations-and-edge-cases)
12. [Debugging Guide](#12-debugging-guide)
    - [12.1 Quick diagnostic checklist](#121-quick-diagnostic-checklist)
    - [12.2 Common failure modes](#122-common-failure-modes)
    - [12.3 Log commands](#123-log-commands)
    - [12.4 Manual testing procedures](#124-manual-testing-procedures)
13. [Maintenance](#13-maintenance)
    - [13.1 Routine tasks](#131-routine-tasks)
    - [13.2 Syncing to GitHub](#132-syncing-changes-to-github)
    - [13.3 Restoring from GitHub](#133-restoring-from-github-after-failure)
    - [13.4 Updating pyscript](#134-updating-pyscript)
14. [Enhancement Ideas](#14-enhancement-ideas)
15. [Changelog](#15-changelog)

---

## 1. System Overview

### 1.1 What it does

The EV Charging Optimizer automatically decides **when** to charge a plug-in
hybrid electric vehicle by selecting the cheapest 15-minute Nordpool electricity
price slots before a configurable departure deadline. Rather than charging
immediately on plug-in (which often means overnight flat-rate or peak-price
charging), it builds a day-ahead schedule from raw Nordpool spot prices, applies
a clustering heuristic to minimize charger toggling, and executes that schedule
via a 5-minute control loop.

Core capabilities:

- **Cost-optimal slot selection** — picks the globally cheapest slots within the
  eligible window using a greedy walk through price-sorted slots.
- **Weekly departure schedule** — a per-day departure time grid means you never
  have to set a deadline manually on weekdays; the system advances deadlines
  automatically as each departure passes.
- **Multi-deadline chaining** — both a manual one-off deadline and the weekly
  auto-computed deadline can coexist; the optimizer always targets whichever
  departure is nearest.
- **Power tariff protection** — during Swedish Ellevio tariff hours (06:00–22:00)
  it can throttle charging current and enforce an hourly kWh cap to avoid
  triggering the monthly peak-power charge.
- **Deadline pressure override** — when slots are running out before a deadline,
  charging is forced on regardless of schedule position, bypassing hysteresis.
- **Opportunistic mode** — when no deadline is set, the system charges whenever
  the spot price is at or below the daily median, effectively exploiting
  overnight valley prices automatically.
- **Safety backstops** — three HA automations provide hard-stop protection
  independent of pyscript.

### 1.2 What it does NOT do

- Does **not** manage battery SoC targets — the Mercedes Me BMS handles charge
  termination natively. HA stops charging based on kWh delivered, not SoC.
- Does **not** perform load balancing across multiple circuits or multiple EVs.
- Does **not** incorporate grid tariffs or distribution network charges — only
  the raw Nordpool spot price is optimized. All-in prices are approximately
  3× the spot price when grid and tax are included.
- Does **not** use solar production data. All slots are equally weighted by
  Nordpool price regardless of solar generation.
- Does **not** push mobile notifications. Decision state is exposed via
  `input_text.ev_decision_reason` only.
- Does **not** adapt to traffic or weather forecasts.
- Does **not** operate on single-phase circuits — the current-limiting math
  assumes 3-phase 400 V TN.

### 1.3 Hardware requirements

| Component | Model used | HA integration | Notes |
|-----------|-----------|----------------|-------|
| EV charger | Zaptec GO (ZAP185102) | `zaptec` v0.8.6 | 3-phase TN, 25A circuit |
| Vehicle | Mercedes-Benz PHEV | `mbapi2020` | Any PHEV with HA integration works |
| Energy monitor | Tibber Pulse | `tibber` | Required for consumption guard |
| Electricity market | Nordpool SE3 | `nordpool` | 15-minute price resolution required |

The Nordpool integration **must** be configured for 15-minute prices (96 slots/day).
Hourly prices will produce incorrect schedules.

### 1.4 Software requirements

| Component | Minimum version | Notes |
|-----------|----------------|-------|
| Home Assistant | 2026.4+ | Docker install recommended |
| pyscript | 1.5+ | Install via HACS; `allow_all_imports: true` required |
| HACS | any | Required to install pyscript |
| Nordpool integration | any | Must expose `today`, `tomorrow`, `tomorrow_valid` attributes |
| Zaptec integration | 0.8.6+ | Earlier versions have different service names |

`configuration.yaml` must include:

```yaml
pyscript:
  allow_all_imports: true
  hass_is_global: true

homeassistant:
  packages: !include_dir_named packages
```

### 1.5 Architecture diagram

```
┌──────────────────────────────────────────────────────────────────────┐
│                          USER INPUTS                                  │
│  ev_charging_mode  ev_required_kwh  ev_deadline  ev_weekly_schedule   │
│  ev_tariff_guard_enabled  ev_max_hourly_kwh  ev_max_tariff_power_kw   │
└─────────────────────────┬────────────────────────────────────────────┘
                          │  state_trigger
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    ev_optimizer.py                                   │
│  ┌─────────────────┐   ┌────────────────────┐   ┌────────────────┐  │
│  │ get_effective_  │   │ compute_schedule() │   │ opportunistic  │  │
│  │ deadline()      │──▶│ cheapest slots     │   │ schedule()     │  │
│  │ nearest-wins    │   │ anti-toggling      │   │ median price   │  │
│  └────────┬────────┘   │ gap-close + trim   │   └───────┬────────┘  │
│           │            └────────────────────┘           │           │
└───────────┼────────────────────────┬────────────────────┼───────────┘
            │                        │                    │
            │            ┌───────────▼──────────┐         │
            │            │   sensor.ev_schedule  │◀────────┘
            │            │  state: epoch windows │
            │            │  attrs: full ISO data │
            │            └───────────┬──────────┘
            │                        │  state_trigger (via time_trigger)
            ▼                        ▼
┌──────────────────────────────────────────────────────────────────────┐
│                    ev_control_loop.py                                 │
│                                                                       │
│   ┌────────────┐  ┌──────────────┐  ┌───────────┐  ┌──────────────┐ │
│   │ 5-min tick │  │ Zaptec state │  │ Hysteresis│  │ Consumption  │ │
│   │  startup   │  │  trigger     │  │  guard    │  │  guard       │ │
│   └────────────┘  └──────────────┘  └───────────┘  └──────────────┘ │
│                                                                       │
│   Priority chain: Stop → Charge now → Pressure → Schedule            │
└──────────────────────────────┬───────────────────────────────────────┘
                               │  service calls
          ┌────────────────────┴───────────────────────┐
          ▼                                            ▼
┌──────────────────────┐               ┌───────────────────────────────┐
│  Zaptec GO charger   │               │  input_text.ev_decision_reason │
│  zaptec.resume/stop  │               │  (human-readable last decision) │
│  zaptec.limit_current│               └───────────────────────────────┘
└──────────────────────┘

External data feeds:
  sensor.nordpool_kwh_se3_sek_3_10_025  ──▶  ev_optimizer.py  (price source)
  sensor.jbb78w_state_of_charge         ──▶  sensor.ev_remaining_kwh  (template)
  sensor.laddbox_session_total_charge   ──▶  sensor.ev_remaining_kwh  (template)
  sensor.laddbox_charge_power           ──▶  sensor.ev_charging_power_kw  (template)
  sensor.tibber_pulse_*                 ──▶  ev_control_loop.py  (consumption guard)
  sensor.laddbox_charger_mode           ──▶  ev_control_loop.py  (state trigger)

Safety backstops (run independently of pyscript):
  automation.ev_safety_target_energy_reached     ──▶  zaptec.stop_charging
  automation.ev_safety_manual_deadline_passed    ──▶  zaptec.stop_charging
  automation.ev_safety_auto_deadline_passed      ──▶  zaptec.stop_charging
  automation.ev_safety_nordpool_unavailable      ──▶  zaptec.stop_charging
```

---

## 2. File Reference

| Filename | Host path | Container path | Purpose | Breaks if missing |
|----------|-----------|---------------|---------|------------------|
| `ev_optimizer.py` | `/media/pi/NextCloud/homeassistant/pyscript/ev_optimizer.py` | `/config/pyscript/ev_optimizer.py` | Schedule computation engine; registered as `pyscript.ev_optimizer_recompute` | No schedule computed; `sensor.ev_schedule` stays `unknown`; control loop uses no schedule |
| `ev_control_loop.py` | `/media/pi/NextCloud/homeassistant/pyscript/ev_control_loop.py` | `/config/pyscript/ev_control_loop.py` | 5-minute charger control loop | Charger never receives start/stop commands from optimizer; may run freely or not at all |
| `ev_optimizer.yaml` | `/media/pi/NextCloud/homeassistant/packages/ev_optimizer.yaml` | `/config/packages/ev_optimizer.yaml` | HA package: template sensors, safety automations, input helpers | Template sensors go `unavailable`; safety automations cease; input helpers (`ev_computed_deadline`, `ev_max_hourly_kwh`, etc.) missing |
| `ev_schedule_grid.html` | `/media/pi/NextCloud/homeassistant/www/ev_schedule_grid.html` | `/config/www/ev_schedule_grid.html` | Lovelace iframe card for weekly departure grid | Departure schedule grid blank; weekly schedule must be edited by manually setting `input_text.ev_weekly_schedule` JSON |
| `ev_schedule_data.json` | `/media/pi/NextCloud/homeassistant/pyscript/ev_schedule_data.json` | `/config/pyscript/ev_schedule_data.json` | Persisted weekly schedule JSON, survives HA restarts | Weekly schedule resets to empty on next HA restart; departures must be re-entered |
| `configuration.yaml` | `/media/pi/NextCloud/homeassistant/configuration.yaml` | `/config/configuration.yaml` | Main HA config; enables pyscript, packages, input helpers | Entire system non-functional without pyscript and packages |

> **Security note:** `ev_schedule_grid.html` contains a long-lived HA access
> token in plain text. **Never commit this file to version control.** The
> `sync_from_ha.sh` script scrubs the token automatically.

---

## 3. Entity Reference

### 3.1 User inputs

These are the entities the user interacts with directly via the Lovelace
dashboard or Developer Tools.

| Entity ID | Type | Who writes | Who reads | Unit | Unavailable effect |
|-----------|------|-----------|-----------|------|--------------------|
| `input_select.ev_charging_mode` | input_select | User | Control loop (step 1/2) | — | Control loop falls through to schedule |
| `input_number.ev_required_kwh` | input_number | User | Optimizer (energy target) | kWh | Optimizer falls back to `sensor.ev_remaining_kwh` |
| `input_datetime.ev_deadline` | input_datetime | **User only** — pyscript never writes this | Optimizer (`get_effective_deadline`); safety automation | — | Optimizer uses auto deadline only |
| `input_text.ev_weekly_schedule` | input_text | User (via grid card or direct) | Optimizer (`get_next_departure`) | JSON | Opportunistic mode; no auto deadline |
| `input_boolean.ev_auto_deadline` | input_boolean | User | Optimizer (`auto_set_deadline` gate); `_auto_deadline_tick` | — | If `off`: computed deadline not updated |
| `input_boolean.ev_tariff_guard_enabled` | input_boolean | User | Optimizer (`effective_power_kw`); Control loop (guard, current limit) | — | If `off`: full power at all hours, no kWh cap |
| `input_number.ev_max_hourly_kwh` | input_number | User | Control loop (consumption guard threshold) | kWh | Guard skipped (fail-open) |
| `input_number.ev_max_tariff_power_kw` | input_number | User | Optimizer (`effective_power_kw`); Control loop (`_apply_current_limit`) | kW | Falls back to 3.0 kW |

### 3.2 Computed sensors

Read-only outputs created by the optimizer. These are recalculated automatically;
do not write to them manually.

| Entity ID | Type | Who writes | Who reads | Unit | Unavailable effect |
|-----------|------|-----------|-----------|------|--------------------|
| `sensor.ev_schedule` | sensor (pyscript) | `ev_optimizer.py` | Control loop (schedule check); Lovelace markdown card | JSON | Control loop safe-stops if charging; no schedule displayed |
| `input_datetime.ev_computed_deadline` | input_datetime | `ev_optimizer.py` (`auto_set_deadline` only) | Optimizer (`get_effective_deadline`); safety automation; grid HTML card | — | Optimizer uses manual deadline or opportunistic |
| `sensor.ev_charging_power_kw` | sensor (template) | `ev_optimizer.yaml` template | Optimizer; Control loop (consumption guard) | kW | Falls back to 7.0 kW in optimizer |
| `sensor.ev_remaining_kwh` | sensor (template) | `ev_optimizer.yaml` template | Optimizer (energy target when `ev_required_kwh=0`) | kWh | Optimizer schedules 0 kWh if unavailable |
| `sensor.ev_slots_needed` | sensor (template) | `ev_optimizer.yaml` template | `binary_sensor.ev_deadline_pressure`; Lovelace | slots | Pressure sensor may fire incorrectly |
| `sensor.ev_slots_available` | sensor (template) | `ev_optimizer.yaml` template | `binary_sensor.ev_deadline_pressure`; Lovelace | slots | Pressure sensor may fire incorrectly |
| `binary_sensor.ev_deadline_pressure` | binary_sensor (template) | `ev_optimizer.yaml` template | Control loop (step 3 priority override) | — | Control loop misses pressure override |

### 3.3 Internal state

Bookkeeping entities that support the control loop's decision-making.

| Entity ID | Type | Who writes | Who reads | Notes |
|-----------|------|-----------|-----------|-------|
| `input_datetime.ev_last_state_change` | input_datetime | Control loop (`set_charger`) | Control loop (`check_hysteresis`) | Format: `YYYY-MM-DD HH:MM:SS` local Stockholm time |
| `input_text.ev_decision_reason` | input_text | Control loop (`set_charger`); safety automations | User (Lovelace); debugging | Max 255 chars; always shows last decision |

### 3.4 External dependencies

Entities provided by third-party integrations. The optimizer reads but never writes these.

| Entity ID | Integration | Unit | Role in system |
|-----------|-------------|------|----------------|
| `sensor.nordpool_kwh_se3_sek_3_10_025` | Nordpool | SEK/kWh | Price data for slot selection |
| `sensor.laddbox_charger_mode` | Zaptec | string | Current charger state; triggers immediate control loop |
| `sensor.laddbox_charge_power` | Zaptec | W | Feeds `sensor.ev_charging_power_kw` template |
| `sensor.laddbox_session_total_charge` | Zaptec | kWh | Feeds `sensor.ev_remaining_kwh` template when `ev_required_kwh > 0` |
| `sensor.jbb78w_state_of_charge` | Mercedes Me (mbapi2020) | % | Feeds `sensor.ev_remaining_kwh` template when `ev_required_kwh = 0` |
| `binary_sensor.jbb78w_charging_active` | Mercedes Me | on/off | Status display only |
| `sensor.jbb78w_charging_status` | Mercedes Me | int | Status display only |
| `sensor.tibber_pulse_dianavagen_15_accumulated_consumption_current_hour` | Tibber | kWh | Consumption guard: kWh delivered so far this hour |
| `sensor.tibber_pulse_dianavagen_15_average_power` | Tibber | W | Consumption guard: average house draw since last hour boundary |
| `sensor.tibber_pulse_dianavagen_15_power` | Tibber | W | House power (informational; not used by control logic) |

---

## 4. User Interface Guide

### 4.1 Main Dashboard (car view)

The `car` view in Lovelace contains the following cards in order:

1. **Controls** — user input entities
2. **Next auto departure** — conditional card, visible only when `ev_computed_deadline` is in the future
3. **Status** — computed sensor readouts
4. **Charging Schedule** — markdown card with window table
5. **Tariff protection** — guard controls
6. **Weekly Departure Schedule** — iframe grid card

### 4.2 EV Charging Mode selector

`input_select.ev_charging_mode` has three options:

**`Smart`** (default)

Full optimizer behavior. The control loop evaluates the schedule every 5 minutes
and starts/stops the charger based on the computed windows, subject to hysteresis
and the consumption guard. Use this mode for all routine charging.

Normal behavior: charger OFF outside windows, ON inside windows.

**`Charge now`**

Immediate override — the charger is started and held ON regardless of schedule,
hysteresis, or consumption guard. The current-limiting logic still applies
(tariff-hour throttling). Use when:

- You need to charge quickly and cannot wait for the next scheduled window.
- The optimizer has scheduled nothing but you know you need charge.
- You want to test the charger physically without modifying the schedule.

**Do not leave in this mode** — it bypasses all cost optimization and will charge
at full price. Switch back to `Smart` when done.

**`Stop`**

Force stop — the charger is stopped and held OFF regardless of everything.
Pyscript calls `zaptec.stop_charging` on every control loop tick (every 5 min)
while in this mode. Hysteresis does not apply. Use when:

- You want to prevent any charging regardless of schedule (e.g., grid instability).
- Debugging and you want a known baseline state.
- Manually managing charging via another method.

Note: the 15-minute hysteresis does NOT protect against rapid toggling between
`Smart` and `Stop`. If you switch modes rapidly, the charger will start/stop
accordingly.

### 4.3 Departure time (manual override)

`input_datetime.ev_deadline` is the **human-only** manual departure deadline.
Pyscript never writes to this entity.

**Setting a deadline:**
Use the date/time picker in the Controls card. The optimizer will recompute
within 2 seconds of the change (via `on_input_changed` state trigger).

**Interaction with the weekly schedule:**

The system uses nearest-deadline logic. If a manual deadline is set further in
the future than the next weekly departure, the weekly departure is used first.
Example: if your weekly schedule has Monday 09:00 and you set `ev_deadline` to
Wednesday 23:55, the optimizer targets Monday 09:00 first. After Monday passes,
`_auto_deadline_tick` rolls the auto deadline to the next weekly departure, and
the manual Wednesday deadline is then the nearest — so it becomes active next.

This means you can plan a long trip beyond your weekly schedule without disabling
auto-deadline mode. Set `ev_deadline` to the trip departure; the system handles
weekly departures in between automatically.

**Clearing the manual deadline:**

Set `ev_deadline` to any past date (e.g., 2000-01-01 00:00:00). The optimizer
will then use `ev_computed_deadline` from the weekly schedule. Setting it to
a past date is the clean way to "disable" the manual override.

**Expiry behavior:**

When the current time passes `ev_deadline`, the safety automation
`ev_safety_manual_deadline_passed` fires: it calls `zaptec.stop_charging` and
resets `ev_deadline` to 2000-01-01 00:00:00 to prevent re-triggering. The
optimizer then falls back to the auto deadline or opportunistic mode on its
next tick.

### 4.4 Weekly Departure Schedule grid

The departure grid is displayed as an iframe card pointing to
`/local/ev_schedule_grid.html`. It communicates directly with the HA REST API
using a long-lived access token embedded in the HTML file.

**How to use:**

1. For each day, enter up to three departure times using the time pickers.
2. Leave fields blank for days when the car won't need charging (or you don't
   care about cost-optimization that day).
3. Press **Save**. The grid POSTs to `input_text.ev_weekly_schedule` as compact
   JSON, e.g.:
   ```json
   {"mon":["09:00","15:00"],"tue":["09:00"],"wed":[],"thu":["09:00"],"fri":["09:00","17:30"],"sat":["12:00"],"sun":[]}
   ```
4. The `persist_weekly_schedule` trigger saves this JSON to
   `/config/pyscript/ev_schedule_data.json` immediately.
5. If `ev_auto_deadline` is on, `_on_weekly_schedule_changed` fires and calls
   `auto_set_deadline()`, which writes `ev_computed_deadline` to the next
   qualifying departure.

**Storage format:**

The schedule is stored in `input_text.ev_weekly_schedule` as compact JSON.
Keys are `mon`/`tue`/`wed`/`thu`/`fri`/`sat`/`sun`. Each value is a list of
`"HH:MM"` strings. Maximum 255 characters (HA `input_text` limit). With 3
departure times per day, the JSON is ~220 characters — near the limit, so use
compact format (no extra spaces).

**Auto-deadline mechanism:**

When `input_boolean.ev_auto_deadline` is on:
- `auto_set_deadline()` walks the next 7 days looking for a departure that is
  ≥ 15 minutes in the future.
- The first qualifying time is written to `input_datetime.ev_computed_deadline`.
- The 5-minute tick `_auto_deadline_tick` repeats this check and updates the
  computed deadline as departures pass (rollover).
- `get_effective_deadline()` then picks the nearest of `ev_deadline` and
  `ev_computed_deadline`.

**What an empty day means:**

If a day has no departure times (empty list `[]`) and all other days in the
next 7 days are also empty, `get_next_departure()` returns `None`.
`auto_set_deadline()` logs "no upcoming departure" and returns without writing
to `ev_computed_deadline`. `get_effective_deadline()` then finds no valid
future deadline and returns `(None, "opportunistic")` — the optimizer runs in
opportunistic mode, selecting cheap slots from the next 24 hours.

**Persistence across HA restarts:**

`input_text.ev_weekly_schedule` has no `initial:` value in YAML — if it did,
HA would reset the schedule on every restart. Instead, `restore_weekly_schedule`
(a `@time_trigger("startup")` function) reads
`/config/pyscript/ev_schedule_data.json` and writes it back to the entity.
This means the schedule survives restarts without any user action.

### 4.5 Tariff protection controls

**`input_boolean.ev_tariff_guard_enabled`** — master switch.

When `on`, both the hourly kWh consumption guard and the tariff-hour current
limiter are active. When `off`, the charger runs at full current at all times
with no kWh cap.

> **Important:** Disable this toggle on **2026-06-01** when Ellevio removes the
> Swedish monthly peak-power tariff scheme. Leaving it on after that date is
> harmless but unnecessarily restricts charging speed during daytime hours.

**`input_number.ev_max_hourly_kwh`** (default: 5.0 kWh)

Maximum kWh allowed in a single clock hour (00:00–59:59), including all house
loads. The consumption guard projects: `accumulated_kwh + (house_kw + ev_kw) ×
(remaining_minutes / 60)`. If this projection exceeds the threshold, charging
is blocked for this tick. Only active during tariff hours (06:00–22:00).

Set this to your electricity contract's monthly peak-power threshold minus a
comfortable margin. With a 5 kWh/h contract limit and typical house base load
of 1–2 kW, 5.0 kWh is appropriate.

**`input_number.ev_max_tariff_power_kw`** (default: 3.0 kW)

Maximum EV charging power during tariff hours (06:00–22:00). The control loop
calls `zaptec.limit_current` with the corresponding amperage on every charging
start during tariff hours. The optimizer also uses this value when estimating
kWh per slot during tariff hours (0.75 kWh/slot at 3 kW vs 1.75 kWh/slot at
7 kW overnight).

The conversion to amps assumes 3-phase 400 V: `A = int(kW × 1000 / (400 × √3))`.
At 3.0 kW this gives 4.3 A, which is clamped to the Zaptec minimum of 6 A
(≈ 4.16 kW effective).

### 4.6 Charging Schedule card

The markdown card in the `car` view reads `sensor.ev_schedule` attributes and
displays the upcoming charging windows.

**Reading the schedule table:**

```
| # | Time | Price | Energy |
|---|------|-------|--------|
| upcoming 1 | 02:45 - 04:15 | 0.5800 SEK | 10.50 kWh |
```

- **Status labels:** `upcoming` (window in the future), `active` (window
  currently in progress), `done` (window has passed).
- **Time:** local CEST start–end.
- **Price:** average Nordpool spot price for the window (SEK/kWh, raw —
  does not include grid tariffs or VAT).
- **Energy:** effective kWh the window will deliver, accounting for tariff-hour
  throttling.

The footer shows:
- **Total kWh** — sum across all windows.
- **Cost** — estimated cost at raw spot prices (multiply by ~3 for all-in).
- **Mode** — `deadline` (targeting a specific departure) or `opportunistic`
  (no deadline, cheap slots only).
- **Next window in** — countdown to the next uncharged window.

**Opportunistic mode appearance:**

In opportunistic mode, multiple short windows spread across the next 24 hours
will appear (at or below the day's median price). There is no single "target
departure" and total kWh may be lower than `ev_required_kwh` since the mode
does not guarantee delivery of a specific energy amount.

**Cost estimate accuracy:**

The cost shown uses raw Nordpool spot prices only. Actual cost on your bill will
be higher due to:
- Grid tariff: ~0.50–0.80 SEK/kWh (Ellevio)
- Energy tax: 0.536 SEK/kWh (SE, 2026)
- VAT: 25%

The displayed figure is useful for comparing relative session costs and
verifying the optimizer is selecting cheap slots, not as an absolute cost predictor.

---

## 5. Optimizer Engine — Deep Dive

### 5.1 Nordpool price data

**Source entity:** `sensor.nordpool_kwh_se3_sek_3_10_025`

The Nordpool integration exposes prices via entity attributes:

```python
{
  "today":          [float, ...],   # 96 values, index 0 = 00:00 local, step 15 min
  "tomorrow":       [float, ...],   # 96 values (only when tomorrow_valid=True)
  "tomorrow_valid": bool,
  "raw_today":      [{"start": "2026-04-27T00:00:00+02:00",
                      "end":   "2026-04-27T00:15:00+02:00",
                      "value": 0.521}, ...],
  "current_price":  float,
  ...
}
```

The optimizer uses `today` and (when valid) `tomorrow` arrays, not `raw_today` /
`raw_tomorrow`. Each element corresponds to a 15-minute slot starting at midnight
local time. `today[0]` is 00:00 CEST, `today[4]` is 01:00 CEST, etc.

**Tomorrow data:** becomes available around 13:00 local time each day after
Nordpool publishes the next-day auction results. When `tomorrow_valid` is `False`,
the optimizer can only schedule within today's remaining slots — which may be
insufficient for overnight charging into tomorrow. The schedule watchdog (`schedule_watchdog`,
15-min trigger) detects an empty schedule with a valid deadline and forces a
recompute once tomorrow's data arrives.

**Timezone handling:**

All internal processing uses UTC epoch timestamps (`.timestamp()`) throughout.
Slot datetime objects are created in local Stockholm time for slot construction
but immediately converted to UTC epoch for filtering and comparisons. This
eliminates CEST (UTC+2) offset errors that would otherwise cause slots to be
included or excluded incorrectly.

Prices are in SEK/kWh raw (no VAT, no grid tariff). Negative prices are possible
during high renewable production periods.

### 5.2 Deadline resolution (`get_effective_deadline`)

```
┌─────────────────────────────────────────────────────────────────┐
│                  get_effective_deadline()                        │
│                                                                  │
│  Read ev_deadline.attributes.timestamp  ──▶  valid? (> now+5m)  │
│  Read ev_computed_deadline.attributes.timestamp ─▶ valid?        │
│                                                                  │
│  candidates = [(ts, "manual"), (ts, "auto")]  (any valid subset) │
│                                                                  │
│  if candidates empty:                                            │
│      return (None, "opportunistic")                              │
│  else:                                                           │
│      return min(candidates, key=lambda c: c[0])                  │
│      → (nearest_ts, nearest_source)                              │
└─────────────────────────────────────────────────────────────────┘
```

**Why nearest wins (not manual priority):**

A common use case is setting a manual deadline for a trip next week while the
weekly schedule handles daily commutes. With manual-priority, the optimizer
would target next week's trip and might run overnight charging for 5 days to
ensure enough energy — even when the daily departures need only a few kWh.

With nearest-wins, the optimizer always targets the most imminent departure.
After each departure passes, `_auto_deadline_tick` rolls the computed deadline
forward to the next weekly time, and the system chains automatically. The manual
long-trip deadline is only activated once all nearer weekly departures have passed.

**The 5-minute minimum buffer:**

Both deadline entities are checked against `now + 5 minutes`. Any deadline
within 5 minutes of now is considered expired and excluded from candidates.
This prevents the optimizer from recomputing a schedule for a departure that is
already imminent (no charging slots would fit anyway).

**`timestamp` attribute vs state string:**

Both entities are read via `state.getattr(entity).get("timestamp")` which
returns a pre-computed float UTC epoch. Reading the state string directly
(`state.get(entity)`) is unreliable in some HA versions — the string may be
a naive local-time string that parses incorrectly when attached to a timezone.

### 5.3 Slot computation (`compute_schedule`)

Called when a valid deadline exists. Steps in order:

**1. Build candidate slot list** (`_build_slots`):

Iterates `today[0..95]` and (if valid) `tomorrow[0..95]`. For each price `p[i]`,
constructs a slot:
```python
{
  "start": midnight_local + timedelta(minutes=15*i),  # converted to UTC
  "end":   start + timedelta(minutes=15),
  "price": float(p[i]),
  "idx":   day_offset*96 + i,                         # unique global index
}
```
`idx` is the key used for adjacency checks (slots with `idx` differing by 1
are consecutive in time).

**2. Filter to eligible window:**

```python
eligible = [s for s in all_slots
            if s["start_ts"] >= now_ts
            and s["end_ts"]  <= deadline_ts]
```

Uses pre-computed epoch timestamps (`.timestamp()`) for all comparisons. This
is correct and timezone-agnostic. If the list is empty (e.g., deadline too close,
no price data for tomorrow), the function returns an empty windows list.

**3. Count required slots:**

Walks the eligible pool in ascending price order, accumulating effective kWh
per slot until `req_kwh` is met:

```python
for s in sorted(eligible, key=lambda s: s["price"]):
    if accumulated >= req_kwh:
        break
    accumulated += effective_power_kw(s["start"]) * 0.25
    n += 1
```

`effective_power_kw(slot_start)` returns:
- `input_number.ev_max_tariff_power_kw` (default 3.0 kW) during tariff hours
  (06:00–22:00) when the guard is on → **0.75 kWh/slot**
- `sensor.ev_charging_power_kw` (default 7.0 kW) at all other times
  → **1.75 kWh/slot**

This means the optimizer knows that overnight slots deliver 2.3× more energy
per slot than daytime slots, and factors this into selecting the minimum number
of slots to meet the target.

**4. Select the N cheapest slots:**

```python
selected = {s["idx"] for s in sorted(eligible, key=lambda s: s["price"])[:n]}
```

**5. Anti-toggling pass (isolation merge):**

Any selected slot that has no adjacent selected neighbor is "isolated" — it
would cause two charger on/off events (start, stop, start, stop...) for minimal
energy. The pass merges isolated slots with their cheapest unselected neighbor
when the price premium is ≤ `MERGE_RATIO × avg_price` (20% of the average
eligible price).

Runs up to 20 iterations (capped to prevent infinite loops on pathological data).

**6. First trim pass:**

After isolation merging, `selected` may include more kWh than needed. Removes
the most expensive surplus slots (those whose removal still leaves total kWh ≥
`req_kwh`) until within one minimum-power slot of the target.

**7. Gap-closing pass:**

If two selected windows are separated by a gap of ≤ 30 minutes (1–2 slots)
whose average price is ≤ 1.5× the average price of already-selected slots,
the gap is filled. A 15–30 minute off period between nearly-identical price
windows provides negligible cost saving while causing two extra charger events.

Runs up to 20 iterations.

**8. Post-gap-close trim:**

After gap-filling, the end-slot trim runs again: drops the most expensive
end-slot (first or last) as long as removing it still delivers ≥ `req_kwh`.
Prevents over-scheduling caused by gap-filling.

**9. Group into contiguous windows** (`merge_into_windows`):

Consecutive slots (adjacent `idx`) are merged into a single window:
```python
{
  "start": ISO string with local offset,
  "end":   ISO string with local offset,
  "price": average SEK/kWh,
  "slots": count,
  "kwh":   effective kWh (sum of per-slot effective_power_kw × 0.25),
  "cost":  sum of price × slot_kwh per slot (not flat price × total_kwh),
}
```

**Output format:**

`sensor.ev_schedule` **state** (255-char limit):
```json
[{"s":1777250700,"e":1777256100}]
```
Compact epoch integers per window (`"s"` = start, `"e"` = end).

`sensor.ev_schedule` **attributes.schedule** (no limit, used by Lovelace):
```json
[{"start":"2026-04-27T02:45:00+02:00","end":"2026-04-27T04:15:00+02:00",
  "price":0.58,"slots":6,"kwh":10.5,"cost":6.09}]
```

### 5.4 Opportunistic mode (`compute_opportunistic_schedule`)

Activated when `get_effective_deadline()` returns `(None, "opportunistic")`.
Called directly from `ev_optimizer_recompute()` — `compute_schedule()` is not
used.

Logic:
1. Collect all future slots in the next **24 hours** (not 48).
2. Compute the median price across those slots.
3. Select all slots at or below the median.
4. Group into contiguous windows via `merge_into_windows`.

No slot-count limit. No energy target guarantee. The intention is to charge
whenever electricity is unusually cheap without over-constraining the window.

`sensor.ev_schedule` attributes will show `"mode": "opportunistic"` and
`"required_slots": 0`.

This mode also activates when `req_kwh = 0` (e.g., `ev_required_kwh = 0` and
`ev_remaining_kwh = 0` — battery is full). In this case the optimizer returns
an empty schedule regardless of mode.

### 5.5 Weekly schedule and auto-deadline

**JSON structure in `input_text.ev_weekly_schedule`:**

```json
{"mon":["09:00","15:00"],"tue":["09:00","15:00"],"wed":["09:00","15:00"],
 "thu":["09:00","15:00"],"fri":["09:00","15:00"],"sat":["12:00","17:00"],
 "sun":["12:00","17:00"]}
```

Keys must be exactly `mon`/`tue`/`wed`/`thu`/`fri`/`sat`/`sun` (lowercase).
Values are lists of `"HH:MM"` strings. Each day supports 0–3 entries. Times
within a day are sorted ascending before use.

**`get_next_departure()` logic:**

Iterates `day_offset` from 0 to 6 (today through 6 days ahead). For each day,
maps the weekday to the corresponding key and iterates the sorted time list.
Constructs a candidate datetime explicitly:

```python
candidate = datetime(year=date.year, month=date.month, day=date.day,
                     hour=h, minute=m, second=0, tzinfo=local_tz)
```

Compares via epoch (`candidate.timestamp() > now_ts + 900`). The 15-minute
(900 s) buffer prevents scheduling for a departure that is already too close.
Returns the first qualifying datetime, or `None` if none found in 7 days.

**`auto_set_deadline()` writes `ev_computed_deadline`:**

Called by `_auto_deadline_tick` every 5 minutes and at the start of every
`ev_optimizer_recompute()` call (when auto mode is on). When a departure is
found, writes its local datetime string to `ev_computed_deadline`. When none
is found, returns without writing (does not write sentinels).

A one-time cleanup block detects stale far-future values (> `SCHEDULE_HORIZON_DAYS`
= 8 days from now) left by older code versions. These are not cleared but logged;
`get_effective_deadline()` rejects them naturally because they pass the `> min_ahead`
check but the nearest-wins comparison means a real near-term deadline will beat them.

**Rollover mechanism:**

When the current time passes a scheduled departure (e.g., 09:00 Monday),
`_auto_deadline_tick` fires within ≤ 5 minutes. `get_next_departure()` now skips
the 09:00 slot (it's in the past + 15 min check) and returns 15:00 Monday
(or Tuesday if Monday has no more times). `auto_set_deadline()` writes this
new value to `ev_computed_deadline`. The state trigger fires `on_input_changed`
which triggers a full recompute with the updated deadline.

### 5.6 Recalculation triggers

| Trigger | Entity / type | Why it exists | Expected latency |
|---------|--------------|---------------|-----------------|
| `on_input_changed` | `input_datetime.ev_deadline` state change | User set a new manual deadline | 2 s (task.sleep) + compute |
| `on_input_changed` | `input_datetime.ev_computed_deadline` state change | Auto deadline rolled over | 2 s + compute |
| `on_input_changed` | `input_number.ev_required_kwh` state change | Energy target changed | 2 s + compute |
| `on_input_changed` | `input_number.ev_max_tariff_power_kw` state change | Tariff power limit changed | 2 s + compute |
| `on_input_changed` | `input_boolean.ev_auto_deadline` state change | Auto mode toggled | 2 s + compute |
| `on_input_changed` | `input_boolean.ev_tariff_guard_enabled` state change | Guard toggled | 2 s + compute |
| `on_input_changed` | `input_select.ev_charging_mode` state change | Mode changed | 2 s + compute |
| `on_price_update` | `sensor.nordpool_kwh_se3_sek_3_10_025` state change | New price data available | 1 s + compute |
| `_on_weekly_schedule_changed` | `input_text.ev_weekly_schedule` state change | Schedule edited in grid | immediate + compute |
| `_auto_deadline_tick` | time trigger every 5 minutes | Deadline rollover detection | 0–5 min |
| `_ev_recompute_hourly` | time trigger every 1 hour | Failsafe if no state changes occur | 0–60 min |
| `schedule_watchdog` | time trigger every 15 minutes | Empty schedule recovery after HA restart | 0–15 min |
| `_ev_recompute_on_startup` | startup trigger | Restore `sensor.ev_schedule` after HA restart | HA startup + seconds |

---

## 6. Control Loop — Deep Dive

### 6.1 Execution cycle

`ev_control_loop()` is a plain function (no decorators). It is called by three
entry points:

**`ev_control_loop_tick()`** — decorated with both `@time_trigger("period(now, 5min)")` and
`@time_trigger("startup")`. Runs the full decision tree every 5 minutes and
once on HA startup to establish initial state.

**`on_zaptec_state_changed()`** — decorated with `@state_trigger("sensor.laddbox_charger_mode")`.
Fires immediately on any charger state transition (plug-in, charging, finished,
unplug). Includes `task.sleep(2)` to let HA state settle. This closes the race
between car plug-in (when Zaptec may auto-start charging) and the next 5-minute
tick — without this, the charger might run unrestricted for up to 5 minutes
before being stopped.

### 6.2 Decision priority chain

The chain is evaluated top-to-bottom; the first matching condition wins. No
fall-through to lower levels.

**Level 1 — Manual Stop**
```
Condition: ev_charging_mode == "Stop"
Action:    set_charger(False, "Manual override: Stop")
Hysteresis: NO
Guard:     NO
```
Absolute override. Charger is stopped on every tick until mode is changed.

**Level 2 — Manual Charge now**
```
Condition: ev_charging_mode == "Charge now"
Action:    set_charger(True, "Manual override: Charge now")
Hysteresis: NO
Guard:     NO
```
Absolute override. Charger is started on every tick. Current limiting still
applies via `_apply_current_limit()` inside `set_charger()`.

**Level 3 — Deadline pressure**
```
Condition: binary_sensor.ev_deadline_pressure == "on"
           (i.e. ev_slots_available <= ev_slots_needed + 1)
Action:    set_charger(True, "Deadline pressure: forced ON (N avail, M needed)")
Hysteresis: NO (bypassed)
Guard:     NO (bypassed)
```
Forces charging even if the schedule says OFF and even if the last state change
was less than 15 minutes ago. This is the safety net for when the optimizer
underestimated slots needed or the car was plugged in late.

**Level 4 — No schedule**
```
Condition: sensor.ev_schedule is empty/unavailable/[]
Action if charging: set_charger(False, "No schedule available — stopping...")
Action if not:      update reason, log, return (do not toggle)
```
Prevents the charger from running indefinitely when the optimizer hasn't
produced a schedule yet. Does not force-stop if already off (avoids unnecessary
API calls to Zaptec).

**Level 5 — Inside scheduled window**
```
Condition: current UTC time is within any window in sensor.ev_schedule state
Action:    set_charger(True, "In scheduled window (X.XXX SEK/kWh)")
Hysteresis: YES
Guard:     YES (consumption guard may block even if desired == True)
```

**Level 6 — Outside scheduled window**
```
Condition: current time is NOT within any window
Action:    set_charger(False, "Outside scheduled windows (X.XXX SEK/kWh)
                               — deadline: [source] [time]")
Hysteresis: YES
Guard:     NOT applied to stops
```

### 6.3 Hysteresis

**Purpose:** Protect the charger and battery from rapid on/off cycling.
Frequent start/stop events wear Zaptec relay contacts and may confuse the
Mercedes Me BMS charging state.

**Mechanism:** `check_hysteresis(desired_state)` compares `desired_state` to
`_is_charging()`. If they match, returns `True` immediately (no change needed —
always allowed). If they differ, reads `input_datetime.ev_last_state_change`
and checks whether at least `HYSTERESIS_MINUTES` (15) minutes have elapsed since
the last transition. Returns `False` if less than 15 minutes have passed.

When hysteresis blocks a transition, `ev_decision_reason` is updated to:
```
Hysteresis: holding ON/OFF (min 15 min between state changes, X.XXX SEK/kWh)
```
The charger state is not changed. The next 5-minute tick will re-evaluate.

**Bypass:** Levels 1 (Stop), 2 (Charge now), and 3 (Deadline pressure) all
bypass hysteresis. These are urgent overrides where the cost of delay exceeds
the benefit of relay protection.

**After HA restart:** `input_datetime.ev_last_state_change` is a persistent
`input_datetime` helper — it retains its last value across restarts. If the
charger was stopped at T and HA restarts 5 minutes later, hysteresis will
still correctly prevent a state change for the remaining 10 minutes.

### 6.4 Consumption guard

**Purpose:** Prevent the hourly kWh consumption from exceeding the threshold
that triggers a monthly peak-power tariff charge (Swedish Ellevio effekttariff).

**Sensors used:**
- `sensor.tibber_pulse_dianavagen_15_accumulated_consumption_current_hour` —
  kWh consumed since the start of the current clock hour (resets at :00).
- `sensor.tibber_pulse_dianavagen_15_average_power` — whole-house average
  power draw in watts (rolling average over the current hour, provided by Tibber).

**Projection formula:**
```python
remaining_minutes = 60 - local_now.minute
projected_total = accumulated_kwh + (current_house_kw + ev_charging_power_kw)
                  × (remaining_minutes / 60)
```

`current_house_kw` is `average_power_w / 1000`. Since the EV is OFF at
evaluation time (the guard only runs when `desired == True`), adding
`ev_charging_power_kw` gives the correct projected total if EV charging starts now.

If `projected_total > threshold` (from `input_number.ev_max_hourly_kwh`):
```python
set_charger(False, "Consumption guard: house X kW + EV Y kW = projected Z kWh
                    this hour (cap W kWh)")
```

**Active hours:** Only during tariff hours (06:00–22:00 local time). Overnight
charging is always unrestricted (no tariff, no guard).

**Fail-open behavior:** If either Tibber sensor is `unavailable` or `unknown`,
the guard is **skipped** (fail-open). Charging is allowed. The log warns:
```
ev_control_loop: Consumption guard: Tibber sensor(s) offline, skipping guard — fail-open
```

**Guard vs. deadline pressure:** Deadline pressure (level 3) fires **before**
the guard check is reached (level 5). When under deadline pressure, the guard
is bypassed. This is intentional — if you're about to miss a departure, charge
regardless of tariff impact.

**Disabling:** Set `input_boolean.ev_tariff_guard_enabled` to `off`. This
disables both the hourly kWh cap and the current limiter simultaneously.

### 6.5 Tariff hour current limiting

Called by `set_charger(True, ...)` via `_apply_current_limit()` on every
charging start, and on every tick when the charger is already ON (to handle
tariff-hour boundary transitions during a charging session).

**Logic:**

```python
if not guard_on or hour < 6 or hour >= 22:
    # Full power — restore circuit max
    zaptec.limit_current(installation_id=INSTALL_ID, available_current=25)
else:
    # Tariff hours — throttle
    max_kw = ev_max_tariff_power_kw  # default 3.0
    amps   = int(max_kw * 1000 / (400 * 1.732))
    amps   = max(6, min(amps, 25))   # clamp to [6, 25]
    zaptec.limit_current(installation_id=INSTALL_ID, available_current=amps)
```

**Parameter note:** `zaptec.limit_current` requires `installation_id` (the
Zaptec API UUID `dcead66e-...`), **not** `charger_id` or the HA device registry
ID. See [Section 8](#8-zaptec-integration).

**Minimum 6A floor:** Zaptec GO will not charge below 6A per phase. The
calculation at 3.0 kW gives 4.3A, so it is clamped to 6A → effective 4.16 kW
(slightly above the configured limit).

**Transition across tariff boundary:** At 22:00, the next 5-minute tick calls
`set_charger(True, ...)` (if in a window), which calls `_apply_current_limit()`,
which detects that we're now outside tariff hours and restores 25A. No separate
trigger is needed.

### 6.6 Decision reason

`input_text.ev_decision_reason` always contains the reason for the most recent
charger decision. Updated on every control loop tick.

| Reason string | Meaning |
|---------------|---------|
| `Manual override: Stop` | Mode is "Stop" |
| `Manual override: Charge now` | Mode is "Charge now" |
| `Deadline pressure: forced ON (N slots available, M needed)` | Under pressure, hysteresis bypassed |
| `In scheduled window (X.XXX SEK/kWh)` | Currently inside an optimizer window |
| `Outside scheduled windows (X.XXX SEK/kWh) — deadline: auto Mon 27 Apr 09:00` | Between windows, shows active deadline |
| `Outside scheduled windows (X.XXX SEK/kWh)` | Between windows, opportunistic mode |
| `No schedule available — stopping unauthorised charge` | No schedule, was charging |
| `No schedule available — charger already off` | No schedule, already off (no action) |
| `Hysteresis: holding ON/OFF (min 15 min between state changes, X.XXX SEK/kWh)` | Hysteresis blocked a state change |
| `Consumption guard: house X kW + EV Y kW = projected Z kWh this hour (cap W kWh)` | Consumption guard blocked start |
| `Safety stop: target energy reached` | Safety automation fired |
| `Safety stop: manual departure deadline passed` | Safety automation fired |
| `Safety stop: auto departure deadline passed` | Safety automation fired |
| `Safety stop: Nordpool price data unavailable` | Safety automation fired |

---

## 7. Safety Layer

The safety layer consists of four HA automations in `packages/ev_optimizer.yaml`.
They run entirely independently of pyscript — even if pyscript crashes or is
unloaded, the safety automations remain active.

**`ev_safety_target_energy_reached`**

| Property | Value |
|----------|-------|
| Trigger | `sensor.ev_remaining_kwh` drops below **0.2 kWh** |
| Condition | `sensor.laddbox_charger_mode` == `charging` |
| Action | `zaptec.stop_charging`; reason = "Safety stop: target energy reached"; mode → "Stop" |
| Auto-reset | Yes — the mode being set to "Stop" prevents restart until user changes it |

Fires when the energy delivery target is met. The 0.2 kWh threshold (about
10 minutes at minimum throttled power) gives a buffer for sensor lag.

**`ev_safety_manual_deadline_passed`**

| Property | Value |
|----------|-------|
| Trigger | Template: `ev_deadline.timestamp < now().timestamp()` AND `> 946684800` (2000-01-01) |
| Condition | None |
| Action | `zaptec.stop_charging`; reason = "Safety stop: manual departure deadline passed"; reset `ev_deadline` to `2000-01-01 00:00:00` |
| Auto-reset | Yes — `ev_deadline` is reset to the sentinel; trigger cannot re-fire |

The lower bound `> 946684800` excludes the reset value (year 2000) so the
automation cannot immediately re-trigger after resetting. The next trigger
requires a new future deadline to be set.

**`ev_safety_auto_deadline_passed`**

| Property | Value |
|----------|-------|
| Trigger | Template: `ev_computed_deadline.timestamp < now().timestamp()` AND `> 946684800` |
| Condition | None |
| Action | `zaptec.stop_charging`; reason = "Safety stop: auto departure deadline passed" |
| Auto-reset | Yes — `_auto_deadline_tick` will write a new future value within 5 minutes |

Pyscript's `_auto_deadline_tick` will roll over the computed deadline to the
next weekly departure within 5 minutes. No manual intervention required.

**`ev_safety_nordpool_unavailable`**

| Property | Value |
|----------|-------|
| Trigger | `sensor.nordpool_kwh_se3_sek_3_10_025` transitions to `unavailable` |
| Condition | None |
| Action | `zaptec.stop_charging`; reason = "Safety stop: Nordpool price data unavailable" |
| Auto-reset | No — charging will not restart until the optimizer recomputes with valid prices |

Charging without price data means the current schedule may be stale or incorrect.
The optimizer will recompute once prices return (via `on_price_update` trigger).

**Why SoC safety stop was removed (2026-04-26):**

A former `ev_safety_soc_reached` automation stopped charging at 95% SoC. This
was removed because the Mercedes Me BMS manages its own charge termination:
it reduces charge current and stops at the target SoC natively. The HA SoC
sensor lags reality by minutes (it's a cloud-polled value), so using it for
hard-stop decisions was unreliable. Do not re-add a SoC threshold stop.

---

## 8. Zaptec Integration

**Integration:** `zaptec` v0.8.6, entity prefix `laddbox`.

**Entities read by the optimizer:**

| Entity | Used for |
|--------|---------|
| `sensor.laddbox_charger_mode` | Current state; triggers immediate control loop |
| `sensor.laddbox_charge_power` | Feeds `sensor.ev_charging_power_kw` template |
| `sensor.laddbox_session_total_charge` | Feeds `sensor.ev_remaining_kwh` template |

**Service calls written by the optimizer:**

```yaml
# Start charging — charger_id is the Zaptec API UUID, NOT the HA device registry ID
service: zaptec.resume_charging
data:
  charger_id: d0775df2-6290-4569-bcd0-b579f8b9dde3

# Stop charging
service: zaptec.stop_charging
data:
  charger_id: d0775df2-6290-4569-bcd0-b579f8b9dde3

# Set current limit — installation_id, NOT charger_id
service: zaptec.limit_current
data:
  installation_id: dcead66e-4c50-4763-bc17-6ef3efe8be1f
  available_current: 10   # amps per phase, 6–25
```

> **Critical:** `resume_charging`/`stop_charging` take `charger_id` (Zaptec API
> UUID). `limit_current` takes `installation_id` (different UUID). Using the
> wrong parameter causes "Unable to find device" errors. The HA internal device
> registry ID (`5a4975267833e4f5f8589c76831d4526`) is a third, different value
> that should never be used with Zaptec services.

**Known Zaptec behaviors:**

*Auto-start on car connection:* When the car plugs in, Zaptec may automatically
start a charging session if it was previously authorized. The `on_zaptec_state_changed`
trigger fires immediately on the transition to `charging` and runs the full
control loop within ~2 seconds. If the current time is outside a scheduled
window, charging is stopped before the first 5-minute tick.

*`connected_finished` state:* After a charging session ends (either the car
reaches SoC target or charging is explicitly stopped), Zaptec reports
`connected_finished`. `switch.laddbox_charging` becomes `unavailable` in this
state. The optimizer uses `resume_charging`/`stop_charging` service calls rather
than the switch, which are reliable regardless of charger state.

*`button.laddbox_resume_charging` and `button.laddbox_stop_charging`:* These
button entities are marked deprecated by the Zaptec integration but are more
reliable than the service calls in some versions. However, `button.laddbox_stop_charging`
shows `unavailable` when no car is connected, making it unreliable for
programmatic use. The optimizer uses the service calls.

*Current limiting scope:* `zaptec.limit_current` applies at the installation
level (affects all chargers on the installation). With a single charger this
is equivalent to per-charger limiting.

---

## 9. Mercedes Me Integration

**Integration:** `mbapi2020`, entity prefix `jbb78w`.

**Entities used by the optimizer:**

| Entity | Used for |
|--------|---------|
| `sensor.jbb78w_state_of_charge` | Feeds `sensor.ev_remaining_kwh` when `ev_required_kwh = 0` |
| `binary_sensor.jbb78w_charging_active` | Status display only |
| `sensor.jbb78w_charging_status` | Status display only (numeric enum) |

**How SoC feeds `ev_remaining_kwh`:**

When `input_number.ev_required_kwh = 0`, the template sensor computes:
```jinja
((100 - soc) / 100 * 10) kWh
```
This assumes a 10 kWh usable battery capacity (rough PHEV estimate). If your
vehicle has a different usable battery size, set `ev_required_kwh` to your
actual target kWh instead.

**Known Mercedes Me behaviors:**

*BMS manages charge termination:* The car stops accepting charge at its own
internal SoC target (typically 80% or 100% depending on mode). HA does not
need to enforce this — hence the SoC safety stop removal.

*SoC recalculates after disconnect:* The reported SoC immediately after unplugging
may differ from the value during charging. This is normal BMS behavior (cell
balancing finalization). The template sensor uses the live value directly.

*Cloud polling latency:* The mbapi2020 integration polls via the Mercedes Me
cloud API. SoC updates may lag real-world state by 1–5 minutes. This is
acceptable for schedule planning but means SoC-based decisions (like
the former safety stop) were inherently unreliable.

*`sensor.jbb78w_charging_status` value 2:* This value means the car is
connected and in `CHARGE_BREAK` state (BMS deciding whether to charge). This
is normal during the gap between plug-in and BMS starting a charge cycle.

---

## 10. Tibber Pulse Integration

**Integration:** `tibber`, entity prefix `tibber_pulse_dianavagen_15`.

**Two sensors used by the consumption guard:**

| Entity | Unit | Role |
|--------|------|------|
| `sensor.tibber_pulse_dianavagen_15_accumulated_consumption_current_hour` | kWh | Energy consumed since the start of the current clock hour |
| `sensor.tibber_pulse_dianavagen_15_average_power` | W | Average house power draw since the start of the current clock hour |

**How `average_power` differs from `power`:**

`sensor.tibber_pulse_dianavagen_15_power` is the instantaneous reading from the
HAN port (sampled ~10 seconds). It is volatile and unsuitable for projection.

`average_power` is computed by Tibber (or the integration) as the mean over the
current hour. This is more representative of actual sustained draw and gives a
more stable projection.

**Why both sensors are needed:**

`accumulated_kwh` gives the baseline (energy already delivered). `average_power`
gives the forward-looking rate. The projection formula:
```
projected = accumulated_kwh + (house_kW + ev_kW) × (remaining_minutes / 60)
```
requires both — `accumulated_kwh` alone can't project forward (doesn't encode
current rate), and `average_power` alone doesn't include what's already consumed.

**Fail-open behavior:**

If either sensor is `unavailable` or `unknown`, the consumption guard is skipped
entirely and charging proceeds. This is deliberate — the guard is a cost-protection
mechanism, not a safety mechanism. Blocking charging because Tibber is momentarily
offline would be worse than the potential tariff cost of an unchecked charge hour.

The log will show:
```
ev_control_loop: Consumption guard: Tibber sensor(s) offline, skipping guard — fail-open
```

---

## 11. Known Limitations and Edge Cases

**1. Zaptec plug-in race condition**

When the car plugs in, Zaptec may auto-start charging before the control loop
runs. Without the `on_zaptec_state_changed` trigger, this unauthorized charging
could run for up to 5 minutes. The state trigger fires within ~2 seconds of the
`laddbox_charger_mode` transition and runs the full priority chain — if outside
a scheduled window, charging is stopped immediately.

**2. `sensor.ev_schedule` lost on HA restart**

`state.set()` values are held in pyscript's in-memory state and are not
persisted to HA's state store. After every HA restart, `sensor.ev_schedule`
returns `unknown` until the `_ev_recompute_on_startup` trigger fires and
recomputes. The `schedule_watchdog` (15-min trigger) provides a second recovery
path if startup recompute fails (e.g., Nordpool unavailable at boot).

**3. `input_datetime` state trigger requires `task.sleep(1–2)`**

When pyscript triggers on an `input_datetime` entity change, the `timestamp`
attribute (which `get_effective_deadline()` reads via `state.getattr()`) may
still reflect the old value within the same HA event cycle. Both
`on_input_changed` and `_on_auto_deadline_toggle` include `task.sleep(2)` to
allow HA state to fully propagate before the optimizer reads it.

**4. Pyscript lambda closure scoping**

Pyscript's AST restricts lambda closures — lambdas cannot close over local
variables from an enclosing scope without a default argument capture. The pattern
`lambda i, d=by_idx: d[i]["price"]` is required instead of `lambda i: by_idx[i]["price"]`.
Lambdas using only their own parameters (e.g., `lambda c: c[0]`) are safe.

**5. Generator expressions not supported in pyscript**

Pyscript's AST parser does not support generator expressions. Always use list
comprehensions:
```python
# WRONG — raises ParseError
sum(x["price"] for x in slots)

# CORRECT
sum([x["price"] for x in slots])
```

**6. `is_state()` not available in pyscript**

`is_state()` is a Jinja2 template helper only. In pyscript, use:
```python
(state.get("input_boolean.ev_tariff_guard_enabled") or "off") == "on"
```

**7. `state.getattr()` is single-argument only**

`state.getattr(entity, "attr")` raises `TypeError`. Always call
`state.getattr(entity)` and then `.get("key")` on the result dict.

**8. Timezone handling — always use epoch**

`state.get("input_datetime.ev_deadline")` may return a naive local-time string
in some HA versions. Parsing it with `.replace(tzinfo=TZ_LOCAL)` can yield a
timestamp 2 hours early during CEST (UTC+2). Always read `attributes.timestamp`
which returns a correct UTC epoch regardless of DST.

**9. Clustering trades price for contiguity**

The anti-toggling and gap-closing passes deliberately select slightly more
expensive slots to build contiguous charging windows. In practice the premium
is typically < 0.02 SEK (< 2 øre) per session. If you want to inspect the
cost of this tradeoff, compare `attributes.expected_cost` with a manual
calculation using the raw Nordpool cheapest-N slots.

**10. Over-scheduling by one slot margin (by design)**

After clustering, the trim pass ensures `total_kwh ≤ req_kwh + one_slot_kwh`.
This one-slot margin is intentional: the energy delivered by a single slot
(0.75–1.75 kWh) is the finest granularity available, so the schedule always
delivers at least `req_kwh` and at most `req_kwh + one_slot_kwh`. There is no
way to deliver exactly `req_kwh` without sub-slot current tapering.

**11. Weekly schedule grid iframe token security**

The HTML card embeds a long-lived HA access token in plain text. This token
allows read/write to any HA entity via the REST API. Anyone who can read the
file or intercept the iframe traffic can use this token. Mitigations:
- Keep the file outside version control (handled by `sync_from_ha.sh`).
- Use HTTPS (Nginx proxy with Let's Encrypt recommended for external access).
- Rotate the token periodically (see [Section 13.1](#131-routine-tasks)).

**12. Lovelace storage mode vs. YAML mode**

This HA instance runs in storage mode — dashboard configuration is held in
`.storage/lovelace.lovelace` in memory, not in YAML files on disk. Editing the
file on disk while HA is running has no effect (HA overwrites it on any UI save).
All Lovelace changes must be made through the HA raw editor (UI) or via
`POST /api/lovelace/config`. The card YAML in `CLAUDE.md § Future Work` is
authoritative for recreating the dashboard if needed.

**13. Multiple departure times per day**

The optimizer targets only the **next** upcoming departure (nearest-wins). With
multiple departure times on the same day (e.g., 09:00 and 15:00), the system
charges to the 09:00 deadline, then `_auto_deadline_tick` advances to 15:00 after
09:00 passes. This means the full `req_kwh` is targeted for each departure
independently. If you need different energy targets for each departure, use
`ev_required_kwh` and update it between trips.

---

## 12. Debugging Guide

### 12.1 Quick diagnostic checklist

Run these steps in order when something seems wrong:

1. **Check `input_text.ev_decision_reason`** — this is always the first thing
   to read. It contains the most recent decision with context.
   ```bash
   curl -s "https://koffern.duckdns.org:8123/api/states/input_text.ev_decision_reason" \
     -H "Authorization: Bearer TOKEN" | python3 -c "import sys,json; print(json.load(sys.stdin)['state'])"
   ```

2. **Check `sensor.ev_schedule` state and attributes** — verify windows exist,
   are in the future, and end before the deadline.
   ```bash
   curl -s "https://koffern.duckdns.org:8123/api/states/sensor.ev_schedule" \
     -H "Authorization: Bearer TOKEN" | python3 -m json.tool
   ```

3. **Check `input_datetime.ev_computed_deadline`** — verify the year is current
   (not 2099 or 2000) and the time is in the future.
   ```bash
   curl -s "https://koffern.duckdns.org:8123/api/states/input_datetime.ev_computed_deadline" \
     -H "Authorization: Bearer TOKEN" | python3 -c "
   import sys,json; d=json.load(sys.stdin)
   print(d['state'], 'ts=', d['attributes'].get('timestamp'))"
   ```

4. **Check `input_datetime.ev_deadline`** — if set to a future time, it may be
   suppressing the auto schedule (if it's nearer than the weekly departure).
   ```bash
   curl -s "https://koffern.duckdns.org:8123/api/states/input_datetime.ev_deadline" \
     -H "Authorization: Bearer TOKEN" | python3 -c "
   import sys,json; d=json.load(sys.stdin)
   print(d['state'], 'ts=', d['attributes'].get('timestamp'))"
   ```

5. **Check `sensor.ev_remaining_kwh`** — if 0 or very low, the optimizer will
   produce an empty schedule (nothing to charge).
   ```bash
   curl -s "https://koffern.duckdns.org:8123/api/states/sensor.ev_remaining_kwh" \
     -H "Authorization: Bearer TOKEN" | python3 -c "import sys,json; print(json.load(sys.stdin)['state'])"
   ```

6. **Check `sensor.nordpool_kwh_se3_sek_3_10_025` state** — if `unavailable` or
   `unknown`, no schedule can be computed.
   ```bash
   curl -s "https://koffern.duckdns.org:8123/api/states/sensor.nordpool_kwh_se3_sek_3_10_025" \
     -H "Authorization: Bearer TOKEN" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['state'], 'tomorrow_valid=', d['attributes'].get('tomorrow_valid'))"
   ```

7. **Check pyscript logs for errors:**
   ```bash
   ssh pi@debian "docker logs homeassistant 2>&1 | grep -E 'ev_optimizer|ev_control' | tail -40"
   ```

### 12.2 Common failure modes

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Schedule is empty (`[]`) | `ev_remaining_kwh` = 0 (battery full or `ev_required_kwh` overrides SoC sensor) | If SoC is actually low, set `ev_required_kwh` to a positive value; if truly full, this is correct |
| Schedule is empty | Nordpool data unavailable | Wait for Nordpool to recover; check integration is connected |
| Schedule is empty | No eligible slots (deadline too close, no slots between now and deadline) | Extend deadline or wait for tomorrow's prices |
| Schedule is `unknown` after HA restart | `_ev_recompute_on_startup` hasn't run yet | Wait 30–60 seconds; check pyscript is loaded via `/api/services` |
| Wrong slots selected (windows end after deadline) | `ev_computed_deadline` has a stale far-future value | Toggle `ev_auto_deadline` off and on; or set `ev_deadline` to the correct time |
| Charger starts outside scheduled window | Zaptec auto-started; `on_zaptec_state_changed` didn't stop it | Check pyscript is loaded; check `ev_decision_reason` for context |
| Charger doesn't start when schedule says ON | Hysteresis blocking | Check `ev_last_state_change`; wait 15 min or switch to "Charge now" |
| Charger doesn't start | Consumption guard blocking | Check `ev_decision_reason` for guard details; disable guard or reduce `ev_max_hourly_kwh` |
| Charger doesn't start | Mode is "Stop" | Switch to "Smart" |
| "safe default OFF" / "No schedule available" | pyscript not loaded or startup recompute failed | Check `pyscript` in `/api/services`; check pyscript logs |
| Year 2000 in `ev_deadline` | Safety automation reset it after deadline passed | Normal — set a new deadline if needed |
| Year 2099 in any deadline entity | Old code version left a sentinel | Toggle `ev_auto_deadline` off and on to re-run `auto_set_deadline()` |
| `ev_computed_deadline` not updating | `ev_auto_deadline` is off | Enable the toggle |
| Consumption guard firing unexpectedly | House base load high (e.g., oven, dryer) | Increase `ev_max_hourly_kwh` or disable guard temporarily |
| Safety stop triggered for target energy | `ev_remaining_kwh` dropped below 0.2 kWh | Normal behavior; charge is complete |
| "No schedule available" at startup | Nordpool unavailable during boot | Schedule watchdog will recover within 15 min |
| Dashboard grid card blank | Token expired or file missing | Check file exists at `/config/www/ev_schedule_grid.html`; verify token is valid |

### 12.3 Log commands

```bash
# All pyscript-related log lines (optimizer + control loop), last 50
ssh pi@debian "docker logs homeassistant 2>&1 | grep -E 'ev_optimizer|ev_control' | tail -50"

# Optimizer recompute traces only
ssh pi@debian "docker logs homeassistant 2>&1 | grep 'ev_optimizer' | tail -30"

# Control loop decisions only
ssh pi@debian "docker logs homeassistant 2>&1 | grep 'ev_control_loop' | tail -30"

# Safety automation triggers (look for automation IDs in HA logs)
ssh pi@debian "docker logs homeassistant 2>&1 | grep -E 'ev_safety|automation' | tail -20"

# Pyscript errors and warnings
ssh pi@debian "docker logs homeassistant 2>&1 | grep -E 'ERROR|WARNING' | grep -i pyscript | tail -30"

# Live log tail (Ctrl+C to stop)
ssh pi@debian "docker logs -f homeassistant 2>&1 | grep -E 'ev_optimizer|ev_control'"
```

### 12.4 Manual testing procedures

**Trigger recompute via API:**
```bash
curl -s -X POST "https://koffern.duckdns.org:8123/api/services/pyscript/ev_optimizer_recompute" \
  -H "Authorization: Bearer TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
```

**Test deadline trigger responsiveness** (set deadline 2 hours from now, verify
schedule updates within 5 seconds):
```bash
NOW=$(TZ="Europe/Stockholm" date +%s)
DEPARTURE=$(TZ="Europe/Stockholm" date -d "@$((NOW + 7200))" "+%Y-%m-%d %H:%M:%S")
curl -s -X POST "https://koffern.duckdns.org:8123/api/services/input_datetime/set_datetime" \
  -H "Authorization: Bearer TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"entity_id\":\"input_datetime.ev_deadline\",\"datetime\":\"$DEPARTURE\"}"
sleep 5
curl -s "https://koffern.duckdns.org:8123/api/states/sensor.ev_schedule" \
  -H "Authorization: Bearer TOKEN" | python3 -c "
import sys,json; d=json.load(sys.stdin)
print('deadline_source:', d['attributes'].get('deadline_source'))
print('windows:', len(d['attributes'].get('schedule', [])))"
```

**Simulate deadline pressure** (set `ev_slots_available` manually is not
possible as it's a template sensor; instead set `ev_deadline` to 15–30 minutes
from now and increase `ev_required_kwh` to force `slots_needed > slots_available`):
```bash
curl -s -X POST "https://koffern.duckdns.org:8123/api/services/input_number/set_value" \
  -H "Authorization: Bearer TOKEN" -H "Content-Type: application/json" \
  -d '{"entity_id":"input_number.ev_required_kwh","value":15.0}'
```

**Test consumption guard threshold** — lower `ev_max_hourly_kwh` to 0.1 during
tariff hours:
```bash
curl -s -X POST "https://koffern.duckdns.org:8123/api/services/input_number/set_value" \
  -H "Authorization: Bearer TOKEN" -H "Content-Type: application/json" \
  -d '{"entity_id":"input_number.ev_max_hourly_kwh","value":0.1}'
```
Check `ev_decision_reason` shows consumption guard block. Restore to 5.0 after.

**Verify Zaptec service calls** — manually call stop/start (car must be connected):
```bash
# Stop
curl -s -X POST "https://koffern.duckdns.org:8123/api/services/zaptec/stop_charging" \
  -H "Authorization: Bearer TOKEN" -H "Content-Type: application/json" \
  -d '{"charger_id":"d0775df2-6290-4569-bcd0-b579f8b9dde3"}'

# Start
curl -s -X POST "https://koffern.duckdns.org:8123/api/services/zaptec/resume_charging" \
  -H "Authorization: Bearer TOKEN" -H "Content-Type: application/json" \
  -d '{"charger_id":"d0775df2-6290-4569-bcd0-b579f8b9dde3"}'
```

---

## 13. Maintenance

### 13.1 Routine tasks

**Updating the weekly schedule:**

Use the grid card at the bottom of the `car` view. Enter times in HH:MM format
and press Save. Changes take effect within seconds (pyscript trigger). The
schedule is immediately persisted to `/config/pyscript/ev_schedule_data.json`.

**Adjusting tariff protection thresholds:**

- `ev_max_hourly_kwh`: set via the slider in the Tariff protection section.
  Start with your monthly peak threshold; reduce if you're consistently
  approaching the limit on high-consumption hours.
- `ev_max_tariff_power_kw`: lower this if you want to be more conservative
  about daytime charging rate; raise it (up to your circuit limit) to charge
  faster during the day when tariff risk is acceptable.

**Disabling tariff guard (planned: 2026-06-01):**

When Ellevio removes the Swedish monthly peak-power tariff scheme, the guard
becomes unnecessary overhead. Set `input_boolean.ev_tariff_guard_enabled` to
`off`. This:
- Disables the kWh/hour consumption cap.
- Removes tariff-hour current throttling (charger runs at 25A/7kW at all times).
- Removes the distinction between daytime and overnight slot kWh estimates in
  the optimizer (all slots become 1.75 kWh).

After disabling, verify that `ev_max_tariff_power_kw` is no longer affecting
slot selection by checking that `sensor.ev_charging_power_kw` shows 7 kW during
daytime hours.

**Rotating the HA long-lived access token in the grid HTML:**

1. In HA, go to Profile → Long-Lived Access Tokens → Create Token.
2. Copy the new token.
3. On the pi host:
   ```bash
   sshpass -p "eankod89" ssh pi@debian "sudo sed -i 's/const HA_TOKEN = \".*\"/const HA_TOKEN = \"NEW_TOKEN_HERE\"/' /media/pi/NextCloud/homeassistant/www/ev_schedule_grid.html"
   ```
4. Update the token in the local working copy and in CLAUDE.md if you use the
   development workflow.
5. Revoke the old token in HA Profile.

### 13.2 Syncing changes to GitHub

After editing any project file:

```bash
cd ~/projects/EV-Charging-Optimizer
./sync_from_ha.sh    # copies files from HA, scrubs the HTML token
git diff             # review changes
git add pyscript/ev_optimizer.py pyscript/ev_control_loop.py \
        packages/ev_optimizer.yaml CLAUDE.md MANUAL.md
git commit -m "describe your change"
git push
```

> **Warning:** Never `git add www/ev_schedule_grid.html` — it contains the real
> HA token. The `sync_from_ha.sh` script scrubs it to `PASTE_TOKEN_HERE` but
> verify before committing.

### 13.3 Restoring from GitHub after failure

If the HA config directory is lost or corrupted:

```bash
# 1. Clone the repo to the pi host
git clone https://github.com/PetrolHead2/EV-Charging-Optimizer.git /tmp/ev-restore

# 2. Copy pyscript files
sshpass -p "eankod89" ssh pi@debian "sudo cp /tmp/ev-restore/pyscript/ev_optimizer.py \
  /media/pi/NextCloud/homeassistant/pyscript/"
sshpass -p "eankod89" ssh pi@debian "sudo cp /tmp/ev-restore/pyscript/ev_control_loop.py \
  /media/pi/NextCloud/homeassistant/pyscript/"

# 3. Copy package file
sshpass -p "eankod89" ssh pi@debian "sudo cp /tmp/ev-restore/packages/ev_optimizer.yaml \
  /media/pi/NextCloud/homeassistant/packages/"

# 4. Copy HTML card (then manually add the token)
sshpass -p "eankod89" ssh pi@debian "sudo cp /tmp/ev-restore/www/ev_schedule_grid.html \
  /media/pi/NextCloud/homeassistant/www/"
# Edit the HTML to replace PASTE_TOKEN_HERE with your real token

# 5. Restore configuration.yaml additions (must be done manually — see CLAUDE.md)

# 6. Restart HA
ssh pi@debian "docker restart homeassistant"

# 7. After restart, verify pyscript loaded
curl -s "https://koffern.duckdns.org:8123/api/services" \
  -H "Authorization: Bearer TOKEN" | python3 -c "
import sys,json
services=json.load(sys.stdin)
print('pyscript' in services)"

# 8. Re-enter the weekly schedule via the grid card (if ev_schedule_data.json was lost)
```

### 13.4 Updating pyscript

Pyscript is installed via HACS. To update:

1. In HA, go to HACS → Integrations → pyscript.
2. If an update is available, click "Update".
3. Restart HA when prompted.
4. After restart, verify the optimizer still functions:
   ```bash
   curl -s -X POST "https://koffern.duckdns.org:8123/api/services/pyscript/ev_optimizer_recompute" \
     -H "Authorization: Bearer TOKEN" -H "Content-Type: application/json" -d '{}'
   sleep 5
   curl -s "https://koffern.duckdns.org:8123/api/states/sensor.ev_schedule" \
     -H "Authorization: Bearer TOKEN" | python3 -c "
   import sys,json; d=json.load(sys.stdin)
   print('state:', d['state'][:80])"
   ```

5. If pyscript changes its AST restrictions (new version may support or break
   generator expressions, lambda closures, etc.), review the Known Quirks in
   `CLAUDE.md` and test each one.

---

## 14. Enhancement Ideas

| Feature | Problem it solves | Approach | Complexity | Prerequisites |
|---------|------------------|----------|------------|---------------|
| **Solar integration** | Misses free solar energy during peak production | Read solar inverter sensor; bias slot selection toward midday when production > threshold; reduce cost calculation for solar-excess hours | Medium | Solar inverter with HA sensor (e.g. SolarEdge, Fronius, Huawei) |
| **Grid load balancing (SE3 congestion pricing)** | Raw spot price doesn't include distribution bottleneck fees | Integrate `sensor.electric_nordpool_current_price` (all-in price ≈ 3× spot) for true cost optimization | Low | Already available as `sensor.electric_nordpool_current_price` |
| **Price threshold for opportunistic mode** | Median threshold hardcoded; user can't tune it | Expose `input_number.ev_opportunistic_price_threshold`; use as ceiling instead of (or in addition to) median | Low | None |
| **Adaptive charging patterns** | Schedule occasionally suboptimal due to static energy estimate | Track actual kWh delivered per session; learn effective kWh/slot for this car; adjust `ev_max_tariff_power_kw` estimate | High | Multi-session history; statistics integration |
| **Multi-vehicle support** | Only one EV manageable | Parameterize all entity IDs; allow multiple optimizer instances with separate config | High | Multiple chargers/vehicles; infrastructure for namespacing |
| **Battery degradation awareness** | SoC estimate degrades as battery ages | Allow user to set actual usable kWh (not hardcoded 10 kWh); or read from vehicle telemetry | Low | User input only; or richer vehicle integration |
| **Push notifications on charging events** | No alerting when charge fails, deadline missed, or session ends | Add `notify.mobile_app_...` calls in control loop on key transitions | Low | HA companion app; mobile_app integration |
| **Cost reporting (weekly/monthly)** | No visibility into actual savings vs. naive charging | Track session costs in a statistics sensor; compare with "always charge immediately" baseline | Medium | HA statistics helpers; long-term recorder |
| **HA Energy dashboard integration** | `sensor.ev_schedule` not visible in HA Energy | Register `sensor.ev_schedule` as an energy sensor; add to Energy dashboard | Low | Proper sensor attributes (`state_class`, `device_class`) |
| **Automatic token rotation for grid card** | Long-lived token in HTML is a security risk | HA automation that creates a new token monthly and updates the file via the File Editor integration | Medium | File Editor integration or SSH automation |
| **Native Lovelace card** | Iframe requires embedded token; cross-origin issues | Implement as a custom Lovelace card (JavaScript) that uses HA's native auth; distribute via HACS | High | Frontend JavaScript; HACS packaging |
| **Weather-aware optimization** | Cold mornings require pre-conditioning; car needs more energy | Read weather forecast temperature; if < 5°C at departure time, add pre-conditioning kWh to target | Medium | HA weather integration |
| **Price spike guard** | Extreme price events can force expensive charging under deadline pressure | Add `input_number.ev_max_price_sek_kwh`; refuse to charge above this regardless of pressure | Low | None; simple additional check in control loop |
| **Tibber real-time prices** | Tibber subscribers have real-time spot prices; could use more granular data | Replace Nordpool integration with Tibber real-time price subscription for tighter accuracy | Medium | Tibber subscription plan with real-time access |

---

## 15. Changelog

Most recent changes first.

```
2026-04-26  Fix: use nearest deadline instead of manual-priority in get_effective_deadline()
            get_effective_deadline() now collects all valid future deadlines (manual + auto)
            and returns the nearest one. Previously manual ev_deadline suppressed the weekly
            auto schedule even when the weekly departure was sooner. System now automatically
            chains from weekly departures to a distant manual deadline.
            sensor.ev_schedule attributes include deadline_source and deadline_ts.
            ev_control_loop decision reason includes active deadline info.

2026-04-26  Add explicit return in auto_set_deadline when no departure found
            No longer writes sentinel dates when schedule is empty — returns
            immediately, leaving ev_computed_deadline at its last valid value.

2026-04-26  Add human-readable schedule card to Lovelace dashboard
            Markdown card in car view shows charging windows with status labels
            (upcoming/active/done), price, kWh, and cost. ASCII dash used instead
            of Unicode en-dash to avoid rendering issues.

2026-04-26  Remove SoC safety stop; fix year-2000 cosmetic display bug
            ev_safety_soc_reached automation removed — Mercedes BMS handles termination.
            loadStatus() in ev_schedule_grid.html now reads attributes.timestamp (UTC epoch)
            to avoid showing year-2000 sentinel dates in the Next departure field.
            Added conditional Lovelace card hiding ev_computed_deadline when in the past.

2026-04-26  Remove all hardcoded year constants (2030/2099 sentinels)
            SCHEDULE_HORIZON_DAYS = 8 replaces all sentinel years. Stale far-future
            values are detected and logged but not written; get_effective_deadline()
            rejects them via the 5-minute minimum buffer.

2026-04-25  Fix: post-merge trim, eliminate 2099 sentinel everywhere
            Surplus-trim pass removes most expensive slots after clustering until
            total kWh is within one minimum-power slot of the target.

2026-04-25  Fix: slot filtering uses explicit start_ts/end_ts, add diagnostic logging
            All slot eligibility checks converted to epoch timestamp comparisons.
            Diagnostic log lines added at compute_schedule() entry and post-filter
            to make timezone bugs immediately visible.

2026-04-25  Fix: remove 2099 sentinel, add gap-closing pass in clustering
            Gap-closing pass merges adjacent windows separated by ≤ 30 min when gap
            price ≤ 1.5× selected average. Reduces charger on/off events.

2026-04-24  Fix: BUG D/E — departure datetime construction and opportunistic mode
            get_next_departure() uses explicit year/month/day components; never
            strptime() without full date. compute_opportunistic_schedule() separated
            from compute_schedule() for clarity.

2026-04-24  Fix slot deadline filtering (BUG A) and over-scheduling after clustering (BUG B)
            BUG A: slots with end > deadline were included due to datetime comparison
            timezone mismatch. Fixed with epoch comparisons throughout.
            BUG B: anti-toggling added more slots than needed; trim pass added.

2026-04-23  Fix unreliable recompute triggers for deadline entity changes
            Multi-entity @state_trigger replaces individual per-entity triggers.
            task.sleep(2) added before reads to handle compound HA state updates.

2026-04-23  Add immediate Zaptec state-change trigger, refactor trigger structure
            on_zaptec_state_changed closes the race between car plug-in and the
            5-minute tick. Stops unauthorized charging within ~2 seconds.

2026-04-23  Fix deadline pressure bypassed by hysteresis in control loop
            Priority chain reordered: pressure check (level 3) now runs before
            hysteresis evaluation, bypassing the 15-minute guard when urgent.

2026-04-23  Fix timezone bug in deadline comparisons (CEST UTC+2 off-by-2h)
            get_effective_deadline() reads attributes.timestamp (UTC epoch float)
            instead of parsing state string with naive local timezone.

2026-04-22  Separate manual deadline from auto-computed deadline
            input_datetime.ev_deadline — human input only; pyscript never writes.
            input_datetime.ev_computed_deadline — pyscript only via auto_set_deadline().
            Weekly schedule grid and auto_deadline_tick added.

2026-04-22  Fix deadline trigger, schedule pruning, and state overflow
            Pruning pass removes windows starting after deadline on recompute.
            Compact epoch JSON format for sensor.ev_schedule state (255-char limit).

2026-04-21  Update CLAUDE.md with GitHub repo URL

2026-04-21  Initial commit: EV Charging Optimizer
            Core optimizer (ev_optimizer.py), control loop (ev_control_loop.py),
            package YAML (ev_optimizer.yaml), weekly schedule grid HTML.
```
