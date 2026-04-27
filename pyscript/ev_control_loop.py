"""
ev_control_loop.py — EV Charging Optimizer control loop
Runs every 5 minutes to start/stop charging based on the computed schedule.

Reads : sensor.ev_schedule            (windows computed by ev_optimizer.py)
        input_select.ev_charging_mode  (Smart / Charge now / Stop)
        binary_sensor.ev_deadline_pressure
        sensor.ev_slots_available / sensor.ev_slots_needed  (for pressure reason)
        input_datetime.ev_last_state_change  (hysteresis tracking)
        sensor.laddbox_charger_mode    (actual charger state)
Writes: zaptec.resume_charging / zaptec.stop_charging
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
ZAPTEC_DEVICE_ID = "d0775df2-6290-4569-bcd0-b579f8b9dde3"

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
GUARD_ENT            = "input_boolean.ev_tariff_guard_enabled"
MAX_KWH_ENT          = "input_number.ev_max_hourly_kwh"
MAX_TARIFF_KW_ENT    = "input_number.ev_max_tariff_power_kw"
SLOTS_AVAIL_ENT      = "sensor.ev_slots_available"
SLOTS_NEEDED_ENT     = "sensor.ev_slots_needed"

# Zaptec installation ID — used by zaptec.limit_current (installation-level service)
ZAPTEC_INSTALL_ID  = "dcead66e-4c50-4763-bc17-6ef3efe8be1f"

# Electrical installation constants — Zaptec GO, 3-phase TN, 25A circuit
CIRCUIT_MAX_AMPS   = 25
PHASE_VOLTAGE      = 400    # line voltage (V)
PHASE_FACTOR       = 1.732  # sqrt(3) for 3-phase power: P = sqrt(3) * V * I

TARIFF_HOUR_START  = 6
TARIFF_HOUR_END    = 22
TZ_LOCAL           = ZoneInfo("Europe/Stockholm")
HYSTERESIS_MINUTES = 15


# ── Internal helpers ───────────────────────────────────────────────────────────

def _is_charging():
    """Return True if the Zaptec charger is currently in active charging mode."""
    mode = state.get(CHARGER_MODE_ENT) or ""
    return str(mode).lower() == "charging"


def _apply_current_limit():
    """
    Set Zaptec charge current via zaptec.limit_current (installation level).

    Inside tariff hours (06:00–22:00) with guard enabled: throttle to
    input_number.ev_max_tariff_power_kw, converted to amps for 3-phase 400 V.
    Minimum enforced: 6 A (below this Zaptec will not charge).
    Outside tariff hours or guard disabled: restore to CIRCUIT_MAX_AMPS.
    """
    local_tz   = ZoneInfo(hass.config.time_zone)
    local_hour = datetime.now(tz=local_tz).hour
    guard_on   = (state.get(GUARD_ENT) or "off") == "on"

    if not guard_on or local_hour < TARIFF_HOUR_START or local_hour >= TARIFF_HOUR_END:
        zaptec.limit_current(
            installation_id  = ZAPTEC_INSTALL_ID,
            available_current = CIRCUIT_MAX_AMPS,
        )
        if not guard_on:
            log.debug("ev_control_loop: Tariff limiting: guard disabled, full power")
        else:
            log.debug("ev_control_loop: Tariff limiting: outside tariff hours, full power")
        return

    # Inside tariff hours — throttle to configured kW limit
    max_kw = float(state.get(MAX_TARIFF_KW_ENT) or 3.0)
    amps   = int(max_kw * 1000 / (PHASE_VOLTAGE * PHASE_FACTOR))
    if amps < 6:
        amps = 6
    if amps > CIRCUIT_MAX_AMPS:
        amps = CIRCUIT_MAX_AMPS

    zaptec.limit_current(
        installation_id  = ZAPTEC_INSTALL_ID,
        available_current = amps,
    )
    log.info(
        f"ev_control_loop: Tariff limiting: {max_kw:.1f} kW → {amps}A "
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
            w_start = float(window["s"])
            w_end   = float(window["e"])
            w_start_str = datetime.fromtimestamp(w_start, tz=local_tz).strftime("%H:%M")
            w_end_str   = datetime.fromtimestamp(w_end,   tz=local_tz).strftime("%H:%M")
            active = now_ts >= w_start and now_ts < w_end
            log.info(
                f"  window {w_start_str}–{w_end_str}: {'ACTIVE' if active else 'inactive'}"
            )
            if active:
                return True
        except Exception as exc:
            log.warning(f"ev_control_loop: cannot parse window {window}: {exc}")

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

    If the charger is already in the desired state the service call is skipped
    but reason and last_change are still updated.

    Args:
        on     : bool – True = start charging, False = stop charging
        reason : str  – human-readable explanation written to ev_decision_reason
    """
    current = _is_charging()

    if on != current:
        now_local = datetime.now(tz=TZ_LOCAL)
        now_str   = now_local.strftime("%Y-%m-%d %H:%M:%S")

        if on:
            zaptec.resume_charging(charger_id=ZAPTEC_DEVICE_ID)
            log.info(f"ev_control_loop: STARTED charging — {reason}")
        else:
            zaptec.stop_charging(charger_id=ZAPTEC_DEVICE_ID)
            log.info(f"ev_control_loop: STOPPED charging — {reason}")
            # Reset current to circuit maximum so manual charging always works at
            # full speed even if HA becomes unreachable after this stop.
            zaptec.limit_current(
                installation_id   = ZAPTEC_INSTALL_ID,
                available_current = CIRCUIT_MAX_AMPS,
            )
            reason = reason + f" | Zaptec reset to {CIRCUIT_MAX_AMPS}A"
            log.info(
                f"ev_control_loop: Zaptec reset to full current {CIRCUIT_MAX_AMPS}A"
                f" — ready for manual use"
            )

        # Record time of this transition for hysteresis tracking
        input_datetime.set_datetime(entity_id=LAST_CHANGE_ENT, datetime=now_str)
    else:
        log.debug(
            f"ev_control_loop: charger already {'ON' if on else 'OFF'} — no action"
        )

    # Apply (or restore) current limit whenever charging is ON.
    # Called even when charger was already on to handle tariff-hour transitions.
    if on:
        _apply_current_limit()

    # Always update reason text so UI reflects current decision
    input_text.set_value(entity_id=REASON_ENT, value=reason[:255])


# ── Main control loop ──────────────────────────────────────────────────────────

def ev_control_loop():
    """
    Core EV charging decision function.  Plain callable — no trigger decorators.

    Called by ev_control_loop_tick() (5-min schedule + startup) and by
    on_zaptec_state_changed() (immediately on any charger state transition).

    Priority chain (evaluated top-to-bottom, first match wins — no fall-through):

    1. Mode = "Stop"          → always OFF — no exceptions, no hysteresis
    2. Mode = "Charge now"    → always ON  — no exceptions, no hysteresis
    3. Deadline pressure ON   → always ON  — bypasses hysteresis, schedule check,
                                             and consumption guard
    4. No schedule available  → stop ONLY if charger is currently charging
                                (avoid unnecessary toggles when already off)
    5. Inside scheduled window  → ON  (subject to hysteresis + consumption guard)
    6. Outside scheduled window → OFF (subject to hysteresis)
    """
    mode = state.get(MODE_ENT) or "Smart"

    # ── Step 1: Manual Stop — absolute OFF, no exceptions ─────────────────────
    if mode == "Stop":
        set_charger(False, "Manual override: Stop")
        return

    # ── Step 2: Manual Charge now — absolute ON, no exceptions ────────────────
    if mode == "Charge now":
        set_charger(True, "Manual override: Charge now")
        return

    # ── Step 3: Deadline pressure — force ON, bypass hysteresis ───────────────
    # ev_deadline_pressure is True when slots_available <= slots_needed + 1,
    # meaning we cannot afford to wait for the next hysteresis window.
    # set_charger() only calls resume_charging and updates last_state_change if
    # the charger is not already ON — no spurious transitions when already charging.
    pressure = state.get(PRESSURE_ENT) or "off"
    if pressure == "on":
        slots_avail  = state.get(SLOTS_AVAIL_ENT)  or "?"
        slots_needed = state.get(SLOTS_NEEDED_ENT) or "?"
        reason = (
            f"Deadline pressure: forced ON "
            f"({slots_avail} slots available, {slots_needed} needed)"
        )
        log.info(
            f"ev_control_loop: deadline pressure — forcing ON, bypassing hysteresis "
            f"({slots_avail} slots avail, {slots_needed} needed)"
        )
        set_charger(True, reason)
        return

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
def reset_zaptec_on_startup(**kwargs):
    """
    Reset Zaptec to full circuit current on every HA startup.

    If HA was restarted while the current was throttled to tariff-hour limits,
    the charger would remain limited with no way to restore it without HA.
    This ensures manual charging always works at full speed immediately after
    startup, regardless of the throttle state at the time of the last shutdown.

    task.sleep(10) lets the Zaptec integration finish loading before we send
    a command — sending too early may silently fail.
    """
    task.sleep(10)
    zaptec.limit_current(
        installation_id   = ZAPTEC_INSTALL_ID,
        available_current = CIRCUIT_MAX_AMPS,
    )
    log.info(
        f"ev_control_loop: startup: Zaptec reset to full current "
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
      disconnected      → car just plugged in (Zaptec may auto-start)
      connected_finished → charging session ended (e.g. SoC target reached)
      charging          → Zaptec began charging (auto-start or manual resume)
      any → disconnected → car unplugged; loop resets decision reason

    Runs the full priority-chain decision so we don't wait up to 5 minutes
    for the time trigger. A 2-second delay lets HA state fully settle before
    entities are read (mirrors the task.sleep(1) pattern used in ev_optimizer.py
    for input_datetime triggers).
    """
    log.info(
        f"ev_control_loop: Zaptec state changed: {old_value} → {value} — "
        f"running immediate control loop"
    )
    task.sleep(2)
    ev_control_loop()
