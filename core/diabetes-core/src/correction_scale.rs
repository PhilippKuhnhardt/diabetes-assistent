//! A correction without food as a scale by how fast the last main meal rose, and the dose slider
//! (app/correction_scale.py).

use crate::calculation::Interpolator;
use crate::dosing::half_units;
use crate::model::{Instant, Reading, Treatment};
use chrono::Duration;
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use serde::Serialize;

pub const FAST_FACTOR: Decimal = dec!(0.6);
pub const SLOW_FACTOR: Decimal = dec!(1.4);
pub const FAST_MIN_GAP: Decimal = dec!(1);
pub const STEEP_MGDL_PER_MIN: Decimal = dec!(2.0);
pub const FLAT_MGDL_PER_MIN: Decimal = dec!(1.0);
pub const RISE_MINUTES: i64 = 60;
pub const SLOPE_MINUTES: i64 = 15;
pub const STEP_MINUTES: i64 = 5;
pub const TO_MINUTES: i64 = 450;

#[derive(Clone, Debug, Serialize, serde::Deserialize)]
pub struct Rise {
    pub meal_at: Instant,
    #[serde(default, deserialize_with = "crate::model::opt_amount")]
    pub slope_mgdl_per_min: Option<Decimal>,
    #[serde(default)]
    pub tempo: Option<String>,
}

/// The latest main meal 1 h to 7.5 h ago and how steeply glucose rose in its first hour.
pub fn meal_rise(entries: &[Reading], treatments: &[Treatment], now: Instant) -> Option<Rise> {
    let meal = treatments.iter().filter(|item| item.is_main_meal() && item.at <= now)
        .fold(None::<&Treatment>, |best, item| if best.is_none_or(|b| item.at > b.at) { Some(item) } else { best })?;
    let minutes = (now - meal.at).num_microseconds().unwrap_or(0) as f64 / 60e6;
    if !(RISE_MINUTES as f64 <= minutes && minutes <= TO_MINUTES as f64) {
        return None;
    }
    let past: Vec<Reading> = entries.iter().filter(|e| e.at <= now).cloned().collect();
    let read = Interpolator::new(&past, 15);
    let steps = (RISE_MINUTES / STEP_MINUTES) as usize;
    let values: Vec<Option<Decimal>> = (0..steps + 2)
        .map(|index| read.value(meal.at + Duration::minutes(STEP_MINUTES * index as i64))).collect();
    let smooth: Vec<Option<Decimal>> = (0..=steps).map(|index| {
        let near: Vec<Decimal> = values[index.saturating_sub(1)..(index + 2).min(values.len())].iter().flatten().copied().collect();
        (values[index].is_some() && !near.is_empty()).then(|| near.iter().sum::<Decimal>() / Decimal::from(near.len()))
    }).collect();
    let back = (SLOPE_MINUTES / STEP_MINUTES) as usize;
    let slopes: Vec<Decimal> = (back..=steps).filter_map(|index| match (smooth[index], smooth[index - back]) {
        (Some(now), Some(before)) => Some((now - before) / Decimal::from(SLOPE_MINUTES)),
        _ => None,
    }).collect();
    let slope = if slopes.len() >= 3 { slopes.iter().max().copied() } else { None };
    let tempo = match slope {
        // Without enough CGM the "Schnell" mark of the meal stands in.
        None => meal.fast.then(|| "fast".to_string()),
        Some(slope) if slope > STEEP_MGDL_PER_MIN => Some("fast".into()),
        Some(slope) if slope < FLAT_MGDL_PER_MIN => Some("slow".into()),
        Some(_) => Some("normal".into()),
    };
    Some(Rise { meal_at: meal.at, slope_mgdl_per_min: slope, tempo })
}

#[derive(Clone, Debug, Serialize)]
pub struct Scale {
    pub meal_at: Instant,
    pub tempo: Option<String>,
    pub slope_mgdl_per_min: Option<Decimal>,
    pub fast_entry_units: Decimal,
    pub fast_expected_glucose_mgdl: Decimal,
    pub slow_entry_units: Decimal,
    pub slow_expected_glucose_mgdl: Decimal,
}

/// The parts of a dose suggestion without carbs the scale reads.
#[derive(Clone, Debug, serde::Deserialize)]
pub struct Normal {
    #[serde(default, deserialize_with = "crate::model::opt_amount")]
    pub forecast_mgdl: Option<Decimal>,
    #[serde(default, deserialize_with = "crate::model::opt_amount")]
    pub correction_units: Option<Decimal>,
    #[serde(default, deserialize_with = "crate::model::opt_amount")]
    pub entry_units: Option<Decimal>,
    #[serde(default, deserialize_with = "crate::model::opt_amount")]
    pub isf_mgdl_per_unit: Option<Decimal>,
}

/// Fast and slow corrections next to the normal one of a suggestion without carbs.
pub fn scale(result: &Normal, rise: Option<&Rise>) -> Option<Scale> {
    let rise = rise?;
    let (start, correction) = (result.forecast_mgdl?, result.correction_units?);
    if correction <= Decimal::ZERO || result.entry_units.unwrap_or(Decimal::ZERO) <= Decimal::ZERO {
        return None;
    }
    let isf = result.isf_mgdl_per_unit?;
    let fast = half_units((correction * FAST_FACTOR).min(correction - FAST_MIN_GAP).max(Decimal::ZERO));
    let slow = half_units((correction * SLOW_FACTOR).max(Decimal::ZERO));
    Some(Scale {
        meal_at: rise.meal_at, tempo: rise.tempo.clone(), slope_mgdl_per_min: rise.slope_mgdl_per_min,
        fast_entry_units: fast, fast_expected_glucose_mgdl: start - fast * isf,
        slow_entry_units: slow, slow_expected_glucose_mgdl: start - slow * isf,
    })
}

pub const MEAL_LOW_FACTOR: Decimal = dec!(0.75);
pub const MEAL_HIGH_FACTOR: Decimal = dec!(1.2);
pub const SLIDER_MIN_SPAN: Decimal = dec!(0.5);

/// Where glucose ended about 4 h later relative to the expected value (10/25/50/75/90 %, mg/dL).
pub fn corridor(tempo: Option<&str>, correction: bool) -> [i64; 5] {
    if !correction {
        return [-89, -44, -8, 33, 79];
    }
    match tempo {
        Some("fast") => [-65, -41, -18, 31, 90],
        Some("normal") => [-72, -31, -11, 31, 64],
        Some("slow") => [-70, -6, 20, 65, 97],
        _ => [-73, -36, -4, 50, 80],
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct DoseRange {
    pub min_units: Decimal,
    pub max_units: Decimal,
    pub recommended_units: Decimal,
    pub normal_units: Decimal,
    pub tempo: Option<String>,
    pub corridor_mgdl: Vec<i64>,
}

/// Slider ends and the recommended dose around the dose help's normal dose (0.5 IE steps).
pub fn dose_range(normal: Decimal, correction: bool, tempo: Option<&str>) -> DoseRange {
    let (low_factor, high_factor) = if correction { (FAST_FACTOR, SLOW_FACTOR) } else { (MEAL_LOW_FACTOR, MEAL_HIGH_FACTOR) };
    let low = half_units((normal * low_factor).min(normal - FAST_MIN_GAP).max(Decimal::ZERO));
    let high = half_units(normal * high_factor).max(normal + SLIDER_MIN_SPAN);
    let recommended = if correction { match tempo { Some("fast") => low, Some("slow") => high, _ => normal } } else { normal };
    DoseRange { min_units: low, max_units: high, recommended_units: recommended, normal_units: normal,
                tempo: if correction { tempo.map(String::from) } else { None }, corridor_mgdl: corridor(tempo, correction).to_vec() }
}
