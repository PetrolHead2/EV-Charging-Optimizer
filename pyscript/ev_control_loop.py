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

Triggers:
  ev_control_loop_tick()       — @time_trigger every 5 min + startup
  on_zaptec_state_changed()    — @state_trigger sensor.laddbox_charger_mode
                                  (fires on connect, charging, finished, disconnect)

Priority chain (first match wins):
  1. Mode="Stop"         → always OFF, no exceptions
  2. Mode="Charge now"   → always ON,  no exceptions
  3. Deadline pressure   → always ON,  bypasses hysteresis + consumption guard
  4. No schedule         → stop only if currently charging, else log + return
  5. In window           → ON  (subject to hysteresis + consumption guard)
  6. Outside window      → OFF (subject to hysteresis)
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
GUARD_ENT        = "input_boolean.ev_tariff_guard_enabled"
MAX_KWH_ENT      = "input_number.ev_max_hourly_kwh"
MAX_TARIFF_KW_ENT = "input_number.ev_max_tariff_power_kw"
SLOTS_AVAIL_ENT  = "sensor.ev_slots_available"
SLOTS_NEEDED_ENT = "sensor.ev_slots_needed"

# ── Zaptec Charger (laddbox) — session-level entities ─────────────────────────
# These affect only the active EV session, not the whole circuit.
SWITCH_ENT              = "switch.laddbox_charging"
CHARGER_MAX_CURRENT_ENT = "number.laddbox_charger_max_current"

# ── Electrical constants — Zaptec GO, 3-phase TN, 400 V, 25 A circuit ─────────
CIRCUIT_MAX_AMPS = 25
CHARGER_MIN_AMPS = 6     # Zaptec GO will not charge below this value
PHASE_VOLTAGE    = 400   # line-to-line voltage (V)
PHASE_FACTOR     = 1.732 # sqrt(3) for 3-phase: P = sqrt(3) × V × I

TARIFF_HOUR_START  = 6
TARIFF_HOUR_END    = 22
TZ_LOCAL           = ZoneInfo("Europe/Stockholm")
HYSTERESIS_MINUTES = 15

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
    Apply or restore the session current limit based on tariff-hour rules.

    Inside tariff hours (06:00–22:00 local) with guard enabled:
      throttle to input_number.ev_max_tariff_power_kw via set_charge_current().
    Otherwise: restore to CIRCUIT_MAX_AMPS via reset_to_full_current().

    Only touches number.laddbox_charger_max_current — never the installation.
    """
    local_tz   = ZoneInfo(hass.config.time_zone)
    local_hour = datetime.now(tz=local_tz).hour
    guard_on   = (state.get(GUARD_ENT) or "off") == "on"

    if not guard_on or local_hour < TARIFF_HOUR_START or local_hour >= TARIFF_HOUR_END:
        reset_to_full_current()
        if not guard_on:
            log.debug("ev_control_loop: Tariff limiting: guard disabled — full power")
        else:
            log.debug("ev_control_loop: Tariff limiting: outside tariff hours — full power")
        return

    # Inside tariff hours — throttle to configured kW limit
    max_kw = float(state.get(MAX_TARIFF_KW_ENT) or 3.0)
    amps   = int(max_kw * 1000 / (PHASE_VOLTAGE * PHASE_FACTOR))
    set_charge_current(amps)   # clamps to [CHARGER_MIN_AMPS, CIRCUIT_MAX_AMPS]
    log.info(
        f"ev_control_loop: Tariff limiting: {max_kw:.1f} kW → "
        f"{max(CHARGER_MIN_AMPS, min(int(amps), CIRCUIT_MAX_AMPS))}A "
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
            # Active: window started within the last 900 s (one 15-min slot) AND
            # has not yet ended.  The lookback covers the full current slot so we
            # never miss a window that opened seconds before the tick ran.
            active = w_start >= now_ts - 900 and w_end > now_ts
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
        # Apply (or restore) current limit even when state unchanged,
        # to handle tariff-hour boundary transitions.
        if on:
            _apply_tariff_current()

    # Always update reason text so UI reflects current decision
    input_text.set_value(entity_id=REASON_ENT, value=reason[:255])


# ── Main control loop ──────────────────────────────────────────────────────────

def ev_control_loop():
    """
    Core EV charging decision function.  Plain callable — no trigger decorators.

    Called by ev_control_loop_tick() (5-min schedule + startup) and by
    on_zaptec_state_changed() (immediately on any charger state transition).

    Priority chain (evaluated top-to-bottom, first match wins — no fall-through):

    0. Car not connected      → update reason, return immediately; no switch calls
    1. Mode = "Stop"          → always OFF — no exceptions, no hysteresis
    2. Mode = "Charge now"    → always ON  — no exceptions, no hysteresis
    3. Deadline pressure ON   → always ON  — bypasses hysteresis, schedule check,
                                             and consumption guard
    4. No schedule available  → stop ONLY if charger is currently charging
                                (avoid unnecessary toggles when already off)
    5. Inside scheduled window  → ON  (subject to hysteresis + consumption guard)
    6. Outside scheduled window → OFF (subject to hysteresis)
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

    # ── Step 3: Deadline pressure — force ON, bypass hysteresis ───────────────
    # ev_deadline_pressure is True when slots_available <= slots_needed + 1,
    # meaning we cannot afford to wait for the next hysteresis window.
    # set_charger() skips the switch call if already charging — no spurious toggles.
    pressure = state.get(PRESSURE_ENT) or "off"
    if pressure == "on":
        slots_avail  = state.get(SLOTS_AVAIL_ENT)  or "?"
        slots_needed = state.get(SLOTS_NEEDED_ENT) or "?"
        reason = (
            f"Deadline pressure: forced ON "
            f"({slots_avail} slots available, {slots_needed} needed)"
        )
        log.info(
            f"ev_control_loop: Priority step 3: deadline pressure → ON "
            f"({slots_avail} slots avail, {slots_needed} needed) — bypassing hysteresis"
        )
        set_charger(True, reason)
        return

    log.info("ev_control_loop: Priority step 3: no deadline pressure")

    # ── Step 4: No schedule — safe default, stop only if currently charging ───
    # sensor.ev_schedule state IS the JSON string; parse it directly.
    # (pyscript state.getattr() returns the full attributes dict — no 2-arg form)
    sched_state = state.get(SCHEDULE_ENT)
    schedule = []
    if sched_state and sched_state not in ("unknown", "unavailable", "[]", ""):
        try:
            schedule = json.loads(sched_state)
        except Exception:
            schedule = []

    if not isinstance(schedule, list) or len(schedule) == 0:
        log.info("ev_control_loop: Priority step 4: no schedule available")
        if _is_charging():
            set_charger(False, "No schedule available — stopping unauthorised charge")
        else:
            input_text.set_value(
                entity_id=REASON_ENT,
                value="No schedule available — charger already off",
            )
            log.debug("ev_control_loop: no schedule, charger already off — no action")
        return

    # ── Steps 5 & 6: Evaluate schedule ────────────────────────────────────────
    desired   = should_charge_now(schedule)
    price_str = _current_price_str()

    # Read deadline info published by ev_optimizer into sensor.ev_schedule attrs
    sched_attrs = state.getattr(SCHEDULE_ENT) or {}
    dl_source   = sched_attrs.get("deadline_source") or "opportunistic"
    dl_ts       = sched_attrs.get("deadline_ts")

    log.info(
        f"ev_control_loop: Priority step {'5' if desired else '6'}: "
        f"should_charge_now={desired} → {'ON' if desired else 'OFF'}"
    )
    if desired:
        reason = f"In scheduled window ({price_str})"
    else:
        if dl_ts and dl_source != "opportunistic":
            try:
                dl_str = datetime.fromtimestamp(float(dl_ts), tz=TZ_LOCAL).strftime("%a %d %b %H:%M")
                reason = f"Outside scheduled windows ({price_str}) — deadline: {dl_source} {dl_str}"
            except Exception:
                reason = f"Outside scheduled windows ({price_str})"
        else:
            reason = f"Outside scheduled windows ({price_str})"

    # ── Hysteresis guard (steps 5 & 6 only — deadline pressure already returned) ──
    if not check_hysteresis(desired):
        current_on  = _is_charging()
        hold_reason = (
            f"Hysteresis: holding {'ON' if current_on else 'OFF'} "
            f"(min {HYSTERESIS_MINUTES} min between state changes, {price_str})"
        )
        input_text.set_value(entity_id=REASON_ENT, value=hold_reason[:255])
        log.debug(f"ev_control_loop: {hold_reason}")
        return

    # ── Consumption guard (only when schedule says charge, tariff hours only) ──
    if desired:
        local_tz   = ZoneInfo(hass.config.time_zone)
        local_now  = datetime.now(tz=local_tz)
        local_hour = local_now.hour

        guard_enabled = (state.get(GUARD_ENT) or "off") == "on"

        if not guard_enabled:
            log.debug("ev_control_loop: Consumption guard: disabled by user")

        elif local_hour < TARIFF_HOUR_START or local_hour >= TARIFF_HOUR_END:
            log.debug("ev_control_loop: Consumption guard: outside tariff hours")

        else:
            accum_raw = state.get(TIBBER_ACCUM_ENT)
            avg_w_raw = state.get(TIBBER_AVG_W_ENT)

            if accum_raw in (None, "unknown", "unavailable") or \
               avg_w_raw  in (None, "unknown", "unavailable"):
                log.warning(
                    "ev_control_loop: Consumption guard: Tibber sensor(s) "
                    "offline, skipping guard — fail-open"
                )
            else:
                accumulated_kwh   = float(accum_raw)
                average_power_w   = float(avg_w_raw)
                current_house_kw  = average_power_w / 1000
                charging_power_kw = float(state.get(CHARGE_PWR_ENT) or 7.0)
                threshold         = float(state.get(MAX_KWH_ENT) or 5.0)

                remaining_minutes = 60 - local_now.minute
                projected_total   = accumulated_kwh + (
                    (current_house_kw + charging_power_kw) * (remaining_minutes / 60)
                )

                if projected_total > threshold:
                    set_charger(
                        False,
                        f"Consumption guard: house {current_house_kw:.1f} kW "
                        f"+ EV {charging_power_kw:.1f} kW = projected "
                        f"{projected_total:.1f} kWh this hour "
                        f"(cap {threshold:.1f} kWh)",
                    )
                    return

                log.debug(
                    f"ev_control_loop: Consumption guard: OK — projected "
                    f"{projected_total:.1f} kWh under cap {threshold:.1f}"
                )

    # ── Act ───────────────────────────────────────────────────────────────────
    set_charger(desired, reason)


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
