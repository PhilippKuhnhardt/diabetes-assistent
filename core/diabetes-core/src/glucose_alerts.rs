//! Early warning of a low or high soon, daytime only (app/glucose_alerts.py). A rough forecast,
//! not dosing advice.

use crate::calculation::Interpolator;
use crate::context::local_zone;
use crate::model::{Instant, Reading};
use crate::on_board::{effects_at, Found};
use chrono::{Duration, Timelike};
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use serde::Serialize;

pub const LOW_MGDL: Decimal = dec!(70);
pub const HIGH_MGDL: Decimal = dec!(250);
pub const HORIZON_MINUTES: i64 = 60;
pub const STEP_MINUTES: i64 = 5;
pub const MISS_MINUTES: i64 = 15;
pub const FADE_MINUTES: Decimal = dec!(45);
pub const LOW_FADE_MINUTES: Decimal = dec!(25);
pub const LOW_WITHIN_MINUTES: i64 = 45;
pub const HIGH_WITHIN_MINUTES: i64 = 30;
pub const LOW_FALL_MINUTES: i64 = 10;
pub const LOW_FALL_MGDL: Decimal = dec!(5);
pub const HIGH_FROM_BELOW: Decimal = dec!(50);
pub const DAY_FROM: u32 = 6;
pub const DAY_TO: u32 = 23;
pub const FRESH_MINUTES: i64 = 10;
pub const LOW_PAUSE_MINUTES: i64 = 15;
pub const HIGH_PAUSE_MINUTES: i64 = 30;

/// Minutes' worth of the miss rate still added after `minutes`, the rate fading linearly to 0.
fn faded(minutes: Decimal, fade: Decimal) -> Decimal {
    let k = minutes.min(fade);
    k - k * k / (dec!(2) * fade)
}

#[derive(Clone, Debug, Serialize)]
pub struct Point {
    pub at: Instant,
    pub mgdl: Decimal,
}

#[derive(Clone, Debug, Serialize)]
pub struct Forecast {
    pub at: Instant,
    pub glucose_mgdl: Decimal,
    pub miss_per_hour: Decimal,
    pub points: Vec<Point>,
}

/// Forecast every 5 minutes for the next hour from the latest fresh reading, or None.
pub fn forecast(entries: &[Reading], found: &Found, now: Instant, fade: Decimal) -> Option<Forecast> {
    let latest = entries.iter().filter(|e| e.sgv.is_some() && e.at <= now)
        .fold(None::<&Reading>, |best, e| if best.is_none_or(|b| e.at > b.at) { Some(e) } else { best })?;
    let (start, glucose) = (latest.at, latest.sgv.unwrap());
    if now - start > Duration::minutes(FRESH_MINUTES) {
        return None;
    }
    let read = Interpolator::new(entries, 15);
    let miss = Duration::minutes(MISS_MINUTES);
    let at_start = effects_at(found, start, None);
    let rate = match read.value(start - miss) {
        Some(earlier) => {
            let before = effects_at(found, start - miss, Some(start));
            let modelled = -(before.lowering_mgdl - at_start.lowering_mgdl) + (before.raising_mgdl - at_start.raising_mgdl);
            ((glucose - earlier) - modelled) / Decimal::from(MISS_MINUTES)
        }
        None => Decimal::ZERO,
    };
    // Ahead, entries made after the reading (up to now) count too.
    let ahead = effects_at(found, start, Some(now));
    let points = (1..=HORIZON_MINUTES / STEP_MINUTES).map(|step| {
        let minutes = step * STEP_MINUTES;
        let later = effects_at(found, start + Duration::minutes(minutes), Some(now));
        Point { at: start + Duration::minutes(minutes),
                mgdl: glucose - (ahead.lowering_mgdl - later.lowering_mgdl) + (ahead.raising_mgdl - later.raising_mgdl)
                    + rate * faded(Decimal::from(minutes), fade) }
    }).collect();
    Some(Forecast { at: start, glucose_mgdl: glucose, miss_per_hour: rate * dec!(60), points })
}

fn since(times: impl Iterator<Item = Instant>, now: Instant, pause_minutes: i64) -> bool {
    times.into_iter().any(|at| now - Duration::minutes(pause_minutes) < at && at <= now)
}

#[derive(Clone, Debug, Serialize)]
pub struct Warning {
    pub kind: &'static str,
    pub glucose_mgdl: Decimal,
    pub at: Instant,
    pub mgdl: Decimal,
}

pub struct Limits {
    pub low_pause_minutes: i64,
    pub high_pause_minutes: i64,
    pub low_mgdl: Decimal,
    pub high_mgdl: Decimal,
}

impl Default for Limits {
    fn default() -> Limits {
        Limits { low_pause_minutes: LOW_PAUSE_MINUTES, high_pause_minutes: HIGH_PAUSE_MINUTES, low_mgdl: LOW_MGDL, high_mgdl: HIGH_MGDL }
    }
}

/// The low and high warnings due now.
pub fn warnings(entries: &[Reading], found: &Found, now: Instant, timezone: &str, limits: &Limits) -> Vec<Warning> {
    let hour = local_zone(timezone).local(&now).hour();
    if !(DAY_FROM..DAY_TO).contains(&hour) {
        return Vec::new();
    }
    let (Some(result), Some(low_result)) = (forecast(entries, found, now, FADE_MINUTES), forecast(entries, found, now, LOW_FADE_MINUTES)) else {
        return Vec::new();
    };
    let mut due = Vec::new();
    let glucose = result.glucose_mgdl;
    let before = Interpolator::new(entries, 15).value(result.at - Duration::minutes(LOW_FALL_MINUTES));
    let falling = before.is_none_or(|b| glucose - b <= -LOW_FALL_MGDL);
    let paused = since(found.carbs.iter().map(|row| row.at), now, limits.low_pause_minutes);
    let low_points: Vec<&Point> = low_result.points.iter().filter(|p| p.at <= result.at + Duration::minutes(LOW_WITHIN_MINUTES)).collect();
    if let Some(low) = low_points.iter().find(|p| p.mgdl < limits.low_mgdl) {
        if glucose >= limits.low_mgdl && falling && !paused {
            due.push(Warning { kind: "low", glucose_mgdl: glucose, at: low.at, mgdl: low_points.iter().map(|p| p.mgdl).min().unwrap() });
        }
    }
    let high_points: Vec<&Point> = result.points.iter().filter(|p| p.at <= result.at + Duration::minutes(HIGH_WITHIN_MINUTES)).collect();
    if let Some(high) = high_points.iter().find(|p| p.mgdl > limits.high_mgdl) {
        if limits.high_mgdl - HIGH_FROM_BELOW <= glucose && glucose <= limits.high_mgdl
            && !since(found.doses.iter().map(|row| row.at), now, limits.high_pause_minutes) {
            due.push(Warning { kind: "high", glucose_mgdl: glucose, at: high.at, mgdl: high_points.iter().map(|p| p.mgdl).max().unwrap() });
        }
    }
    due
}
