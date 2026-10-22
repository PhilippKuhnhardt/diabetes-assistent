"""The diary's "Wirkungen" view: what lowered and raised glucose over a day, and the model against the CGM."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from app import forecast, on_board
from app.common import json_safe, parse_instant
from app.services import day_view, factors
from app.store import config, marks

HORIZON = timedelta(hours=3)
# A miss counts as notable from this many mg/dL between the model and the CGM.
NOTABLE_MGDL = Decimal(40)


def day_effects(params: dict) -> dict:
    day = params.get("date", [None])[0]
    if day is None:
        raise ValueError("Datum fehlt")
    parsed = date.fromisoformat(day)
    _, settings = config.get_settings()
    zone = ZoneInfo(settings["timezone"])
    payload = day_view.day_payload(day, refresh=False)
    entries = [{"at": parse_instant(row["at"]), "sgv": row["sgv"]} for row in payload["entries"]]
    treatments = [{**row, "at": parse_instant(row["at"])} for row in payload["treatments"]]
    start = datetime.combine(parsed, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)
    end = min(start + timedelta(days=1), datetime.now(timezone.utc).replace(second=0, microsecond=0))
    end -= timedelta(minutes=end.minute % 5)
    overrides, periods = marks.analysis_day_types(), marks.vacation_periods()
    day_type = marks.day_type_for(day, overrides, periods)[0]
    _, _, lookback = factors.forecast_settings(settings)
    points = factors.meal_points(parsed, lookback, overrides, periods)
    method = settings.get("auto_method", "percentile")
    try:
        found = on_board.sources(treatments, end, settings, factors.carb_factor(settings, parsed, points, day_type),
                                 factors.surcharge_mgdl(settings) if method == forecast.SURCHARGE else None,
                                 forecast.surcharge_share, keep_all=True,
                                 sport_overrides=marks.sport_session_effects())
    except ValueError:
        return {"day": day, "rows": [], "reason": "Korrekturfaktor-Profil aus Nightscout fehlt"}
    rows = on_board.timeline(found, entries, start, end, HORIZON) if end > start else []
    pairs = [(row["at"], row["glucose_mgdl"] - row["model_mgdl"]) for row in rows
             if row["glucose_mgdl"] is not None and row["model_mgdl"] is not None]
    above = max(pairs, key=lambda pair: pair[1], default=None)
    below = min(pairs, key=lambda pair: pair[1], default=None)
    return json_safe({
        "day": day, "step_minutes": int(on_board.STEP.total_seconds() // 60),
        "horizon_minutes": int(HORIZON.total_seconds() // 60), "from": start, "to": end,
        "rows": [{key: (round(value, 1) if isinstance(value, Decimal) else value) for key, value in row.items()}
                 for row in rows],
        "summary": {
            "count": len(pairs),
            "mean_abs_mgdl": round(sum((abs(diff) for _, diff in pairs), Decimal(0)) / len(pairs), 0) if pairs else None,
            "mean_mgdl": round(sum((diff for _, diff in pairs), Decimal(0)) / len(pairs), 0) if pairs else None,
            "max_above": {"at": above[0], "mgdl": round(above[1], 0)} if above and above[1] >= NOTABLE_MGDL else None,
            "max_below": {"at": below[0], "mgdl": round(below[1], 0)} if below and below[1] <= -NOTABLE_MGDL else None,
        },
    })
