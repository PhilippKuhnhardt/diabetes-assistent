"""Deterministic, auditable meal-endpoint calculation.

This is a mathematical model, not an insulin dosing algorithm. Complete carb
absorption and full dose effect at the endpoint are explicit assumptions.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, getcontext
from zoneinfo import ZoneInfo

getcontext().prec = 28


def dec(value: object) -> Decimal:
    return Decimal(str(value))


def schedule_value_at(moment: datetime, schedule: list[dict], timezone: str, key: str) -> Decimal:
    local = moment.astimezone(ZoneInfo(timezone))
    minute = local.hour * 60 + local.minute
    candidates = sorted(schedule, key=lambda row: int(row["start_minute"]))
    if not candidates:
        raise ValueError("Profil-Zeitplan fehlt")
    chosen = candidates[-1]
    for row in candidates:
        if int(row["start_minute"]) <= minute:
            chosen = row
        else:
            break
    value = dec(chosen[key])
    if value <= 0:
        raise ValueError("Profilwert muss positiv sein")
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
    points = sorted((entry["at"], dec(entry["sgv"])) for entry in entries if entry.get("sgv") is not None)
    for at, glucose in points:
        if at == moment:
            return {"value": glucose, "before": at, "after": at}
    before = next(((at, glucose) for at, glucose in reversed(points) if at < moment), None)
    after = next(((at, glucose) for at, glucose in points if at > moment), None)
    if before is None or after is None:
        raise ValueError("CGM-Werte um den Messzeitpunkt fehlen")
    gap = minutes_between(after[0], before[0])
    if gap > max_gap_minutes:
        raise ValueError("CGM-Lücke am Messzeitpunkt ist zu groß")
    fraction = minutes_between(moment, before[0]) / gap
    value = before[1] + (after[1] - before[1]) * fraction
    return {"value": value, "before": before[0], "after": after[0]}


def calculate_episode(episode: dict, all_treatments: list[dict], entries: list[dict], settings: dict) -> dict:
    """Solve one endpoint balance for every carb and rapid dose in a meal window."""
    start = episode["at"]
    window = timedelta(minutes=int(settings["window_minutes"]))
    nominal_end = start + window
    end = nominal_end
    next_main_meals = sorted(
        item["at"] for item in all_treatments
        if (not episode.get("source_id") or item.get("id") != episode["source_id"])
        and (item.get("role") == "meal" or item.get("eventType") == "Meal Bolus")
        and dec(item.get("carbs") or 0) > 0
        and start < item["at"]
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
    if next_main_meal_at and next_main_meal_at < end and minutes_between(next_main_meal_at, start) > Decimal(120):
        candidates = [
            item["at"] for item in entries
            if item.get("sgv") is not None
            and start + timedelta(minutes=120) <= item["at"] < next_main_meal_at
            and minutes_between(next_main_meal_at, item["at"]) <= Decimal(15)
        ]
        if candidates:
            end = max(candidates)
    g0 = interpolate_glucose(entries, start)
    g1 = interpolate_glucose(entries, end)
    delta = g1["value"] - g0["value"]
    carbs = dec(episode["carbs"])
    if carbs <= 0:
        raise ValueError("Kohlenhydrate müssen positiv sein")
    meal_isf = isf_at(start, settings["isf_schedule"], settings["timezone"])
    meal_carb_effect = carbs * meal_isf
    carb_events = [{
        "id": episode.get("source_id"), "at": start.isoformat(), "carbs": str(carbs),
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
        local_time = at.astimezone(ZoneInfo(settings["timezone"])).strftime("%H:%M")
        label = "Zwischenmahlzeit" if kind == "snack" else "Weitere Hauptmahlzeit" if kind == "meal" else "Weitere Kohlenhydrate"
        extra_carbs.append({
            "id": treatment.get("id"), "at": at.isoformat(), "carbs": str(amount),
            "isf": str(sensitivity), "carbs_times_isf": str(amount * sensitivity), "kind": kind,
        })
        if kind == "meal":
            blockers.append({"kind": kind, "at": at.isoformat(), "carbs": str(amount),
                             "message": f"{label} um {local_time} ({amount} g KH) im Auswertungsfenster"})
    effects = []
    insulin_effect = Decimal(0)
    rapid_units_in_window = Decimal(0)
    for treatment in all_treatments:
        if treatment.get("eventType") == "Exercise":
            continue
        units = dec(treatment.get("insulin") or 0)
        at = treatment["at"]
        if units <= 0 or not (start <= at < end):
            continue
        if not is_rapid_insulin(treatment):
            local_time = at.astimezone(ZoneInfo(settings["timezone"])).strftime("%H:%M")
            blockers.append({"kind": "unknown_insulin", "at": at.isoformat(), "units": str(units),
                             "message": f"Insulinereignis unbekannten Typs um {local_time} ({units} IE)"})
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
    denominator = delta + insulin_effect
    all_carb_events = carb_events + extra_carbs
    carbs_total = sum((dec(item["carbs"]) for item in all_carb_events), Decimal(0))
    carbs_times_isf_total = sum((dec(item["carbs_times_isf"]) for item in all_carb_events), Decimal(0))
    ratio = None
    factor = None
    if blockers:
        reason = "; ".join(item["message"] for item in blockers)
    elif denominator <= 0:
        reason = "Kein positiver endlicher Faktor für diese Eingaben"
    else:
        ratio = str(carbs_times_isf_total / denominator)
        factor = str(Decimal(10) * denominator / carbs_times_isf_total)
        reason = None
    return {
        "algorithm": "endpoint-full-dose-v1", "start": start.isoformat(), "end": end.isoformat(),
        "nominal_end": nominal_end.isoformat(), "window_minutes": int(settings["window_minutes"]),
        "actual_window_minutes": str(minutes_between(end, start)),
        "extended_end": extended_end.isoformat(),
        "end_reason": "before_next_meal" if end < extended_end else "after_last_injection" if extended_end > nominal_end else "fixed_window",
        "next_main_meal_at": next_main_meal_at.isoformat() if next_main_meal_at else None,
        "carbs": str(carbs), "carbs_total": str(carbs_total),
        "carbs_times_isf_total": str(carbs_times_isf_total),
        "extra_carb_count": len(extra_carbs), "g0": str(g0["value"]), "g3": str(g1["value"]),
        "g0_sources": [g0["before"].isoformat(), g0["after"].isoformat()],
        "g3_sources": [g1["before"].isoformat(), g1["after"].isoformat()],
        "delta_mgdl": str(delta), "meal_isf": str(meal_isf),
        "rapid_units_in_window": str(rapid_units_in_window),
        "insulin_effect_mgdl": str(insulin_effect), "denominator_mgdl": str(denominator),
        "doses": effects, "carb_events": all_carb_events,
        "blockers": blockers,
        "ratio_g_per_unit": ratio, "factor_units_per_ke": factor, "reason": reason,
        "assumptions": ["alle KH im Fenster wirken bis zum Endpunkt vollständig", "volle IE jeder schnell wirksamen Injektion im Fenster", "alle KH im Fenster teilen einen Insulinfaktor", "kein zusätzlicher Hintergrundtrend"],
    }
