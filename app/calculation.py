"""Deterministic, auditable meal-endpoint calculation.

This is a mathematical model, not an insulin dosing algorithm. Complete carb
absorption and full dose effect at the endpoint are explicit assumptions.
"""
from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timedelta
from decimal import Decimal, getcontext

from app.common import local_zone
from app.i18n import tr

getcontext().prec = 28


def dec(value: object) -> Decimal:
    return Decimal(str(value))


# Activity curve of a rapid dose (Loop's Fiasp preset; derivation in app/on_board.py).
INSULIN_PEAK = Decimal(55)
INSULIN_DURATION = Decimal(360)
INSULIN_DELAY = Decimal(10)


def insulin_remaining(minutes: Decimal) -> Decimal:
    """Share of a rapid dose that has not acted yet, `minutes` after the injection."""
    t = dec(minutes) - INSULIN_DELAY
    if t <= 0:
        return Decimal(1)
    if t >= INSULIN_DURATION:
        return Decimal(0)
    peak, duration = INSULIN_PEAK, INSULIN_DURATION
    tau = peak * (1 - peak / duration) / (1 - 2 * peak / duration)
    a = 2 * tau / duration
    scale = 1 / (1 - a + (1 + a) * (-duration / tau).exp())
    used = scale * (1 - a) * ((t * t / (tau * duration * (1 - a)) - t / tau - 1) * (-t / tau).exp() + 1)
    return min(Decimal(1), max(Decimal(0), 1 - used))


def schedule_value_at(moment: datetime, schedule: list[dict], timezone: str, key: str) -> Decimal:
    local = moment.astimezone(local_zone(timezone))
    minute = local.hour * 60 + local.minute
    candidates = sorted(schedule, key=lambda row: int(row["start_minute"]))
    if not candidates:
        raise ValueError(tr("Profil-Zeitplan fehlt"))
    chosen = candidates[-1]
    for row in candidates:
        if int(row["start_minute"]) <= minute:
            chosen = row
        else:
            break
    value = dec(chosen[key])
    if value <= 0:
        raise ValueError(tr("Profilwert muss positiv sein"))
    return value


def isf_at(moment: datetime, schedule: list[dict], timezone: str) -> Decimal:
    return schedule_value_at(moment, schedule, timezone, "mgdl_per_unit")


def minutes_between(later: datetime, earlier: datetime) -> Decimal:
    elapsed = later - earlier
    seconds = Decimal(elapsed.days * 86400 + elapsed.seconds) + Decimal(elapsed.microseconds) / Decimal(1000000)
    return seconds / Decimal(60)


def is_rapid_insulin(treatment: dict) -> bool:
    return (treatment.get("role") in ("meal", "meal-dose", "correction")
            or treatment.get("eventType") in ("Meal Bolus", "Correction Bolus", "Snack Bolus", "Bolus"))


def interpolate_glucose(entries: list[dict], moment: datetime, max_gap_minutes: int = 15) -> dict:
    """Piecewise linear interpolation; no silent nearest-reading substitution."""
    return glucose_interpolator(entries, max_gap_minutes)(moment)


def glucose_interpolator(entries: list[dict], max_gap_minutes: int = 15):
    """interpolate_glucose for many moments over the same readings, sorted only once."""
    points = sorted((entry["at"], dec(entry["sgv"])) for entry in entries if entry.get("sgv") is not None)
    times = [at for at, _ in points]

    def at_moment(moment: datetime) -> dict:
        index = bisect_left(times, moment)
        if index < len(times) and times[index] == moment:
            return {"value": points[index][1], "before": moment, "after": moment}
        if index == 0 or index == len(times):
            raise ValueError(tr("CGM-Werte um den Messzeitpunkt fehlen"))
        before, after = points[index - 1], points[index]
        gap = minutes_between(after[0], before[0])
        if gap > max_gap_minutes:
            raise ValueError(tr("CGM-Lücke am Messzeitpunkt ist zu groß"))
        fraction = minutes_between(moment, before[0]) / gap
        value = before[1] + (after[1] - before[1]) * fraction
        return {"value": value, "before": before[0], "after": after[0]}
    return at_moment


def finger_glucose(treatments: list[dict], moment: datetime, max_gap_minutes: int = 15) -> dict | None:
    """The blood glucose measured by hand (Nightscout BG Check) nearest to moment, within the gap."""
    checks = [(abs(minutes_between(item["at"], moment)), item) for item in treatments if item.get("finger_mgdl")]
    checks = [(gap, item) for gap, item in checks if gap <= max_gap_minutes]
    if not checks:
        return None
    item = min(checks, key=lambda pair: pair[0])[1]
    return {"value": dec(item["finger_mgdl"]), "before": item["at"], "after": item["at"]}


# Injecting first and eating a little later is common (user report 2026-11-15: 10 IE at 06:43, breakfast
# entered at 07:05 without insulin gave no factor until the window was moved by hand). A rapid dose without
# carbs at most this long before a main meal whose own entry carries no rapid insulin is its bolus.
PREBOLUS_LEAD = timedelta(minutes=30)


def _is_main_meal(item: dict) -> bool:
    return (item.get("role") == "meal" or item.get("eventType") == "Meal Bolus") and dec(item.get("carbs") or 0) > 0


def prebolus_start(episode: dict, all_treatments: list[dict], entries: list[dict], settings: dict) -> datetime | None:
    """The earliest pre-bolus of the meal, or None.

    It must be a rapid dose without carbs within PREBOLUS_LEAD before the meal, the meal's own entry
    holds no rapid insulin, no carbs lie between dose and meal, and no earlier main meal's automatic
    window still counts the dose (else it is that meal's correction)."""
    meal_at = episode["at"]
    source_id = episode.get("source_id")
    own = next((item for item in all_treatments if source_id and item.get("id") == source_id), None)
    if own is not None and dec(own.get("insulin") or 0) > 0 and is_rapid_insulin(own):
        return None
    doses = sorted(
        item["at"] for item in all_treatments
        if item.get("eventType") != "Exercise" and item is not own
        and meal_at - PREBOLUS_LEAD <= item["at"] < meal_at
        and dec(item.get("insulin") or 0) > 0 and dec(item.get("carbs") or 0) <= 0 and is_rapid_insulin(item))
    if not doses:
        return None
    first = doses[0]
    if any(item is not own and first <= item["at"] < meal_at and dec(item.get("carbs") or 0) > 0
           for item in all_treatments if item.get("eventType") != "Exercise"):
        return None
    earlier = [item for item in all_treatments if _is_main_meal(item) and item["at"] < first and item is not own]
    if earlier:
        previous = max(earlier, key=lambda item: item["at"])
        span = automatic_window({"at": previous["at"], "source_id": previous.get("id")},
                                all_treatments, entries, settings, prebolus=False)
        if span["end"] > first:
            return None
    return first


def automatic_window(episode: dict, all_treatments: list[dict], entries: list[dict], settings: dict,
                     prebolus: bool = True) -> dict:
    """Default meal window: from the meal (or its pre-bolus) for a fixed time, extended by doses,
    bounded by the next main meal."""
    meal_at = episode["at"]
    start = (prebolus_start(episode, all_treatments, entries, settings) if prebolus else None) or meal_at
    window = timedelta(minutes=int(settings["window_minutes"]))
    nominal_end = meal_at + window
    end = nominal_end
    next_main_meals = sorted(
        item["at"] for item in all_treatments
        if (not episode.get("source_id") or item.get("id") != episode["source_id"])
        and _is_main_meal(item)
        and meal_at < item["at"]
    )
    next_main_meal_at = next_main_meals[0] if next_main_meals else None
    # Each rapid dose reached by the current window starts a full new window.
    # A following main meal bounds the doses that belong to this meal.
    for treatment in sorted(all_treatments, key=lambda item: item["at"]):
        at = treatment["at"]
        if at >= end:
            break
        if (at >= start and (next_main_meal_at is None or at < next_main_meal_at)
                and dec(treatment.get("insulin") or 0) > 0 and is_rapid_insulin(treatment)):
            end = max(end, at + window)
    extended_end = end
    if next_main_meal_at and next_main_meal_at < end and minutes_between(next_main_meal_at, meal_at) > Decimal(120):
        candidates = [
            item["at"] for item in entries
            if item.get("sgv") is not None
            and meal_at + timedelta(minutes=120) <= item["at"] < next_main_meal_at
            and minutes_between(next_main_meal_at, item["at"]) <= Decimal(15)
        ]
        if candidates:
            end = max(candidates)
    return {"start": start, "end": end, "nominal_end": nominal_end,
            "extended_end": extended_end, "next_main_meal_at": next_main_meal_at}


def window_totals(episode: dict, all_treatments: list[dict], start: datetime, end: datetime) -> tuple[Decimal, Decimal]:
    """Carbs and rapid insulin a window counts, as calculate_episode sums them: half-open
    [start, end), every dose in full, and the meal's own entry always."""
    source_id = episode.get("source_id")
    carbs = dec(episode["carbs"])
    units = Decimal(0)
    for treatment in all_treatments:
        if treatment.get("eventType") == "Exercise":
            continue
        own_entry = bool(source_id) and treatment.get("id") == source_id
        inside = start <= treatment["at"] < end
        amount = dec(treatment.get("carbs") or 0)
        if inside and not own_entry and amount > 0:
            carbs += amount
        dose = dec(treatment.get("insulin") or 0)
        if dose > 0 and (own_entry or inside) and is_rapid_insulin(treatment):
            units += dose
    return carbs, units


def calculate_episode(episode: dict, all_treatments: list[dict], entries: list[dict], settings: dict,
                      sport_effect_mgdl: Decimal = Decimal(0), manual_window: dict | None = None) -> dict:
    """Solve one endpoint balance for every carb and rapid dose in a meal window.

    A user-confirmed manual window replaces the automatic start and endpoint.
    Its start may lie after the recorded meal time (e.g. eating began later);
    the meal's own entry, carbs and bolus, always counts in full.
    """
    meal_at = episode["at"]
    automatic = automatic_window(episode, all_treatments, entries, settings)
    nominal_end = automatic["nominal_end"]
    extended_end = automatic["extended_end"]
    next_main_meal_at = automatic["next_main_meal_at"]
    start, end = automatic["start"], automatic["end"]
    auto_start, auto_end = start, end
    if manual_window:
        start, end = manual_window["start"], manual_window["end"]
        if not meal_at < end or not start < end:
            raise ValueError(tr("Manueller Zeitraum muss nach dem Mahlzeitbeginn enden"))
    try:
        g0 = interpolate_glucose(entries, start)
        g0_source = "cgm"
    except ValueError:
        # Without CGM at the start (sensor failure or warm-up), a finger measurement close to it stands in.
        g0 = finger_glucose(all_treatments, start)
        if g0 is None:
            raise
        g0_source = "finger"
    g1 = interpolate_glucose(entries, end)
    delta = g1["value"] - g0["value"]
    carbs = dec(episode["carbs"])
    if carbs <= 0:
        raise ValueError(tr("Kohlenhydrate müssen positiv sein"))
    meal_isf = isf_at(meal_at, settings["isf_schedule"], settings["timezone"])
    meal_carb_effect = carbs * meal_isf
    carb_events = [{
        "id": episode.get("source_id"), "at": meal_at.isoformat(), "carbs": str(carbs),
        "isf": str(meal_isf), "carbs_times_isf": str(meal_carb_effect), "kind": "meal",
    }]
    extra_carbs = []
    blockers = []
    for treatment in all_treatments:
        if treatment.get("eventType") == "Exercise":
            continue
        if episode.get("source_id") and treatment.get("id") == episode["source_id"]:
            continue
        at = treatment["at"]
        amount = dec(treatment.get("carbs") or 0)
        if not (start <= at < end and amount > 0):
            continue
        event_type = treatment.get("eventType") or ""
        kind = "snack" if event_type == "Snack Bolus" else "meal" if event_type == "Meal Bolus" else "other"
        sensitivity = isf_at(at, settings["isf_schedule"], settings["timezone"])
        local_time = at.astimezone(local_zone(settings["timezone"])).strftime("%H:%M")
        label = tr("Zwischenmahlzeit") if kind == "snack" else tr("Weitere Hauptmahlzeit") if kind == "meal" else tr("Weitere Kohlenhydrate")
        extra_carbs.append({
            "id": treatment.get("id"), "at": at.isoformat(), "carbs": str(amount),
            "isf": str(sensitivity), "carbs_times_isf": str(amount * sensitivity), "kind": kind,
        })
        if kind == "meal":
            blockers.append({"kind": kind, "at": at.isoformat(), "carbs": str(amount),
                             "message": tr("{label} um {time} ({carbs} g KH) im Auswertungsfenster", label=label, time=local_time, carbs=amount)})
    effects = []
    insulin_effect = Decimal(0)
    rapid_units_in_window = Decimal(0)
    for treatment in all_treatments:
        if treatment.get("eventType") == "Exercise":
            continue
        units = dec(treatment.get("insulin") or 0)
        at = treatment["at"]
        own_entry = bool(episode.get("source_id")) and treatment.get("id") == episode["source_id"]
        if units <= 0 or not (own_entry or start <= at < end):
            continue
        if not is_rapid_insulin(treatment):
            local_time = at.astimezone(local_zone(settings["timezone"])).strftime("%H:%M")
            blockers.append({"kind": "unknown_insulin", "at": at.isoformat(), "units": str(units),
                             "message": tr("Insulinereignis unbekannten Typs um {time} ({units} IE)", time=local_time, units=units)})
            continue
        sensitivity = isf_at(at, settings["isf_schedule"], settings["timezone"])
        effect = units * sensitivity
        rapid_units_in_window += units
        insulin_effect += effect
        effects.append({
            "id": treatment.get("id"), "at": at.isoformat(), "units": str(units),
            "role": treatment.get("role") or treatment.get("eventType") or "Bolus",
            "event_type": treatment.get("eventType") or "",
            "isf": str(sensitivity), "effect_mgdl": str(effect),
        })
    # Rapid insulin from before the window still lowers glucose inside it. The dose help subtracts it
    # as "Wirkt noch", so the factor counts the share that acts between start and end (activity curve);
    # without it the factor of such a meal came out too small and the insulin was deducted twice
    #.
    prior_doses = []
    prior_insulin_effect = Decimal(0)
    for treatment in all_treatments:
        if treatment.get("eventType") == "Exercise":
            continue
        units = dec(treatment.get("insulin") or 0)
        at = treatment["at"]
        own_entry = bool(episode.get("source_id")) and treatment.get("id") == episode["source_id"]
        if units <= 0 or own_entry or at >= start or not is_rapid_insulin(treatment):
            continue
        share = insulin_remaining(minutes_between(start, at)) - insulin_remaining(minutes_between(end, at))
        if share <= 0:
            continue
        sensitivity = isf_at(at, settings["isf_schedule"], settings["timezone"])
        effect = units * share * sensitivity
        prior_insulin_effect += effect
        prior_doses.append({
            "id": treatment.get("id"), "at": at.isoformat(), "units": str(units),
            "share": str(share), "isf": str(sensitivity), "effect_mgdl": str(effect),
        })
    insulin_effect += prior_insulin_effect
    sport_effect_mgdl = dec(sport_effect_mgdl)
    if not sport_effect_mgdl.is_finite() or not -500 <= sport_effect_mgdl <= 500:
        raise ValueError(tr("Ungültiger Sporteffekt"))
    adjusted_delta = delta + sport_effect_mgdl
    denominator = adjusted_delta + insulin_effect
    all_carb_events = carb_events + extra_carbs
    carbs_total = sum((dec(item["carbs"]) for item in all_carb_events), Decimal(0))
    carbs_times_isf_total = sum((dec(item["carbs_times_isf"]) for item in all_carb_events), Decimal(0))
    ratio = None
    factor = None
    if blockers:
        reason = "; ".join(item["message"] for item in blockers)
    elif denominator <= 0:
        reason = tr("Kein positiver endlicher Faktor für diese Eingaben")
    else:
        ratio = str(carbs_times_isf_total / denominator)
        factor = str(Decimal(10) * denominator / carbs_times_isf_total)
        reason = None
    return {
        "algorithm": "endpoint-full-dose-v2", "start": start.isoformat(), "end": end.isoformat(),
        "nominal_end": nominal_end.isoformat(), "window_minutes": int(settings["window_minutes"]),
        "actual_window_minutes": str(minutes_between(end, start)),
        "extended_end": extended_end.isoformat(),
        "end_reason": ("manual" if manual_window else "before_next_meal" if end < extended_end
                       else "after_last_injection" if extended_end > nominal_end else "fixed_window"),
        "manual_window": bool(manual_window),
        "auto_start": auto_start.isoformat(), "auto_end": auto_end.isoformat(),
        "prebolus": not manual_window and auto_start < meal_at,
        "next_main_meal_at": next_main_meal_at.isoformat() if next_main_meal_at else None,
        "carbs": str(carbs), "carbs_total": str(carbs_total),
        "carbs_times_isf_total": str(carbs_times_isf_total),
        "extra_carb_count": len(extra_carbs), "g0": str(g0["value"]), "g3": str(g1["value"]),
        "g0_sources": [g0["before"].isoformat(), g0["after"].isoformat()], "g0_source": g0_source,
        "g3_sources": [g1["before"].isoformat(), g1["after"].isoformat()],
        "delta_mgdl": str(delta), "adjusted_delta_mgdl": str(adjusted_delta),
        "sport_effect_mgdl": str(sport_effect_mgdl), "meal_isf": str(meal_isf),
        "rapid_units_in_window": str(rapid_units_in_window),
        "insulin_effect_mgdl": str(insulin_effect), "prior_insulin_effect_mgdl": str(prior_insulin_effect),
        "prior_doses": prior_doses, "denominator_mgdl": str(denominator),
        "doses": effects, "carb_events": all_carb_events,
        "blockers": blockers,
        "ratio_g_per_unit": ratio, "factor_units_per_ke": factor, "reason": reason,
        "assumptions": [tr("alle KH im Fenster wirken bis zum Endpunkt vollständig"), tr("volle IE jeder schnell wirksamen Injektion im Fenster"), tr("frühere schnell wirksame Injektionen mit dem Anteil, der im Fenster wirkt"), tr("alle KH im Fenster teilen einen Insulinfaktor"), tr("kein zusätzlicher Hintergrundtrend"), tr("bestätigter Sport- oder Rennsporteffekt (Senkung oder Anstieg) als additive Korrektur der Glukoseänderung")]
        + ([tr("manuell gewählter Zeitraum statt automatischem Endpunkt")] if manual_window else [])
        + ([tr("Beginn bei der Injektion vor dem Essen (Spritz-Ess-Abstand)")] if not manual_window and auto_start < meal_at else [])
        + ([tr("Startwert blutig gemessen, weil am Beginn kein CGM-Wert vorlag")] if g0_source == "finger" else []),
    }
