//! Current CGM status, trend, target range and colours (app/glucose.py).

use crate::calculation::minutes_between;
use crate::context::{english, local_zone};
use crate::model::{Instant, Reading, Settings, TargetRow};
use chrono::{Duration, NaiveTime};
use rust_decimal::{Decimal, RoundingStrategy};
use rust_decimal_macros::dec;
use serde::Serialize;

pub const TREND_WINDOW_MINUTES: Decimal = dec!(15);
pub const TREND_FLAT_MGDL: Decimal = dec!(10);
pub const TREND_FAST_MGDL: Decimal = dec!(30);
pub const LOW_MGDL: Decimal = dec!(70);
pub const HIGH_MGDL: Decimal = dec!(250);
pub const MGDL_PER_MMOL: Decimal = dec!(18.0182);

/// Low and high limit in mg/dL.
pub fn limits(settings: &Settings) -> (Decimal, Decimal) {
    (settings.low_limit_mgdl.filter(|v| !v.is_zero()).unwrap_or(LOW_MGDL),
     settings.high_limit_mgdl.filter(|v| !v.is_zero()).unwrap_or(HIGH_MGDL))
}

/// A glucose value or change as push messages show it: whole mg/dL, or mmol/L with one decimal.
pub fn shown(mgdl: Decimal, settings: &Settings, signed: bool) -> String {
    let mmol = settings.glucose_unit.as_deref() == Some("mmol");
    let mut text = if mmol {
        format!("{:.1}", (mgdl / MGDL_PER_MMOL).round_dp_with_strategy(1, RoundingStrategy::MidpointNearestEven))
    } else {
        format!("{:.0}", mgdl.round_dp_with_strategy(0, RoundingStrategy::MidpointNearestEven))
    };
    if mmol && !english() {
        text = text.replace('.', ",");
    }
    format!("{}{text}", if signed && mgdl >= Decimal::ZERO { "+" } else { "" })
}

/// The target range row in force at the local clock time of `at`.
pub fn target_at<'a>(at: Instant, settings: &'a Settings) -> Option<&'a TargetRow> {
    let minute = local_zone(&settings.timezone).minute_of_day(&at);
    settings.target_schedule.iter().filter(|row| row.start_minute <= minute)
        .fold(None, |best: Option<&TargetRow>, row| if best.is_none_or(|b| row.start_minute > b.start_minute) { Some(row) } else { best })
}

/// Colour class of a CGM value.
pub fn tone(value: Option<Decimal>, at: Instant, settings: &Settings) -> &'static str {
    let Some(value) = value else { return "none" };
    let (low, high) = limits(settings);
    if value < low {
        return "low";
    }
    if value > high {
        return "high";
    }
    match target_at(at, settings) {
        None => "neutral",
        Some(target) if target.low_mgdl <= value && value <= target.high_mgdl => "in-range",
        Some(_) => "outside",
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct Band {
    pub from: Instant,
    pub to: Instant,
    pub low_mgdl: Decimal,
    pub high_mgdl: Decimal,
}

/// The target range as time segments between two instants.
pub fn target_bands(start: Instant, end: Instant, settings: &Settings) -> Vec<Band> {
    let zone = local_zone(&settings.timezone);
    let mut rows = settings.target_schedule.clone();
    rows.sort_by_key(|row| row.start_minute);
    let mut bands: Vec<Band> = Vec::new();
    let mut day = zone.local(&start).date() - Duration::days(1);
    while day <= zone.local(&end).date() {
        let midnight = day.and_time(NaiveTime::MIN);
        for (index, row) in rows.iter().enumerate() {
            let begin = zone.from_local(midnight + Duration::minutes(row.start_minute));
            let stop_minute = rows.get(index + 1).map(|next| next.start_minute).unwrap_or(1440);
            let stop = zone.from_local(midnight + Duration::minutes(stop_minute));
            if stop <= start || begin >= end {
                continue;
            }
            let band = Band { from: begin.max(start), to: stop.min(end), low_mgdl: row.low_mgdl, high_mgdl: row.high_mgdl };
            match bands.last_mut() {
                Some(last) if last.to == band.from && last.low_mgdl == band.low_mgdl && last.high_mgdl == band.high_mgdl => last.to = band.to,
                _ => bands.push(band),
            }
        }
        day += Duration::days(1);
    }
    bands
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct Trend {
    pub trend_delta_mgdl: Option<Decimal>,
    pub trend_minutes: Option<Decimal>,
    pub trend_direction: Option<&'static str>,
}

/// Least-squares slope over the last 15 minutes, as change per 15 minutes; needs 5 minutes of course.
pub fn trend(points: &[(Instant, Decimal)]) -> Option<Trend> {
    let latest = points.last()?.0;
    let window: Vec<(Decimal, Decimal)> = points.iter().filter(|(at, _)| latest - *at <= Duration::minutes(16))
        .map(|(at, value)| (-minutes_between(latest, *at), *value)).collect();
    let span = window.first().map(|(x, _)| -x).unwrap_or(Decimal::ZERO);
    if window.len() < 2 || span < dec!(5) {
        return None;
    }
    let n = Decimal::from(window.len());
    let mean_x = window.iter().map(|(x, _)| *x).sum::<Decimal>() / n;
    let mean_y = window.iter().map(|(_, y)| *y).sum::<Decimal>() / n;
    let slope = window.iter().map(|(x, y)| (x - mean_x) * (y - mean_y)).sum::<Decimal>()
        / window.iter().map(|(x, _)| (x - mean_x) * (x - mean_x)).sum::<Decimal>();
    let delta = (slope * TREND_WINDOW_MINUTES).round_dp_with_strategy(1, RoundingStrategy::MidpointNearestEven);
    let direction = if delta.abs() < TREND_FLAT_MGDL { "flat" }
        else if delta > Decimal::ZERO { if delta >= TREND_FAST_MGDL { "up_fast" } else { "up" } }
        else if delta <= -TREND_FAST_MGDL { "down_fast" } else { "down" };
    Some(Trend { trend_delta_mgdl: Some(delta), trend_minutes: Some(TREND_WINDOW_MINUTES), trend_direction: Some(direction) })
}

#[derive(Clone, Debug, Serialize)]
pub struct Status {
    pub glucose_mgdl: Option<Decimal>,
    pub glucose_at: Option<Instant>,
    pub fresh: bool,
    pub status: &'static str,
    #[serde(flatten)]
    pub trend: Trend,
    pub target_low_mgdl: Option<Decimal>,
    pub target_high_mgdl: Option<Decimal>,
}

/// The header's glucose: latest reading of the last hour, freshness, trend and range.
pub fn current_status(entries: &[Reading], settings: &Settings, now: Instant) -> Status {
    let mut points: Vec<(Instant, Decimal)> = entries.iter()
        .filter_map(|e| e.sgv.filter(|_| now - Duration::hours(1) <= e.at && e.at <= now).map(|v| (e.at, v))).collect();
    points.sort();
    let Some(&(latest_at, value)) = points.last() else {
        return Status { glucose_mgdl: None, glucose_at: None, fresh: false, status: "unavailable",
                        trend: Trend::default(), target_low_mgdl: None, target_high_mgdl: None };
    };
    let fresh = now - latest_at <= Duration::minutes(15);
    let course = if fresh { trend(&points) } else { None }.unwrap_or_default();
    let target = target_at(now, settings);
    let (low, high) = (target.map(|t| t.low_mgdl), target.map(|t| t.high_mgdl));
    let (low_limit, high_limit) = limits(settings);
    let status = if !fresh { "stale" } else if value < low_limit { "low" } else if value > high_limit { "high" }
        else { match (low, high) {
            (Some(low), Some(high)) if low <= value && value <= high => "in_range",
            (Some(_), Some(_)) => "outside",
            _ => "no_target",
        } };
    Status { glucose_mgdl: Some(value), glucose_at: Some(latest_at), fresh, status, trend: course,
             target_low_mgdl: low, target_high_mgdl: high }
}
