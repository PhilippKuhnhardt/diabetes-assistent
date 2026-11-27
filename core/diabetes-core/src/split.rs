//! Split bolus for large main meals: a share now, the rest later (app/split.py).

use crate::dosing::half_units;
use crate::glucose::current_status;
use crate::model::{Instant, Reading, Settings, Treatment};
use chrono::Duration;
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use serde::Serialize;

pub const OPEN_AFTER_DUE_HOURS: i64 = 3;
pub const FIRST_PART_MINUTES: i64 = 15;
pub const EARLY_FROM_MINUTES: i64 = 60;
pub const EARLY_BEFORE_DUE_MINUTES: i64 = 15;
pub const EARLY_MGDL: Decimal = dec!(180);
pub const EARLY_RISE_MGDL: Decimal = dec!(15);
pub const WAIT_BELOW_MGDL: Decimal = dec!(110);
pub const WAIT_FALL_MGDL: Decimal = dec!(5);
pub const WAIT_AT_MOST_MINUTES: i64 = 60;

#[derive(Clone, Debug, Serialize)]
pub struct Offer {
    pub first_percent: i64,
    pub delay_minutes: i64,
}

/// Whether the entry form offers a split: a main meal from split_min_grams, or any fat/protein main meal.
pub fn offer(carbs: Decimal, snack: bool, settings: &Settings, fat: bool) -> Option<Offer> {
    let minimum = settings.split_min_grams;
    if snack || minimum <= 0 || carbs <= Decimal::ZERO || (carbs < Decimal::from(minimum) && !fat) {
        return None;
    }
    Some(Offer { first_percent: settings.split_first_percent, delay_minutes: settings.split_delay_minutes })
}

/// Whether the meal's full dose is large enough to split.
pub fn worth_it(full_units: Option<Decimal>, settings: &Settings) -> bool {
    full_units.is_none_or(|units| units >= settings.split_min_units)
}

/// The injectable first part and the rest; with later_units (calculated surcharge) the rest is that.
pub fn parts(full_units: Decimal, first_percent: i64, later_units: Option<Decimal>) -> (Decimal, Decimal) {
    if let Some(later) = later_units {
        let later = later.max(Decimal::ZERO).min(full_units);
        return (full_units - later, later);
    }
    let now = half_units(full_units * Decimal::from(first_percent) / dec!(100));
    (now, full_units - now)
}

#[derive(Clone, Debug, Serialize)]
pub struct Plan {
    pub meal_at: Instant,
    pub units: Decimal,
    pub due_at: Instant,
}

/// The latest planned second part that has not been injected yet.
pub fn open_plan(treatments: &[Treatment], now: Instant) -> Option<Plan> {
    let meal = treatments.iter().filter(|item| item.split.is_some() && item.is_main_meal() && item.at <= now)
        .fold(None::<&Treatment>, |best, item| if best.is_none_or(|b| item.at > b.at) { Some(item) } else { best })?;
    let split = meal.split.as_ref().unwrap();
    let due = meal.at + Duration::minutes(split.delay_minutes);
    if now > due + Duration::hours(OPEN_AFTER_DUE_HOURS) {
        return None;
    }
    // A dose with carbs of its own covers those carbs only, so the second part stays open.
    let injected = treatments.iter().any(|item| meal.at + Duration::minutes(FIRST_PART_MINUTES) < item.at && item.at <= now
        && item.has_rapid_dose() && item.carbs <= Decimal::ZERO);
    if injected {
        return None;
    }
    Some(Plan { meal_at: meal.at, units: split.units, due_at: due })
}

#[derive(Clone, Debug, Serialize)]
pub struct Timing {
    pub state: &'static str,
    pub latest_at: Instant,
}

/// When the second part fits now: early, due, wait (low or falling at the planned time) or later.
pub fn timing(plan: &Plan, entries: &[Reading], settings: &Settings, now: Instant) -> Timing {
    let status = current_status(entries, settings, now);
    let value = status.glucose_mgdl.filter(|_| status.fresh);
    let rise = value.and(status.trend.trend_delta_mgdl);
    let latest = plan.due_at + Duration::minutes(WAIT_AT_MOST_MINUTES);
    if now < plan.due_at {
        let early = plan.meal_at + Duration::minutes(EARLY_FROM_MINUTES) <= now
            && now < plan.due_at - Duration::minutes(EARLY_BEFORE_DUE_MINUTES)
            && value.is_some_and(|v| v >= EARLY_MGDL) && rise.is_some_and(|r| r >= EARLY_RISE_MGDL);
        return Timing { state: if early { "early" } else { "later" }, latest_at: latest };
    }
    let waiting = now < latest && value.is_some_and(|v| v < WAIT_BELOW_MGDL || rise.is_some_and(|r| r < -WAIT_FALL_MGDL));
    Timing { state: if waiting { "wait" } else { "due" }, latest_at: latest }
}
