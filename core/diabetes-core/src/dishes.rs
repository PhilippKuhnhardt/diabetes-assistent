//! How a dish turned out: the meal factor of its main meals against the usual factor of that meal
//! kind at the time (`ratio`; 1.2 = the meal needed 20 % more insulin than usual, so it behaved like
//! 20 % more carbs), the carbs a new entry of it starts with, and the line the catalogue shows.
//!
//! The adapter (app/services/dishes.py) reads the stored day results, the catalogue and the records;
//! names, categories and descriptions are checked there.

use crate::context::tr;
use chrono::{Duration, NaiveDate};
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

fn number(value: Option<&Value>) -> Option<f64> {
    match value? {
        Value::Number(n) => n.as_f64(),
        Value::String(text) => text.trim().parse().ok(),
        _ => None,
    }
}

fn text<'a>(row: &'a Row, key: &str) -> &'a str {
    row.get(key).and_then(Value::as_str).unwrap_or_default()
}

fn excluded(row: &Row) -> bool {
    row.get("excluded").and_then(Value::as_bool).unwrap_or(false)
}

/// statistics.median of floats.
fn median(values: &[f64]) -> Option<f64> {
    let mut sorted = values.to_vec();
    sorted.sort_by(f64::total_cmp);
    let n = sorted.len();
    match n {
        0 => None,
        _ if n % 2 == 1 => Some(sorted[n / 2]),
        _ => Some((sorted[n / 2 - 1] + sorted[n / 2]) / 2.0),
    }
}

/// Python's round(x, digits): the exact value rounded, ties to even.
fn round_to(value: f64, digits: usize) -> f64 {
    format!("{value:.digits$}").parse().unwrap_or(value)
}

/// Python's round(x): to a whole number, ties to even.
fn whole(value: f64) -> i64 {
    value.round_ties_even() as i64
}

/// Carbs a new entry of the dish starts with: the median of its last entries (oldest first).
pub fn typical_carbs(carbs: &[Option<f64>]) -> Option<i64> {
    let positive: Vec<f64> = carbs.iter().flatten().copied().filter(|v| *v > 0.0).collect();
    let recent = &positive[positive.len().saturating_sub(TYPICAL_ENTRIES)..];
    median(recent).map(whole)
}

/// The usual factor when the meal was eaten: median factor of the valid, not excluded main meals of its
/// kind in the REFERENCE_DAYS before its day (the meal itself and its day stay out).
pub fn reference_factor(meals: &[Row], meal: &Row) -> Option<f64> {
    let day = text(meal, "day");
    let first = NaiveDate::parse_from_str(day, "%Y-%m-%d").ok()? - Duration::days(REFERENCE_DAYS);
    let first = first.to_string();
    let values: Vec<f64> = meals.iter()
        .filter(|other| other.get("meal") == meal.get("meal") && !excluded(other))
        .filter(|other| first.as_str() <= text(other, "day") && text(other, "day") < day)
        .filter_map(|other| number(other.get("factor"))).collect();
    if values.len() >= REFERENCE_MIN_MEALS { median(&values) } else { None }
}

/// The main meal with the usual factor of its kind then and its ratio to it.
pub fn with_ratio(meals: &[Row], meal: &Row) -> Row {
    let reference = reference_factor(meals, meal).filter(|r| *r != 0.0);
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
fn spread(values: &[f64]) -> (f64, f64) {
    let mut ordered = values.to_vec();
    ordered.sort_by(f64::total_cmp);
    let n = ordered.len();
    if n < 4 {
        return (ordered[0], ordered[n - 1]);
    }
    let quantile = |share: f64| {
        let position = (n - 1) as f64 * share;
        let low = position as usize;
        let high = (low + 1).min(n - 1);
        ordered[low] + (ordered[high] - ordered[low]) * (position - low as f64)
    };
    (quantile(0.25), quantile(0.75))
}

/// How a dish turned out over its main meals (history rows): how many, the ratio to the usual factor with
/// its spread, and the carbs the typical entry behaved like.
pub fn outcome(rows: &[Row], typical: Option<i64>) -> Value {
    let ratios: Vec<f64> = rows.iter().filter_map(|row| number(row.get("ratio"))).collect();
    let valid = |key: &str| -> Vec<f64> { rows.iter().filter(|row| !excluded(row)).filter_map(|row| number(row.get(key))).collect() };
    let mut result = json!({
        "meals": rows.len(), "valid": ratios.len(), "ratio": null, "ratio_low": null, "ratio_high": null,
        "effective_carbs": null, "effective_low": null, "effective_high": null,
        "delta_mgdl": median(&valid("delta_mgdl")).map(whole), "end_mgdl": median(&valid("g_end_mgdl")).map(whole),
    });
    if ratios.len() >= RATIO_MIN_MEALS {
        let (low, high) = spread(&ratios);
        let middle = median(&ratios).unwrap_or_default();
        result["ratio"] = json!(round_to(middle, 2));
        result["ratio_low"] = json!(round_to(low, 2));
        result["ratio_high"] = json!(round_to(high, 2));
        if let Some(typical) = typical.filter(|t| *t != 0) {
            let typical = typical as f64;
            result["effective_carbs"] = json!(whole(typical * middle));
            result["effective_low"] = json!(whole(typical * low));
            result["effective_high"] = json!(whole(typical * high));
        }
    }
    result
}

/// One line on a dish for the entry form and the catalogue.
pub fn hint(count: usize, typical: Option<i64>, result: Option<&Value>) -> String {
    let mut parts = vec![if count > 0 { tr("{count}× eingetragen", &[("count", count.to_string())]) } else { tr("noch nie eingetragen", &[]) }];
    let typical = typical.filter(|t| *t != 0);
    if let Some(grams) = typical {
        parts.push(tr("meist {grams} g", &[("grams", grams.to_string())]));
    }
    if let Some(ratio) = result.and_then(|r| number(r.get("ratio"))) {
        let result = result.unwrap();
        let percent = whole((ratio - 1.0) * 100.0);
        let need = if percent.abs() < 5 { tr("wie üblich", &[]) }
            else if percent > 0 { tr("{percent} % mehr Insulin als üblich", &[("percent", percent.abs().to_string())]) }
            else { tr("{percent} % weniger Insulin als üblich", &[("percent", percent.abs().to_string())]) };
        let mut line = tr("brauchte {need} ({count} Mahlzeiten)", &[("need", need), ("count", result["valid"].to_string())]);
        if typical.is_some() && percent.abs() >= 5 {
            let shown = |key: &str| match &result[key] { Value::Null => "None".to_string(), other => other.to_string() };
            line += &tr(", verhielt sich wie {low}–{high} g", &[("low", shown("effective_low")), ("high", shown("effective_high"))]);
        }
        parts.push(line);
    }
    parts.join(", ")
}

/// The catalogue's numbers for every dish: entries, typical carbs, outcome, hint and its latest main meals
/// (newest first), in the order of `dish_ids`. `records` are the dish records oldest first (dish_id, carbs_g, at).
pub fn catalogue(meals: &[Row], dish_ids: &[i64], records: &[Row]) -> Value {
    let rows: Vec<Value> = dish_ids.iter().map(|id| {
        let own: Vec<&Row> = records.iter().filter(|record| record.get("dish_id").and_then(Value::as_i64) == Some(*id)).collect();
        let typical = typical_carbs(&own.iter().map(|record| number(record.get("carbs_g"))).collect::<Vec<_>>());
        let history = history(meals, *id);
        let outcome = outcome(&history, typical);
        let shown: Vec<Row> = history[history.len().saturating_sub(HISTORY_ROWS)..].iter().rev()
            .map(|row| { let mut row = row.clone(); row.remove("dish_id"); row }).collect();
        json!({
            "id": id, "count": own.len(), "typical_carbs": typical,
            "last_at": own.last().and_then(|record| record.get("at")).cloned().unwrap_or(Value::Null),
            "hint": hint(own.len(), typical, Some(&outcome)), "outcome": outcome, "history": shown,
        })
    }).collect();
    Value::Array(rows)
}
