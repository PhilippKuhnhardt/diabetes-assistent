//! The analysis page (app/services/summary.py compute_summary with app/services/factors.py meal_points
//! and app/sport_analysis.py): factor groups, the Automatik view (suggestions, backtest matrix, tuning,
//! trade-offs, examples), the misses against the Automatik, the weekly trend, sport, nights and quiet
//! stretches, from the stored day payloads in a snapshot.

use crate::calculation::{schedule_value_at, ScheduleKey};
use crate::context::{t, tr};
use crate::day::iso;
use crate::forecast::{self, MealKey, MealPoint, Variant};
use crate::model::{decimal_of, Instant};
use crate::services::{self, VacationPeriod};
use crate::stats::{median, quartile, strong_outlier_flags};
use crate::{CoreError, Res};
use chrono::{Duration, NaiveDate, NaiveTime};
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use serde::Deserialize;
use serde_json::{json, Map, Value};
use std::collections::{BTreeSet, HashMap, HashSet};
use std::sync::Mutex;

pub const MEALS: [&str; 3] = ["breakfast", "lunch", "dinner"];
pub const TREND_WEEKS: i64 = 8;
pub const DRIFT_WEEKS: usize = 3;
pub const DRIFT_MGDL: Decimal = dec!(20);
pub const PERIODS: [i64; 5] = [1, 3, 7, 14, 30];
/// sport_analysis.suggested_drop: from this many meals, the ratio within 0..2.
pub const SPORT_SUGGEST_MIN_MEALS: usize = 2;
pub const SPORT_SUGGEST_MAX_RATIO: Decimal = dec!(2);

#[derive(Clone, Debug, Deserialize)]
pub struct Place {
    pub id: i64,
    #[serde(default)]
    pub adjust: bool,
}

/// Everything compute_summary reads: the stored day payloads from `today` back over history_days(days).
#[derive(Clone, Debug, Deserialize)]
pub struct Snapshot {
    pub today: NaiveDate,
    pub days: i64,
    pub settings: Value,
    #[serde(default)]
    pub analysis_exclusions: HashSet<String>,
    #[serde(default)]
    pub day_types: Option<HashMap<NaiveDate, String>>,
    #[serde(default)]
    pub vacation_periods: Vec<VacationPeriod>,
    #[serde(default)]
    pub places: Vec<Place>,
    /// Sport sessions with their own effect (marks.sport_session_effects).
    #[serde(default)]
    pub own_sport_sessions: HashSet<String>,
    /// Day payloads by day (ISO date).
    #[serde(default)]
    pub payloads: HashMap<String, Value>,
    /// The meal points over history_days(days) (factors.meal_points), when the caller already has them;
    /// else they are taken from `payloads`.
    #[serde(default)]
    pub points: Option<Vec<MealPoint>>,
}

/// How many days before today the analysis reads (period, 90-day matrix with lookback, stress tile).
pub fn history_days(days: i64) -> i64 {
    ((days - 1).max(forecast::BACKTEST_DAYS - 1) + forecast::LOOKBACKS[2] + 1).max(forecast::RATIO_DAYS + forecast::RATIO_REFERENCE_DAYS)
}

fn s(value: Decimal) -> Value {
    Value::String(value.to_string())
}

fn opt(value: Option<Decimal>) -> Value {
    value.map(s).unwrap_or(Value::Null)
}

fn dec_of(value: Option<&Value>) -> Option<Decimal> {
    value.filter(|v| !v.is_null()).and_then(decimal_of)
}

fn instant(value: &Value) -> Option<Instant> {
    value.as_str().and_then(|text| chrono::DateTime::parse_from_rfc3339(text).ok()).map(|at| at.with_timezone(&chrono::Utc))
}

fn to_value<T: serde::Serialize>(value: &T) -> Value {
    let mut value = serde_json::to_value(value).unwrap_or(Value::Null);
    python_instants(&mut value);
    value
}

fn python_instants(value: &mut Value) {
    match value {
        Value::String(text) if text.len() >= 20 && text.ends_with('Z') && text.as_bytes().get(10) == Some(&b'T') => {
            if let Ok(parsed) = chrono::DateTime::parse_from_rfc3339(text) {
                *text = iso(&parsed.with_timezone(&chrono::Utc));
            }
        }
        Value::Array(items) => items.iter_mut().for_each(python_instants),
        Value::Object(map) => map.values_mut().for_each(python_instants),
        _ => {}
    }
}

/// The Automatik's configured percentile, half-life and lookback (factors.forecast_settings).
pub struct Configured {
    percentile: Decimal,
    half_life: Decimal,
    lookback: i64,
    method: String,
    surcharge_mgdl: Decimal,
    fat_mgdl: Decimal,
    target: Decimal,
    day_types: bool,
}

impl Configured {
    pub fn of(settings: &Value) -> Res<Configured> {
        let number = |key: &str, default: Decimal| dec_of(settings.get(key)).unwrap_or(default);
        Ok(Configured {
            percentile: dec_of(settings.get("auto_percentile")).ok_or_else(|| CoreError::Value("auto_percentile".into()))?,
            half_life: dec_of(settings.get("auto_half_life_days")).ok_or_else(|| CoreError::Value("auto_half_life_days".into()))?,
            lookback: dec_of(settings.get("auto_lookback_days")).and_then(|v| v.trunc().try_into().ok())
                .ok_or_else(|| CoreError::Value("auto_lookback_days".into()))?,
            method: settings.get("auto_method").and_then(Value::as_str).unwrap_or("percentile").to_string(),
            surcharge_mgdl: number("auto_surcharge_mgdl", forecast::SURCHARGE_MGDL),
            fat_mgdl: number("auto_fat_surcharge_mgdl", forecast::FAT_SURCHARGE_MGDL),
            // config.py's default; only the backtest reads it.
            target: number("dose_target_mgdl", dec!(100)),
            day_types: settings.get("day_types").map(|v| v.as_bool().unwrap_or(true)).unwrap_or(false),
        })
    }

    fn surcharge(&self) -> bool {
        self.method == forecast::SURCHARGE
    }

    fn active_surcharge(&self) -> Decimal {
        if self.surcharge() { self.surcharge_mgdl } else { Decimal::ZERO }
    }

    fn keys(&self) -> Vec<MealKey> {
        let mut keys: Vec<MealKey> = if self.day_types {
            vec![("breakfast".into(), Some("work".into())), ("breakfast".into(), Some("free".into()))]
        } else {
            vec![("breakfast".into(), None)]
        };
        keys.push(("lunch".into(), None));
        keys.push(("dinner".into(), None));
        keys
    }
}

/// Valid, non-excluded meal factors from today back over days_back days (factors.meal_points).
/// The misses of every point against the suggestion from the points before it (summary.miss_trend's input).
pub fn point_misses(points: &[MealPoint], configured: &Configured) -> HashMap<String, (Option<Decimal>, Option<Decimal>)> {
    points.iter().map(|point| (point_id(point), suggestion_miss(points, point, configured))).collect()
}

pub fn meal_points(today: NaiveDate, days_back: i64, snapshot: &Snapshot, mode: &str) -> Vec<MealPoint> {
    let own_places: HashSet<i64> = snapshot.places.iter().filter(|place| place.adjust).map(|place| place.id).collect();
    let mut points = Vec::new();
    for (real_day, day) in services::counted_days(today, days_back, &snapshot.vacation_periods, mode) {
        let Some(payload) = snapshot.payloads.get(&real_day.to_string()) else { continue };
        let day_type = services::day_type_for(real_day, snapshot.day_types.as_ref(), &snapshot.vacation_periods).0;
        for meal in payload.get("meals").and_then(Value::as_array).into_iter().flatten() {
            let result = meal.get("calculation").cloned().unwrap_or(Value::Null);
            let kind = meal.get("meal").and_then(Value::as_str).unwrap_or_default();
            let id = meal.get("id").and_then(Value::as_str).unwrap_or_default();
            let Some(factor) = dec_of(result.get("factor_units_per_ke")) else { continue };
            if !MEALS.contains(&kind) || snapshot.analysis_exclusions.contains(id) {
                continue;
            }
            let at = meal.get("at").and_then(instant)
                .unwrap_or_else(|| real_day.and_time(NaiveTime::from_hms_opt(12, 0, 0).unwrap()).and_utc());
            let place_id = meal.get("place").and_then(|p| p.get("id")).and_then(Value::as_i64);
            points.push(MealPoint {
                id: json!(id), day, real_day: Some(real_day), at,
                key: (kind.to_string(), if kind == "breakfast" { day_type.clone() } else { None }),
                day_type: day_type.clone(), value: factor,
                end_glucose: dec_of(result.get("g3")), carbs_isf: dec_of(result.get("carbs_times_isf_total")),
                carbs_g: dec_of(result.get("carbs_total")),
                fat: meal.get("quality").and_then(|q| q.get("fat_protein")).and_then(Value::as_bool).unwrap_or(false),
                surcharge_mgdl: meal.get("fpe_surcharge").and_then(|plan| dec_of(plan.get("mgdl"))),
                place_id, own_place: place_id.is_some_and(|id| own_places.contains(&id)),
            });
        }
    }
    points
}

/// The Automatik suggestion a meal would have got from the days before it, and the factor error in mg/dL.
pub fn suggestion_miss(points: &[MealPoint], point: &MealPoint, configured: &Configured) -> (Option<Decimal>, Option<Decimal>) {
    let Some(carbs_isf) = point.carbs_isf.filter(|v| !v.is_zero()) else { return (None, None) };
    let suggested = if configured.surcharge() {
        let values = forecast::base_history(points, &point.key, point.day, configured.lookback, configured.surcharge_mgdl, configured.fat_mgdl);
        forecast::predict_surcharge(&values, configured.percentile, configured.half_life, point, configured.surcharge_mgdl, configured.fat_mgdl)
    } else {
        forecast::predict(points, &point.key, point.day, configured.percentile, configured.half_life, configured.lookback).units_per_ke
    };
    match suggested {
        Some(value) => (Some(value), Some((point.value - value) * carbs_isf / dec!(10))),
        None => (None, None),
    }
}

fn miss_stats(deviations: &[Decimal]) -> Map<String, Value> {
    let mut out = Map::new();
    if deviations.is_empty() {
        out.insert("median_abs_deviation_mgdl".into(), Value::Null);
        out.insert("median_deviation_mgdl".into(), Value::Null);
        return out;
    }
    let mut absolute: Vec<Decimal> = deviations.iter().map(|v| v.abs()).collect();
    absolute.sort();
    let mut signed = deviations.to_vec();
    signed.sort();
    out.insert("median_abs_deviation_mgdl".into(), opt(median(&absolute)));
    out.insert("median_deviation_mgdl".into(), opt(median(&signed)));
    out
}

fn tuning_apply(key: &str, value: &Value) -> Value {
    match key {
        "percentile" => json!({"auto_percentile": value_text(value)}),
        "half_life" => json!({"auto_half_life_days": value_text(value)}),
        "lookback" => json!({"auto_lookback_days": decimal_of(value).and_then(|v| i64::try_from(v.trunc()).ok())}),
        _ => if decimal_of(value).is_some_and(|v| !v.is_zero()) {
            json!({"auto_method": forecast::SURCHARGE, "auto_surcharge_mgdl": value_text(value)})
        } else {
            json!({"auto_method": "percentile"})
        },
    }
}

fn value_text(value: &Value) -> Value {
    match value {
        Value::String(_) => value.clone(),
        other => Value::String(other.to_string()),
    }
}

/// The period-independent part: suggestions, backtest matrix with the Automatik view, day-type ratio
/// and each meal's miss against its own suggestion.
struct Automatik {
    predictions: HashMap<MealKey, Value>,
    derivation: Value,
    day_type_ratio: Value,
    misses: HashMap<String, (Option<Decimal>, Option<Decimal>)>,
}

/// The last Automatik part computed, by its inputs: across the five periods of a background round and
/// across rounds while new readings change no meal factor, it is the same.
static AUTOMATIK_CACHE: Mutex<Option<(String, std::sync::Arc<Automatik>)>> = Mutex::new(None);

fn automatik(today: NaiveDate, points: &[MealPoint], settings: &Value, configured: &Configured) -> std::sync::Arc<Automatik> {
    let key = serde_json::to_string(&json!([today.to_string(), settings, to_value(&points)])).unwrap_or_default();
    if let Some((cached_key, cached)) = AUTOMATIK_CACHE.lock().unwrap().as_ref() {
        if *cached_key == key {
            return cached.clone();
        }
    }
    let computed = std::sync::Arc::new(compute_automatik(today, points, configured));
    *AUTOMATIK_CACHE.lock().unwrap() = Some((key, computed.clone()));
    computed
}

fn compute_automatik(today: NaiveDate, points: &[MealPoint], c: &Configured) -> Automatik {
    let predictions: HashMap<MealKey, Value> = c.keys().into_iter().map(|key| {
        let prediction = forecast::predict_method(points, &key, today, c.percentile, c.half_life, c.lookback, &c.method,
                                                  c.surcharge_mgdl, c.fat_mgdl);
        (key, json!({"count": prediction.count, "units_per_ke": opt(prediction.units_per_ke), "surcharge": c.surcharge()}))
    }).collect();
    // Backtest matrix: every lookback × half-life (plus the configured ones), each at 25 %, the configured
    // percentile and 50 %, plus the configured percentile with the surcharge, on the meals of the last 90 days.
    let mut percentiles: Vec<Decimal> = BTreeSet::from([dec!(25), c.percentile, dec!(50)]).into_iter().collect();
    percentiles.sort();
    let lookbacks: Vec<i64> = BTreeSet::from_iter(forecast::LOOKBACKS.iter().copied().chain([c.lookback])).into_iter().collect();
    let half_lives: Vec<Decimal> = {
        let mut values: Vec<Decimal> = forecast::HALF_LIVES.to_vec();
        if !values.contains(&c.half_life) {
            values.push(c.half_life);
        }
        values.sort();
        values
    };
    let rows: Vec<(i64, Decimal)> = lookbacks.iter().flat_map(|l| half_lives.iter().map(move |h| (*l, *h))).collect();
    let since = today - Duration::days(forecast::BACKTEST_DAYS - 1);
    let mut variants = Vec::new();
    for (lookback, half_life) in &rows {
        for percentile in &percentiles {
            variants.push(Variant { lookback: *lookback, half_life: *half_life, percentile: *percentile, surcharge_mgdl: None });
        }
        variants.push(Variant { lookback: *lookback, half_life: *half_life, percentile: c.percentile, surcharge_mgdl: Some(c.surcharge_mgdl) });
    }
    let test = forecast::backtest(points, since, &variants, c.target, c.fat_mgdl);
    let mut cells = test.stats.iter();
    let mut cell_percentiles: Vec<Value> = percentiles.iter().map(|p| s(*p)).collect();
    cell_percentiles.push(json!(forecast::SURCHARGE));
    let variant_rows: Vec<Value> = rows.iter().map(|(lookback, half_life)| json!({
        "lookback_days": lookback, "half_life_days": s(*half_life),
        "cells": cell_percentiles.iter().map(|p| json!({"percentile": p, "stats": to_value(cells.next().unwrap())})).collect::<Vec<_>>(),
    })).collect();
    let mut derivation = json!({
        "percentile": s(c.percentile), "half_life_days": s(c.half_life), "lookback_days": c.lookback,
        "method": c.method, "surcharge_mgdl": s(c.surcharge_mgdl), "fat_surcharge_mgdl": s(c.fat_mgdl),
        "surcharge_full_grams": s(forecast::SURCHARGE_FULL_GRAMS),
        "min_meals": forecast::MIN_MEALS, "meal_count": test.rows.len(),
        "backtest_days": forecast::BACKTEST_DAYS,
        "variants": variant_rows,
        "glucose_low_mgdl": s(forecast::GLUCOSE_LOW), "glucose_high_mgdl": s(forecast::GLUCOSE_HIGH),
        "dose_target_mgdl": s(c.target),
    });
    mark_matrix(&mut derivation);
    let active = c.active_surcharge();
    let tuning: Vec<Value> = forecast::tuning(points, since, c.lookback, c.half_life, c.percentile, active, c.target, c.fat_mgdl)
        .iter().map(|row| {
            let mut value = to_value(row);
            let key = row.key;
            if let Some(options) = value.get_mut("options").and_then(Value::as_array_mut) {
                for option in options {
                    if key == "lookback" {
                        let days = option.get("value").and_then(decimal_of).and_then(|v| i64::try_from(v.trunc()).ok());
                        option["value"] = json!(days);
                    } else if let Some(number) = option.get("value").and_then(decimal_of)
                        .and_then(|v| v.normalize().to_string().parse::<serde_json::Number>().ok()) {
                        // A number like the page's settings, not text.
                        option["value"] = Value::Number(number);
                    }
                    let apply = tuning_apply(key, &option["value"]);
                    option["apply"] = apply;
                }
            }
            value
        }).collect();
    derivation["tuning"] = Value::Array(tuning);
    derivation["active_surcharge_mgdl"] = s(active);
    let mut tradeoffs = to_value(&forecast::tradeoffs(points, since, c.lookback, c.half_life, c.percentile, active, c.target, c.fat_mgdl));
    let tradeoff_map = tradeoffs.as_object_mut().into_iter().flat_map(|map| map.iter_mut());
    let mut options: Vec<&mut Value> = Vec::new();
    for (key, value) in tradeoff_map {
        match key.as_str() {
            "current" if value.is_object() => options.push(value),
            "line" => options.extend(value.as_array_mut().into_iter().flatten()),
            _ => {}
        }
    }
    for option in options {
        let changes: Vec<String> = option.get("changes").and_then(Value::as_array).into_iter().flatten()
            .filter_map(|v| v.as_str().map(str::to_string)).collect();
        let mut apply = Map::new();
        for (key, field) in [("lookback", "lookback_days"), ("half_life", "half_life_days"), ("percentile", "percentile"), ("surcharge", "surcharge_mgdl")] {
            if changes.iter().any(|change| change == key) {
                if let Value::Object(map) = tuning_apply(key, &option[field]) {
                    apply.extend(map);
                }
            }
        }
        option["apply"] = Value::Object(apply);
    }
    derivation["tradeoffs"] = tradeoffs;
    derivation["examples"] = Value::Array(c.keys().iter().map(|key| to_value(&forecast::explain(
        points, key, today, c.lookback, c.half_life, c.percentile, active, c.fat_mgdl))).collect());
    let mut ages: BTreeSet<i64> = BTreeSet::from([1, c.lookback]);
    for k in [1, 2, 4] {
        ages.insert(i64::try_from((c.half_life * Decimal::from(k)).trunc()).unwrap_or(0));
    }
    derivation["weights"] = Value::Array(ages.into_iter().filter(|age| (1..=c.lookback).contains(age))
        .map(|age| json!({"age_days": age, "weight": s(forecast::weight(age, c.half_life))})).collect());
    let day_type_ratio = if c.day_types { to_value(&forecast::day_type_ratios(points, today)) } else { Value::Null };
    let misses = point_misses(points, c);
    Automatik { predictions, derivation, day_type_ratio, misses }
}

fn point_id(point: &MealPoint) -> String {
    point.id.as_str().map(str::to_string).unwrap_or_else(|| point.id.to_string())
}

/// Add to every backtest cell its shares below / in / above range, whether it is the configured variant,
/// and the best cells: most endpoints in range, then fewest below, per lookback period and overall.
fn mark_matrix(derivation: &mut Value) {
    let method = derivation["method"].as_str().unwrap_or_default().to_string();
    let percentile = decimal_of(&derivation["percentile"]).unwrap_or_default();
    let lookback = derivation["lookback_days"].as_i64().unwrap_or_default();
    let half_life = decimal_of(&derivation["half_life_days"]).unwrap_or_default();
    let count = |stats: &Value, key: &str| stats[key].as_u64().unwrap_or(0);
    let better = |cell: &Value, top: Option<&Value>| match top {
        None => true,
        Some(best) => count(&cell["stats"], "in_range") > count(&best["stats"], "in_range")
            || (count(&cell["stats"], "in_range") == count(&best["stats"], "in_range") && count(&cell["stats"], "below") < count(&best["stats"], "below")),
    };
    let mut overall: Option<(usize, usize)> = None;
    let mut per_lookback: Vec<(i64, (usize, usize))> = Vec::new();
    let variants = derivation["variants"].as_array_mut().unwrap();
    for (vi, variant) in variants.iter_mut().enumerate() {
        let variant_lookback = variant["lookback_days"].as_i64().unwrap_or_default();
        let variant_half_life = decimal_of(&variant["half_life_days"]).unwrap_or_default();
        for cell in variant["cells"].as_array_mut().unwrap().iter_mut() {
            let total = count(&cell["stats"], "glucose_count");
            let mut shares = Map::new();
            for key in ["below", "in_range", "above"] {
                let share = if total > 0 { Decimal::from(count(&cell["stats"], key)) * dec!(100) / Decimal::from(total) } else { Decimal::ZERO };
                shares.insert(key.into(), s(share));
            }
            cell["shares"] = Value::Object(shares);
            let surcharge = cell["percentile"] == forecast::SURCHARGE;
            let active = (if method == forecast::SURCHARGE { surcharge } else { !surcharge && decimal_of(&cell["percentile"]) == Some(percentile) })
                && variant_lookback == lookback && variant_half_life == half_life;
            cell["active"] = json!(active);
            cell["best"] = Value::Null;
        }
        let _ = vi;
    }
    // Second pass: pick the best cells (the first of equal cells wins).
    for (vi, variant) in variants.iter().enumerate() {
        let variant_lookback = variant["lookback_days"].as_i64().unwrap_or_default();
        for (ci, cell) in variant["cells"].as_array().unwrap().iter().enumerate() {
            if better(cell, overall.map(|(v, c)| &variants[v]["cells"][c])) {
                overall = Some((vi, ci));
            }
            let current = per_lookback.iter().find(|(l, _)| *l == variant_lookback).map(|(_, at)| *at);
            if better(cell, current.map(|(v, c)| &variants[v]["cells"][c])) {
                match per_lookback.iter_mut().find(|(l, _)| *l == variant_lookback) {
                    Some(entry) => entry.1 = (vi, ci),
                    None => per_lookback.push((variant_lookback, (vi, ci))),
                }
            }
        }
    }
    for (_, (v, c)) in per_lookback {
        variants[v]["cells"][c]["best"] = json!("lookback");
    }
    if let Some((v, c)) = overall {
        variants[v]["cells"][c]["best"] = json!("overall");
    }
}

fn suggestion_factor(prediction: &Value, c: &Configured) -> Value {
    json!({
        "auto_units_per_ke": prediction["units_per_ke"], "count": prediction["count"], "days": c.lookback,
        "min_meals": forecast::MIN_MEALS, "percentile": s(c.percentile), "half_life_days": s(c.half_life),
        "surcharge": prediction.get("surcharge").cloned().unwrap_or(json!(false)),
        "surcharge_mgdl": s(c.surcharge_mgdl), "surcharge_full_grams": s(forecast::SURCHARGE_FULL_GRAMS),
    })
}

fn sorted(mut values: Vec<Decimal>) -> Vec<Decimal> {
    values.sort();
    values
}

fn stats_block(values: &[Decimal], out: &mut Map<String, Value>) {
    let mean = if values.is_empty() { None } else { Some(values.iter().copied().sum::<Decimal>() / Decimal::from(values.len())) };
    out.insert("mean_units_per_ke".into(), opt(mean));
    out.insert("median_units_per_ke".into(), opt(median(values)));
    out.insert("min_units_per_ke".into(), opt(values.first().copied()));
    out.insert("max_units_per_ke".into(), opt(values.last().copied()));
    // With fewer than five meals, quartiles imply more precision than the data support.
    out.insert("q1_units_per_ke".into(), if values.len() >= 5 { s(quartile(values, dec!(0.25))) } else { Value::Null });
    out.insert("q3_units_per_ke".into(), if values.len() >= 5 { s(quartile(values, dec!(0.75))) } else { Value::Null });
}

/// The analysis page for the last `days` days.
pub fn compute_summary(snapshot: &Snapshot) -> Res<Value> {
    let today = snapshot.today;
    let days = snapshot.days;
    if !PERIODS.contains(&days) {
        return Err(CoreError::Value(t("Ungültiger Zeitraum")));
    }
    let settings = &snapshot.settings;
    let c = Configured::of(settings)?;
    let day_types = snapshot.day_types.as_ref();
    let mut groups: HashMap<&str, Map<String, Value>> = HashMap::new();
    let mut values: HashMap<&str, (Vec<Decimal>, Vec<Decimal>)> = HashMap::new();
    let mut calculations: HashMap<&str, Vec<(Map<String, Value>, Value)>> = HashMap::new();
    for name in MEALS {
        let mut group = Map::new();
        for key in ["total", "count", "valid_count", "excluded_count", "blocked_count", "multi_carb_count", "shortened_count"] {
            group.insert(key.into(), json!(0));
        }
        groups.insert(name, group);
        values.insert(name, (Vec::new(), Vec::new()));
        calculations.insert(name, Vec::new());
    }
    let bump = |group: &mut Map<String, Value>, key: &str| {
        let value = group[key].as_i64().unwrap_or(0) + 1;
        group.insert(key.into(), json!(value));
    };
    let mut night_records = Vec::new();
    let mut quiet_records = Vec::new();
    let mut sport_meals = Vec::new();
    let mut sport_sessions = Vec::new();
    let empty = json!({"meals": []});
    for offset in 0..days {
        let day = today - Duration::days(offset);
        let key = day.to_string();
        let payload = snapshot.payloads.get(&key).unwrap_or(&empty);
        night_records.push(night_record(&key, payload));
        for item in payload.get("quiet_periods").and_then(Value::as_array).into_iter().flatten() {
            let mut row = Map::from_iter([("day".to_string(), json!(key))]);
            if let Value::Object(map) = item {
                row.extend(map.clone());
            }
            quiet_records.push(Value::Object(row));
        }
        let day_type = services::day_type_for(day, day_types, &snapshot.vacation_periods).0;
        for item in payload.get("timeline").and_then(Value::as_array).into_iter().flatten() {
            if item["kind"] == "exercise" {
                sport_sessions.push(json!({"id": item["id"], "at": item["at"], "duration_minutes": item["duration_minutes"],
                                           "kind": item["sport_kind"], "notes": item["notes"]}));
            }
        }
        for meal in payload.get("meals").and_then(Value::as_array).into_iter().flatten() {
            let id = meal["id"].as_str().unwrap_or_default();
            let excluded = snapshot.analysis_exclusions.contains(id);
            let result = meal.get("calculation").filter(|v| v.is_object()).cloned().unwrap_or(json!({}));
            sport_meals.push(json!({"id": meal["id"], "day": key, "meal": meal["meal"], "at": meal["at"], "excluded": excluded,
                                    "calculation": result, "sport": meal.get("sport").filter(|v| v.is_object()).cloned().unwrap_or(json!({}))}));
            let Some(name) = MEALS.iter().find(|name| meal["meal"] == **name).copied() else { continue };
            let group = groups.get_mut(name).unwrap();
            bump(group, "total");
            if result.get("end_reason").and_then(Value::as_str) == Some("before_next_meal") {
                bump(group, "shortened_count");
            }
            if result.get("extra_carb_count").and_then(Value::as_i64).unwrap_or(0) != 0 {
                bump(group, "multi_carb_count");
            }
            let factor = result.get("factor_units_per_ke").filter(|v| !v.is_null()).cloned();
            let quality = meal.get("quality").cloned().unwrap_or(json!({}));
            let mut item = Map::new();
            item.insert("id".into(), meal["id"].clone());
            item.insert("day".into(), json!(key));
            item.insert("at".into(), meal["at"].clone());
            item.insert("day_type".into(), json!(day_type));
            item.insert("units_per_ke".into(), factor.clone().unwrap_or(Value::Null));
            for field in ["carbs_total", "rapid_units_in_window", "delta_mgdl", "end", "reason"] {
                item.insert(field.into(), result.get(field).cloned().unwrap_or(Value::Null));
            }
            item.insert("manual_window".into(), json!(result.get("manual_window").and_then(Value::as_bool).unwrap_or(false)));
            item.insert("excluded".into(), json!(excluded));
            item.insert("sport".into(), meal.get("sport").cloned().unwrap_or(json!({})));
            item.insert("certainty".into(), quality.get("carbs_certainty").cloned().unwrap_or(Value::Null));
            item.insert("fast".into(), json!(quality.get("fast").and_then(Value::as_bool).unwrap_or(false)));
            item.insert("dish".into(), meal.get("dish").and_then(|d| d.get("name")).cloned().unwrap_or(Value::Null));
            item.insert("expected_delta_mgdl".into(), Value::Null);
            item.insert("deviation_mgdl".into(), Value::Null);
            item.insert("expected_units_per_ke".into(), Value::Null);
            match factor.as_ref().and_then(decimal_of) {
                None => bump(group, "blocked_count"),
                Some(value) => {
                    bump(group, "valid_count");
                    if excluded {
                        bump(group, "excluded_count");
                    } else {
                        bump(group, "count");
                        let entry = values.get_mut(name).unwrap();
                        entry.0.push(value);
                        entry.1.push(dec_of(result.get("ratio_g_per_unit")).unwrap_or_default());
                    }
                }
            }
            calculations.get_mut(name).unwrap().push((item, result));
        }
    }
    let factor_of = |item: &Map<String, Value>| dec_of(item.get("units_per_ke"));
    for name in MEALS {
        let items = calculations.get_mut(name).unwrap();
        for (item, _) in items.iter_mut() {
            item.insert("strong_outlier".into(), json!(false));
        }
        let comparable: Vec<usize> = (0..items.len()).filter(|i| factor_of(&items[*i].0).is_some()).collect();
        let sets: Vec<Vec<usize>> = if name == "breakfast" && day_types.is_some() {
            ["work", "free"].iter().map(|kind| comparable.iter().copied().filter(|i| items[*i].0["day_type"] == *kind).collect()).collect()
        } else {
            vec![comparable.clone()]
        };
        for set in sets {
            let factors: Vec<Decimal> = set.iter().map(|i| factor_of(&items[*i].0).unwrap()).collect();
            for (index, flag) in set.iter().zip(strong_outlier_flags(&factors)) {
                items[*index].0.insert("strong_outlier".into(), json!(flag));
            }
        }
        let group = groups.get_mut(name).unwrap();
        group.insert("strong_outlier_count".into(), json!(comparable.iter().filter(|i| items[**i].0["strong_outlier"] == true).count()));
        let (own, nightscout) = values.remove(name).unwrap();
        let own = sorted(own);
        let nightscout = sorted(nightscout);
        stats_block(&own, group);
        group.insert("median_g_per_unit".into(), opt(median(&nightscout)));
        let mut chronological: Vec<&Map<String, Value>> = items.iter().map(|(item, _)| item)
            .filter(|item| factor_of(item).is_some() && item["excluded"] == false).collect();
        chronological.sort_by(|a, b| b["at"].as_str().cmp(&a["at"].as_str()));
        if chronological.len() >= 6 {
            let recent = median(&sorted(chronological[..3].iter().map(|item| factor_of(item).unwrap()).collect())).unwrap();
            let previous = median(&sorted(chronological[3..6].iter().map(|item| factor_of(item).unwrap()).collect())).unwrap();
            group.insert("recent_median_units_per_ke".into(), s(recent));
            group.insert("previous_median_units_per_ke".into(), s(previous));
            group.insert("trend_units_per_ke".into(), s(recent - previous));
        } else {
            for field in ["recent_median_units_per_ke", "previous_median_units_per_ke", "trend_units_per_ke"] {
                group.insert(field.into(), Value::Null);
            }
        }
    }
    let mut by_day_type = Map::new();
    if day_types.is_some() {
        let items = &calculations["breakfast"];
        for kind in ["work", "free"] {
            let mine: Vec<&Map<String, Value>> = items.iter().map(|(item, _)| item).filter(|item| item["day_type"] == kind).collect();
            let kind_values = sorted(mine.iter().filter(|item| item["excluded"] == false).filter_map(|item| factor_of(item)).collect());
            let mut row = Map::new();
            row.insert("total".into(), json!(mine.len()));
            row.insert("count".into(), json!(kind_values.len()));
            row.insert("valid_count".into(), json!(mine.iter().filter(|item| factor_of(item).is_some()).count()));
            row.insert("excluded_count".into(), json!(mine.iter().filter(|item| factor_of(item).is_some() && item["excluded"] == true).count()));
            row.insert("blocked_count".into(), json!(mine.iter().filter(|item| factor_of(item).is_none()).count()));
            stats_block(&kind_values, &mut row);
            by_day_type.insert(kind.into(), Value::Object(row));
        }
    }
    let mode = settings.get("auto_vacation").and_then(Value::as_str).unwrap_or(services::DEFAULT_AUTO_VACATION);
    let points = snapshot.points.clone().unwrap_or_else(|| meal_points(today, history_days(days), snapshot, mode));
    let part = automatik(today, &points, settings, &c);
    let by_id: HashMap<String, &MealPoint> = points.iter().map(|point| (point_id(point), point)).collect();
    // Erwartet / Delta per meal, the typical miss per meal kind, carb certainty and "Schnell" mark.
    let mut certainty: Vec<(String, Vec<Decimal>)> = Vec::new();
    let mut fast: Vec<(String, Vec<Decimal>)> = Vec::new();
    let push = |list: &mut Vec<(String, Vec<Decimal>)>, key: String, value: Decimal| match list.iter_mut().find(|(k, _)| *k == key) {
        Some(entry) => entry.1.push(value),
        None => list.push((key, vec![value])),
    };
    for name in MEALS {
        let items = calculations.get_mut(name).unwrap();
        for (item, result) in items.iter_mut() {
            let id = item["id"].as_str().unwrap_or_default().to_string();
            let Some(delta) = dec_of(result.get("delta_mgdl")) else { continue };
            if !by_id.contains_key(&id) {
                continue;
            }
            let (Some(suggested), Some(deviation)) = part.misses.get(&id).copied().unwrap_or((None, None)) else { continue };
            item.insert("expected_units_per_ke".into(), s(suggested));
            item.insert("expected_delta_mgdl".into(), s(delta - deviation));
            item.insert("deviation_mgdl".into(), s(deviation));
            push(&mut certainty, item["certainty"].as_str().unwrap_or("none").to_string(), deviation);
            push(&mut fast, if item["fast"] == true { "fast".into() } else { "other".into() }, deviation);
        }
        let deviations: Vec<Decimal> = items.iter().filter_map(|(item, _)| dec_of(item.get("deviation_mgdl"))).collect();
        let group = groups.get_mut(name).unwrap();
        group.extend(miss_stats(&deviations));
        group.insert("expected_count".into(), json!(deviations.len()));
    }
    let levels = |list: Vec<(String, Vec<Decimal>)>| -> Value {
        Value::Object(list.into_iter().map(|(key, values)| {
            let mut row = Map::from_iter([("count".to_string(), json!(values.len()))]);
            row.extend(miss_stats(&values));
            (key, Value::Object(row))
        }).collect())
    };
    let misses = json!({"certainty": levels(certainty), "fast": levels(fast)});
    let trend = miss_trend(&points, today, &part.misses);
    for (kind, row) in by_day_type.iter_mut() {
        let key: MealKey = ("breakfast".into(), Some(kind.clone()));
        row["suggestion"] = suggestion_factor(&part.predictions[&key], &c);
    }
    let named: &[&str] = if day_types.is_some() { &["lunch", "dinner"] } else { &MEALS };
    for name in named {
        let key: MealKey = (name.to_string(), None);
        groups.get_mut(name).unwrap().insert("suggestion".into(), suggestion_factor(&part.predictions[&key], &c));
    }
    let timezone = settings.get("timezone").and_then(Value::as_str).unwrap_or("UTC");
    let ic_schedule: Vec<crate::model::IsfRow> = serde_json::from_value(settings.get("ic_schedule").cloned().unwrap_or(json!([]))).unwrap_or_default();
    for name in MEALS {
        let mut current: Vec<Decimal> = Vec::new();
        if !ic_schedule.is_empty() {
            for (item, _) in &calculations[name] {
                if let Some(at) = instant(&item["at"]) {
                    let grams = schedule_value_at(at, &ic_schedule, timezone, ScheduleKey::GramsPerUnit)?;
                    let value = dec!(10) / grams;
                    if !current.contains(&value) {
                        current.push(value);
                    }
                }
            }
        }
        current.sort();
        let group = groups.get_mut(name).unwrap();
        group.insert("nightscout_units_per_ke".into(), Value::Array(current.into_iter().map(s).collect()));
        if name == "breakfast" {
            group.insert("by_day_type".into(), Value::Object(by_day_type.clone()));
        }
        group.insert("calculations".into(), Value::Array(calculations[name].iter().map(|(item, _)| Value::Object(item.clone())).collect()));
    }
    let names = sport_names(settings);
    let standards: HashMap<String, Decimal> = settings.get("sport_effects_mgdl").and_then(Value::as_object).into_iter().flatten()
        .filter_map(|(kind, value)| decimal_of(value).map(|v| (kind.clone(), v))).collect();
    let lookback_minutes = dec_of(settings.get("sport_lookback_minutes")).and_then(|v| i64::try_from(v.trunc()).ok()).unwrap_or(360);
    let window_minutes = dec_of(settings.get("window_minutes")).and_then(|v| i64::try_from(v.trunc()).ok()).unwrap_or(180);
    let mut sport = sport_analysis(&sport_meals, &sport_sessions, lookback_minutes, window_minutes, &names, &standards, &snapshot.own_sport_sessions);
    for group in sport["kinds"].as_array_mut().into_iter().flatten() {
        let kind = group["kind"].as_str().map(str::to_string);
        group["standard_drop_mgdl"] = kind.as_ref().and_then(|k| standards.get(k)).map(|v| s(*v)).unwrap_or(Value::Null);
        let suggested = group["suggested_drop_mgdl"].clone();
        group["apply"] = if suggested.is_null() { Value::Null } else {
            let mut effects = settings.get("sport_effects_mgdl").cloned().unwrap_or(json!({}));
            if let (Some(map), Some(kind)) = (effects.as_object_mut(), kind) {
                map.insert(kind, suggested);
            }
            json!({"sport_effects_mgdl": effects})
        };
    }
    let mut out = Map::new();
    out.insert("days".into(), json!(days));
    out.insert("groups".into(), Value::Object(groups.into_iter().map(|(k, v)| (k.to_string(), Value::Object(v))).collect()));
    out.insert("misses".into(), misses);
    out.insert("trend".into(), trend);
    out.insert("profile_sync_error".into(), Value::Null);
    out.insert("suggestion_derivation".into(), part.derivation.clone());
    out.insert("day_types".into(), json!(day_types.is_some()));
    out.insert("day_type_ratio".into(), part.day_type_ratio.clone());
    out.insert("sport_analysis".into(), sport);
    out.insert("night".into(), night_overview(&night_records, days));
    out.insert("quiet".into(), quiet_overview(quiet_records));
    Ok(Value::Object(out))
}

/// The miss week by week over the last TREND_WEEKS weeks before today, per meal kind; `drift` names a
/// direction when the last DRIFT_WEEKS weeks with meals all missed the same way by at least DRIFT_MGDL.
pub fn miss_trend(points: &[MealPoint], today: NaiveDate, misses: &HashMap<String, (Option<Decimal>, Option<Decimal>)>) -> Value {
    let weeks: Vec<Value> = (0..TREND_WEEKS).rev().map(|index| json!({
        "start": (today - Duration::days(7 * (index + 1))).to_string(),
        "end": (today - Duration::days(7 * index + 1)).to_string()})).collect();
    let mut buckets: HashMap<&str, Vec<Vec<Decimal>>> = MEALS.iter().map(|meal| (*meal, vec![Vec::new(); TREND_WEEKS as usize])).collect();
    for point in points {
        let age = (today - point.day).num_days();
        if !(1..=7 * TREND_WEEKS).contains(&age) || point.own_place || !MEALS.contains(&point.key.0.as_str()) {
            continue;
        }
        if let Some((_, Some(deviation))) = misses.get(&point_id(point)) {
            buckets.get_mut(point.key.0.as_str()).unwrap()[(TREND_WEEKS - 1 - (age - 1) / 7) as usize].push(*deviation);
        }
    }
    let mut kinds = Map::new();
    for meal in MEALS {
        let cells: Vec<Value> = buckets[meal].iter().map(|values| {
            let mut row = Map::from_iter([("count".to_string(), json!(values.len()))]);
            row.extend(miss_stats(values));
            Value::Object(row)
        }).collect();
        let medians: Vec<Decimal> = cells.iter().filter(|cell| cell["count"].as_u64().unwrap_or(0) > 0)
            .filter_map(|cell| decimal_of(&cell["median_deviation_mgdl"])).collect();
        let recent = &medians[medians.len().saturating_sub(DRIFT_WEEKS)..];
        let drift = if recent.len() == DRIFT_WEEKS && recent.iter().all(|v| *v >= DRIFT_MGDL) { json!("up") }
            else if recent.len() == DRIFT_WEEKS && recent.iter().all(|v| *v <= -DRIFT_MGDL) { json!("down") }
            else { Value::Null };
        kinds.insert(meal.into(), json!({"weeks": cells, "drift": drift}));
    }
    json!({"weeks": weeks, "kinds": kinds})
}

fn night_record(day: &str, payload: &Value) -> Value {
    let night = payload.get("night").cloned().unwrap_or(json!({}));
    let mut row = Map::from_iter([("day".to_string(), json!(day))]);
    for key in ["start", "end", "dinner_at", "breakfast_at", "last_injection_at", "morning_dose_at", "delta_mgdl",
                "start_glucose", "end_glucose", "minimum_glucose", "minimum_at", "drop_mgdl", "rise_mgdl", "carbs_g", "reason"] {
        row.insert(key.into(), night.get(key).cloned().unwrap_or(Value::Null));
    }
    let basal = payload.get("basal").and_then(Value::as_array).filter(|doses| !doses.is_empty());
    row.insert("basal_units".into(), match basal {
        Some(doses) => s(doses.iter().filter_map(|dose| dec_of(dose.get("units"))).sum()),
        None => Value::Null,
    });
    Value::Object(row)
}

fn night_overview(records: &[Value], days: i64) -> Value {
    let complete: Vec<&Value> = records.iter().filter(|item| !item["minimum_glucose"].is_null() && !item["start_glucose"].is_null()).collect();
    let clean: Vec<&&Value> = complete.iter().filter(|item| dec_of(item.get("carbs_g")).unwrap_or_default().is_zero()).collect();
    let middle = |key: &str| opt(median(&sorted(clean.iter().filter_map(|item| dec_of(item.get(key))).collect())));
    json!({
        "count": clean.len(), "with_carbs_count": complete.len() - clean.len(),
        "unavailable_count": days - complete.len() as i64,
        "median_start_glucose": middle("start_glucose"), "median_end_glucose": middle("end_glucose"),
        "median_minimum_glucose": middle("minimum_glucose"), "median_delta_mgdl": middle("delta_mgdl"),
        "median_drop_mgdl": middle("drop_mgdl"), "median_rise_mgdl": middle("rise_mgdl"),
        "nights": records,
    })
}

fn quiet_overview(mut records: Vec<Value>) -> Value {
    let rates = sorted(records.iter().filter_map(|item| dec_of(item.get("rate_mgdl_per_hour"))).collect());
    let hours: Decimal = records.iter().filter_map(|item| dec_of(item.get("hours"))).sum();
    let count = records.len();
    records.sort_by(|a, b| b["start"].as_str().unwrap_or_default().cmp(a["start"].as_str().unwrap_or_default()));
    json!({"count": count, "hours": s(hours), "median_rate_mgdl_per_hour": opt(median(&rates)), "periods": records})
}

fn sport_names(settings: &Value) -> HashMap<String, String> {
    let mut names: HashMap<String, String> = crate::episodes::SPORT_LABELS.iter().map(|(kind, label)| (kind.to_string(), t(label))).collect();
    if let Some(custom) = settings.get("sport_custom").and_then(Value::as_object) {
        for (kind, name) in custom {
            names.insert(kind.clone(), name.as_str().unwrap_or_default().to_string());
        }
    }
    names
}

fn sport_median(values: &[Decimal]) -> Option<Decimal> {
    median(&sorted(values.to_vec()))
}

/// Factor without any sport correction, or None when blocked (sport_analysis.raw_factor).
fn raw_factor(result: &Value) -> Option<Decimal> {
    if result.get("blockers").and_then(Value::as_array).is_some_and(|items| !items.is_empty())
        || result.get("start").is_none_or(|v| v.is_null() || v == "") {
        return None;
    }
    let delta = dec_of(result.get("delta_mgdl"))?;
    let insulin = dec_of(result.get("insulin_effect_mgdl"))?;
    let carb_effect = dec_of(result.get("carbs_times_isf_total"))?;
    if carb_effect <= Decimal::ZERO {
        return None;
    }
    Some(dec!(10) * (delta + insulin) / carb_effect)
}

/// The kind's standard drop that would have matched what the meals measured (sport_analysis.suggested_drop).
pub fn suggested_drop(standard: Option<Decimal>, ratios: &[Decimal]) -> Value {
    let Some(standard) = standard.filter(|v| !v.is_zero()) else { return Value::Null };
    if ratios.len() < SPORT_SUGGEST_MIN_MEALS {
        return Value::Null;
    }
    let ratio = sport_median(ratios).unwrap().clamp(Decimal::ZERO, SPORT_SUGGEST_MAX_RATIO);
    let rounded = (standard * ratio / dec!(5)).round_dp_with_strategy(0, rust_decimal::RoundingStrategy::MidpointNearestEven) * dec!(5);
    let mut value = rounded.clamp(dec!(-500), dec!(500));
    if value.is_zero() {
        value = Decimal::ZERO;
    }
    if value == standard { Value::Null } else { Value::String(value.normalize().to_string()) }
}

/// Link sport sessions to meals and estimate each meal's sport effect (sport_analysis.analyze).
pub fn sport_analysis(meals: &[Value], exercises: &[Value], lookback_minutes: i64, window_minutes: i64,
                  labels: &HashMap<String, String>, standards: &HashMap<String, Decimal>, own_sessions: &HashSet<String>) -> Value {
    struct Session { item: Value, start: Instant, end: Instant, duration: i64, meal_ids: Vec<String> }
    let mut sessions: Vec<Session> = exercises.iter().filter_map(|item| {
        let start = instant(&item["at"])?;
        let duration = dec_of(item.get("duration_minutes")).map(|v| i64::try_from(v.trunc()).unwrap_or(0)).unwrap_or(0);
        Some(Session { item: item.clone(), start, end: start + Duration::minutes(duration), duration, meal_ids: Vec::new() })
    }).collect();
    sessions.sort_by_key(|session| session.start);
    let lookback = Duration::minutes(lookback_minutes);
    struct Prepared<'a> { meal: &'a Value, start: Instant, end: Instant, sessions: Vec<usize>, confirmed: bool, factor: Option<Decimal> }
    let mut prepared = Vec::new();
    for meal in meals {
        let result = &meal["calculation"];
        let Some(start) = instant(&meal["at"]) else { continue };
        let end = result.get("end").and_then(instant).unwrap_or(start + Duration::minutes(window_minutes));
        let linked: Vec<usize> = (0..sessions.len()).filter(|i| sessions[*i].end >= start - lookback && sessions[*i].start < end).collect();
        for index in &linked {
            sessions[*index].meal_ids.push(meal["id"].as_str().unwrap_or_default().to_string());
        }
        prepared.push(Prepared { meal, start, end, sessions: linked, confirmed: meal["sport"].get("status").and_then(Value::as_str) == Some("affected"),
                                 factor: raw_factor(result) });
    }
    let mut baselines = Map::new();
    for kind in MEALS {
        let found: Vec<Decimal> = prepared.iter().filter(|item| item.meal["meal"] == kind && item.sessions.is_empty() && !item.confirmed
                && item.meal["excluded"] != true).filter_map(|item| item.factor).filter(|f| *f > Decimal::ZERO).collect();
        baselines.insert(kind.into(), json!({"median_units_per_ke": opt(sport_median(&found)), "count": found.len()}));
    }
    let mut influenced: Vec<(String, Value)> = Vec::new();
    for item in &prepared {
        if item.sessions.is_empty() && !item.confirmed {
            continue;
        }
        let meal = item.meal;
        let result = &meal["calculation"];
        let baseline = baselines.get(meal["meal"].as_str().unwrap_or_default()).map(|b| b["median_units_per_ke"].clone()).unwrap_or(Value::Null);
        let (mut expected, mut effect, mut reason) = (None, None, Value::Null);
        if item.factor.is_none() {
            reason = json!(result.get("reason").and_then(Value::as_str).filter(|r| !r.is_empty()).map(str::to_string).unwrap_or_else(|| t("Mahlzeit nicht berechenbar")));
        } else if let Some(base) = decimal_of(&baseline) {
            let carb_effect = dec_of(result.get("carbs_times_isf_total")).unwrap_or_default();
            let value = base * carb_effect / dec!(10) - dec_of(result.get("insulin_effect_mgdl")).unwrap_or_default();
            expected = Some(value);
            effect = Some(dec_of(result.get("delta_mgdl")).unwrap_or_default() - value);
        } else {
            reason = json!(t("Keine Vergleichsmahlzeiten ohne Sport"));
        }
        let sport = &meal["sport"];
        let id = meal["id"].as_str().unwrap_or_default().to_string();
        influenced.push((id.clone(), json!({
            "id": id, "day": meal["day"], "meal": meal["meal"], "start": iso(&item.start), "end": iso(&item.end),
            "excluded": meal["excluded"] == true,
            "carbs_total": result.get("carbs_total").cloned().unwrap_or(Value::Null),
            "rapid_units_in_window": result.get("rapid_units_in_window").cloned().unwrap_or(Value::Null),
            "delta_mgdl": result.get("delta_mgdl").cloned().unwrap_or(Value::Null),
            "expected_delta_mgdl": opt(expected), "effect_mgdl": opt(effect),
            "factor_without_sport_units_per_ke": opt(item.factor), "baseline_units_per_ke": baseline,
            "confirmed_effect_mgdl": if item.confirmed { sport.get("effect_mgdl").cloned().unwrap_or(Value::Null) } else { Value::Null },
            "applied_mgdl": dec_of(sport.get("effect_mgdl")).map(|v| s(-v)).unwrap_or(Value::Null),
            "shared": item.sessions.len() > 1,
            "session_ids": item.sessions.iter().map(|i| sessions[*i].item["id"].clone()).collect::<Vec<_>>(),
            "reason": reason,
        })));
    }
    let find = |id: &str| influenced.iter().find(|(key, _)| key == id).map(|(_, v)| v.clone()).unwrap_or(Value::Null);
    let mut rendered: Vec<Value> = sessions.iter().map(|session| {
        let kind = session.item["kind"].as_str();
        json!({"id": session.item["id"], "kind": session.item["kind"],
               "label": kind.and_then(|k| labels.get(k)).cloned().unwrap_or_else(|| t("Sportart unbekannt")),
               "start": iso(&session.start), "end": iso(&session.end), "duration_minutes": session.duration,
               "notes": session.item["notes"].as_str().unwrap_or_default(),
               "meals": session.meal_ids.iter().map(|id| find(id)).collect::<Vec<_>>()})
    }).collect();
    for (_, meal) in &influenced {
        if meal["session_ids"].as_array().is_some_and(|ids| ids.is_empty()) {
            rendered.push(json!({"id": null, "kind": null, "label": t("Sport ohne Eintrag"), "start": meal["start"], "end": null,
                                 "duration_minutes": null, "notes": "", "meals": [meal]}));
        }
    }
    rendered.sort_by(|a, b| b["start"].as_str().unwrap_or_default().cmp(a["start"].as_str().unwrap_or_default()));
    struct Kind { kind: Value, label: Value, sessions: usize, meals: usize, effects: Vec<Decimal>, applied: Vec<Decimal>, ratios: Vec<Decimal> }
    let mut by_kind: Vec<(String, Kind)> = Vec::new();
    for session in &rendered {
        let key = session["kind"].as_str().unwrap_or_default().to_string();
        let index = match by_kind.iter().position(|(k, _)| *k == key) {
            Some(index) => index,
            None => {
                by_kind.push((key.clone(), Kind { kind: session["kind"].clone(), label: session["label"].clone(), sessions: 0, meals: 0,
                                                  effects: Vec::new(), applied: Vec::new(), ratios: Vec::new() }));
                by_kind.len() - 1
            }
        };
        let group = &mut by_kind[index].1;
        group.sessions += 1;
        for meal in session["meals"].as_array().into_iter().flatten() {
            group.meals += 1;
            // Shared meals cannot be attributed to one session; excluded meals stay out of medians.
            if let Some(effect) = dec_of(meal.get("effect_mgdl")) {
                if meal["shared"] != true && meal["excluded"] != true {
                    group.effects.push(effect);
                    if let Some(applied) = dec_of(meal.get("applied_mgdl")) {
                        group.applied.push(applied);
                        let own = session["id"].as_str().is_some_and(|id| own_sessions.contains(id));
                        if !applied.is_zero() && !own {
                            group.ratios.push(effect / applied);
                        }
                    }
                }
            }
        }
    }
    let mut kinds: Vec<Value> = by_kind.into_iter().map(|(_, group)| {
        let standard = group.kind.as_str().and_then(|k| standards.get(k)).copied();
        json!({"kind": group.kind, "label": group.label, "session_count": group.sessions, "meal_count": group.meals,
               "suggested_drop_mgdl": suggested_drop(standard, &group.ratios),
               "median_applied_mgdl": opt(sport_median(&group.applied)),
               "effect_count": group.effects.len(), "median_effect_mgdl": opt(sport_median(&group.effects)),
               "min_effect_mgdl": opt(group.effects.iter().min().copied()), "max_effect_mgdl": opt(group.effects.iter().max().copied())})
    }).collect();
    kinds.sort_by(|a, b| a["label"].as_str().unwrap_or_default().cmp(b["label"].as_str().unwrap_or_default()));
    json!({"baselines": baselines, "kinds": kinds, "sessions": rendered, "lookback_minutes": lookback_minutes})
}

#[allow(dead_code)]
fn unused(text: &str) -> String {
    tr(text, &[])
}
