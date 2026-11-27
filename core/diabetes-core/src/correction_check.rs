//! The "BZ prüfen" reminder (app/correction_check.py): high glucose hours after a main meal that
//! what still acts will not bring down.

use crate::calculation::Interpolator;
use crate::model::{Instant, Reading, Treatment};
use crate::on_board::{effects_at, Found};
use crate::split::open_plan;
use chrono::Duration;
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use serde::Serialize;

pub const FROM_MINUTES: f64 = 90.0;
pub const TO_MINUTES: f64 = 450.0;
pub const STRICT_UNTIL_MINUTES: f64 = 120.0;
pub const MIN_MGDL: Decimal = dec!(160);
pub const END_MGDL: Decimal = dec!(160);
pub const EARLY_MGDL: Decimal = dec!(180);
pub const EARLY_RISE_MGDL: Decimal = dec!(10);
pub const EARLY_HIGH_MGDL: Decimal = dec!(200);
pub const RISE_MINUTES: i64 = 30;
pub const NO_INSULIN_MINUTES: i64 = 90;
pub const REPEAT_MINUTES: i64 = 90;
pub const FRESH_MINUTES: i64 = 10;

#[derive(Clone, Debug, Serialize)]
pub struct Check {
    pub meal_at: Instant,
    pub glucose_mgdl: Decimal,
    pub rise_mgdl: Decimal,
    pub end_mgdl: Decimal,
}

/// The reminder's numbers when it is due now, else None.
pub fn due(entries: &[Reading], found: &Found, treatments: &[Treatment], now: Instant) -> Option<Check> {
    let meal_at = treatments.iter().filter(|item| item.is_main_meal() && item.at <= now).map(|item| item.at).max()?;
    let minutes = (now - meal_at).num_microseconds().unwrap_or(0) as f64 / 60e6;
    if !(FROM_MINUTES..=TO_MINUTES).contains(&minutes) || open_plan(treatments, now).is_some() {
        return None;
    }
    if treatments.iter().any(|item| now - Duration::minutes(NO_INSULIN_MINUTES) < item.at && item.at <= now && item.has_rapid_dose()) {
        return None;
    }
    let latest = entries.iter().filter(|e| e.sgv.is_some() && e.at <= now)
        .fold(None::<&Reading>, |best, e| if best.is_none_or(|b| e.at > b.at) { Some(e) } else { best })?;
    if now - latest.at > Duration::minutes(FRESH_MINUTES) {
        return None;
    }
    let glucose = latest.sgv.unwrap();
    let before = Interpolator::new(entries, 15).value(latest.at - Duration::minutes(RISE_MINUTES))?;
    let rise = glucose - before;
    if minutes < STRICT_UNTIL_MINUTES {
        if glucose < EARLY_HIGH_MGDL && (glucose < EARLY_MGDL || rise < EARLY_RISE_MGDL) {
            return None;
        }
    } else if glucose < MIN_MGDL {
        return None;
    }
    let acting = effects_at(found, latest.at, Some(now));
    let end = glucose - acting.lowering_mgdl + acting.raising_mgdl;
    if end < END_MGDL {
        return None;
    }
    Some(Check { meal_at, glucose_mgdl: glucose, rise_mgdl: rise, end_mgdl: end })
}
