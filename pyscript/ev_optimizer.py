"""
ev_optimizer.py — EV Charging Optimizer pyscript module

Computes the cheapest set of 15-min Nordpool slots to charge the EV.

Service  : pyscript.ev_optimizer_recompute
Output   : sensor.ev_schedule  (state = JSON list of windows, rich attributes)
Triggers : Nordpool price update, deadline/target change, hourly failsafe,
           weekly schedule change, 5-min auto-deadline tick

Price data structure (sensor.nordpool_kwh_se3_sek_3_10_025):
  today[0..95]    – 96 floats, index 0 = 00:00 local time, step = 15 min, SEK/kWh
  tomorrow[0..95] – same for next day (valid when tomorrow_valid == True)

Effective charging power is time-aware:
  - Tariff hours (06:00–22:00) with ev_tariff_guard_enabled on:
      power = input_number.ev_max_tariff_power_kw
  - Outside tariff hours or guard disabled:
      power = sensor.ev_charging_power_kw  (defaults to 7 kW when idle)
The optimizer accounts for this when selecting slots, calculating how many
are needed, and reporting expected_cost and total_kwh.

Deadline arbitration — two distinct entities:
  input_datetime.ev_deadline          — human input only; never written by pyscript
  input_datetime.ev_computed_deadline — written only by pyscript (auto_set_deadline);
                                        never shown as an editable field in the UI
  get_effective_deadline() arbitrates: manual deadline takes priority if it is in
  the future; otherwise falls back to the computed deadline; else opportunistic mode.

Auto-deadline mode (input_boolean.ev_auto_deadline = on):
  Reads input_text.ev_weekly_schedule JSON every 5 minutes and writes the next
  upcoming departure to input_datetime.ev_computed_deadline.
  Empty days trigger opportunistic mode (no deadline).
  When off: ev_computed_deadline is not updated; ev_deadline is fully manual.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Entity IDs ─────────────────────────────────────────────────────────────────
PRICE_ENT              = "sensor.nordpool_kwh_se3_sek_3_10_025"
REQ_KWH_ENT            = "input_number.ev_required_kwh"
REM_KWH_ENT            = "sensor.ev_remaining_kwh"
DEADLINE_ENT           = "input_datetime.ev_deadline"           # human input only
COMPUTED_DEADLINE_ENT  = "input_datetime.ev_computed_deadline"  # pyscript only
SCHEDULE_ENT           = "sensor.ev_schedule"
GUARD_ENT              = "input_boolean.ev_tariff_guard_enabled"
MAX_TARIFF_KW_ENT      = "input_number.ev_max_tariff_power_kw"
CHARGE_PWR_ENT         = "sensor.ev_charging_power_kw"
WEEKLY_SCHED_ENT       = "input_text.ev_weekly_schedule"
AUTO_DEADLINE_ENT      = "input_boolean.ev_auto_deadline"

# ── Charger / slot constants ───────────────────────────────────────────────────
TZ_LOCAL    = ZoneInfo("Europe/Stockholm")
CHARGER_KW  = 7.0           # full-power fallback when sensor unavailable
SLOT_MIN    = 15
SLOT_H      = SLOT_MIN / 60.0   # 0.25 h
# SLOT_KWH at full power = CHARGER_KW * SLOT_H = 1.75 kWh (reference; actual
# kWh per slot varies with effective_power_kw() during tariff hours)
TARIFF_HOUR_START    = 6
TARIFF_HOUR_END      = 22
MERGE_RATIO          = 0.20    # merge isolated slot if neighbour ≤ 20 % of avg price
DEADLINE_SENTINEL_YR = 2099    # year used as "no departure" sentinel in ev_computed_deadline
SCHEDULE_DATA_FILE   = "/config/pyscript/ev_schedule_data.json"

# Weekday index → schedule JSON key (Monday=0 … Sunday=6)
_DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# ── Internal helpers ───────────────────────────────────────────────────────────

def _build_slots():
    """
    Build full slot list from Nordpool today + tomorrow prices.

    Returns list of dicts:
      {start (UTC datetime), end (UTC datetime), price (float SEK/kWh), idx (int)}
    today[0]  → midnight local time
    today[95] → 23:45 local time
    """
    _attrs     = state.getattr(PRICE_ENT) or {}
    today_px   = _attrs.get("today", [])   or []
    tmrw_px    = _attrs.get("tomorrow", []) or []
    tmrw_valid = _attrs.get("tomorrow_valid", False) or False

    local_now      = datetime.now(tz=TZ_LOCAL)
    midnight_today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)

    slots = []
    days  = [(today_px, 0)]
    if tmrw_valid and tmrw_px:
        days.append((tmrw_px, 1))

    for prices, day_off in days:
        midnight = midnight_today + timedelta(days=day_off)
        for i, price in enumerate(prices):
            t0 = (midnight + timedelta(minutes=SLOT_MIN * i)).astimezone(timezone.utc)
            slots.append({
                "start": t0,
                "end":   t0 + timedelta(minutes=SLOT_MIN),
                "price": float(price),
                "idx":   day_off * 96 + i,
            })
    return slots


def effective_power_kw(slot_start):
    """
    Returns the effective charging power in kW for a given slot start time,
    accounting for tariff hour limiting.

    slot_start must be a timezone-aware datetime object.
    """
    local_hour    = slot_start.astimezone(ZoneInfo(hass.config.time_zone)).hour
    tariff_active = (local_hour >= TARIFF_HOUR_START and local_hour < TARIFF_HOUR_END)
    guard_enabled = (state.get(GUARD_ENT) or "off") == "on"

    if tariff_active and guard_enabled:
        raw = state.get(MAX_TARIFF_KW_ENT)
        return float(raw) if raw not in (None, 'unknown', 'unavailable') else 3.0
    else:
        raw = state.get(CHARGE_PWR_ENT)
        return float(raw) if raw not in (None, 'unknown', 'unavailable') else CHARGER_KW


def get_next_departure():
    """
    Reads input_text.ev_weekly_schedule JSON and returns the next upcoming
    departure as a timezone-aware datetime.  Returns None if no departure
    is found within the next 7 days (triggers opportunistic mode).

    Logic:
      1. Parse JSON from input_text.ev_weekly_schedule
      2. Get current local datetime using hass.config.time_zone
      3. For each of the next 7 days (starting today):
           map weekday to key: mon/tue/wed/thu/fri/sat/sun
           look through that day's departure times sorted ascending
           skip any time that is < 15 minutes in the future
           return the first qualifying datetime
      4. If no qualifying time found across all 7 days: return None
    """
    raw = state.get(WEEKLY_SCHED_ENT)
    if not raw or raw in ("unknown", "unavailable", ""):
        log.warning("ev_optimizer: weekly schedule unavailable, falling back to opportunistic mode")
        return None

    try:
        schedule = json.loads(str(raw))
    except Exception as exc:
        log.warning(f"ev_optimizer: weekly schedule parse error: {exc}, falling back to opportunistic mode")
        return None

    local_tz  = ZoneInfo(hass.config.time_zone)
    now_local = datetime.now(tz=local_tz)
    min_ahead = now_local + timedelta(minutes=15)

    for day_offset in range(7):
        target_date = (now_local + timedelta(days=day_offset)).date()
        day_key     = _DAY_KEYS[target_date.weekday()]
        times       = schedule.get(day_key, []) or []

        for time_str in sorted(times):
            try:
                parts = str(time_str).split(":")
                h = int(parts[0])
                m = int(parts[1])
            except Exception:
                continue
            candidate = datetime(
                target_date.year, target_date.month, target_date.day,
                h, m, 0, tzinfo=local_tz,
            )
            if candidate >= min_ahead:
                return candidate

    return None


def auto_set_deadline():
    """
    Calls get_next_departure() and writes the result to
    input_datetime.ev_computed_deadline.

    This is the ONLY function that writes to ev_computed_deadline.
    input_datetime.ev_deadline is NEVER written by pyscript — it belongs
    to the user as a manual override.

    When auto-deadline mode is active (input_boolean.ev_auto_deadline = on):
      - If a departure is found: sets ev_computed_deadline to that datetime.
      - If no departure found: sets ev_computed_deadline to the 2099-12-31
        sentinel so get_effective_deadline() uses opportunistic mode.
    """
    departure = get_next_departure()

    if departure is None:
        input_datetime.set_datetime(
            entity_id = COMPUTED_DEADLINE_ENT,
            datetime  = "2099-12-31 23:59:59",
        )
        log.info("ev_optimizer: auto deadline: no departure in next 7 days — opportunistic mode")
    else:
        dt_str = departure.strftime("%Y-%m-%d %H:%M:%S")
        input_datetime.set_datetime(
            entity_id = COMPUTED_DEADLINE_ENT,
            datetime  = dt_str,
        )
        log.info(f"ev_optimizer: auto deadline computed: {dt_str}")


def get_effective_deadline():
    """
    Returns the deadline timestamp the optimizer should use, as a float
    (epoch seconds) or None for opportunistic mode.

    Priority:
      1. input_datetime.ev_deadline — manual user override — if set and
         more than 5 minutes in the future.
      2. input_datetime.ev_computed_deadline — auto from weekly schedule —
         if set and more than 5 minutes in the future.
      3. None — opportunistic mode (no deadline).

    Both entities are read via their pre-computed 'timestamp' attribute
    (always a correct UTC epoch regardless of DST) rather than parsing the
    state string (which may be a naive local-time string).
    """
    now_ts    = datetime.now(tz=timezone.utc).timestamp()
    min_ahead = now_ts + 5 * 60   # must be at least 5 minutes away

    for ent, label in [
        (DEADLINE_ENT,         "manual"),
        (COMPUTED_DEADLINE_ENT, "auto"),
    ]:
        attrs  = state.getattr(ent) or {}
        ts_raw = attrs.get("timestamp")
        if ts_raw in (None, "unknown", "unavailable"):
            continue
        try:
            ts = float(ts_raw)
        except Exception:
            continue
        sentinel_ts = datetime(DEADLINE_SENTINEL_YR, 1, 1, tzinfo=timezone.utc).timestamp()
        if ts >= sentinel_ts:
            continue   # far-future sentinel = no departure scheduled
        if ts > min_ahead:
            log.info(f"ev_optimizer: using {label} deadline: {state.get(ent)}")
            return ts

    log.debug("ev_optimizer: no valid deadline — opportunistic mode")
    return None


def compute_schedule(req_kwh, deadline_ts):
    """
    Select the cheapest slots within the eligible period to deliver req_kwh.

    Args:
        req_kwh     (float)      – energy required in kWh
        deadline_ts (float|None) – POSIX timestamp of departure deadline,
                                   or None for opportunistic mode

    Returns:
        (windows, mode, required_slots)
        windows        – list of contiguous charging windows sorted by start time:
                         [{start (ISO str), end (ISO str), price (float),
                           slots (int), kwh (float), cost (float)}]
        mode           – "deadline" | "opportunistic"
        required_slots – number of 15-min slots counted to meet req_kwh

    Effective kWh per slot is time-aware (see effective_power_kw()).
    Slot count is computed by walking the eligible pool cheapest-first and
    accumulating effective kWh until the target is met. Overnight slots
    (full power, 1.75 kWh each) cover the target in fewer slots than
    tariff-limited daytime slots (0.75 kWh each), so they are preferred
    both on price and energy efficiency.

    Opportunistic mode: 48-hour horizon, only slots at or below median price.
    Anti-toggling: isolated selected slots (no adjacent selected neighbour) are
    merged with their cheapest neighbour if the price difference is within
    MERGE_RATIO × average eligible price.
    """
    if req_kwh <= 0:
        return [], "opportunistic", 0

    all_slots = _build_slots()
    now_utc   = datetime.now(tz=timezone.utc)

    # ── Determine eligible window ──────────────────────────────────────────────
    if deadline_ts:
        dl_dt    = datetime.fromtimestamp(deadline_ts, tz=timezone.utc)
        eligible = [s for s in all_slots if s["start"] >= now_utc and s["end"] <= dl_dt]
        mode     = "deadline"
    else:
        horizon  = now_utc + timedelta(hours=48)
        eligible = [s for s in all_slots if s["start"] >= now_utc and s["end"] <= horizon]
        mode     = "opportunistic"

    if not eligible:
        log.warning(
            f"ev_optimizer: no eligible slots "
            f"(required={req_kwh:.2f} kWh, deadline={deadline_ts})"
        )
        return [], mode, 0

    # Opportunistic: restrict candidate pool to ≤ median price slots
    if mode == "opportunistic":
        sorted_px = sorted([s["price"] for s in eligible])
        median    = sorted_px[len(sorted_px) // 2]
        eligible  = [s for s in eligible if s["price"] <= median]

    # ── Count required slots by walking eligible pool in price order ──────────
    # Slots deliver different kWh depending on time of day (effective_power_kw).
    # Walk cheapest-first, accumulating kWh until the target is met. This gives
    # the minimum n such that the n cheapest slots actually deliver ≥ req_kwh —
    # consistent with the selection step below. Overnight slots (1.75 kWh each)
    # require fewer slots than tariff-limited daytime slots (0.75 kWh each).
    by_price    = sorted(eligible, key=lambda s: s["price"])
    n           = 0
    accumulated = 0.0
    for s in by_price:
        if accumulated >= req_kwh:
            break
        accumulated += effective_power_kw(s["start"]) * SLOT_H
        n += 1
    n = min(n, len(eligible))

    by_idx   = {s["idx"]: s for s in eligible}
    selected = {s["idx"] for s in by_price[:n]}

    # ── Anti-toggling pass ─────────────────────────────────────────────────────
    # An isolated slot (no adjacent selected neighbour) causes two start/stop
    # events. Merge it with its cheapest unselected neighbour when the price
    # premium is small (≤ MERGE_RATIO × average price).
    avg_px    = sum([s["price"] for s in eligible]) / len(eligible)
    gap_limit = avg_px * MERGE_RATIO

    for _ in range(20):          # cap passes to prevent loops on unusual data
        merged = False
        for idx in sorted(selected):
            if (idx - 1) in selected or (idx + 1) in selected:
                continue         # already has a neighbour – not isolated
            candidates = [
                i for i in (idx - 1, idx + 1)
                if i in by_idx and i not in selected
            ]
            if not candidates:
                continue
            best = min(candidates, key=lambda i, d=by_idx: d[i]["price"])
            if abs(by_idx[best]["price"] - by_idx[idx]["price"]) <= gap_limit:
                selected.add(best)
                merged = True
        if not merged:
            break

    # ── Group consecutive indices into contiguous windows ─────────────────────
    seq     = sorted(selected)
    groups  = []
    current = [seq[0]]
    for idx in seq[1:]:
        if idx == current[-1] + 1:
            current.append(idx)
        else:
            groups.append(current)
            current = [idx]
    groups.append(current)

    # Read HA timezone dynamically so the output is portable to any installation
    local_tz = ZoneInfo(hass.config.time_zone)

    windows = []
    for grp in groups:
        px_grp   = [by_idx[i]["price"] for i in grp]
        kwh_grp  = [effective_power_kw(by_idx[i]["start"]) * SLOT_H for i in grp]
        cost_grp = [px * kwh for px, kwh in zip(px_grp, kwh_grp)]
        windows.append({
            "start": by_idx[grp[0]]["start"].astimezone(local_tz).isoformat(),
            "end":   by_idx[grp[-1]]["end"].astimezone(local_tz).isoformat(),
            "price": round(sum(px_grp) / len(px_grp), 4),
            "slots": len(grp),
            "kwh":   round(sum(kwh_grp), 2),
            "cost":  round(sum(cost_grp), 4),
        })

    return windows, mode, n


# ── Public service ─────────────────────────────────────────────────────────────

@service
def ev_optimizer_recompute(**kwargs):
    """
    Recompute EV charging schedule and write result to sensor.ev_schedule.

    Registered as: pyscript.ev_optimizer_recompute
    Call manually: Developer Tools → Services → pyscript.ev_optimizer_recompute
    """
    # ── Entry log ─────────────────────────────────────────────────────────────
    log.info(
        f"ev_optimizer: recomputing at {datetime.now(tz=TZ_LOCAL).isoformat()}, "
        f"manual_deadline={state.get(DEADLINE_ENT)}, "
        f"auto_deadline={state.get(COMPUTED_DEADLINE_ENT)}, "
        f"remaining={state.get(REM_KWH_ENT) or '?'} kWh"
    )

    # ── Auto deadline: refresh ev_computed_deadline ───────────────────────────
    # Called here so startup / Nordpool-update / hourly paths always have a
    # fresh computed deadline without waiting for the 5-min tick.
    # NEVER writes to input_datetime.ev_deadline — that belongs to the user.
    if (state.get(AUTO_DEADLINE_ENT) or "off") == "on":
        auto_set_deadline()

    # ── Required energy ────────────────────────────────────────────────────────
    req_raw = state.get(REQ_KWH_ENT)
    req_kwh = float(req_raw) if req_raw not in (None, "unknown", "unavailable") else 0.0

    if req_kwh <= 0:
        rem_raw = state.get(REM_KWH_ENT)
        req_kwh = float(rem_raw) if rem_raw not in (None, "unknown", "unavailable") else 0.0

    # ── Effective deadline ─────────────────────────────────────────────────────
    # Manual ev_deadline takes priority; falls back to auto ev_computed_deadline;
    # returns None for opportunistic mode.
    deadline_ts = get_effective_deadline()

    # ── Compute schedule ───────────────────────────────────────────────────────
    windows, mode, required_slots = compute_schedule(req_kwh, deadline_ts)

    # ── Prune windows that start at or after the deadline ─────────────────────
    # compute_schedule filters slots so that slot.end <= deadline, but when the
    # deadline moves earlier while a cached schedule is in flight, windows from
    # the previous computation may be stale. Re-filter here to be safe.
    if deadline_ts is not None:
        pre_prune = len(windows)
        windows = [
            w for w in windows
            if datetime.fromisoformat(w["start"]).timestamp() < deadline_ts
        ]
        pruned = pre_prune - len(windows)
        if pruned:
            log.info(f"ev_optimizer: pruned {pruned} window(s) that exceeded new deadline")

    total_kwh  = round(sum([w["kwh"]  for w in windows]), 2)
    total_cost = round(sum([w["cost"] for w in windows]), 2)
    now_iso    = datetime.now(tz=timezone.utc).isoformat()

    # ── Publish to sensor.ev_schedule ─────────────────────────────────────────
    # State string is limited to 255 chars in HA. Store only epoch-integer
    # start/end per window using compact keys {"s":..., "e":...}  (~30 chars each
    # vs ~75 chars for ISO timestamps with offset). Supports ≥ 8 windows safely.
    # Full window data (ISO times, price, slots, kwh, cost) goes in
    # attributes.schedule for Lovelace display.
    state_windows = [
        {
            "s": int(datetime.fromisoformat(w["start"]).timestamp()),
            "e": int(datetime.fromisoformat(w["end"]).timestamp()),
        }
        for w in windows
    ]

    state.set(
        SCHEDULE_ENT,
        value          = json.dumps(state_windows, separators=(',', ':')),
        new_attributes = {
            "schedule":       windows,
            "expected_cost":  total_cost,
            "total_kwh":      total_kwh,
            "computed_at":    now_iso,
            "mode":           mode,
            "required_slots": required_slots,
            "required_kwh":   round(req_kwh, 2),
            "friendly_name":  "EV Charging Schedule",
            "icon":           "mdi:calendar-clock",
        },
    )

    log.info(
        f"ev_optimizer: mode={mode}  need={required_slots} slots ({req_kwh:.2f} kWh)"
        f"  → {len(windows)} window(s) | {total_kwh} kWh | {total_cost:.2f} SEK"
    )


# ── Automatic triggers ─────────────────────────────────────────────────────────

@state_trigger(
    "sensor.nordpool_kwh_se3_sek_3_10_025",
    "input_number.ev_required_kwh",
    "sensor.ev_remaining_kwh",
    "input_boolean.ev_tariff_guard_enabled",
    "input_number.ev_max_tariff_power_kw",
)
def _ev_recompute_on_change(**kwargs):
    """Fire recompute whenever price data, target energy, or tariff settings change."""
    ev_optimizer_recompute()


@state_trigger("input_datetime.ev_deadline")
def on_deadline_changed(**kwargs):
    """
    Dedicated trigger for manual deadline changes.

    Uses task.sleep(1) to allow HA state to fully settle before reading the
    new deadline value. Without the delay, state.getattr(DEADLINE_ENT) may
    still return the old value in the same event cycle.

    NOTE: pyscript never writes to input_datetime.ev_deadline, so this
    trigger fires only on genuine user actions (UI, automation, API call).
    """
    log.info("ev_optimizer: manual deadline changed, recomputing")
    task.sleep(1)
    ev_optimizer_recompute()


@state_trigger("input_text.ev_weekly_schedule")
def _on_weekly_schedule_changed(**kwargs):
    """Recompute computed deadline and schedule whenever the weekly schedule is edited."""
    if (state.get(AUTO_DEADLINE_ENT) or "off") == "on":
        auto_set_deadline()
        ev_optimizer_recompute()


@state_trigger("input_text.ev_weekly_schedule")
def persist_weekly_schedule(**kwargs):
    """Save schedule to file whenever it changes so it survives HA restarts."""
    schedule = state.get(WEEKLY_SCHED_ENT)
    if schedule in (None, "unknown", "unavailable", ""):
        return
    try:
        task.executor(Path(SCHEDULE_DATA_FILE).write_text, schedule)
        log.info("ev_optimizer: weekly schedule persisted to file")
    except Exception as e:
        log.warning(f"ev_optimizer: weekly schedule persist failed: {e}")


@state_trigger("input_boolean.ev_auto_deadline")
def _on_auto_deadline_toggle(**kwargs):
    """When auto deadline is switched on, immediately compute the next departure."""
    if (state.get(AUTO_DEADLINE_ENT) or "off") == "on":
        task.sleep(1)
        auto_set_deadline()
        ev_optimizer_recompute()


@time_trigger("period(now, 5min)")
def _auto_deadline_tick(**kwargs):
    """
    Re-evaluate the next departure every 5 minutes and update ev_computed_deadline.

    Handles rollover: when a departure time passes (e.g. 08:00 → 18:00 same day,
    or last time today → first time tomorrow), the computed deadline advances.
    Only runs when input_boolean.ev_auto_deadline is on.

    Never touches input_datetime.ev_deadline — that is the user's manual override.
    """
    if (state.get(AUTO_DEADLINE_ENT) or "off") != "on":
        return
    old_dl = state.get(COMPUTED_DEADLINE_ENT)
    auto_set_deadline()
    new_dl = state.get(COMPUTED_DEADLINE_ENT)
    if old_dl != new_dl:
        log.info(f"ev_optimizer: computed deadline rolled over: {old_dl} → {new_dl}")
        ev_optimizer_recompute()


@time_trigger("period(now, 1h)")
def _ev_recompute_hourly(**kwargs):
    """Hourly failsafe — keeps the schedule fresh even without state changes."""
    ev_optimizer_recompute()


@time_trigger("startup")
def restore_weekly_schedule(**kwargs):
    """
    Read the persisted schedule from ev_schedule_data.json and write it to
    input_text.ev_weekly_schedule on HA startup.

    input_text helpers lose their value on restart unless an initial: is set.
    We intentionally leave initial: blank and restore from this file instead,
    so changes made via the UI survive restarts.
    """
    try:
        schedule_json = task.executor(Path(SCHEDULE_DATA_FILE).read_text).strip()
        input_text.set_value(entity_id=WEEKLY_SCHED_ENT, value=schedule_json)
        log.info("ev_optimizer: weekly schedule restored from file")
    except Exception as e:
        log.warning(f"ev_optimizer: weekly schedule restore failed: {e}")


@time_trigger("startup")
def _ev_recompute_on_startup(**kwargs):
    """Recompute schedule on HA startup to restore lost pyscript sensor state."""
    log.info("ev_optimizer: recomputing schedule after startup")
    ev_optimizer_recompute()
