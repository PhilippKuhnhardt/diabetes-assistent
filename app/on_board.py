"""What still acts on glucose right now: rapid insulin, carbs and the meal surcharge still to come.

Analysis and approval in the project files, analysen/fiasp-wirkkurve.md (2026-10-08).

Insulin: exponential activity curve as Loop's Fiasp preset (peak 55 min after a 10-min delay,
6 h duration); Philipp's data fit it (about 0 % after 30 min, 50 % after 90 min, 75 % after 2 h,
nearly all after 4 h). Remaining effect = Σ rapid IE × still-active share × KF at the dose.

Carbs: each entry is absorbed linearly, over 2 h up to 40 g and 3 h from 100 g (linear in between;
re-estimated 2026-10-22, analysen/pruefstand-2026-10/annahmen.md: the former 2.5–3.5 h and the fat/protein
stretch by half made the forecast too high after meals). Remaining effect = remaining KE × meal factor
× KF at the entry (the model equation: one KE raises glucose by factor × KF).

Surcharge (surcharge method only): the main meal's surcharge × share acts linearly from 2 h to 5 h
after the meal start (6 h with a fat/protein mark). Snacks never carry it.

These are model assumptions for a rough forecast, not physiological facts or dosing advice.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from app import calculation

INSULIN_PEAK = Decimal(55)
INSULIN_DURATION = Decimal(360)
INSULIN_DELAY = Decimal(10)
CARBS_SHORT_MINUTES, CARBS_SHORT_GRAMS = Decimal(120), Decimal(40)
CARBS_LONG_MINUTES, CARBS_LONG_GRAMS = Decimal(180), Decimal(100)
SURCHARGE_FROM = Decimal(120)
SURCHARGE_TO, SURCHARGE_TO_FAT = Decimal(300), Decimal(360)
# Typical forecast miss after a post-meal correction (11 corrections, analysis 2026-10-08).
FORECAST_SPREAD_MGDL = Decimal(60)
# Likely range of the expected value (8 of 10 cases, P10..P90 of the miss), by how much still acts in
# total (insulin + carbs + surcharge, mg/dL): (from, below expected, above expected). Estimated offline
# from 23 days, analysis in the project files analysen/fruehprognose.md (2026-10-11).
FORECAST_BAND = ((Decimal(150), Decimal(75), Decimal(90)),
                 (Decimal(40), Decimal(50), Decimal(60)),
                 (Decimal(0), Decimal(30), Decimal(30)))
# Within this time after a main meal the carbs still rise and the forecast is least certain.
FRESH_MEAL_MINUTES = 90
# An effect counts as finished once less than this is left.
NEGLIGIBLE_MGDL = Decimal(5)
STEP = timedelta(minutes=5)
HORIZON = timedelta(hours=8)


def insulin_remaining(minutes: Decimal) -> Decimal:
    """Share of a rapid dose that has not acted yet, `minutes` after the injection."""
    t = calculation.dec(minutes) - INSULIN_DELAY
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


def carb_minutes(grams: Decimal) -> Decimal:
    """Absorption time of one carb entry: longer for larger amounts."""
    share = min(Decimal(1), max(Decimal(0), (grams - CARBS_SHORT_GRAMS) / (CARBS_LONG_GRAMS - CARBS_SHORT_GRAMS)))
    return CARBS_SHORT_MINUTES + share * (CARBS_LONG_MINUTES - CARBS_SHORT_MINUTES)


def carb_remaining(minutes: Decimal, grams: Decimal) -> Decimal:
    if minutes <= 0:
        return Decimal(1)
    return max(Decimal(0), 1 - minutes / carb_minutes(grams))


def surcharge_remaining(minutes: Decimal, fat: bool = False) -> Decimal:
    end = SURCHARGE_TO_FAT if fat else SURCHARGE_TO
    if minutes <= SURCHARGE_FROM:
        return Decimal(1)
    return max(Decimal(0), (end - minutes) / (end - SURCHARGE_FROM))


def _minutes(later: datetime, earlier: datetime) -> Decimal:
    return Decimal(str((later - earlier).total_seconds())) / 60


def is_main_meal(item: dict) -> bool:
    return ((item.get("role") == "meal" or item.get("eventType") == "Meal Bolus")
            and calculation.dec(item.get("carbs") or 0) > 0)


def sources(treatments: list[dict], now: datetime, settings: dict, carb_factor, surcharge_mgdl: Decimal | None,
            surcharge_share, keep_all: bool = False) -> dict:
    """The doses, carb entries and surcharges that may still act at `now`, with their full effect in mg/dL.
    carb_factor(at) gives the IE/KE that values a carb entry made at `at`. keep_all keeps everything up to
    `now` however old, for a whole-day timeline."""
    zone = settings["timezone"]
    isf = lambda at: calculation.isf_at(at, settings.get("isf_schedule", []), zone)
    seen, doses, carbs, surcharges, meals = set(), [], [], [], []
    for item in sorted(treatments, key=lambda row: row["at"]):
        key = item.get("id") or (item["at"], item.get("insulin"), item.get("carbs"))
        if key in seen or item["at"] > now:
            continue
        seen.add(key)
        age = Decimal(0) if keep_all else _minutes(now, item["at"])
        units = calculation.dec(item.get("insulin") or 0)
        if units > 0 and calculation.is_rapid_insulin(item) and age < INSULIN_DELAY + INSULIN_DURATION:
            doses.append({"at": item["at"], "units": units, "mgdl_per_unit": isf(item["at"])})
        grams = calculation.dec(item.get("carbs") or 0)
        fat = bool(item.get("fat_protein"))
        if grams > 0 and age < carb_minutes(grams):
            factor = carb_factor(item["at"])
            if factor is not None:
                carbs.append({"at": item["at"], "grams": grams,
                              "mgdl_per_gram": factor * isf(item["at"]) / 10})
        if is_main_meal(item) and age < FRESH_MEAL_MINUTES:
            meals.append(item["at"])
        if surcharge_mgdl is not None and is_main_meal(item) and age < SURCHARGE_TO_FAT:
            surcharges.append({"at": item["at"], "fat": fat, "mgdl": surcharge_mgdl * surcharge_share(grams)})
    return {"doses": doses, "carbs": carbs, "surcharges": surcharges, "fresh_meals": meals}


def effects_at(found: dict, moment: datetime, cutoff: datetime | None = None) -> dict:
    """Effect still to come at `moment` in mg/dL (positive numbers) and the active insulin in IE.
    Entries after `cutoff` (default `moment`) do not count; entries between `moment` and a later cutoff
    count in full, so effects_at(a, b) − effects_at(b, b) is what acted from a to b."""
    cutoff = cutoff or moment
    found = {key: [row for row in found[key] if row["at"] <= cutoff] for key in ("doses", "carbs", "surcharges")}
    units = sum((dose["units"] * insulin_remaining(_minutes(moment, dose["at"])) for dose in found["doses"]), Decimal(0))
    lowering = sum((dose["units"] * insulin_remaining(_minutes(moment, dose["at"])) * dose["mgdl_per_unit"]
                    for dose in found["doses"]), Decimal(0))
    carbs = sum((row["grams"] * carb_remaining(_minutes(moment, row["at"]), row["grams"]) * row["mgdl_per_gram"]
                 for row in found["carbs"]), Decimal(0))
    surcharge = sum((row["mgdl"] * surcharge_remaining(_minutes(moment, row["at"]), row["fat"])
                     for row in found["surcharges"]), Decimal(0))
    return {"insulin_units": units, "lowering_mgdl": lowering, "carbs_mgdl": carbs,
            "surcharge_mgdl": surcharge, "raising_mgdl": carbs + surcharge}


def forecast_band(lowering_mgdl: Decimal, raising_mgdl: Decimal) -> tuple[Decimal, Decimal]:
    """How far below and above the expected value glucose likely ends (8 of 10 cases)."""
    acting = lowering_mgdl + raising_mgdl
    for start, below, above in FORECAST_BAND:
        if acting >= start:
            return below, above
    return FORECAST_BAND[-1][1], FORECAST_BAND[-1][2]


def _until(found: dict, now: datetime, key: str) -> datetime | None:
    """First 5-minute step from now at which less than NEGLIGIBLE_MGDL of the effect is left."""
    if effects_at(found, now)[key] < NEGLIGIBLE_MGDL:
        return None
    moment = now
    while moment < now + HORIZON:
        moment += STEP
        if effects_at(found, moment)[key] < NEGLIGIBLE_MGDL:
            return moment
    return moment


def summary(found: dict, now: datetime) -> dict:
    """Current remaining effects plus when each side has finished acting."""
    current = effects_at(found, now)
    lowering_until, raising_until = _until(found, now, "lowering_mgdl"), _until(found, now, "raising_mgdl")
    ends = [moment for moment in (lowering_until, raising_until) if moment]
    return {**current, "lowering_until": lowering_until, "raising_until": raising_until,
            "settled_at": max(ends) if ends else None, "fresh_meal_at": max(found["fresh_meals"], default=None),
            "doses": [{"at": dose["at"], "units": dose["units"],
                       "remaining_units": dose["units"] * insulin_remaining(_minutes(now, dose["at"]))}
                      for dose in found["doses"]],
            "carbs": [{"at": row["at"], "grams": row["grams"],
                       "remaining_grams": row["grams"] * carb_remaining(_minutes(now, row["at"]), row["grams"])}
                      for row in found["carbs"]]}


def timeline(found: dict, entries: list[dict], start: datetime, end: datetime,
             horizon: timedelta = timedelta(hours=3)) -> list[dict]:
    """Every STEP from start to end: how fast insulin lowers and carbs plus surcharge raise glucose
    (mg/dL per hour over the step), the measured glucose and the model's value from the glucose
    `horizon` earlier: G(t − h) − insulin acted + carbs and surcharge acted since then."""
    def reading(moment):
        try:
            return calculation.interpolate_glucose(entries, moment)["value"]
        except ValueError:
            return None
    per_hour = Decimal(3600) / Decimal(str(STEP.total_seconds()))
    rows, moment = [], start
    while moment <= end:
        now, after = effects_at(found, moment), effects_at(found, moment + STEP)
        step_start, before = effects_at(found, moment, moment + STEP), effects_at(found, moment - horizon, moment)
        earlier = reading(moment - horizon)
        model = (None if earlier is None else earlier - (before["lowering_mgdl"] - now["lowering_mgdl"])
                 + (before["raising_mgdl"] - now["raising_mgdl"]))
        rows.append({"at": moment, "glucose_mgdl": reading(moment), "model_mgdl": model,
                     "lowering_per_hour": (step_start["lowering_mgdl"] - after["lowering_mgdl"]) * per_hour,
                     "raising_per_hour": (step_start["raising_mgdl"] - after["raising_mgdl"]) * per_hour,
                     "insulin_units": now["insulin_units"]})
        moment += STEP
    return rows
