//! The data the core reads: CGM readings, treatments (normalized Nightscout records) and settings.
//!
//! The JSON shape is the server's (app/episodes.py normalize_*, the settings of app/store/config.py),
//! so a snapshot from the server or the phone's own store deserializes directly. Numbers may come
//! as JSON numbers or strings; a missing or empty amount is 0, as in the reference (`value or 0`).

use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use std::str::FromStr;

pub type Instant = DateTime<Utc>;

/// Parse a JSON number or numeric string as Decimal (scientific notation too).
pub fn decimal_of(value: &Value) -> Option<Decimal> {
    let text = match value {
        Value::Number(number) => number.to_string(),
        Value::String(text) => text.trim().to_string(),
        Value::Bool(flag) => return Some(Decimal::from(*flag as i64)),
        _ => return None,
    };
    if text.is_empty() {
        return None;
    }
    Decimal::from_str(&text).ok().or_else(|| Decimal::from_scientific(&text).ok())
        .or_else(|| text.parse::<f64>().ok().and_then(|v| Decimal::try_from(v).ok()))
}

/// Amount that defaults to 0 when missing, null, empty or not a number (`item.get(k) or 0`).
pub fn amount<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Decimal, D::Error> {
    let value = Value::deserialize(deserializer)?;
    Ok(decimal_of(&value).unwrap_or(Decimal::ZERO))
}

pub fn opt_amount<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Option<Decimal>, D::Error> {
    let value = Value::deserialize(deserializer)?;
    Ok(decimal_of(&value))
}

pub fn int<'de, D: Deserializer<'de>>(deserializer: D) -> Result<i64, D::Error> {
    let value = Value::deserialize(deserializer)?;
    decimal_of(&value).map(|v| v.trunc().try_into().unwrap_or(0))
        .ok_or_else(|| serde::de::Error::custom(format!("expected an integer, got {value}")))
}

pub fn opt_int<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Option<i64>, D::Error> {
    let value = Value::deserialize(deserializer)?;
    Ok(decimal_of(&value).map(|v| v.trunc().try_into().unwrap_or(0)))
}

/// Truthiness of a JSON value (`bool(item.get(k))`).
pub fn truthy<'de, D: Deserializer<'de>>(deserializer: D) -> Result<bool, D::Error> {
    let value = Value::deserialize(deserializer)?;
    Ok(match value {
        Value::Null => false,
        Value::Bool(flag) => flag,
        Value::Number(number) => number.to_string().parse::<f64>().map(|v| v != 0.0).unwrap_or(true),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(items) => !items.is_empty(),
    })
}

/// Text that defaults to "" for null.
pub fn text<'de, D: Deserializer<'de>>(deserializer: D) -> Result<String, D::Error> {
    let value = Value::deserialize(deserializer)?;
    Ok(match value {
        Value::Null => String::new(),
        Value::String(text) => text,
        other => other.to_string(),
    })
}

/// A non-empty optional text (`item.get(k)` used as truthy).
pub fn opt_text<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Option<String>, D::Error> {
    let value = Value::deserialize(deserializer)?;
    Ok(match value {
        Value::Null => None,
        Value::String(text) if text.is_empty() => None,
        Value::String(text) => Some(text),
        other => Some(other.to_string()),
    })
}

/// A CGM reading.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Reading {
    #[serde(default, deserialize_with = "text")]
    pub id: String,
    pub at: Instant,
    #[serde(default, deserialize_with = "opt_amount")]
    pub sgv: Option<Decimal>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
pub struct SplitPlan {
    #[serde(deserialize_with = "amount")]
    pub units: Decimal,
    #[serde(deserialize_with = "int")]
    pub delay_minutes: i64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
pub struct SurchargePlan {
    #[serde(deserialize_with = "amount")]
    pub mgdl: Decimal,
    #[serde(deserialize_with = "int")]
    pub minutes: i64,
}

/// A normalized treatment (app/episodes.py normalize_treatments).
#[derive(Clone, Debug, Deserialize, Serialize, Default)]
pub struct Treatment {
    #[serde(default, deserialize_with = "text")]
    pub id: String,
    pub at: Instant,
    #[serde(rename = "eventType", default, deserialize_with = "text")]
    pub event_type: String,
    #[serde(default, deserialize_with = "amount")]
    pub carbs: Decimal,
    #[serde(default, deserialize_with = "amount")]
    pub insulin: Decimal,
    #[serde(default, deserialize_with = "amount")]
    pub basal_units: Decimal,
    #[serde(default, deserialize_with = "amount")]
    pub duration: Decimal,
    #[serde(default, deserialize_with = "text")]
    pub notes: String,
    #[serde(default, deserialize_with = "opt_text")]
    pub sport_type: Option<String>,
    #[serde(default, deserialize_with = "opt_text")]
    pub carbs_certainty: Option<String>,
    #[serde(default, deserialize_with = "truthy")]
    pub fat_protein: bool,
    #[serde(default, deserialize_with = "truthy")]
    pub alcohol: bool,
    #[serde(default, deserialize_with = "truthy")]
    pub fast: bool,
    #[serde(default)]
    pub split: Option<SplitPlan>,
    #[serde(default)]
    pub surcharge: Option<SurchargePlan>,
    #[serde(default, deserialize_with = "opt_amount")]
    pub finger_mgdl: Option<Decimal>,
    #[serde(default, deserialize_with = "opt_text")]
    pub event_id: Option<String>,
    #[serde(default, deserialize_with = "opt_text")]
    pub episode_id: Option<String>,
    #[serde(default, deserialize_with = "opt_text")]
    pub meal: Option<String>,
    #[serde(default, deserialize_with = "opt_text")]
    pub role: Option<String>,
}

impl Treatment {
    pub fn is_exercise(&self) -> bool {
        self.event_type == "Exercise"
    }

    /// calculation.is_rapid_insulin: a bolus by role or Nightscout event type.
    pub fn is_rapid(&self) -> bool {
        matches!(self.role.as_deref(), Some("meal" | "meal-dose" | "correction"))
            || matches!(self.event_type.as_str(), "Meal Bolus" | "Correction Bolus" | "Snack Bolus" | "Bolus")
    }

    /// on_board.is_main_meal / calculation._is_main_meal.
    pub fn is_main_meal(&self) -> bool {
        (self.role.as_deref() == Some("meal") || self.event_type == "Meal Bolus") && self.carbs > Decimal::ZERO
    }

    pub fn has_rapid_dose(&self) -> bool {
        self.insulin > Decimal::ZERO && self.is_rapid()
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct IsfRow {
    #[serde(deserialize_with = "int")]
    pub start_minute: i64,
    #[serde(default, deserialize_with = "opt_amount")]
    pub mgdl_per_unit: Option<Decimal>,
    #[serde(default, deserialize_with = "opt_amount")]
    pub grams_per_unit: Option<Decimal>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct TargetRow {
    #[serde(deserialize_with = "int")]
    pub start_minute: i64,
    #[serde(deserialize_with = "amount")]
    pub low_mgdl: Decimal,
    #[serde(deserialize_with = "amount")]
    pub high_mgdl: Decimal,
}

fn d(value: i64) -> Decimal {
    Decimal::from(value)
}

macro_rules! default_fns {
    ($($name:ident: $ty:ty = $value:expr;)*) => {$(fn $name() -> $ty { $value })*};
}

default_fns! {
    utc_name: String = "UTC".into();
    window_minutes: i64 = 180;
    breakfast_end: i64 = 660;
    lunch_end: i64 = 1020;
    target_default: Decimal = d(100);
    cautious_default: Decimal = d(140);
    split_min_grams: i64 = 80;
    split_first_percent: i64 = 60;
    split_delay_minutes: i64 = 120;
    split_min_units: Decimal = Decimal::new(65, 1);
    fpe_default: Decimal = d(20);
    percentile_method: String = "percentile".into();
    auto_percentile: Decimal = Decimal::new(375, 1);
    auto_half_life: Decimal = d(7);
    auto_lookback: i64 = 30;
    auto_surcharge: Decimal = d(40);
    auto_fat_surcharge: Decimal = d(120);
}

/// The calculation settings (app/store/config.py) the core reads, with the reference's defaults.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Settings {
    #[serde(default = "utc_name")]
    pub timezone: String,
    #[serde(default)]
    pub isf_schedule: Vec<IsfRow>,
    #[serde(default)]
    pub ic_schedule: Vec<IsfRow>,
    #[serde(default)]
    pub target_schedule: Vec<TargetRow>,
    #[serde(default = "window_minutes", deserialize_with = "int")]
    pub window_minutes: i64,
    #[serde(default = "breakfast_end", deserialize_with = "int")]
    pub breakfast_end_minute: i64,
    #[serde(default = "lunch_end", deserialize_with = "int")]
    pub lunch_end_minute: i64,
    #[serde(default = "target_default", deserialize_with = "amount")]
    pub dose_target_mgdl: Decimal,
    #[serde(default = "cautious_default", deserialize_with = "amount")]
    pub dose_cautious_target_mgdl: Decimal,
    #[serde(default, deserialize_with = "opt_amount")]
    pub low_limit_mgdl: Option<Decimal>,
    #[serde(default, deserialize_with = "opt_amount")]
    pub high_limit_mgdl: Option<Decimal>,
    #[serde(default, deserialize_with = "opt_text")]
    pub glucose_unit: Option<String>,
    #[serde(default)]
    pub sport_effects_mgdl: BTreeMap<String, Value>,
    #[serde(default)]
    pub sport_durations_minutes: BTreeMap<String, Value>,
    #[serde(default = "split_min_grams", deserialize_with = "int")]
    pub split_min_grams: i64,
    #[serde(default = "split_first_percent", deserialize_with = "int")]
    pub split_first_percent: i64,
    #[serde(default = "split_delay_minutes", deserialize_with = "int")]
    pub split_delay_minutes: i64,
    #[serde(default = "split_min_units", deserialize_with = "amount")]
    pub split_min_units: Decimal,
    #[serde(default = "fpe_default", deserialize_with = "amount")]
    pub fpe_surcharge_mgdl: Decimal,
    #[serde(default = "percentile_method")]
    pub auto_method: String,
    #[serde(default = "auto_percentile", deserialize_with = "amount")]
    pub auto_percentile: Decimal,
    #[serde(default = "auto_half_life", deserialize_with = "amount")]
    pub auto_half_life_days: Decimal,
    #[serde(default = "auto_lookback", deserialize_with = "int")]
    pub auto_lookback_days: i64,
    #[serde(default = "auto_surcharge", deserialize_with = "amount")]
    pub auto_surcharge_mgdl: Decimal,
    #[serde(default = "auto_fat_surcharge", deserialize_with = "amount")]
    pub auto_fat_surcharge_mgdl: Decimal,
}

impl Settings {
    pub fn surcharge_method(&self) -> bool {
        self.auto_method == "surcharge"
    }
}
