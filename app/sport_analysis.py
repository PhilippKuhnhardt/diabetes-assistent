"""Estimate how individual sport sessions shifted the glucose of nearby meals.

The estimate compares each sport-influenced meal with the median factor of
meals of the same type in the period that had no sport. With the model
equation IE/KE = 10 × (ΔG + Σ[IE × KF]) / Σ[g KH × KF], the glucose change
expected at the endpoint for the baseline factor F is F × Σ[g KH × KF] / 10 −
Σ[IE × KF]. The sport effect is the observed ΔG minus that expectation:
negative values mean the meal ended lower than usual, positive values higher
(for example adrenaline while racing). This is a model estimate, not a
physiological measurement or dosing advice.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from app.i18n import tr

TEN = Decimal(10)


def _median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _instant(value) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def raw_factor(result: dict) -> Decimal | None:
    """Factor without any confirmed sport correction, or None when blocked."""
    if not result or result.get("blockers") or not result.get("start"):
        return None
    try:
        delta = Decimal(result["delta_mgdl"])
        insulin = Decimal(result["insulin_effect_mgdl"])
        carb_effect = Decimal(result["carbs_times_isf_total"])
    except (KeyError, TypeError, ArithmeticError):
        return None
    if carb_effect <= 0:
        return None
    return TEN * (delta + insulin) / carb_effect


def analyze(meals: list[dict], exercises: list[dict], lookback_minutes: int,
            window_minutes: int, labels: dict[str, str], standards: dict[str, Decimal] | None = None,
            own_sessions: set | None = None) -> dict:
    """Link sport sessions to meals and estimate each meal's sport effect.

    meals: {id, day, meal, at, excluded, calculation, sport}
    exercises: {id, at, duration_minutes, kind, notes}
    A session influences a meal when it ends at most lookback_minutes before the
    meal starts and begins before the meal's endpoint.
    """
    lookback = timedelta(minutes=int(lookback_minutes))
    sessions = []
    for item in sorted(exercises, key=lambda item: _instant(item["at"])):
        start = _instant(item["at"])
        duration = int(Decimal(str(item.get("duration_minutes") or 0)))
        sessions.append({**item, "start": start, "end": start + timedelta(minutes=duration),
                         "duration_minutes": duration, "meal_ids": []})

    prepared = []
    for meal in meals:
        result = meal.get("calculation") or {}
        start = _instant(meal["at"])
        end = _instant(result["end"]) if result.get("end") else start + timedelta(minutes=int(window_minutes))
        linked = [session for session in sessions
                  if session["end"] >= start - lookback and session["start"] < end]
        for session in linked:
            session["meal_ids"].append(meal["id"])
        sport = meal.get("sport") or {}
        prepared.append({
            "meal": meal, "start": start, "end": end, "sessions": linked,
            "confirmed": sport.get("status") == "affected",
            "factor": raw_factor(result),
        })

    baselines = {}
    for kind in ("breakfast", "lunch", "dinner"):
        values = [item["factor"] for item in prepared
                  if item["meal"]["meal"] == kind and item["factor"] is not None and item["factor"] > 0
                  and not item["sessions"] and not item["confirmed"] and not item["meal"].get("excluded")]
        median = _median(values)
        baselines[kind] = {"median_units_per_ke": str(median) if median is not None else None,
                           "count": len(values)}

    influenced = {}
    for item in prepared:
        if not item["sessions"] and not item["confirmed"]:
            continue
        meal = item["meal"]
        result = meal.get("calculation") or {}
        baseline_text = baselines[meal["meal"]]["median_units_per_ke"]
        expected = effect = None
        reason = None
        if item["factor"] is None:
            reason = result.get("reason") or tr("Mahlzeit nicht berechenbar")
        elif baseline_text is None:
            reason = tr("Keine Vergleichsmahlzeiten ohne Sport")
        else:
            carb_effect = Decimal(result["carbs_times_isf_total"])
            expected = Decimal(baseline_text) * carb_effect / TEN - Decimal(result["insulin_effect_mgdl"])
            effect = Decimal(result["delta_mgdl"]) - expected
        sport = meal.get("sport") or {}
        influenced[meal["id"]] = {
            "id": meal["id"], "day": meal["day"], "meal": meal["meal"],
            "start": item["start"].isoformat(), "end": item["end"].isoformat(),
            "excluded": bool(meal.get("excluded")),
            "carbs_total": result.get("carbs_total"),
            "rapid_units_in_window": result.get("rapid_units_in_window"),
            "delta_mgdl": result.get("delta_mgdl"),
            "expected_delta_mgdl": str(expected) if expected is not None else None,
            "effect_mgdl": str(effect) if effect is not None else None,
            "factor_without_sport_units_per_ke": str(item["factor"]) if item["factor"] is not None else None,
            "baseline_units_per_ke": baseline_text,
            "confirmed_effect_mgdl": sport.get("effect_mgdl") if item["confirmed"] else None,
            # What the factor counted for sport in this meal (the sessions' shares), as a glucose change.
            "applied_mgdl": str(-Decimal(sport["effect_mgdl"])) if sport.get("effect_mgdl") is not None else None,
            "shared": len(item["sessions"]) > 1,
            "session_ids": [session["id"] for session in item["sessions"]],
            "reason": reason,
        }

    rendered_sessions = []
    for session in sessions:
        rendered_sessions.append({
            "id": session["id"], "kind": session.get("kind"),
            "label": labels.get(session.get("kind"), tr("Sportart unbekannt")),
            "start": session["start"].isoformat(), "end": session["end"].isoformat(),
            "duration_minutes": session["duration_minutes"], "notes": session.get("notes") or "",
            "meals": [influenced[meal_id] for meal_id in session["meal_ids"]],
        })
    # Meals the user marked as sport-affected without any recorded session.
    for meal_id, meal in influenced.items():
        if not meal["session_ids"]:
            rendered_sessions.append({
                "id": None, "kind": None, "label": tr("Sport ohne Eintrag"),
                "start": meal["start"], "end": None, "duration_minutes": None, "notes": "",
                "meals": [meal],
            })
    rendered_sessions.sort(key=lambda item: item["start"], reverse=True)

    by_kind = {}
    for session in rendered_sessions:
        group = by_kind.setdefault(session["kind"] or "", {
            "kind": session["kind"], "label": session["label"], "session_count": 0,
            "meal_count": 0, "effects": [], "applied": [], "ratios": []})
        group["session_count"] += 1
        for meal in session["meals"]:
            group["meal_count"] += 1
            # Shared meals cannot be attributed to one session; excluded meals stay out of medians.
            if meal["effect_mgdl"] is not None and not meal["shared"] and not meal["excluded"]:
                group["effects"].append(Decimal(meal["effect_mgdl"]))
                if meal["applied_mgdl"] is not None:
                    group["applied"].append(Decimal(meal["applied_mgdl"]))
                    # Measured ÷ assumed, for sessions on the kind's standard effect only.
                    if Decimal(meal["applied_mgdl"]) and session["id"] not in (own_sessions or set()):
                        group["ratios"].append(Decimal(meal["effect_mgdl"]) / Decimal(meal["applied_mgdl"]))
    kinds = []
    for group in by_kind.values():
        effects = group.pop("effects")
        ratios = group.pop("ratios")
        group["suggested_drop_mgdl"] = suggested_drop((standards or {}).get(group["kind"]), ratios)
        applied = _median(group.pop("applied"))
        group["median_applied_mgdl"] = str(applied) if applied is not None else None
        median = _median(effects)
        group["effect_count"] = len(effects)
        group["median_effect_mgdl"] = str(median) if median is not None else None
        group["min_effect_mgdl"] = str(min(effects)) if effects else None
        group["max_effect_mgdl"] = str(max(effects)) if effects else None
        kinds.append(group)
    kinds.sort(key=lambda group: group["label"])
    return {"baselines": baselines, "kinds": kinds, "sessions": rendered_sessions,
            "lookback_minutes": int(lookback_minutes)}


SUGGEST_MIN_MEALS = 2
SUGGEST_MAX_RATIO = Decimal(2)


def suggested_drop(standard: Decimal | None, ratios: list[Decimal]) -> str | None:
    """The kind's standard drop that would have matched what the meals measured (user decision 2026-11-27):
    the standard scaled by the median of measured ÷ assumed over meals after sessions on the standard,
    rounded to 5 mg/dL within the settings' −500..500; None without a standard, too few meals or no change.
    The ratio stays within 0..SUGGEST_MAX_RATIO: meals that went the other way (glucose rose after jogging)
    say the session did little, not that the kind raises glucose, and a few meals never more than double it."""
    if not standard or len(ratios) < SUGGEST_MIN_MEALS:
        return None
    ratio = max(Decimal(0), min(SUGGEST_MAX_RATIO, _median(ratios)))
    value = max(Decimal(-500), min(Decimal(500), (Decimal(standard) * ratio / 5).quantize(Decimal(1)) * 5))
    value = value if value else Decimal(0)  # no "-0"
    return str(value) if value != Decimal(standard) else None
