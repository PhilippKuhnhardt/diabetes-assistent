//! Input limits every way into the app shares (entry form, entry card, sport-session and basal endpoints,
//! MCP): one place for each range, so the form, the card and the API can never disagree.

use crate::context::t;
use crate::model::decimal_of;
use crate::{CoreError, Res};
use rust_decimal::prelude::ToPrimitive;
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use serde_json::Value;
use std::ops::RangeInclusive;

/// Carbohydrates of one entry in grams.
pub const MAX_CARBS_G: Decimal = dec!(500);
/// Rapid or basal insulin of one entry in IE.
pub const MAX_UNITS: Decimal = dec!(100);
/// Basal is injected in these steps.
pub const BASAL_STEP_UNITS: Decimal = dec!(0.5);
/// Length of a sport session in minutes.
pub const SPORT_DURATION_MINUTES: RangeInclusive<i64> = 1..=1440;
/// A session's own effect duration in minutes.
pub const SPORT_EFFECT_MINUTES: RangeInclusive<i64> = 30..=1440;
/// A session's own glucose effect, either way, in mg/dL.
pub const SPORT_EFFECT_MAX_MGDL: Decimal = dec!(500);
/// Blood glucose measured by hand, in mg/dL.
pub const FINGER_MGDL: RangeInclusive<Decimal> = dec!(20)..=dec!(600);

fn invalid(message: String) -> CoreError {
    CoreError::Value(message)
}

/// A basal dose: more than 0, at most MAX_UNITS, in BASAL_STEP_UNITS.
pub fn basal_units(value: &Value) -> Res<Decimal> {
    match decimal_of(value) {
        Some(units) if Decimal::ZERO < units && units <= MAX_UNITS && (units % BASAL_STEP_UNITS).is_zero() => Ok(units.normalize()),
        _ => Err(invalid(t("Basal muss zwischen 0,5 und 100 IE in 0,5er-Schritten liegen"))),
    }
}

/// A sport session's own effect duration: null resets it to its kind's, else whole minutes in range.
pub fn sport_effect_minutes(value: &Value) -> Res<Option<i64>> {
    if value.is_null() {
        return Ok(None);
    }
    let minutes = decimal_of(value).and_then(|m| m.trunc().to_i64()).ok_or_else(|| invalid(t("Ungültige Wirkdauer")))?;
    if !SPORT_EFFECT_MINUTES.contains(&minutes) {
        return Err(invalid(t("Sport-Wirkdauer muss zwischen 30 und 1440 Minuten liegen")));
    }
    Ok(Some(minutes))
}

/// A sport session's own glucose effect as stored (a drop in mg/dL; missing is 0).
pub fn sport_effect_mgdl(value: &Value) -> Res<Decimal> {
    let effect = if value.is_null() { Some(Decimal::ZERO) } else { decimal_of(value) };
    match effect {
        Some(effect) if effect.abs() <= SPORT_EFFECT_MAX_MGDL => Ok(effect.normalize()),
        _ => Err(invalid(t("Ungültiger Sporteffekt"))),
    }
}

/// Blood glucose measured by hand.
pub fn finger_mgdl(value: &Value) -> Res<Decimal> {
    match decimal_of(value) {
        Some(mgdl) if FINGER_MGDL.contains(&mgdl) => Ok(mgdl),
        _ => Err(invalid(t("Blutzucker muss zwischen 20 und 600 mg/dL liegen"))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn basal_takes_half_units_up_to_the_maximum() {
        assert_eq!(basal_units(&json!("12.5")).unwrap(), dec!(12.5));
        assert_eq!(basal_units(&json!(100)).unwrap(), dec!(100));
        for refused in [json!(0), json!("12.3"), json!(100.5), json!("x"), json!(null)] {
            assert!(basal_units(&refused).is_err(), "{refused}");
        }
    }

    #[test]
    fn sport_effects_stay_in_range() {
        assert_eq!(sport_effect_minutes(&json!(null)).unwrap(), None);
        assert_eq!(sport_effect_minutes(&json!("90")).unwrap(), Some(90));
        assert!(sport_effect_minutes(&json!(20)).is_err());
        assert!(sport_effect_minutes(&json!("soon")).is_err());
        assert_eq!(sport_effect_mgdl(&json!(-40)).unwrap(), dec!(-40));
        assert_eq!(sport_effect_mgdl(&json!(null)).unwrap(), Decimal::ZERO);
        assert!(sport_effect_mgdl(&json!(501)).is_err());
    }

    #[test]
    fn finger_glucose_between_20_and_600() {
        assert_eq!(finger_mgdl(&json!("85")).unwrap(), dec!(85));
        assert!(finger_mgdl(&json!(19)).is_err());
        assert!(finger_mgdl(&json!(601)).is_err());
    }
}
