"""
ev_control_loop.py — EV Charging Optimizer control loop
Runs every 5 minutes to start/stop charging based on the computed schedule.

TWO ZAPTEC DEVICES:
  Charger (laddbox): session-level control — ON/OFF and current throttling.
    switch.laddbox_charging            — turn session on/off
    number.laddbox_charger_max_current — per-session current limit (A)
    sensor.laddbox_charger_mode        — charger state
  Installation (magnus_niemi): circuit capacity.
    NEVER written by this file — circuit changes affect all appliances.
    Read by the Zaptec integration but controlled only from the Zaptec portal.

Reads : sensor.ev_schedule            (windows computed by ev_optimizer.py)
        input_select.ev_charging_mode  (Smart / Charge now / Stop)
        binary_sensor.ev_deadline_pressure
        sensor.ev_slots_available / sensor.ev_slots_needed  (for pressure reason)
        input_datetime.ev_last_state_change  (hysteresis tracking)
        sensor.laddbox_charger_mode    (actual charger state)
Writes: switch.laddbox_charging
        number.laddbox_charger_max_current
        input_datetime.ev_last_state_change
        input_text.ev_decision_reason
        input_boolean.ev_consumption_guard_active  (cooldown flag)

Triggers:
  ev_control_loop_tick()       — @time_trigger every 5 min + startup
  on_zaptec_state_changed()    — @state_trigger sensor.laddbox_charger_mode
                                  (fires on connect, charging, finished, disconnect)

Priority chain (evaluated top-to-bottom):
  1. Mode="Stop"          → always OFF, no exceptions
  2. Mode="Charge now"    → always ON,  no exceptions
  3. Deadline pressure    → set forced_on flag, do NOT return yet
  4. Consumption guard    → ALWAYS runs, even during deadline pressure
                            price-aware latest-start algorithm:
                            if should_hold + forced_on → OFF + combined reason
                            if should_hold only        → OFF + guard reason
  5. No schedule          → if forced_on: force ON; else safe OFF
  6. Deadline pressure    → force ON, NO hysteresis (guard cleared, hard override)
  7. In window            → ON  (subject to hysteresis)
  8. Outside window       → OFF (subject to hysteresis)
"""

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ── Constants ──────────────────────────────────────────────────────────────────
SCHEDULE_ENT     = "sensor.ev_schedule"
MODE_ENT         = "input_select.ev_charging_mode"
PRESSURE_ENT     = "binary_sensor.ev_deadline_pressure"
LAST_CHANGE_ENT  = "input_datetime.ev_last_state_change"
REASON_ENT       = "input_text.ev_decision_reason"
CHARGER_MODE_ENT = "sensor.laddbox_charger_mode"
PRICE_ENT        = "sensor.nordpool_kwh_se3_sek_3_10_025"
CHARGE_PWR_ENT   = "sensor.ev_charging_power_kw"
TIBBER_ACCUM_ENT = "sensor.tibber_pulse_dianavagen_15_accumulated_consumption_current_hour"
TIBBER_AVG_W_ENT = "sensor.tibber_pulse_dianavagen_15_average_power"
GUARD_ENT         = "input_boolean.ev_tariff_guard_enabled"
GUARD_COOLDOWN_ENT = "input_boolean.ev_consumption_guard_active"
MAX_KWH_ENT      = "input_number.ev_max_hourly_kwh"
MAX_TARIFF_KW_ENT = "input_number.ev_max_tariff_power_kw"
MAX_PRICE_ENT    = "input_number.ev_max_price_sek"
SLOTS_AVAIL_ENT  = "sensor.ev_slots_available"
SLOTS_NEEDED_ENT = "sensor.ev_slots_needed"

# ── Zaptec Charger (laddbox) — session-level entities ─────────────────────────
# These affect only the active EV session, not the whole circuit.
SWITCH_ENT              = "switch.laddbox_charging"
CHARGER_MAX_CURRENT_ENT = "number.laddbox_charger_max_current"

# ── Electrical constants — Zaptec GO, 3-phase TN, 400 V, 25 A circuit ─────────
CIRCUIT_MAX_AMPS = 25
CHARGER_MIN_AMPS = 10    # Mercedes PHEV minimum — car terminates session below this
PHASE_VOLTAGE    = 400   # line-to-line voltage (V)
PHASE_FACTOR     = 1.732 # sqrt(3) for 3-phase: P = sqrt(3) × V × I

TARIFF_HOUR_START  = 6
TARIFF_HOUR_END    = 22
TZ_LOCAL           = ZoneInfo("Europe/Stockholm")
HYSTERESIS_MINUTES = 15

# ── All-in price constants (mirror of ev_optimizer.py) ─────────────────────────
# Used in get_slot_price() for consumption guard price comparisons.
_PRICE_VAT  = 1.25
_PRICE_GRID = 0.835
_PRICE_TAX  = 0.04803

def _spot_to_allin(spot):
    return spot * _PRICE_VAT + _PRICE_GRID + _PRICE_TAX

# ── Charger state sets ─────────────────────────────────────────────────────────
# States where the car is connected and the session can be started or stopped.
CHARGEABLE_STATES = {
    "Waiting",              # Zaptec firmware alternate label
    "connected_requesting", # car connected, awaiting charge start
    "connected_finishing",  # session winding down (rare transient)
    "connected_finished",   # session done (BMS limit, manual stop, or optimizer stop)
    "paused",               # session paused by integration
}
# States where the charger is actively delivering energy.
CHARGING_STATES = {
    "connected_charging",   # Zaptec integration v0.8.x active state
    "Charging",             # Zaptec firmware alternate label
}
# States where no car is physically present.
DISCONNECTED_STATES = {
    "Disconnected",         # firmware alternate label
    "disconnected",         # integration v0.8.x
}
# Union used by the connection guard — any car-present state passes through.
CONNECTED_STATES = CHARGEABLE_STATES | CHARGING_STATES


# ── Current-control helpers ────────────────────────────────────────────────────

def set_charge_current(amps):
    """
    Set the per-session charge current limit via the charger-level entity.

    Uses number.laddbox_charger_max_current which only affects the current
    EV session — the installation circuit capacity is unchanged.
    Clamps to [CHARGER_MIN_AMPS, CIRCUIT_MAX_AMPS].

    3-phase 400 V power: kW = amps × 400 × 1.732 / 1000
    """
    amps = max(CHARGER_MIN_AMPS, min(int(amps), CIRCUIT_MAX_AMPS))
    number.set_value(entity_id=CHARGER_MAX_CURRENT_ENT, value=amps)
    kw = amps * PHASE_VOLTAGE * PHASE_FACTOR / 1000
    log.info(f"ev_control_loop: set_charge_current: {amps}A = {kw:.1f} kW")


def reset_to_full_current():
    """
    Reset the charger-level current to circuit maximum.

    Called on every stop and HA startup so that manual charging always
    starts at full speed even if HA was unreachable during a throttled session.
    Does NOT touch the installation (magnus_niemi) entities.
    """
    number.set_value(entity_id=CHARGER_MAX_CURRENT_ENT, value=CIRCUIT_MAX_AMPS)
    log.info(f"ev_control_loop: reset_to_full_current: {CIRCUIT_MAX_AMPS}A")


# ── Internal helpers ───────────────────────────────────────────────────────────

def _is_charging():
    """Return True if the charger is in an active-charging state."""
    mode = str(state.get(CHARGER_MODE_ENT) or "")
    return mode in CHARGING_STATES


def _apply_tariff_current():
    """
    Apply tariff hour current throttling if configured.

    If ev_max_tariff_power_kw == 0: throttling is disabled for this
    installation.  Protection during tariff hours comes from:
      1. Optimizer scheduling only the cheapest slots.
      2. Tibber consumption guard stopping charging when the projected
         hourly kWh would exceed ev_max_hourly_kwh.

    If ev_max_tariff_power_kw > 0 but the resulting amps would fall
    below CHARGER_MIN_AMPS (10 A for the Mercedes PHEV), the throttle
    is also disabled and ON/OFF control is used instead.

    Only touches number.laddbox_charger_max_current — never the installation.
    """
    local_tz   = ZoneInfo(hass.config.time_zone)
    local_hour = datetime.now(tz=local_tz).hour
    guard_on   = (state.get(GUARD_ENT) or "off") == "on"
    tariff_active = TARIFF_HOUR_START <= local_hour < TARIFF_HOUR_END

    if not (tariff_active and guard_on):
        reset_to_full_current()
        if not guard_on:
            log.debug("ev_control_loop: Tariff limiting: guard disabled — full power")
        else:
            log.debug("ev_control_loop: Tariff limiting: outside tariff hours — full power")
        return

    # Inside tariff hours with guard on — check configured kW limit
    max_kw = float(state.get(MAX_TARIFF_KW_ENT) or 0.0)

    if max_kw == 0:
        # 0 = current throttling disabled for this installation.
        # Rely on consumption guard + ON/OFF scheduling only.
        log.debug(
            "ev_control_loop: Tariff limiting: disabled (0 kW configured) "
            "— using ON/OFF + consumption guard only"
        )
        reset_to_full_current()
        return

    raw_amps = int(max_kw * 1000 / (PHASE_VOLTAGE * PHASE_FACTOR))

    if raw_amps < CHARGER_MIN_AMPS:
        # Configured tariff power is too low for the car to accept.
        # Fall back to ON/OFF control — consumption guard handles the cap.
        log.warning(
            f"ev_control_loop: Tariff limit {max_kw:.1f} kW = {raw_amps}A is below "
            f"car minimum {CHARGER_MIN_AMPS}A — throttling disabled, using ON/OFF only"
        )
        reset_to_full_current()
        return

    set_charge_current(raw_amps)   # clamps to [CHARGER_MIN_AMPS, CIRCUIT_MAX_AMPS]
    clamped = max(CHARGER_MIN_AMPS, min(raw_amps, CIRCUIT_MAX_AMPS))
    log.info(
        f"ev_control_loop: Tariff limiting: {max_kw:.1f} kW → {clamped}A "
        f"(3-phase {PHASE_VOLTAGE}V, tariff hours)"
    )


def _current_price_str():
    """Return a formatted price string for use in log/reason messages."""
    raw = state.get(PRICE_ENT)
    if raw in (None, "unknown", "unavailable"):
        return "price unknown"
    try:
        return f"{float(raw):.3f} SEK/kWh"
    except (ValueError, TypeError):
        return f"{raw} SEK/kWh"


# ── Public functions ───────────────────────────────────────────────────────────

def get_schedule():
    """
    Read the current schedule fresh from sensor.ev_schedule.
    Never cached — always reads live state on every call.
    Returns a list of compact-epoch window dicts {s, e} or empty list.
    """
    try:
        sched_state = state.get(SCHEDULE_ENT)
        if not sched_state or sched_state in ("unknown", "unavailable", "[]", ""):
            log.debug("get_schedule: no schedule available")
            return []
        return json.loads(sched_state)
    except Exception as exc:
        log.warning(f"get_schedule: failed: {exc}")
        return []


def should_charge_now(schedule):
    """
    Return True if the current UTC time falls within any window in schedule.

    Args:
        schedule: list of dicts – each with epoch-integer keys 's' (start) and 'e' (end)

    Returns:
        bool – True if we are currently inside a scheduled charging window

    Window matching uses a 900-second (one slot) lookback so a window that just
    opened is never missed due to sub-second timing skew between the optimizer
    tick and the control loop tick.  This mirrors the -900s slot-filter lookback
    in compute_schedule() / compute_opportunistic_schedule().

    Keys are read with .get() fallbacks so a stale schedule stored with legacy
    key names ('start_ts', 'end_ts') is tolerated rather than causing a silent
    KeyError that makes every window appear inactive.
    """
    if not schedule:
        return False

    now_ts = datetime.now(tz=timezone.utc).timestamp()
    local_tz = ZoneInfo("Europe/Stockholm")
    now_local_str = datetime.fromtimestamp(now_ts, tz=local_tz).strftime("%H:%M:%S")

    log.info(
        f"should_charge_now: now={now_local_str} checking {len(schedule)} window(s)"
    )

    for window in schedule:
        try:
            # Use .get() with fallbacks so a KeyError never silently skips a window.
            # Primary keys are compact epoch integers 's' / 'e' written by ev_optimizer.
            # Fallbacks guard against any legacy format still in sensor state on startup.
            w_start = float(window.get("s", window.get("start_ts", 0)))
            w_end   = float(window.get("e", window.get("end_ts",   0)))
            if w_start == 0 and w_end == 0:
                log.warning(f"ev_control_loop: window missing 's'/'e' keys: {window}")
                continue
            w_start_str = datetime.fromtimestamp(w_start, tz=local_tz).strftime("%H:%M")
            w_end_str   = datetime.fromtimestamp(w_end,   tz=local_tz).strftime("%H:%M")
            # Active: window has already started (w_start <= now_ts) AND
            # has not yet ended (w_end > now_ts).
            # A small forward tolerance (+60 s) catches the edge case where the
            # control loop fires 1-2 seconds before a window boundary; this is
            # much safer than a 900 s lookback which incorrectly activates ANY
            # future window (since future timestamps always satisfy >= past values).
            active = w_start <= now_ts + 60 and w_end > now_ts
            log.info(
                f"  window {w_start_str}–{w_end_str}: {'ACTIVE' if active else 'inactive'}"
            )
            if active:
                return True
        except Exception as exc:
            log.warning(f"ev_control_loop: cannot parse window {window}: {exc}")

    log.info("should_charge_now: no active window found")
    return False


def check_hysteresis(desired_state):
    """
    Return True if it is safe to change the charger state.

    Always returns True when desired_state == current state (no change needed).
    Returns True if no prior change is recorded.
    Returns False if the last state change was < HYSTERESIS_MINUTES ago.

    Args:
        desired_state: bool – True = want charging ON

    Returns:
        bool – True means it is safe to act on desired_state
    """
    current = _is_charging()
    if desired_state == current:
        return True   # no change needed — always safe to confirm current state

    last_raw = state.get(LAST_CHANGE_ENT)
    if not last_raw or last_raw in ("unknown", "unavailable", ""):
        return True   # no history → allow change

    try:
        # input_datetime state: "YYYY-MM-DD HH:MM:SS" in local Stockholm time
        dt_naive   = datetime.strptime(str(last_raw), "%Y-%m-%d %H:%M:%S")
        dt_utc     = dt_naive.replace(tzinfo=TZ_LOCAL).astimezone(timezone.utc)
        elapsed_s  = (datetime.now(tz=timezone.utc) - dt_utc).total_seconds()
        return elapsed_s >= HYSTERESIS_MINUTES * 60
    except Exception as exc:
        log.warning(f"ev_control_loop: hysteresis parse failed '{last_raw}': {exc}")
        return True   # parse error → allow change


def set_charger(on, reason):
    """
    Start or stop the Zaptec charger and record the decision.

    ON/OFF is controlled via switch.laddbox_charging (session-level).
    Current throttling uses number.laddbox_charger_max_current (session-level).
    The installation entity (magnus_niemi) is never written here.

    If the charger is already in the desired state the switch call is skipped
    but reason and last_change are still updated.

    Args:
        on     : bool – True = start charging, False = stop charging
        reason : str  – human-readable explanation written to ev_decision_reason
    """
    current    = _is_charging()
    zaptec_mode = str(state.get(CHARGER_MODE_ENT) or "unknown")

    if on != current:
        now_local = datetime.now(tz=TZ_LOCAL)
        now_str   = now_local.strftime("%Y-%m-%d %H:%M:%S")

        if on:
            if zaptec_mode in DISCONNECTED_STATES:
                # Charger reports no car — should have been caught by connection guard,
                # but guard here as a safety net.
                log.info(
                    f"ev_control_loop: set_charger ON: skipping — "
                    f"car disconnected ({zaptec_mode})"
                )
                input_text.set_value(entity_id=REASON_ENT, value=reason[:255])
                return

            if zaptec_mode not in CHARGEABLE_STATES and zaptec_mode not in CHARGING_STATES:
                log.warning(
                    f"ev_control_loop: set_charger ON: unexpected mode "
                    f"'{zaptec_mode}' — attempting switch.turn_on anyway"
                )

            switch.turn_on(entity_id=SWITCH_ENT)
            log.info(f"ev_control_loop: STARTED charging — {reason}")

            # Give Zaptec time to transition from Waiting → connected_charging
            # before setting current limit, to avoid a race condition where the
            # current write is ignored or overridden by the charger firmware.
            task.sleep(2)

            # Apply tariff current limit (or restore full power outside tariff hours)
            _apply_tariff_current()

        else:
            switch.turn_off(entity_id=SWITCH_ENT)
            log.info(f"ev_control_loop: STOPPED charging — {reason}")

            # Always reset to full current on stop so manual charging starts at
            # full speed if HA becomes unreachable after this stop.
            reset_to_full_current()
            reason = reason + f" | current reset to {CIRCUIT_MAX_AMPS}A"

        # Record time of this transition for hysteresis tracking
        input_datetime.set_datetime(entity_id=LAST_CHANGE_ENT, datetime=now_str)
    else:
        log.debug(
            f"ev_control_loop: charger already {'ON' if on else 'OFF'} — no action"
        )
        # Do NOT call _apply_tariff_current() here — adjusting current on an
        # already-charging session can cause the Mercedes PHEV to terminate the
        # session if the new value falls below the car's minimum acceptance threshold.
        # Tariff limiting is applied only on fresh start (on != current branch above).

    # Always update reason text so UI reflects current decision
    input_text.set_value(entity_id=REASON_ENT, value=reason[:255])


# ── Price-aware consumption guard helpers ─────────────────────────────────────

def get_slot_price(dt):
    """
    Return the Nordpool SEK/kWh price for the 15-minute slot that contains dt.
    Returns None if price data is unavailable or dt is outside the known range.

    Uses raw_today + raw_tomorrow attributes from the Nordpool sensor which
    contain 15-min slot dicts with 'start', 'end', and 'value' keys.
    """
    try:
        attrs        = state.getattr(PRICE_ENT) or {}
        raw_today    = attrs.get("raw_today")    or []
        raw_tomorrow = attrs.get("raw_tomorrow") or []
        all_slots    = raw_today + raw_tomorrow

        dt_utc = dt.astimezone(timezone.utc)

        for slot in all_slots:
            start_raw  = slot["start"]
            end_raw    = slot["end"]
            slot_start = (datetime.fromisoformat(start_raw) if isinstance(start_raw, str) else start_raw).astimezone(timezone.utc)
            slot_end   = (datetime.fromisoformat(end_raw)   if isinstance(end_raw,   str) else end_raw).astimezone(timezone.utc)
            if slot_start <= dt_utc < slot_end:
                return _spot_to_allin(float(slot["value"]))
        return None
    except Exception as exc:
        log.warning(f"ev_control_loop: get_slot_price: failed: {exc}")
        return None


def get_smoothed_house_kw(now_dt, accumulated_kwh):
    """
    Derive average house draw this hour from accumulated consumption.

    Formula: accumulated_kwh / hours_elapsed
    This is inherently smooth — it represents actual average draw since
    the hour started, immune to momentary spikes (kettle, oven, etc.)
    that cause instantaneous average_power to fluctuate.

    Falls back to instantaneous average_power for the first 5 minutes
    of each hour when accumulated data is too sparse for a reliable average.

    Args:
        now_dt          : timezone-aware datetime for the current moment
        accumulated_kwh : float — kWh consumed since start of current hour

    Returns:
        float : smoothed house draw in kW
        None  : data unavailable — caller should fail-open
    """
    minutes_elapsed = now_dt.minute + (now_dt.second / 60.0)

    if minutes_elapsed < 5:
        # Too early in the hour — instantaneous reading is more reliable
        pwr_raw = state.get(TIBBER_AVG_W_ENT)
        if pwr_raw in (None, "unavailable", "unknown"):
            return None
        try:
            return float(pwr_raw) / 1000.0
        except (ValueError, TypeError):
            return None

    # Smooth: actual average = kWh consumed / hours elapsed
    hours_elapsed = minutes_elapsed / 60.0
    avg_kw = accumulated_kwh / hours_elapsed
    log.debug(
        f"get_smoothed_house_kw: "
        f"{accumulated_kwh:.3f} kWh in {minutes_elapsed:.1f} min = {avg_kw:.3f} kW avg"
    )
    return avg_kw


def check_consumption_guard():
    """
    Price-aware consumption guard using a latest-start calculation.

    Replaces the old projection-based guard that fired immediately when the
    projected kWh exceeded the cap.  The new algorithm never stops charging
    unnecessarily early and optimises across hour boundaries using Nordpool prices.

    Algorithm:
      1. headroom = cap - accumulated_this_hour
      2. If headroom <= 0: hold until next hour (cap already reached).
      3. max_charge_minutes = headroom / (house_kw + ev_kw) × 60
      4. latest_start_minute = 60 - max_charge_minutes
         (start this late and you finish exactly at the hour boundary)
      5. If now >= latest_start: allow charging (within the optimal window).
      6. If now < latest_start: compare current vs next-hour Nordpool price.
         - next_price >= current_price × 0.95 (same or more expensive):
             hold until latest_start (use cheaper current-hour price).
         - next_price < current_price × 0.95 (clearly cheaper next hour):
             skip to next hour entirely — optimizer will pick cheap slots.

    Always applies during tariff hours (06:00–22:00) when guard is enabled.
    Fail-open when Tibber sensors are unavailable.

    Returns:
        (should_hold: bool, reason: str, resume_at: datetime | None)
    """
    local_tz   = ZoneInfo(hass.config.time_zone)
    now_dt     = datetime.now(tz=local_tz)
    local_hour = now_dt.hour

    # Outside tariff hours — guard does not apply
    if not (TARIFF_HOUR_START <= local_hour < TARIFF_HOUR_END):
        return False, "", None

    # Guard disabled by user
    if (state.get(GUARD_ENT) or "off") != "on":
        return False, "", None

    # Read accumulated consumption — fail-open if unavailable
    accum_raw = state.get(TIBBER_ACCUM_ENT)
    if accum_raw in (None, "unknown", "unavailable"):
        log.warning(
            "ev_control_loop: Consumption guard: Tibber accumulated sensor "
            "unavailable — skipping (fail-open)"
        )
        return False, "", None

    accumulated_kwh  = float(accum_raw)

    # Use smoothed house draw (accumulated / hours_elapsed) rather than
    # instantaneous average_power to avoid momentary spikes causing
    # unnecessary charging holds. Falls back to instantaneous for first 5 min.
    current_house_kw = get_smoothed_house_kw(now_dt, accumulated_kwh)
    if current_house_kw is None:
        log.warning(
            "ev_control_loop: Consumption guard: house draw unavailable "
            "— skipping (fail-open)"
        )
        return False, "", None
    charging_power_kw = float(state.get(CHARGE_PWR_ENT) or 7.0)
    threshold         = float(state.get(MAX_KWH_ENT)    or 5.0)

    # Step 1 — headroom remaining in this hour
    headroom_kwh = threshold - accumulated_kwh

    # Step 2 — cap already reached: hold until next hour
    if headroom_kwh <= 0:
        next_hour_dt = (
            now_dt.replace(minute=0, second=0, microsecond=0)
            + timedelta(hours=1)
        )
        reason = (
            f"Consumption guard: hourly cap reached "
            f"({accumulated_kwh:.2f}/{threshold:.1f} kWh) — "
            f"holding until {next_hour_dt.strftime('%H:%M')}"
        )
        log.info(f"ev_control_loop: {reason}")
        return True, reason, next_hour_dt

    # Step 3 — max minutes the EV can charge before hitting the cap
    total_load_kw      = current_house_kw + charging_power_kw
    max_charge_minutes = (headroom_kwh / total_load_kw) * 60.0

    # Step 4 — latest start: charge from here to end of hour, exactly fits cap
    latest_start_minute = 60.0 - max_charge_minutes
    latest_start_dt = (
        now_dt.replace(minute=0, second=0, microsecond=0)
        + timedelta(minutes=latest_start_minute)
    )
    current_minute_float = now_dt.minute + (now_dt.second / 60.0)

    log.info(
        f"ev_control_loop: Consumption guard: "
        f"accumulated={accumulated_kwh:.2f} kWh "
        f"headroom={headroom_kwh:.2f} kWh "
        f"house={current_house_kw:.2f} kW "
        f"ev={charging_power_kw:.2f} kW "
        f"max_charge={max_charge_minutes:.1f} min "
        f"latest_start={latest_start_dt.strftime('%H:%M:%S')}"
    )

    # Step 5 — already past latest_start: charge now, within optimal window
    if current_minute_float >= latest_start_minute:
        log.info(
            "ev_control_loop: Consumption guard: OK — "
            "within optimal charging window for this hour"
        )
        return False, "", None

    # Step 6 — too early; compare current vs next-hour price
    current_price = get_slot_price(now_dt)
    next_hour_dt  = (
        now_dt.replace(minute=0, second=0, microsecond=0)
        + timedelta(hours=1)
    )
    next_price = get_slot_price(next_hour_dt)

    log.info(
        f"ev_control_loop: Consumption guard: price comparison — "
        f"current={current_price} SEK/kWh "
        f"next_hour={next_price} SEK/kWh "
        f"threshold=5%"
    )

    if next_price is None or current_price is None or \
       next_price >= current_price * 0.95:
        # Price data missing, or current hour is same price / cheaper than next.
        # Wait until latest_start so we use the cheap current-hour price.
        wait_minutes = max(0, int(latest_start_minute - current_minute_float))
        cp_str = f"{current_price:.3f}" if current_price is not None else "unknown"
        reason = (
            f"Consumption guard: holding OFF — "
            f"optimal start at {latest_start_dt.strftime('%H:%M')} "
            f"in ~{wait_minutes} min "
            f"(headroom {headroom_kwh:.2f} kWh = {max_charge_minutes:.0f} min "
            f"at {charging_power_kw:.1f} kW, "
            f"price {cp_str} SEK/kWh)"
        )
        return True, reason, latest_start_dt

    # Next hour is clearly cheaper (>5%): skip to next hour entirely.
    reason = (
        f"Consumption guard: skipping to next hour — "
        f"cheaper ({next_price:.3f} vs {current_price:.3f} SEK/kWh, "
        f">5% saving) — resuming at {next_hour_dt.strftime('%H:%M')}"
    )
    log.info(f"ev_control_loop: {reason}")
    return True, reason, next_hour_dt


# ── Main control loop ──────────────────────────────────────────────────────────

@service
def ev_control_loop():
    """
    Core EV charging decision function.

    The @service decorator registers this function in pyscript's shared global
    namespace so it can be called cross-file from ev_optimizer.py
    (on_price_update, on_input_changed).  Without @service, pyscript only
    exposes decorated trigger/service functions across files — plain helpers
    are invisible to other files at call time.

    Also callable as HA service pyscript.ev_control_loop for manual testing.

    Called by ev_control_loop_tick() (5-min schedule + startup) and by
    on_zaptec_state_changed() (immediately on any charger state transition).

    Priority chain (evaluated top-to-bottom):

    0. Car not connected        → update reason, return immediately; no switch calls
    1. Mode = "Stop"            → always OFF — no exceptions, no hysteresis
    2. Mode = "Charge now"      → always ON  — no exceptions, no hysteresis
    3. Deadline pressure        → set forced_on flag — do NOT return yet
    4. No schedule available    → if forced_on: force ON; else safe OFF
    5. Evaluate schedule        → compute desired=True/False via should_charge_now()
    6. Outside window, no pressure → OFF with hysteresis; guard SKIPPED (not charging)
    7. Consumption guard        → only reached when desired=True or forced_on=True
                                   price-aware latest-start: if should_hold + forced_on
                                   → OFF with combined reason; else → OFF with guard reason
    8. Deadline pressure        → force ON — guard cleared at step 7, NO hysteresis
    9. Inside scheduled window  → ON  (subject to hysteresis)
    """
    # ── Step 0: Connection guard — no car, no switch calls ────────────────────
    # switch.laddbox_charging is unavailable when no car is connected.
    # Bail out early and write a stable reason so the UI does not flicker.
    #
    # Race-condition tolerance: Zaptec transitions through 'unknown'/'unavailable'
    # when the car is first plugged in.  Wait 3 s and re-read once before giving
    # up so the connection event is not missed by the 5-min tick.
    zaptec_mode = state.get(CHARGER_MODE_ENT) or "unknown"
    if zaptec_mode not in CONNECTED_STATES:
        if zaptec_mode in ("unknown", "unavailable"):
            log.info(
                f"ev_control_loop: Zaptec state transitioning ({zaptec_mode}), "
                f"waiting 3 s for integration to settle..."
            )
            task.sleep(3)
            zaptec_mode = state.get(CHARGER_MODE_ENT) or "unknown"

        if zaptec_mode not in CONNECTED_STATES:
            reason_str = f"No car connected ({zaptec_mode})"
            if (state.get(REASON_ENT) or "") != reason_str:
                input_text.set_value(entity_id=REASON_ENT, value=reason_str)
            log.debug(f"ev_control_loop: {reason_str} — skipping")
            return

        log.info(f"ev_control_loop: Zaptec settled to '{zaptec_mode}' after wait")

    mode = state.get(MODE_ENT) or "Smart"

    log.info(
        f"ev_control_loop: tick — "
        f"mode={mode} "
        f"pressure={state.get(PRESSURE_ENT) or 'off'} "
        f"zaptec={zaptec_mode}"
    )

    # ── Step 1: Manual Stop — absolute OFF, no exceptions ─────────────────────
    if mode == "Stop":
        log.info("ev_control_loop: Priority step 1: mode=Stop → OFF")
        set_charger(False, "Manual override: Stop")
        return

    # ── Step 2: Manual Charge now — absolute ON, no exceptions ────────────────
    if mode == "Charge now":
        log.info("ev_control_loop: Priority step 2: mode=Charge now → ON")
        set_charger(True, "Manual override: Charge now")
        return

    # ── Step 3: Deadline pressure — set flag, do NOT return yet ─────────────────
    # The consumption guard at step 4 must run even when deadline pressure is
    # active — the guard may legitimately need to hold off charging (e.g. hourly
    # cap already nearly full). A combined reason message informs the user.
    pressure  = state.get(PRESSURE_ENT) or "off"
    forced_on = (pressure == "on")
    slots_needed = 0
    slots_avail  = 0
    if forced_on:
        sn_raw = state.get(SLOTS_NEEDED_ENT)
        sa_raw = state.get(SLOTS_AVAIL_ENT)
        slots_needed = int(sn_raw) if sn_raw not in (None, "unknown", "unavailable") else 0
        slots_avail  = int(sa_raw) if sa_raw not in (None, "unknown", "unavailable") else 0
        log.info(
            f"ev_control_loop: Priority step 3: deadline pressure active "
            f"({slots_avail} slots available, {slots_needed} needed)"
        )
    else:
        log.info("ev_control_loop: Priority step 3: no deadline pressure")

    # ── Step 4: No schedule — safe default ────────────────────────────────────
    # Always read fresh from sensor state — never use a cached value.
    schedule = get_schedule()

    if not isinstance(schedule, list) or len(schedule) == 0:
        log.info("ev_control_loop: Priority step 4: no schedule available")
        if forced_on:
            pressure_reason = (
                f"Deadline pressure: forced ON — no schedule "
                f"({slots_avail} slots available, {slots_needed} needed)"
            )
            set_charger(True, pressure_reason)
            return
        # Check if price ceiling is the reason the schedule is empty
        max_price_raw = state.get(MAX_PRICE_ENT)
        try:
            max_price = float(max_price_raw) \
                if max_price_raw not in (None, "unknown", "unavailable") else 0.0
        except (ValueError, TypeError):
            max_price = 0.0
        no_sched_reason = None
        if max_price > 0:
            price_raw = state.get(PRICE_ENT)
            try:
                current_price = float(price_raw) \
                    if price_raw not in (None, "unknown", "unavailable") else None
            except (ValueError, TypeError):
                current_price = None
            if current_price is not None and current_price > max_price:
                no_sched_reason = (
                    f"No slots below price ceiling "
                    f"({max_price:.2f} SEK/kWh, current {current_price:.3f} SEK/kWh)"
                )
        if _is_charging():
            set_charger(False, no_sched_reason or "No schedule available — stopping unauthorised charge")
        else:
            input_text.set_value(
                entity_id=REASON_ENT,
                value=(no_sched_reason or "No schedule available — charger already off"),
            )
            log.debug("ev_control_loop: no schedule, charger already off — no action")
        return

    # ── Step 5: Evaluate schedule ─────────────────────────────────────────────
    desired   = should_charge_now(schedule)
    price_str = _current_price_str()

    # Read deadline info published by ev_optimizer into sensor.ev_schedule attrs
    sched_attrs = state.getattr(SCHEDULE_ENT) or {}
    dl_source   = sched_attrs.get("deadline_source") or "opportunistic"
    dl_ts       = sched_attrs.get("deadline_ts")

    log.info(
        f"ev_control_loop: Priority step 5: "
        f"should_charge_now={desired}"
    )
    if desired:
        schedule_reason = f"In scheduled window ({price_str})"
    else:
        if dl_ts and dl_source != "opportunistic":
            try:
                dl_str = datetime.fromtimestamp(float(dl_ts), tz=TZ_LOCAL).strftime("%a %d %b %H:%M")
                schedule_reason = f"Outside scheduled windows ({price_str}) — deadline: {dl_source} {dl_str}"
            except Exception:
                schedule_reason = f"Outside scheduled windows ({price_str})"
        else:
            schedule_reason = f"Outside scheduled windows ({price_str})"

    # ── Step 6: Outside window, no deadline pressure → OFF, skip guard ────────
    # The consumption guard must NEVER run when desired=False — it would produce
    # misleading "optimal start at HH:MM" messages when the optimizer has already
    # decided not to charge because prices are expensive or outside a window.
    if not desired and not forced_on:
        log.info(
            f"ev_control_loop: Priority step 6: "
            f"outside scheduled window — skipping guard, going OFF"
        )
        if not check_hysteresis(False):
            current_on  = _is_charging()
            hold_reason = (
                f"Hysteresis: holding {'ON' if current_on else 'OFF'} "
                f"(min {HYSTERESIS_MINUTES} min between state changes, {price_str})"
            )
            input_text.set_value(entity_id=REASON_ENT, value=hold_reason[:255])
            log.debug(f"ev_control_loop: {hold_reason}")
            return
        set_charger(False, schedule_reason)
        return

    # ── Step 7: Consumption guard ─────────────────────────────────────────────
    # Only reached when desired=True or forced_on=True (wanting to charge).
    # Price-aware latest-start: calculates the optimal window end-of-hour and
    # compares current vs next-hour Nordpool price to decide whether to wait
    # in this hour or skip to the next. Fail-open when Tibber is unavailable.
    should_hold, guard_reason, resume_at = check_consumption_guard()

    if should_hold:
        if forced_on:
            local_tz_now = ZoneInfo(hass.config.time_zone)
            now_ts = datetime.now(tz=local_tz_now).timestamp()
            if resume_at:
                wait_mins = max(0, int((resume_at.timestamp() - now_ts) / 60))
                combined_reason = (
                    f"Deadline pressure active — "
                    f"consumption guard holding OFF for ~{wait_mins} min | "
                    f"{guard_reason}"
                )
            else:
                combined_reason = (
                    f"Deadline pressure active — "
                    f"consumption guard holding OFF | "
                    f"{guard_reason}"
                )
            log.info(
                f"ev_control_loop: Priority step 7: "
                f"guard holds despite deadline — {combined_reason}"
            )
            set_charger(False, combined_reason[:255])
        else:
            log.info(
                f"ev_control_loop: Priority step 7: "
                f"consumption guard holding — {guard_reason}"
            )
            set_charger(False, guard_reason[:255])
        return

    log.info("ev_control_loop: Priority step 7: consumption guard OK — charging allowed")

    # ── Step 8: Deadline pressure — force ON (consumption guard cleared) ──────
    # Consumption guard passed at step 7, so it is safe to start charging.
    # Hysteresis is NOT checked here — deadline pressure is a hard override.
    # The system must start charging immediately when time is running out,
    # regardless of how recently the last state change occurred.
    if forced_on:
        pressure_reason = (
            f"Deadline pressure: forced ON "
            f"({slots_avail} slots available, {slots_needed} needed)"
        )
        log.info(
            f"ev_control_loop: Priority step 8: deadline pressure → ON "
            f"(bypassing hysteresis — {slots_avail} slots avail, {slots_needed} needed)"
        )
        set_charger(True, pressure_reason)
        return

    # ── Step 9: Inside scheduled window — ON with hysteresis ──────────────────
    log.info(f"ev_control_loop: Priority step 9: in scheduled window → ON")

    if not check_hysteresis(True):
        current_on  = _is_charging()
        hold_reason = (
            f"Hysteresis: holding {'ON' if current_on else 'OFF'} "
            f"(min {HYSTERESIS_MINUTES} min between state changes, {price_str})"
        )
        input_text.set_value(entity_id=REASON_ENT, value=hold_reason[:255])
        log.debug(f"ev_control_loop: {hold_reason}")
        return

    # ── Act ───────────────────────────────────────────────────────────────────
    set_charger(True, schedule_reason)


# ── Startup: restore full current ─────────────────────────────────────────────

@time_trigger("startup")
def reset_charger_on_startup(**kwargs):
    """
    Reset charger session current to CIRCUIT_MAX_AMPS on every HA startup.

    If HA was restarted while the charger was throttled to tariff-hour limits,
    the current limit would persist with no way to restore it without HA.
    Uses the charger-level entity only — does not touch the installation.

    task.sleep(15) lets the Zaptec integration finish loading before sending
    a command — sending too early may silently fail.
    """
    task.sleep(15)
    reset_to_full_current()
    log.info(
        f"ev_control_loop: startup: charger current reset to "
        f"{CIRCUIT_MAX_AMPS}A — ready for manual use"
    )


# ── Scheduled tick ────────────────────────────────────────────────────────────

@time_trigger("period(now, 5min)")
@time_trigger("startup")
def ev_control_loop_tick(**kwargs):
    """Run the full control loop on the 5-minute schedule and at startup."""
    ev_control_loop()


# ── Immediate Zaptec state-change trigger ──────────────────────────────────────

@state_trigger("sensor.laddbox_charger_mode")
def on_zaptec_state_changed(value=None, old_value=None, **kwargs):
    """
    React immediately whenever Zaptec changes state.

    Covers all transitions:
      disconnected/unknown   → car unplugged; writes "No car connected" reason
      connected_requesting   → car just plugged in; may need start command
      connected_charging     → Zaptec began charging (auto-start or resume)
      connected_finished     → session done (BMS limit, schedule stop, etc.)

    Runs the full priority-chain decision so we don't wait up to 5 minutes
    for the time trigger. A 2-second delay lets HA state fully settle before
    entities are read (mirrors the task.sleep(1) pattern used in ev_optimizer.py
    for input_datetime triggers).
    """
    log.info(
        f"ev_control_loop: Zaptec state changed: {old_value} → {value}"
    )
    if value not in CONNECTED_STATES:
        # Car unplugged or state unknown — no point running the control loop.
        # Write reason directly so the UI updates immediately.
        log.info(
            f"ev_control_loop: car not connected ({value}) — skipping control loop"
        )
        input_text.set_value(entity_id=REASON_ENT, value=f"No car connected ({value})")
        return
    # Car is (now) connected — let HA state settle then run the full decision chain.
    task.sleep(2)
    ev_control_loop()


# ── Consumption guard hourly reset ────────────────────────────────────────────

@time_trigger("period(now, 1h)")
def reset_consumption_guard_hourly(**kwargs):
    """
    Backup reset for the consumption guard cooldown boolean.

    The primary reset is a task.sleep() inside ev_control_loop() that clears
    the boolean at the next hour boundary.  If HA restarts during that sleep
    the task is killed and the boolean stays 'on', permanently blocking charging.
    This trigger fires every hour and clears the boolean as a safety net.
    """
    if (state.get(GUARD_COOLDOWN_ENT) or "off") == "on":
        input_boolean.turn_off(entity_id=GUARD_COOLDOWN_ENT)
        log.info("ev_control_loop: Consumption guard: hourly reset")
