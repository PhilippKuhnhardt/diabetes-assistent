"""Analysis page: factor groups, Automatik derivation, night and sport overview.

The rules live in the Rust core (core/diabetes-core/src/summary.rs); this module reads the stored days,
mirrors the ones never mirrored, stores the result per period and hands the core its snapshot."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal

from app import core
from app.common import json_safe, local_zone
from app.ingestion import nightscout, sync
from app.services import day_view, factors
from app.store import config, marks, mirror, results
from app.i18n import tr


def summary(params: dict) -> dict:
    days = int(params.get("days", ["14"])[0])
    if days not in (1, 3, 7, 14, 30):
        raise ValueError(tr("Ungültiger Zeitraum"))
    profile_sync_error = None
    sync_errors = []
    _, settings = config.get_settings()
    today = datetime.now(local_zone(settings["timezone"])).date()
    if nightscout.is_configured():
        try:
            sync.sync_profile_cache()
        except nightscout.NightscoutError as error:
            profile_sync_error = str(error)
        # The page loads its period by itself (user request 2026-11-26, replacing "Zeitraum aus Nightscout
        # laden"): the background job keeps yesterday and today current, and every day the analysis reads
        # that was never mirrored is fetched once here.
        missing = mirror.never_synced([(today - timedelta(days=offset)).isoformat()
                                       for offset in range(history_days(days) + 1)])
        if missing:
            _, sync_errors = sync.mirror_days(missing, settings["timezone"])
            memo = getattr(day_view._pass, "memo", None)
            if memo is not None:
                memo.clear()  # inside a background round the memo holds these days as they were before
    # Stored per period while today, the exclusions and every day it reads are unchanged.
    fingerprint = hashlib.sha256(json.dumps([
        day_view.CODE_VERSION, today.isoformat(), sorted(marks.analysis_exclusions()),
        [day_view.day_fingerprint((today - timedelta(days=offset)).isoformat())
         for offset in range(history_days(days) + 1)],
    ]).encode()).hexdigest()
    result = results.summary_result(str(days), fingerprint)
    if result is None:
        result = json_safe(compute_summary(days, today))
        results.save_summary_result(str(days), fingerprint, result)
    result["profile_sync_error"] = profile_sync_error
    result["sync_error_days"] = [item["day"] for item in sync_errors]
    return result


def history_days(days: int) -> int:
    """How many days before today the analysis reads (period, 90-day matrix with lookback, stress tile)."""
    return core.call("summary.history_days", days=days)


TREND_WEEKS = 8  # summary.rs TREND_WEEKS: weeks of the miss trend


def summary_snapshot(days: int, today: date) -> dict:
    """Everything compute_summary reads from the store, as JSON (core/diabetes-core/src/summary.rs Snapshot):
    the stored days of the period and the meal points over history_days(days)."""
    from app.store import places as place_store
    _, settings = config.get_settings()
    day_types, vacations = marks.analysis_day_types(), marks.vacation_periods()
    loaded = {}
    for offset in range(days):
        day = (today - timedelta(days=offset)).isoformat()
        loaded[day] = day_view.day_payload(day, refresh=False)
    points = factors.meal_points(today, history_days(days), day_types, vacations, dict(loaded),
                                 mode=factors.vacation_mode(settings))
    return json_safe({
        "today": today.isoformat(), "days": days, "settings": settings,
        "analysis_exclusions": sorted(marks.analysis_exclusions()),
        "day_types": day_types, "vacation_periods": vacations,
        "places": [{"id": place["id"], "adjust": bool(place.get("adjust"))} for place in place_store.all_places()],
        "own_sport_sessions": sorted(marks.sport_session_effects()),
        "payloads": loaded, "points": core.encode(points),
    })


def compute_summary(days: int, today: date) -> dict:
    """The analysis page for the last `days` days, from stored diary days only."""
    return core.call("summary.compute_summary", **summary_snapshot(days, today))


def suggestion_miss(points: list[dict], point: dict, settings: dict) -> tuple[Decimal | None, Decimal | None]:
    """The Automatik suggestion a meal would have got from the days before it, and the factor error in mg/dL
    ((actual − suggested) × Σ[g × KF] / 10); (None, None) below the Automatik's minimum of earlier meals."""
    return tuple(core.call("summary.suggestion_miss", points=points, point=point, settings=settings))


def miss_trend(points: list[dict], settings: dict, today: date) -> dict:
    """The median signed miss per meal kind and week over the last TREND_WEEKS weeks, with a drift when the
    last weeks all missed the same way."""
    return core.call("summary.miss_trend", points=points, settings=settings, today=today)
