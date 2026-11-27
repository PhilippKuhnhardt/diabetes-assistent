//! Calculated meal surcharge from the estimated fat and protein (app/fpe.py).

use crate::model::{decimal_of, Settings};
use rust_decimal::prelude::ToPrimitive;
use rust_decimal::{Decimal, RoundingStrategy};
use rust_decimal_macros::dec;
use serde::Serialize;
use serde_json::Value;

pub const PER_FPE_MGDL: Decimal = dec!(20);
pub const MAX_MGDL: Decimal = dec!(200);
pub const BASE_MINUTES: Decimal = dec!(180);
pub const MINUTES_PER_FPE: Decimal = dec!(30);
pub const MAX_MINUTES: Decimal = dec!(480);
pub const LEVELS: [(&str, Decimal); 3] = [("none", dec!(0)), ("normal", dec!(4)), ("fat", dec!(10))];
pub const DEFAULT_LEVEL: &str = "normal";
pub const SPLIT_LATER_MIN_UNITS: Decimal = dec!(2);

/// FPE of a meal: (9 × fat + 4 × protein) / 100; None without a valid estimate.
pub fn units(fat_g: &Value, protein_g: &Value) -> Option<Decimal> {
    let (fat, protein) = (decimal_of(fat_g)?, decimal_of(protein_g)?);
    if fat < Decimal::ZERO || protein < Decimal::ZERO || fat + protein > dec!(1000) {
        return None;
    }
    Some((dec!(9) * fat + dec!(4) * protein) / dec!(100))
}

pub fn surcharge_mgdl(fpe: Decimal, per_fpe: Decimal) -> Decimal {
    (per_fpe * fpe).min(MAX_MGDL)
}

/// How long the surcharge acts, linearly from the meal start.
pub fn minutes(fpe: Decimal) -> i64 {
    (BASE_MINUTES + MINUTES_PER_FPE * fpe).min(MAX_MINUTES).trunc().to_i64().unwrap_or(0)
}

#[derive(Clone, Debug, Serialize)]
pub struct Estimate {
    pub fpe: Decimal,
    pub mgdl: Decimal,
    pub minutes: i64,
    pub level: Option<&'static str>,
}

/// The calculated surcharge for fat_g and protein_g, else for fpe_level; None without either or
/// without the surcharge method.
pub fn estimate(values: &Value, settings: &Settings) -> Option<Estimate> {
    if !settings.surcharge_method() {
        return None;
    }
    let null = Value::Null;
    let mut fpe = units(values.get("fat_g").unwrap_or(&null), values.get("protein_g").unwrap_or(&null));
    let mut level = None;
    if fpe.is_none() {
        if let Some((name, value)) = LEVELS.iter().find(|(name, _)| values.get("fpe_level").and_then(Value::as_str) == Some(*name)) {
            level = Some(*name);
            fpe = Some(*value);
        }
    }
    let fpe = fpe?;
    Some(Estimate {
        fpe, mgdl: surcharge_mgdl(fpe, settings.fpe_surcharge_mgdl).round_dp_with_strategy(0, RoundingStrategy::MidpointNearestEven),
        minutes: minutes(fpe), level,
    })
}

/// Whether the dose uses the calculated surcharge: always for a level, for grams when picked.
pub fn chosen(found: Option<&Estimate>, payload: &Value) -> bool {
    found.is_some_and(|f| f.level.is_some() || payload.get("surcharge_mode").and_then(Value::as_str) == Some("fpe"))
}

/// The level a saved entry shows: from its stored surcharge's duration, else from the fat mark.
pub fn level_of(surcharge_minutes: Option<i64>, fat_mark: bool) -> &'static str {
    if let Some(stored) = surcharge_minutes {
        if let Some((name, _)) = LEVELS.iter().find(|(_, value)| minutes(*value) == stored) {
            return name;
        }
    }
    if fat_mark { "fat" } else { DEFAULT_LEVEL }
}
