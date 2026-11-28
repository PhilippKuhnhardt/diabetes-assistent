//! How a dish turned out: the meal factor of its main meals against the usual factor of that meal
//! kind at the time (`ratio`; 1.2 = the meal needed 20 % more insulin than usual, so it behaved like
//! 20 % more carbs), the carbs a new entry of it starts with, and the line the catalogue shows.
//!
//! The adapter (app/services/dishes.py) reads the stored day results, the catalogue and the records;
//! names, categories and descriptions are checked there.

use crate::context::tr;
use crate::model::decimal_of;
use crate::stats;
use chrono::{Duration, NaiveDate};
use rust_decimal::prelude::ToPrimitive;
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

/// The usual factor of a meal kind: the median of its valid meals in this many days before the meal.
pub const REFERENCE_DAYS: i64 = 30;
pub const REFERENCE_MIN_MEALS: usize = 4;
/// A dish's ratio is named from this many valid main meals on.
pub const RATIO_MIN_MEALS: usize = 3;
/// A new entry's suggested carbs: the median of the dish's last entries.
pub const TYPICAL_ENTRIES: usize = 5;
/// The catalogue shows this many of a dish's latest main meals.
pub const HISTORY_ROWS: usize = 12;

type Row = Map<String, Value>;

fn number(value: Option<&Value>) -> Option<Decimal> {
    value.and_then(decimal_of)
}

fn text<'a>(row: &'a Row, key: &str) -> &'a str {
    row.get(key).and_then(Value::as_str).unwrap_or_default()
}

fn excluded(row: &Row) -> bool {
    row.get("excluded").and_then(Value::as_bool).unwrap_or(false)
}

fn median(values: &[Decimal]) -> Option<Decimal> {
    let mut sorted = values.to_vec();
    sorted.sort();
    stats::median(&sorted)
}

/// To `digits` decimal places, ties to even.
fn round_to(value: Decimal, digits: u32) -> Decimal {
    value.round_dp(digits).normalize()
}

/// To a whole number, ties to even.
fn whole(value: Decimal) -> i64 {
    value.round().to_i64().unwrap_or_default()
}

/// Carbs a new entry of the dish starts with: the median of its last entries (oldest first).
pub fn typical_carbs(carbs: &[Option<Decimal>]) -> Option<i64> {
    let positive: Vec<Decimal> = carbs.iter().flatten().copied().filter(|v| v.is_sign_positive() && !v.is_zero()).collect();
    let recent = &positive[positive.len().saturating_sub(TYPICAL_ENTRIES)..];
    median(recent).map(whole)
}

/// The usual factor when the meal was eaten: median factor of the valid, not excluded main meals of its
/// kind in the REFERENCE_DAYS before its day (the meal itself and its day stay out).
pub fn reference_factor(meals: &[Row], meal: &Row) -> Option<Decimal> {
    let day = text(meal, "day");
    let first = NaiveDate::parse_from_str(day, "%Y-%m-%d").ok()? - Duration::days(REFERENCE_DAYS);
    let first = first.to_string();
    let values: Vec<Decimal> = meals.iter()
        .filter(|other| other.get("meal") == meal.get("meal") && !excluded(other))
        .filter(|other| first.as_str() <= text(other, "day") && text(other, "day") < day)
        .filter_map(|other| number(other.get("factor"))).collect();
    if values.len() >= REFERENCE_MIN_MEALS { median(&values) } else { None }
}

/// The main meal with the usual factor of its kind then and its ratio to it.
pub fn with_ratio(meals: &[Row], meal: &Row) -> Row {
    let reference = reference_factor(meals, meal).filter(|r| !r.is_zero());
    let factor = number(meal.get("factor")).filter(|_| !excluded(meal));
    let mut row = meal.clone();
    row.insert("reference_factor".into(), json!(reference.map(|r| round_to(r, 3))));
    row.insert("ratio".into(), json!(factor.zip(reference).map(|(f, r)| round_to(f / r, 3))));
    row
}

/// The dish's main meals, oldest first, each with the usual factor then and its ratio to it.
pub fn history(meals: &[Row], dish_id: i64) -> Vec<Row> {
    let mut own: Vec<Row> = meals.iter().filter(|meal| meal.get("dish_id").and_then(Value::as_i64) == Some(dish_id))
        .map(|meal| with_ratio(meals, meal)).collect();
    own.sort_by(|a, b| text(a, "at").cmp(text(b, "at")));
    own
}

/// The middle half of the values (quartiles) from 4 values on, else their range.
fn spread(values: &[Decimal]) -> (Decimal, Decimal) {
    let mut ordered = values.to_vec();
    ordered.sort();
    let n = ordered.len();
    if n < 4 {
        return (ordered[0], ordered[n - 1]);
    }
    (stats::quartile(&ordered, dec!(0.25)), stats::quartile(&ordered, dec!(0.75)))
}

/// How a dish turned out over its main meals.
#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(default)]
pub struct Outcome {
    pub meals: usize,
    pub valid: usize,
    #[serde(deserialize_with = "crate::model::opt_amount")]
    pub ratio: Option<Decimal>,
    #[serde(deserialize_with = "crate::model::opt_amount")]
    pub ratio_low: Option<Decimal>,
    #[serde(deserialize_with = "crate::model::opt_amount")]
    pub ratio_high: Option<Decimal>,
    pub effective_carbs: Option<i64>,
    pub effective_low: Option<i64>,
    pub effective_high: Option<i64>,
    pub delta_mgdl: Option<i64>,
    pub end_mgdl: Option<i64>,
}

/// How a dish turned out over its main meals (history rows): how many, the ratio to the usual factor with
/// its spread, and the carbs the typical entry behaved like.
pub fn outcome(rows: &[Row], typical: Option<i64>) -> Outcome {
    let ratios: Vec<Decimal> = rows.iter().filter_map(|row| number(row.get("ratio"))).collect();
    let valid = |key: &str| -> Vec<Decimal> { rows.iter().filter(|row| !excluded(row)).filter_map(|row| number(row.get(key))).collect() };
    let mut result = Outcome {
        meals: rows.len(), valid: ratios.len(),
        delta_mgdl: median(&valid("delta_mgdl")).map(whole), end_mgdl: median(&valid("g_end_mgdl")).map(whole),
        ..Outcome::default()
    };
    if ratios.len() >= RATIO_MIN_MEALS {
        let (low, high) = spread(&ratios);
        let middle = median(&ratios).unwrap_or_default();
        result.ratio = Some(round_to(middle, 2));
        result.ratio_low = Some(round_to(low, 2));
        result.ratio_high = Some(round_to(high, 2));
        if let Some(typical) = typical.filter(|t| *t != 0) {
            let typical = Decimal::from(typical);
            result.effective_carbs = Some(whole(typical * middle));
            result.effective_low = Some(whole(typical * low));
            result.effective_high = Some(whole(typical * high));
        }
    }
    result
}

/// One line on a dish for the entry form and the catalogue.
pub fn hint(count: usize, typical: Option<i64>, result: Option<&Outcome>) -> String {
    let mut parts = vec![if count > 0 { tr("{count}× eingetragen", &[("count", count.to_string())]) } else { tr("noch nie eingetragen", &[]) }];
    let typical = typical.filter(|t| *t != 0);
    if let Some(grams) = typical {
        parts.push(tr("meist {grams} g", &[("grams", grams.to_string())]));
    }
    if let Some((result, ratio)) = result.and_then(|r| r.ratio.map(|ratio| (r, ratio))) {
        let percent = whole((ratio - Decimal::ONE) * dec!(100));
        let need = if percent.abs() < 5 { tr("wie üblich", &[]) }
            else if percent > 0 { tr("{percent} % mehr Insulin als üblich", &[("percent", percent.abs().to_string())]) }
            else { tr("{percent} % weniger Insulin als üblich", &[("percent", percent.abs().to_string())]) };
        let mut line = tr("brauchte {need} ({count} Mahlzeiten)", &[("need", need), ("count", result.valid.to_string())]);
        if let Some((low, high)) = result.effective_low.zip(result.effective_high).filter(|_| typical.is_some() && percent.abs() >= 5) {
            line += &tr(", verhielt sich wie {low}–{high} g", &[("low", low.to_string()), ("high", high.to_string())]);
        }
        parts.push(line);
    }
    parts.join(", ")
}

/// A dish in the catalogue.
#[derive(Clone, Debug, Serialize)]
pub struct CatalogueRow {
    pub id: i64,
    pub count: usize,
    pub typical_carbs: Option<i64>,
    /// The latest record's time as stored.
    pub last_at: Value,
    pub hint: String,
    pub outcome: Outcome,
    /// Its latest main meals (rows from the adapter with the ratio added).
    pub history: Vec<Row>,
}

/// The catalogue's numbers for every dish: entries, typical carbs, outcome, hint and its latest main meals
/// (newest first), in the order of `dish_ids`. `records` are the dish records oldest first (dish_id, carbs_g, at).
pub fn catalogue(meals: &[Row], dish_ids: &[i64], records: &[Row]) -> Vec<CatalogueRow> {
    dish_ids.iter().map(|id| {
        let own: Vec<&Row> = records.iter().filter(|record| record.get("dish_id").and_then(Value::as_i64) == Some(*id)).collect();
        let typical = typical_carbs(&own.iter().map(|record| number(record.get("carbs_g"))).collect::<Vec<_>>());
        let history = history(meals, *id);
        let outcome = outcome(&history, typical);
        CatalogueRow {
            id: *id,
            count: own.len(),
            typical_carbs: typical,
            last_at: own.last().and_then(|record| record.get("at")).cloned().unwrap_or(Value::Null),
            hint: hint(own.len(), typical, Some(&outcome)),
            outcome,
            history: history[history.len().saturating_sub(HISTORY_ROWS)..].iter().rev()
                .map(|row| { let mut row = row.clone(); row.remove("dish_id"); row }).collect(),
        }
    }).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(value: Value) -> Row {
        value.as_object().cloned().unwrap()
    }

    #[test]
    fn a_dish_that_needed_more_insulin_behaved_like_more_carbs() {
        let rows: Vec<Row> = ["1.1", "1.2", "1.3"].iter().map(|ratio| row(json!({"ratio": ratio, "delta_mgdl": 10, "g_end_mgdl": 120}))).collect();
        let result = outcome(&rows, Some(50));
        assert_eq!((result.ratio, result.ratio_low, result.ratio_high), (Some(dec!(1.2)), Some(dec!(1.1)), Some(dec!(1.3))));
        assert_eq!((result.effective_carbs, result.effective_low, result.effective_high), (Some(60), Some(55), Some(65)));
        assert_eq!(hint(3, Some(50), Some(&result)),
                   "3× eingetragen, meist 50 g, brauchte 20 % mehr Insulin als üblich (3 Mahlzeiten), verhielt sich wie 55–65 g");
    }

    #[test]
    fn typical_carbs_are_the_median_of_the_last_five_positive_entries() {
        let carbs = [Some(dec!(200)), Some(dec!(40)), None, Some(dec!(0)), Some(dec!(50)), Some(dec!(60)), Some(dec!(45)), Some(dec!(55))];
        assert_eq!(typical_carbs(&carbs), Some(50));
        assert_eq!(typical_carbs(&[Some(dec!(2.5))]), Some(2));
        assert_eq!(typical_carbs(&[]), None);
    }
}
