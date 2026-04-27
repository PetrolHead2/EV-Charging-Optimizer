"""
ev_optimizer.py — EV Charging Optimizer pyscript module

Computes the cheapest set of 15-min Nordpool slots to charge the EV.

Service  : pyscript.ev_optimizer_recompute
Output   : sensor.ev_schedule  (state = JSON list of windows, rich attributes)
Triggers : Nordpool price update, deadline/target change, hourly failsafe,
           weekly schedule change, 5-min auto-deadline tick

Price data structure (sensor.nordpool_kwh_se3_sek_3_10_025):
  raw_today    – list of 96 dicts {start, end, value}; 15-min slots, full 24 h
  raw_tomorrow – same for next day (only valid when tomorrow_valid == True)
  today/tomorrow – flat 24-entry hourly arrays; NOT used by _build_slots() because
                   treating 24 hourly entries as 96×15-min indices caps coverage at 6 h

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
SCHEDULE_HORIZON_DAYS = 8   # get_next_departure() looks 7 days ahead; anything beyond this is stale
SCHEDULE_DATA_FILE   = "/config/pyscript/ev_schedule_data.json"

# Weekday index → schedule JSON key (Monday=0 … Sunday=6)
_DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# ── Internal helpers ───────────────────────────────────────────────────────────

def _build_slots():
    """
    Build full slot list from Nordpool raw_today + raw_tomorrow prices.

    Uses raw_today/raw_tomorrow (lists of dicts with start, end, value keys)
    rather than the flat today/tomorrow price arrays.  The flat arrays contain
    only 24 hourly entries; using them as 15-min slot indices (SLOT_MIN=15)
    produced coverage of only 00:00–05:45 per day — slots after 06:00 were
    absent from the pool entirely.  The raw attributes carry explicit UTC-aware
    start/end timestamps for all 96 15-minute slots per day.

    start/end may be pre-parsed datetime objects or ISO strings in pyscript —
    both are handled transparently (same fix as get_slot_price in ev_control_loop).

    Returns list of dicts:
      {start (UTC datetime), end (UTC datetime), price (float SEK/kWh), idx (int)}
    Sorted by start time; idx is sequential position used by merge_into_windows().
    """
    _attrs       = state.getattr(PRICE_ENT) or {}
    raw_today    = _attrs.get("raw_today",    []) or []
    raw_tomorrow = _attrs.get("raw_tomorrow", []) or []
    tmrw_valid   = _attrs.get("tomorrow_valid", False) or False

    all_raw = raw_today + (raw_tomorrow if tmrw_valid else [])

    log.info(
        f"_build_slots: raw_today={len(raw_today)} "
        f"raw_tomorrow={len(raw_tomorrow) if tmrw_valid else 0} "
        f"(tomorrow_valid={tmrw_valid}) "
        f"total_raw={len(all_raw)}"
    )

    local_tz = ZoneInfo(hass.config.time_zone)
    slots = []
    for s in all_raw:
        try:
            start_raw = s["start"]
            end_raw   = s["end"]
            start = (datetime.fromisoformat(start_raw) if isinstance(start_raw, str)
                     else start_raw).astimezone(timezone.utc)
            end   = (datetime.fromisoformat(end_raw)   if isinstance(end_raw,   str)
                     else end_raw).astimezone(timezone.utc)
            price = float(s["value"])
        except Exception as exc:
            log.warning(f"ev_optimizer: _build_slots: skipping slot: {exc}")
            continue
        slots.append({"start": start, "end": end, "price": price})

    slots.sort(key=lambda s: s["start"].timestamp())
    for i, s in enumerate(slots):
        s["idx"] = i

    if slots:
        log.info(
            f"_build_slots: {len(slots)} valid slots "
            f"{slots[0]['start'].astimezone(local_tz).strftime('%d %b %H:%M')} → "
            f"{slots[-1]['end'].astimezone(local_tz).strftime('%d %b %H:%M')}"
        )
    return slots


def effective_power_kw(slot_start):
    """
    Returns the effective charging power in kW for a given slot start time,
    accounting for tariff hour current throttling.

    slot_start must be a timezone-aware datetime object.

    When ev_max_tariff_power_kw == 0, current throttling is disabled and
    the full charging power is used for all slots regardless of time of day.
    When ev_max_tariff_power_kw > 0 but the resulting amps would be below
    CHARGER_MIN_AMPS (10 A), throttling is also treated as disabled.
    """
    local_hour    = slot_start.astimezone(ZoneInfo(hass.config.time_zone)).hour
    tariff_active = (local_hour >= TARIFF_HOUR_START and local_hour < TARIFF_HOUR_END)
    guard_enabled = (state.get(GUARD_ENT) or "off") == "on"

    if tariff_active and guard_enabled:
        raw_kw = state.get(MAX_TARIFF_KW_ENT)
        max_kw = float(raw_kw) if raw_kw not in (None, 'unknown', 'unavailable') else 0.0
        if max_kw > 0:
            # Check that configured power is above the car's minimum current threshold
            raw_amps = int(max_kw * 1000 / (400 * 1.732))
            if raw_amps >= 10:   # CHARGER_MIN_AMPS for Mercedes PHEV
                return max_kw
        # 0 kW configured, or below car minimum — throttling disabled, use full power

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
    now_ts    = now_local.timestamp()   # POSIX epoch; compare with .timestamp() to avoid DST ambiguity

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
            # Always construct with explicit year/month/day from target_date —
            # never use strptime() without a full date (defaults to 1900/2000).
            candidate = datetime(
                year=target_date.year, month=target_date.month, day=target_date.day,
                hour=h, minute=m, second=0, tzinfo=local_tz,
            )
            # Use epoch comparison (same pattern as BUG A fix) to avoid potential
            # DST ambiguity when comparing ZoneInfo-aware datetimes directly.
            if candidate.timestamp() > now_ts + 900:   # at least 15 min ahead
                return candidate

    log.info("get_next_departure: no departures in next 7 days, switching to opportunistic mode")
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
      - If no departure found: leaves ev_computed_deadline unchanged and logs.
        The optimizer falls through to opportunistic mode via get_effective_deadline().
        No far-future sentinel date is written — the entity retains its last valid
        value (or whatever value it currently holds).  get_effective_deadline()
        rejects any timestamp that is not > 5 minutes in the future, so stale
        values are naturally ignored.
    """
    # ── One-time cleanup: clear any stale far-future value ────────────────────
    # Old code versions wrote far-future sentinel dates (2099, 2030) to
    # ev_computed_deadline when no departure was found.  Clear such values now
    # (anything beyond SCHEDULE_HORIZON_DAYS from now is implausible because
    # get_next_departure() only looks 7 days ahead).
    local_tz = ZoneInfo(hass.config.time_zone)
    now_ts   = datetime.now(local_tz).timestamp()
    _attrs   = state.getattr(COMPUTED_DEADLINE_ENT) or {}
    _ts_raw  = _attrs.get("timestamp")
    if _ts_raw not in (None, "unknown", "unavailable"):
        try:
            if float(_ts_raw) > now_ts + SCHEDULE_HORIZON_DAYS * 86400:
                log.warning(
                    "ev_optimizer: auto_set_deadline: stale far-future "
                    "ev_computed_deadline detected — leaving entity unchanged "
                    "(get_effective_deadline will reject it)"
                )
        except Exception as _exc:
            log.warning(f"ev_optimizer: auto_set_deadline: stale deadline cleanup error: {_exc}")

    departure = get_next_departure()

    if departure is None:
        log.info("ev_optimizer: auto deadline: no upcoming departure — opportunistic mode active")
        return
    else:
        dt_str = departure.strftime("%Y-%m-%d %H:%M:%S")
        input_datetime.set_datetime(
            entity_id = COMPUTED_DEADLINE_ENT,
            datetime  = dt_str,
        )
        log.info(f"ev_optimizer: auto deadline computed: {dt_str}")


def get_effective_deadline():
    """
    Returns (deadline_ts, source) where deadline_ts is a float UTC epoch seconds
    (or None for opportunistic mode) and source is "manual", "auto", or
    "opportunistic".

    Collects ALL valid future deadlines (manual + auto) and returns the NEAREST
    one.  This allows multiple trips to be planned simultaneously — the system
    charges for the nearest deadline first, then automatically chains to the next
    as each passes.  Example: weekly schedule has Mon 09:00 (auto) and a manual
    deadline is set for Wed 23:55; the optimizer picks Mon 09:00 first, then
    automatically switches to Wed 23:55 after Monday's departure.

    Both entities are read via their pre-computed 'timestamp' attribute
    (always a correct UTC epoch regardless of DST) rather than parsing the
    state string (which may be a naive local-time string in some HA versions).

    now_ts uses datetime.now(tz=TZ_LOCAL).timestamp() — .timestamp() always
    returns a POSIX UTC epoch regardless of the tz argument, so this is safe.
    """
    now_ts    = datetime.now(tz=TZ_LOCAL).timestamp()
    min_ahead = now_ts + 5 * 60   # must be at least 5 minutes away

    candidates = []
    for ent, label in [
        (DEADLINE_ENT,          "manual"),
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
        if ts > min_ahead:
            candidates.append((ts, label))

    if not candidates:
        log.debug("ev_optimizer: no valid deadline — opportunistic mode")
        return None, "opportunistic"

    nearest = min(candidates, key=lambda c: c[0])
    log.info(
        f"ev_optimizer: using {nearest[1]} deadline "
        f"{datetime.fromtimestamp(nearest[0], tz=TZ_LOCAL).strftime('%a %d %b %H:%M')} "
        f"(of {len(candidates)} candidate(s))"
    )
    return nearest[0], nearest[1]


def merge_into_windows(slots):
    """
    Group a list of slot dicts into contiguous charging windows.

    Slots must share a consistent 'idx' numbering (consecutive idx == adjacent
    15-min slots).  The list need not be pre-sorted — this function sorts by idx.

    Each input slot dict: {start (UTC datetime), end (UTC datetime), price, idx}

    Returns list of window dicts:
      {start (ISO str with offset), end (ISO str with offset),
       price (float, avg), slots (int), kwh (float), cost (float)}
    """
    if not slots:
        return []

    local_tz     = ZoneInfo(hass.config.time_zone)
    sorted_slots = sorted(slots, key=lambda s: s["idx"])

    groups  = []
    current = [sorted_slots[0]]
    for slot in sorted_slots[1:]:
        if slot["idx"] == current[-1]["idx"] + 1:
            current.append(slot)
        else:
            groups.append(current)
            current = [slot]
    groups.append(current)

    windows = []
    for grp in groups:
        px_grp   = [s["price"] for s in grp]
        kwh_grp  = [effective_power_kw(s["start"]) * SLOT_H for s in grp]
        cost_grp = [px * kwh for px, kwh in zip(px_grp, kwh_grp)]
        windows.append({
            "start": grp[0]["start"].astimezone(local_tz).isoformat(),
            "end":   grp[-1]["end"].astimezone(local_tz).isoformat(),
            "price": round(sum(px_grp) / len(px_grp), 4),
            "slots": len(grp),
            "kwh":   round(sum(kwh_grp), 2),
            "cost":  round(sum(cost_grp), 4),
        })
    return windows


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
    Trim pass: after clustering, surplus slots are removed (most expensive first)
    until total kWh ≤ req_kwh + one minimum-power slot (avoids over-scheduling).
    Slot filtering uses epoch .timestamp() comparisons throughout to avoid
    CEST (UTC+2) offset errors in datetime comparisons.
    """
    if req_kwh <= 0:
        return [], "opportunistic", 0

    all_slots = _build_slots()

    # ── Determine eligible window — epoch timestamps throughout ───────────────
    # Convert every slot's start/end to POSIX epoch seconds and store as
    # start_ts / end_ts alongside the original datetime objects.  All
    # comparisons against now_ts, deadline_ts and horizon_ts use these epoch
    # fields — never datetime objects — to eliminate any CEST (UTC+2) ambiguity.
    local_tz = ZoneInfo(hass.config.time_zone)
    now_ts   = datetime.now(tz=local_tz).timestamp()

    # Enrich every slot with pre-computed epoch timestamps
    for s in all_slots:
        if hasattr(s["start"], "timestamp"):
            s["start_ts"] = s["start"].timestamp()
            s["end_ts"]   = s["end"].timestamp()
        else:
            s["start_ts"] = float(s["start"])
            s["end_ts"]   = float(s["end"])

    # Diagnostic: log the comparison anchors so timezone bugs are immediately
    # visible in the logs.
    if deadline_ts:
        log.info(
            f"compute_schedule: now={datetime.fromtimestamp(now_ts, local_tz).isoformat()}"
            f"  deadline={datetime.fromtimestamp(deadline_ts, local_tz).isoformat()}"
            f"  total_slots={len(all_slots)}"
        )
    else:
        log.info(
            f"compute_schedule: now={datetime.fromtimestamp(now_ts, local_tz).isoformat()}"
            f"  mode=opportunistic  total_slots={len(all_slots)}"
        )

    # Include the currently-active slot by looking back one full slot duration.
    # Without this, a recompute triggered 1 second after a slot boundary (e.g.
    # by task.sleep(1) in on_price_update) would exclude that slot because
    # slot_start == now_ts - 1, failing the strict >= now_ts test.  The -900s
    # lookback means: include any slot that started within the last 15 minutes
    # and has not yet ended.  Slots that have already fully elapsed are still
    # excluded by the end_ts <= deadline_ts / horizon_ts upper bound.
    slot_lookback_ts = now_ts - 900   # one 15-minute slot duration in seconds

    if deadline_ts:
        eligible = [
            s for s in all_slots
            if s["start_ts"] >= slot_lookback_ts
            and s["end_ts"]   <= deadline_ts
        ]
        mode = "deadline"
    else:
        horizon_ts = now_ts + 48 * 3600
        eligible   = [
            s for s in all_slots
            if s["start_ts"] >= slot_lookback_ts
            and s["end_ts"]   <= horizon_ts
        ]
        mode = "opportunistic"

    if not eligible:
        log.warning(
            f"ev_optimizer: no eligible slots "
            f"(required={req_kwh:.2f} kWh, deadline={deadline_ts})"
        )
        return [], mode, 0

    # Diagnostic: confirm the actual filtered range
    log.info(
        f"compute_schedule: {len(eligible)} candidate slots"
        f"  first={datetime.fromtimestamp(eligible[0]['start_ts'], local_tz).isoformat()}"
        f"  last_end={datetime.fromtimestamp(eligible[-1]['end_ts'], local_tz).isoformat()}"
    )

    # Log cheapest 5 slots — makes it immediately visible whether tomorrow's
    # prices are included and whether the cheapest available slots are correct.
    cheapest_5 = sorted(eligible, key=lambda s: s["price"])[:5]
    for s in cheapest_5:
        log.info(
            f"ev_optimizer: cheap slot: "
            f"{s['start'].astimezone(local_tz).strftime('%d %b %H:%M')} "
            f"price={s['price']:.3f} SEK/kWh"
        )

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

    # ── Trim surplus slots added by anti-toggling (BUG B fix) ─────────────────
    # Clustering may expand `selected` beyond what is needed to deliver req_kwh.
    # Remove the most expensive removable slot in each iteration: a slot is
    # removable when total_kwh - its_kwh >= req_kwh (energy target still met).
    # Use the minimum effective slot kWh as the threshold tolerance so we only
    # stop trimming when we are within one (minimum-power) slot of the target —
    # conservative, avoids under-trimming on mixed tariff/off-peak selections.
    slot_kwh_map   = {s["idx"]: effective_power_kw(s["start"]) * SLOT_H
                      for s in eligible if s["idx"] in selected}
    total_kwh_sel  = sum(slot_kwh_map.values())
    min_slot_kwh   = min(slot_kwh_map.values()) if slot_kwh_map else CHARGER_KW * SLOT_H
    trim_threshold = req_kwh + min_slot_kwh
    trimmed        = 0

    while total_kwh_sel > trim_threshold:
        # A slot is removable if dropping it still delivers at least req_kwh
        removable = [
            idx for idx in selected
            if total_kwh_sel - slot_kwh_map[idx] >= req_kwh
        ]
        if not removable:
            break
        # Remove the costliest removable slot first (minimise wasted spend)
        worst = max(removable, key=lambda i, d=by_idx: d[i]["price"])
        selected.discard(worst)
        total_kwh_sel -= slot_kwh_map.pop(worst)
        trimmed += 1

    if trimmed:
        log.info(
            f"ev_optimizer: trimmed {trimmed} surplus slot(s) after clustering — "
            f"{total_kwh_sel:.2f} kWh scheduled (target {req_kwh:.2f} kWh)"
        )

    # ── Gap-closing pass ───────────────────────────────────────────────────────
    # If two selected windows are separated by a gap of ≤ 30 min (1–2 slots)
    # whose average price is ≤ 1.5 × the average price of already-selected
    # slots, fill the gap.  A short off-period between nearly-identical price
    # windows provides negligible cost saving while causing two extra start/stop
    # events.  This pass runs after the trim so that gap-fill slots do not
    # interfere with the energy-target trim logic above.
    if selected:
        sel_avg = sum([by_idx[i]["price"] for i in selected]) / len(selected)
        for _ in range(20):   # cap iterations to prevent loops on edge-case data
            gap_filled = False
            seq = sorted(selected)
            pos = 0
            while pos < len(seq):
                # Advance pos to the end of the current consecutive run
                run_tail = pos
                while run_tail + 1 < len(seq) and seq[run_tail + 1] == seq[run_tail] + 1:
                    run_tail += 1
                # Check gap to the next run
                if run_tail + 1 < len(seq):
                    gap_start   = seq[run_tail] + 1
                    gap_end     = seq[run_tail + 1] - 1
                    gap_range   = list(range(gap_start, gap_end + 1))
                    gap_minutes = len(gap_range) * 15
                    # Only proceed when all gap indices are in the eligible pool
                    if gap_minutes <= 30 and all([k in by_idx for k in gap_range]):
                        gap_prices = [by_idx[k]["price"] for k in gap_range]
                        gap_avg    = sum(gap_prices) / len(gap_prices)
                        if gap_avg <= sel_avg * 1.5:
                            for k in gap_range:
                                selected.add(k)
                            log.info(
                                f"ev_optimizer: gap-close: merged {len(gap_range)}-slot "
                                f"gap at {gap_avg:.4f} SEK/kWh "
                                f"(threshold {sel_avg * 1.5:.4f} SEK/kWh)"
                            )
                            gap_filled = True
                            break   # restart outer loop — seq is stale
                pos = run_tail + 1
            if not gap_filled:
                break

    # ── Post-gap-close trim ───────────────────────────────────────────────────
    # The gap-closing pass may have added 1–2 slots that push total kWh above
    # the target.  Walk the selected set in chronological order and drop the
    # most expensive end-slot as long as the remaining slots still deliver
    # at least req_kwh.  Only end-slots are eligible to avoid splitting windows.
    if selected:
        sorted_sel    = sorted(selected)
        n_pre_trim    = len(sorted_sel)
        total_kwh_pg  = sum([effective_power_kw(by_idx[i]["start"]) * SLOT_H
                             for i in sorted_sel])

        while len(sorted_sel) > 1:
            first_kwh = effective_power_kw(by_idx[sorted_sel[0]]["start"])  * SLOT_H
            last_kwh  = effective_power_kw(by_idx[sorted_sel[-1]]["start"]) * SLOT_H
            can_drop_first = (total_kwh_pg - first_kwh) >= req_kwh
            can_drop_last  = (total_kwh_pg - last_kwh)  >= req_kwh

            if not can_drop_first and not can_drop_last:
                break

            first_price = by_idx[sorted_sel[0]]["price"]
            last_price  = by_idx[sorted_sel[-1]]["price"]

            # Drop the droppable end with the higher price
            if can_drop_first and (not can_drop_last or first_price >= last_price):
                total_kwh_pg -= first_kwh
                sorted_sel.pop(0)
            else:
                total_kwh_pg -= last_kwh
                sorted_sel.pop()

        if len(sorted_sel) < n_pre_trim:
            log.info(
                f"ev_optimizer: post-gap trim: removed "
                f"{n_pre_trim - len(sorted_sel)} slot(s) — "
                f"{total_kwh_pg:.2f} kWh (target {req_kwh:.2f} kWh)"
            )
        selected = set(sorted_sel)

    # ── Group consecutive slots into contiguous windows ───────────────────────
    windows = merge_into_windows([by_idx[i] for i in selected])

    return windows, mode, n


def compute_opportunistic_schedule(all_slots):
    """
    No deadline — select slots below median price in the next 24 hours.

    No slot-count limit; the intention is to charge whenever electricity is
    cheap without over-constraining the window.  Returns windows in the same
    format as compute_schedule() (via merge_into_windows()).

    Called from ev_optimizer_recompute() when get_effective_deadline() returns
    (None, "opportunistic") — i.e. no valid manual or computed deadline exists.
    """
    now_ts  = datetime.now(tz=ZoneInfo(hass.config.time_zone)).timestamp()
    horizon = now_ts + 86400   # 24 h ahead

    slot_lookback_ts = now_ts - 900   # include currently-active slot
    future_slots = [
        s for s in all_slots
        if s["start"].timestamp() >= slot_lookback_ts
        and s["end"].timestamp() <= horizon
    ]

    if not future_slots:
        log.warning("ev_optimizer: opportunistic mode — no future slots found in next 24h")
        return []

    prices       = [s["price"] for s in future_slots]
    median_price = sorted(prices)[len(prices) // 2]

    cheap_slots = [s for s in future_slots if s["price"] <= median_price]

    log.info(
        f"ev_optimizer: opportunistic mode — {len(cheap_slots)} slots at or below "
        f"median {median_price:.4f} SEK/kWh"
    )

    return merge_into_windows(cheap_slots)


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
    # returns (None, "opportunistic") for opportunistic mode.
    deadline_ts, deadline_source = get_effective_deadline()
    log.info(
        f"ev_optimizer: deadline_mode={deadline_source}, deadline_ts={deadline_ts}"
    )

    # ── Compute schedule ───────────────────────────────────────────────────────
    if req_kwh <= 0:
        # Nothing to charge — skip scheduling regardless of deadline mode
        windows        = []
        mode           = "opportunistic"
        required_slots = 0
    elif deadline_ts is None:
        # Opportunistic mode — no deadline constraint; select cheap slots in next 24 h
        log.info("ev_optimizer: opportunistic mode — selecting cheapest slots in next 24h")
        all_slots      = _build_slots()
        windows        = compute_opportunistic_schedule(all_slots)
        mode           = "opportunistic"
        required_slots = 0
    else:
        # Deadline mode — select cheapest slots that deliver req_kwh before deadline
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
            "schedule":        windows,
            "expected_cost":   total_cost,
            "total_kwh":       total_kwh,
            "computed_at":     now_iso,
            "mode":            mode,
            "required_slots":  required_slots,
            "required_kwh":    round(req_kwh, 2),
            "deadline_source": deadline_source,
            "deadline_ts":     deadline_ts,
            "friendly_name":   "EV Charging Schedule",
            "icon":            "mdi:calendar-clock",
        },
    )

    log.info(
        f"ev_optimizer: mode={mode}  deadline={deadline_source}"
        f"  need={required_slots} slots ({req_kwh:.2f} kWh)"
        f"  → {len(windows)} window(s) | {total_kwh} kWh | {total_cost:.2f} SEK"
    )


# ── Automatic triggers ─────────────────────────────────────────────────────────

@state_trigger(
    "input_datetime.ev_deadline",
    "input_datetime.ev_computed_deadline",
    "input_number.ev_required_kwh",
    "input_number.ev_max_tariff_power_kw",
    "input_select.ev_charging_mode",
    "input_boolean.ev_auto_deadline",
    "input_boolean.ev_tariff_guard_enabled",
)
def on_input_changed(var_name=None, value=None, old_value=None, **kwargs):
    """
    Recompute whenever any input helper or deadline entity changes.

    Multi-entity @state_trigger is more reliable than separate per-entity
    triggers for input_datetime — pyscript may miss individual attribute
    updates when HA refreshes several attributes simultaneously (e.g. when
    the UI writes a new datetime value the state string AND the 'timestamp'
    attribute update in the same HA event, and a single-entity trigger may
    not fire on every such compound update).

    task.sleep(2) ensures HA state (including the 'timestamp' attribute read
    by get_effective_deadline()) fully settles before the recompute reads it.

    NOTE: pyscript never writes to input_datetime.ev_deadline — that entity
    belongs to the user. ev_computed_deadline is written only by
    auto_set_deadline(). Including both here ensures the optimizer responds
    the moment either changes.
    """
    log.info(
        f"ev_optimizer: {var_name} changed: {old_value} → {value}, recomputing"
    )
    task.sleep(2)
    ev_optimizer_recompute()


@state_trigger("sensor.nordpool_kwh_se3_sek_3_10_025")
def on_price_update(var_name=None, value=None, old_value=None, **kwargs):
    """
    Recompute schedule whenever Nordpool price data updates.

    Skips unavailable/unknown transitions — these happen at midnight during
    the API data gap and would produce an empty schedule. The watchdog and
    hourly failsafe cover recovery once prices become available again.
    """
    if value in ("unavailable", "unknown"):
        log.debug(
            f"ev_optimizer: Nordpool price {value} — skipping recompute"
        )
        return
    log.info(f"ev_optimizer: Nordpool updated to {value} SEK/kWh, recomputing")
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


@time_trigger("period(now, 5min)")
def _auto_deadline_tick(**kwargs):
    """
    Re-evaluate the next departure every 5 minutes and update ev_computed_deadline.

    Handles rollover: when a departure time passes (e.g. 08:00 → 18:00 same day,
    or last time today → first time tomorrow), the computed deadline advances.
    Only runs when input_boolean.ev_auto_deadline is on.

    Never touches input_datetime.ev_deadline — that is the user's manual override.
    Writing ev_computed_deadline from here fires on_input_changed() above, which
    triggers a full recompute with the updated deadline.
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


@time_trigger("period(now, 15min)")
def schedule_watchdog(**kwargs):
    """
    Check every 15 minutes whether the schedule has gone missing.

    sensor.ev_schedule holds its value only in pyscript's in-memory state —
    it is lost on HA restart and may be empty if all triggers fired before
    Nordpool data was available. If the schedule is empty but a valid deadline
    exists, recompute immediately rather than waiting for the hourly failsafe.
    """
    sched = state.get(SCHEDULE_ENT)
    if sched not in (None, "unknown", "unavailable", "[]", ""):
        return   # schedule is present — nothing to do
    deadline_ts, source = get_effective_deadline()
    if deadline_ts is not None:
        log.warning(
            f"ev_optimizer: watchdog — schedule empty but {source} deadline "
            f"exists (ts={deadline_ts:.0f}), forcing recompute"
        )
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
    """Recompute schedule on HA startup to restore lost pyscript sensor state.

    Also clears any stale far-future value from ev_computed_deadline that may
    have been written by older code versions (anything beyond
    SCHEDULE_HORIZON_DAYS from now is implausible — get_next_departure() only
    looks 7 days ahead).
    """
    local_tz = ZoneInfo(hass.config.time_zone)
    now_ts   = datetime.now(local_tz).timestamp()
    attrs    = state.getattr(COMPUTED_DEADLINE_ENT) or {}
    ts_raw   = attrs.get("timestamp")
    if ts_raw not in (None, "unknown", "unavailable"):
        try:
            if float(ts_raw) > now_ts + SCHEDULE_HORIZON_DAYS * 86400:
                log.warning(
                    "ev_optimizer: stale far-future ev_computed_deadline detected on startup "
                    "— leaving entity unchanged (get_effective_deadline will reject it)"
                )
        except Exception as exc:
            log.warning(f"ev_optimizer: startup deadline cleanup error: {exc}")

    log.info("ev_optimizer: recomputing schedule after startup")
    ev_optimizer_recompute()
