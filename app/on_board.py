"""What still acts on glucose right now: rapid insulin, carbs and the meal surcharge still to come.

Insulin: the active profile's activity curve (app/insulin.py, default Fiasp: peak 55 min after a
10-min delay, 6 h duration). Remaining effect = Σ rapid IE × still-active share × KF at the dose.

Simple model: carbs are digested,
insulin lowers, glucose is the sum; no rules about meals, snacks or time windows.

Carbs: every carb entry, meal, snack or carbs alone, goes into one stomach STOMACH_DELAY minutes after it is entered.
The stomach passes on at most STOMACH_RATE g/min, less when little is in it, so a large meal or a second one
while the first is still there digests more slowly. Remaining effect = remaining KE × meal factor × KF at the
entry (the model equation: one KE raises glucose by factor × KF; the factor follows the clock time of the entry).

Surcharge (surcharge method only): each entry carries the share its carbs add to what is still in the stomach,
of the full surcharge (plus the fat surcharge with the fat/protein mark, or the calculated fat/protein surcharge,
app/fpe.py), linearly from the entry over 5 h (6 h fat/protein, calculated: its own time). Backtest over 60 days
(3-h forecast, 8–23 h): MAE 50.5 instead of 51.7 with the former rules (linear 2–3 h absorption, surcharge from
2–5 h, breakfast 1–4 h, meals grouped by 60 min). A place switched on for the place factor scales the carbs and
surcharge of its entries.

Sport: each session lowers glucose by its effect (the setting of its kind, or the value the user set on the
session in "Tag durchgehen"; negative raises, e.g. racing), linearly from the session's end over its kind's
duration (setting, default 5 h). After
gym the glucose fell about 130 mg/dL more than on days without sport by 6 h after the start, mostly 1-5 h after
the end. A session entered ahead counts already.

These are model assumptions for a rough forecast, not physiological facts or dosing advice.
"""
from __future__ import annotations

from app import core
from datetime import datetime, timedelta
from decimal import Decimal
from app import calculation
from app.calculation import INSULIN_DELAY, INSULIN_DURATION, INSULIN_PEAK, insulin_remaining  # noqa: F401


# Stomach: every carb entry reaches the stomach
# STOMACH_DELAY minutes after it is entered; the stomach passes on at most STOMACH_RATE g/min, less when little is
# in it: rate = STOMACH_RATE × S / (S + STOMACH_HALF_GRAMS) for S g inside, shared by all entries in it. Alone,
# 40 g are half absorbed after about 1 h and 90 % after 2.4 h, 120 g after 1.7 h and 3.6 h; a second meal
# while the first is still in the stomach digests more slowly. Entries older than CARBS_LOOKBACK no longer count.
STOMACH_DELAY = 15
STOMACH_RATE = 1.0
STOMACH_HALF_GRAMS = 40.0
CARBS_LOOKBACK = Decimal(720)
# A carb entry with less than this left no longer shows as acting.
NEGLIGIBLE_GRAMS = Decimal("0.5")
# The surcharge of an entry acts linearly from the entry over this time (fat/protein mark: longer).
SURCHARGE_MINUTES, SURCHARGE_MINUTES_FAT = Decimal(300), Decimal(360)
SPORT_MINUTES = Decimal(300)  # default; settings sport_durations_minutes per kind
# Sessions entered for later count once they start within this time.
SPORT_AHEAD = timedelta(hours=6)
# Typical forecast miss after a post-meal correction (11 corrections, analysis 2026-10-08).
FORECAST_SPREAD_MGDL = Decimal(60)
# Likely range of the expected value (8 of 10 cases, P10..P90 of the miss), by how much still acts in
# total (insulin + carbs + surcharge, mg/dL): (from, below expected, above expected). Estimated offline
# from 23 days of the author's data.
FORECAST_BAND = ((Decimal(150), Decimal(75), Decimal(90)),
                 (Decimal(40), Decimal(50), Decimal(60)),
                 (Decimal(0), Decimal(30), Decimal(30)))
# Within this time after a main meal the carbs still rise and the forecast is least certain.
FRESH_MEAL_MINUTES = 90
# An effect counts as finished once less than this is left.
NEGLIGIBLE_MGDL = Decimal(5)
STEP = timedelta(minutes=5)
HORIZON = timedelta(hours=8)


def stomach(entries: list[tuple[datetime, Decimal]]) -> list[list[float]]:
    """Absorbed share of each (time, grams) entry, minute by minute from the first entry's time (index 0)."""
    return core.call("on_board.stomach", entries=entries)


def attach_stomach(rows: list[dict]) -> list[dict]:
    """Give each carb row ({"at", "grams"}) its absorption curve from the stomach shared with the others."""
    if not rows:
        return rows
    first = min(row["at"] for row in rows)
    for row, curve in zip(rows, stomach([(row["at"], row["grams"]) for row in rows])):
        row["absorbed"], row["origin"] = curve, first
    return rows


def carb_left(row: dict, moment: datetime) -> Decimal:
    """Share of a carb row not yet absorbed at `moment` (a row without a curve digests alone)."""
    if "absorbed" not in row:
        attach_stomach([row])
    if moment <= row["at"]:
        return Decimal(1)
    curve = row["absorbed"]
    position = float(_minutes(moment, row["origin"]))
    index = int(position)
    if index + 1 >= len(curve):
        done = curve[-1]
    else:
        done = curve[index] + (curve[index + 1] - curve[index]) * (position - index)
    return max(Decimal(0), Decimal(1) - Decimal(str(round(done, 6))))


def stomach_grams(treatments: list[dict], now: datetime) -> Decimal:
    """Carbs still in the stomach (or on their way to it) at `now`: what a new entry joins."""
    return core.call("on_board.stomach_grams", treatments=treatments, now=now)


def surcharge_remaining(minutes: Decimal, duration: Decimal = SURCHARGE_MINUTES) -> Decimal:
    """Share of an entry's surcharge still to come: linear from the entry over `duration` minutes."""
    return core.call("on_board.surcharge_remaining", minutes=minutes, duration=duration)


def sport_remaining(minutes_after_end: Decimal, duration: Decimal = SPORT_MINUTES) -> Decimal:
    return core.call("on_board.sport_remaining", minutes_after_end=minutes_after_end, duration=duration)


def sport_minutes(item: dict, settings: dict, overrides: dict | None = None) -> Decimal:
    """Minutes after its end over which a session acts: the user's value for this session, else the
    setting of its kind."""
    if overrides and item.get("id") in overrides:
        return Decimal(int(overrides[item["id"]]))
    return Decimal(int(settings.get("sport_durations_minutes", {}).get(item.get("sport_type"), SPORT_MINUTES)))


def sport_effect(item: dict, settings: dict, overrides: dict | None = None) -> Decimal:
    """A session's glucose drop in mg/dL: the user's value for this session, else the setting of its kind."""
    if overrides and item.get("id") in overrides:
        return calculation.dec(overrides[item["id"]])
    return calculation.dec(settings.get("sport_effects_mgdl", {}).get(item.get("sport_type"), 0))


def sport_end(item: dict) -> datetime:
    return item["at"] + timedelta(minutes=int(item.get("duration") or 0))


def sport_share(item: dict, start: datetime, end: datetime, duration: Decimal = SPORT_MINUTES) -> Decimal:
    """Share of a session's effect that acts between start and end."""
    finish = sport_end(item)
    return sport_remaining(_minutes(start, finish), duration) - sport_remaining(_minutes(end, finish), duration)


def _minutes(later: datetime, earlier: datetime) -> Decimal:
    return Decimal(str((later - earlier).total_seconds())) / 60


def is_main_meal(item: dict) -> bool:
    return ((item.get("role") == "meal" or item.get("eventType") == "Meal Bolus")
            and calculation.dec(item.get("carbs") or 0) > 0)


def entry_surcharges(carbs: list[dict], treatments: list[dict], now: datetime, surcharge_mgdl: Decimal, surcharge_share,
                     fat_surcharge_mgdl: Decimal, keep_all: bool, scale=None) -> list[dict]:
    """The surcharge of each carb entry: the share
    its carbs add to what is still in the stomach, share(in stomach + carbs) − share(in stomach), of the full
    surcharge (the configured one, plus the fat surcharge with the entry's fat/protein mark, or the calculated
    fat/protein surcharge the entry was dosed with, app/fpe.py). So a bite, starter, main and dessert in one entry
    or in several get the same surcharge in total, capped as one meal, without grouping by time; once the stomach
    is empty again, the next entry starts afresh. Each part acts linearly from its entry over SURCHARGE_MINUTES
    (fat/protein SURCHARGE_MINUTES_FAT, calculated: its own minutes). `carbs` are the rows with stomach curves."""
    rows = []
    by_id = {item.get("id") or (item["at"], item.get("insulin"), item.get("carbs")): item for item in treatments}
    for index, row in enumerate(carbs):
        item = by_id.get(row["key"], {})
        before = sum((other["grams"] * carb_left(other, row["at"]) for other in carbs[:index]), Decimal(0))
        own = item.get("surcharge")
        fat = bool(item.get("fat_protein"))
        full = calculation.dec(own["mgdl"]) if own else surcharge_mgdl + (fat_surcharge_mgdl if fat else Decimal(0))
        duration = (Decimal(int(own["minutes"])) if own else SURCHARGE_MINUTES_FAT if fat else SURCHARGE_MINUTES)
        part = full * (surcharge_share(before + row["grams"]) - surcharge_share(before))
        place = (scale(item) if scale and item else None) or Decimal(1)
        age = Decimal(0) if keep_all else _minutes(now, row["at"])
        if part > 0 and age < duration:
            rows.append({"at": row["at"], "fat": fat, "minutes": duration, "mgdl": part * place})
    return rows


def sources(treatments: list[dict], now: datetime, settings: dict, carb_factor, surcharge_mgdl: Decimal | None,
            keep_all: bool = False, sport_overrides: dict | None = None,
            fat_surcharge_mgdl: Decimal = Decimal(0), scale=None, sport_minutes_overrides: dict | None = None) -> dict:
    """The doses, carb entries, surcharges and sport sessions that may still act at `now`, with their full
    effect in mg/dL. carb_factor(at) gives the IE/KE that values a carb entry made at `at`. keep_all keeps
    everything up to `now` however old, for a whole-day timeline. sport_overrides maps a session's id to
    the drop the user set for it, sport_minutes_overrides its effect duration. A fat/protein main meal carries fat_surcharge_mgdl more surcharge;
    scale(item) gives the place factor of an entry (None = 1) for its carbs and surcharge."""
    return core.sources(treatments=treatments, now=now, settings=settings, carb_factor=carb_factor,
                        surcharge_mgdl=surcharge_mgdl, keep_all=keep_all,
                        sport_overrides=sport_overrides, fat_surcharge_mgdl=fat_surcharge_mgdl, scale=scale,
                        sport_minutes_overrides=sport_minutes_overrides)


def effects_at(found: dict, moment: datetime, cutoff: datetime | None = None) -> dict:
    """Effect still to come at `moment` in mg/dL (positive numbers) and the active insulin in IE.
    Entries after `cutoff` (default `moment`) do not count; entries between `moment` and a later cutoff
    count in full, so effects_at(a, b) − effects_at(b, b) is what acted from a to b. Sport sessions count
    whatever the cutoff, as they are known ahead. sport_mgdl is signed: positive lowers glucose."""
    return core.call("on_board.effects_at", found=found, moment=moment, cutoff=cutoff)


def forecast_band(lowering_mgdl: Decimal, raising_mgdl: Decimal) -> tuple[Decimal, Decimal]:
    """How far below and above the expected value glucose likely ends (8 of 10 cases)."""
    return core.call("on_board.forecast_band", lowering_mgdl=lowering_mgdl, raising_mgdl=raising_mgdl)


def summary(found: dict, now: datetime) -> dict:
    """Current remaining effects plus when each side has finished acting."""
    return core.call("on_board.summary", found=found, now=now)


def timeline(found: dict, entries: list[dict], start: datetime, end: datetime,
             horizon: timedelta = timedelta(hours=3)) -> list[dict]:
    """Every STEP from start to end: how fast insulin lowers and carbs plus surcharge raise glucose
    (mg/dL per hour over the step), the measured glucose and the model's value from the glucose
    `horizon` earlier: G(t − h) − insulin acted + carbs and surcharge acted since then."""
    return core.call("on_board.timeline", found=found, entries=entries, start=start, end=end,
                     horizon_seconds=horizon.total_seconds())
