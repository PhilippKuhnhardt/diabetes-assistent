//! The analysis page (app/services/summary.py compute_summary with app/services/factors.py meal_points
//! and app/sport_analysis.py): factor groups, the Automatik view (suggestions, backtest matrix, tuning,
//! trade-offs, examples), the misses against the Automatik, the weekly trend, sport, nights and quiet
//! stretches, from the stored day payloads in a snapshot.

use crate::calculation::{schedule_value_at, ScheduleKey};
use crate::context::t;
use crate::day::QuietPeriod;
use crate::forecast::{self, CloudPoint, DayTypeRatios, Explanation, MealKey, MealPoint, Prediction, TradeoffOption, Variant,
                      VariantStats, Verdict};
use crate::model::{decimal_of, lenient, opt_amount, Instant};
use crate::services::{self, VacationPeriod};
use crate::stats::{median, quartile, strong_outlier_flags};
use crate::wire::{py_iso, py_iso_opt, to_value};
use crate::{CoreError, Res};
use chrono::{Duration, NaiveDate, NaiveTime};
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use serde::{Deserialize, Serialize, Serializer};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
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
    pub payloads: HashMap<String, StoredDay>,
    /// The meal points over history_days(days) (factors.meal_points), when the caller already has them;
    /// else they are taken from `payloads`.
    #[serde(default)]
    pub points: Option<Vec<MealPoint>>,
}

/// What the analysis reads of a stored day payload (day.rs DayPayload). Every part is read leniently:
/// a payload stored by an older version may lack or shape a field differently.
#[derive(Clone, Debug, Default, Deserialize)]
pub struct StoredDay {
    #[serde(default, deserialize_with = "lenient")]
    pub meals: Vec<StoredMeal>,
    #[serde(default, deserialize_with = "lenient")]
    pub timeline: Vec<StoredRow>,
    #[serde(default, deserialize_with = "lenient")]
    pub quiet_periods: Vec<QuietPeriod>,
    #[serde(default, deserialize_with = "lenient")]
    pub night: StoredNight,
    #[serde(default, deserialize_with = "lenient")]
    pub basal: Vec<StoredBasal>,
}

fn empty_object() -> Value {
    json!({})
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct StoredMeal {
    #[serde(default, deserialize_with = "lenient")]
    pub id: String,
    #[serde(default, deserialize_with = "lenient")]
    pub meal: String,
    #[serde(default, deserialize_with = "lenient")]
    pub at: Option<String>,
    #[serde(default, deserialize_with = "lenient")]
    pub calculation: StoredCalc,
    /// Passed through to the page as stored.
    #[serde(default = "empty_object")]
    pub sport: Value,
    #[serde(default, deserialize_with = "lenient")]
    pub quality: StoredQuality,
    #[serde(default, deserialize_with = "lenient")]
    pub place: Option<StoredId>,
    #[serde(default, deserialize_with = "lenient")]
    pub dish: Option<StoredName>,
    #[serde(default, deserialize_with = "lenient")]
    pub fpe_surcharge: Option<StoredSurcharge>,
}

/// The fields of a meal's calculation the analysis reads (calculation.rs MealCalculation or a blocked window).
#[derive(Clone, Debug, Default, Deserialize)]
pub struct StoredCalc {
    #[serde(default, deserialize_with = "opt_amount")]
    pub factor_units_per_ke: Option<Decimal>,
    #[serde(default, deserialize_with = "opt_amount")]
    pub ratio_g_per_unit: Option<Decimal>,
    #[serde(default, deserialize_with = "opt_amount")]
    pub g3: Option<Decimal>,
    #[serde(default, deserialize_with = "opt_amount")]
    pub carbs_times_isf_total: Option<Decimal>,
    #[serde(default, deserialize_with = "opt_amount")]
    pub carbs_total: Option<Decimal>,
    #[serde(default, deserialize_with = "opt_amount")]
    pub rapid_units_in_window: Option<Decimal>,
    #[serde(default, deserialize_with = "opt_amount")]
    pub delta_mgdl: Option<Decimal>,
    #[serde(default, deserialize_with = "opt_amount")]
    pub insulin_effect_mgdl: Option<Decimal>,
    #[serde(default, deserialize_with = "crate::model::opt_text")]
    pub start: Option<String>,
    #[serde(default, deserialize_with = "lenient")]
    pub end: Option<String>,
    #[serde(default, deserialize_with = "lenient")]
    pub reason: Option<String>,
    #[serde(default, deserialize_with = "lenient")]
    pub end_reason: Option<String>,
    #[serde(default, deserialize_with = "crate::model::opt_int")]
    pub extra_carb_count: Option<i64>,
    #[serde(default, deserialize_with = "lenient")]
    pub manual_window: bool,
    #[serde(default, deserialize_with = "lenient")]
    pub blockers: Vec<Value>,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct StoredQuality {
    #[serde(default, deserialize_with = "lenient")]
    pub carbs_certainty: Option<String>,
    #[serde(default, deserialize_with = "lenient")]
    pub fast: bool,
    #[serde(default, deserialize_with = "lenient")]
    pub fat_protein: bool,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct StoredId {
    #[serde(default, deserialize_with = "lenient")]
    pub id: Option<i64>,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct StoredName {
    #[serde(default, deserialize_with = "lenient")]
    pub name: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct StoredSurcharge {
    #[serde(default, deserialize_with = "opt_amount")]
    pub mgdl: Option<Decimal>,
}

/// A timeline row: only sport sessions are read.
#[derive(Clone, Debug, Default, Deserialize)]
pub struct StoredRow {
    #[serde(default, deserialize_with = "lenient")]
    pub kind: String,
    #[serde(default)]
    pub id: Value,
    #[serde(default, deserialize_with = "lenient")]
    pub at: Option<String>,
    #[serde(default)]
    pub duration_minutes: Value,
    #[serde(default, deserialize_with = "lenient")]
    pub sport_kind: Option<String>,
    #[serde(default, deserialize_with = "lenient")]
    pub notes: String,
}

/// A day's night (day.rs Night) with every field optional: the outcomes share them.
#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct StoredNight {
    #[serde(default, deserialize_with = "lenient", serialize_with = "py_iso_opt")]
    pub start: Option<Instant>,
    #[serde(default, deserialize_with = "lenient", serialize_with = "py_iso_opt")]
    pub end: Option<Instant>,
    #[serde(default, deserialize_with = "lenient", serialize_with = "py_iso_opt")]
    pub dinner_at: Option<Instant>,
    #[serde(default, deserialize_with = "lenient", serialize_with = "py_iso_opt")]
    pub breakfast_at: Option<Instant>,
    #[serde(default, deserialize_with = "lenient", serialize_with = "py_iso_opt")]
    pub last_injection_at: Option<Instant>,
    #[serde(default, deserialize_with = "lenient", serialize_with = "py_iso_opt")]
    pub morning_dose_at: Option<Instant>,
    #[serde(default, deserialize_with = "opt_amount")]
    pub delta_mgdl: Option<Decimal>,
    #[serde(default, deserialize_with = "opt_amount")]
    pub start_glucose: Option<Decimal>,
    #[serde(default, deserialize_with = "opt_amount")]
    pub end_glucose: Option<Decimal>,
    #[serde(default, deserialize_with = "opt_amount")]
    pub minimum_glucose: Option<Decimal>,
    #[serde(default, deserialize_with = "lenient", serialize_with = "py_iso_opt")]
    pub minimum_at: Option<Instant>,
    #[serde(default, deserialize_with = "opt_amount")]
    pub drop_mgdl: Option<Decimal>,
    #[serde(default, deserialize_with = "opt_amount")]
    pub rise_mgdl: Option<Decimal>,
    #[serde(default, deserialize_with = "opt_amount")]
    pub carbs_g: Option<Decimal>,
    #[serde(default, deserialize_with = "lenient")]
    pub reason: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct StoredBasal {
    #[serde(default, deserialize_with = "opt_amount")]
    pub units: Option<Decimal>,
}

/// How many days before today the analysis reads (period, 90-day matrix with lookback, stress tile).
pub fn history_days(days: i64) -> i64 {
    ((days - 1).max(forecast::BACKTEST_DAYS - 1) + forecast::LOOKBACKS[2] + 1).max(forecast::RATIO_DAYS + forecast::RATIO_REFERENCE_DAYS)
}

fn dec_of(value: Option<&Value>) -> Option<Decimal> {
    value.filter(|v| !v.is_null()).and_then(decimal_of)
}

fn instant(value: Option<&str>) -> Option<Instant> {
    value.and_then(|text| chrono::DateTime::parse_from_rfc3339(text).ok()).map(|at| at.with_timezone(&chrono::Utc))
}

fn sorted(mut values: Vec<Decimal>) -> Vec<Decimal> {
    values.sort();
    values
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

/// The misses of every point against the suggestion from the points before it (summary.miss_trend's input).
pub fn point_misses(points: &[MealPoint], configured: &Configured) -> HashMap<String, (Option<Decimal>, Option<Decimal>)> {
    points.iter().map(|point| (point_id(point), suggestion_miss(points, point, configured))).collect()
}

/// Valid, non-excluded meal factors from today back over days_back days (factors.meal_points).
pub fn meal_points(today: NaiveDate, days_back: i64, snapshot: &Snapshot, mode: &str) -> Vec<MealPoint> {
    let own_places: HashSet<i64> = snapshot.places.iter().filter(|place| place.adjust).map(|place| place.id).collect();
    let mut points = Vec::new();
    for (real_day, day) in services::counted_days(today, days_back, &snapshot.vacation_periods, mode) {
        let Some(payload) = snapshot.payloads.get(&real_day.to_string()) else { continue };
        let day_type = services::day_type_for(real_day, snapshot.day_types.as_ref(), &snapshot.vacation_periods).0;
        for meal in &payload.meals {
            let result = &meal.calculation;
            let Some(factor) = result.factor_units_per_ke else { continue };
            if !MEALS.contains(&meal.meal.as_str()) || snapshot.analysis_exclusions.contains(&meal.id) {
                continue;
            }
            let at = instant(meal.at.as_deref())
                .unwrap_or_else(|| real_day.and_time(NaiveTime::from_hms_opt(12, 0, 0).unwrap()).and_utc());
            let place_id = meal.place.as_ref().and_then(|place| place.id);
            points.push(MealPoint {
                id: json!(meal.id), day, real_day: Some(real_day), at,
                key: (meal.meal.clone(), if meal.meal == "breakfast" { day_type.clone() } else { None }),
                day_type: day_type.clone(), value: factor,
                end_glucose: result.g3, carbs_isf: result.carbs_times_isf_total, carbs_g: result.carbs_total,
                fat: meal.quality.fat_protein,
                surcharge_mgdl: meal.fpe_surcharge.as_ref().and_then(|plan| plan.mgdl),
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

/// The typical miss and its direction.
#[derive(Clone, Debug, Default, Serialize)]
pub struct MissStats {
    pub median_abs_deviation_mgdl: Option<Decimal>,
    pub median_deviation_mgdl: Option<Decimal>,
}

impl MissStats {
    fn of(deviations: &[Decimal]) -> Self {
        MissStats {
            median_abs_deviation_mgdl: median(&sorted(deviations.iter().map(|v| v.abs()).collect())),
            median_deviation_mgdl: median(&sorted(deviations.to_vec())),
        }
    }
}

/// Misses with how many meals they rest on.
#[derive(Clone, Debug, Serialize)]
pub struct MissCell {
    pub count: usize,
    #[serde(flatten)]
    pub stats: MissStats,
}

impl MissCell {
    fn of(deviations: &[Decimal]) -> Self {
        MissCell { count: deviations.len(), stats: MissStats::of(deviations) }
    }
}

/// A settings change the page saves with "Übernehmen".
#[derive(Clone, Debug, Default, Serialize)]
pub struct SettingsPatch {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub auto_percentile: Option<Decimal>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub auto_half_life_days: Option<Decimal>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub auto_lookback_days: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub auto_method: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub auto_surcharge_mgdl: Option<Decimal>,
}

impl SettingsPatch {
    /// The change that sets one Automatik setting (`key` as forecast.tuning names it) to `value`.
    fn set(mut self, key: &str, value: Decimal) -> Self {
        match key {
            "percentile" => self.auto_percentile = Some(value),
            "half_life" => self.auto_half_life_days = Some(value),
            "lookback" => self.auto_lookback_days = i64::try_from(value.trunc()).ok(),
            _ if !value.is_zero() => {
                self.auto_method = Some(forecast::SURCHARGE);
                self.auto_surcharge_mgdl = Some(value);
            }
            _ => self.auto_method = Some("percentile"),
        }
        self
    }
}

/// A backtest cell's column: a percentile or the configured percentile with the surcharge.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Column {
    Percentile(Decimal),
    Surcharge,
}

impl Serialize for Column {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        match self {
            Column::Percentile(value) => Serialize::serialize(value, serializer),
            Column::Surcharge => serializer.serialize_str(forecast::SURCHARGE),
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct Shares {
    pub below: Decimal,
    pub in_range: Decimal,
    pub above: Decimal,
}

/// Which best cell: per lookback period or overall.
#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Best {
    Lookback,
    Overall,
}

#[derive(Clone, Debug, Serialize)]
pub struct Cell {
    pub percentile: Column,
    pub stats: VariantStats,
    pub shares: Shares,
    pub active: bool,
    pub best: Option<Best>,
}

#[derive(Clone, Debug, Serialize)]
pub struct MatrixRow {
    pub lookback_days: i64,
    pub half_life_days: Decimal,
    pub cells: Vec<Cell>,
}

/// One option of a tuning row: the value as a number like the page's settings, and its settings change.
#[derive(Clone, Debug, Serialize)]
pub struct TuningOptionView {
    pub value: serde_json::Number,
    pub active: bool,
    #[serde(flatten)]
    pub stats: VariantStats,
    pub best: bool,
    pub apply: SettingsPatch,
}

#[derive(Clone, Debug, Serialize)]
pub struct TuningView {
    pub key: &'static str,
    pub meal_count: usize,
    pub options: Vec<TuningOptionView>,
    #[serde(flatten)]
    pub verdict: Verdict,
}

#[derive(Clone, Debug, Serialize)]
pub struct TradeoffView {
    #[serde(flatten)]
    pub option: TradeoffOption,
    pub apply: SettingsPatch,
}

impl From<TradeoffOption> for TradeoffView {
    fn from(option: TradeoffOption) -> Self {
        let mut apply = SettingsPatch::default();
        for (key, value) in [("lookback", Decimal::from(option.lookback_days)), ("half_life", option.half_life_days),
                             ("percentile", option.percentile), ("surcharge", option.surcharge_mgdl)] {
            if option.changes.contains(&key) {
                apply = apply.set(key, value);
            }
        }
        TradeoffView { option, apply }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct TradeoffsView {
    pub meal_count: usize,
    pub current: TradeoffView,
    pub line: Vec<TradeoffView>,
    pub cloud: Vec<CloudPoint>,
}

#[derive(Clone, Debug, Serialize)]
pub struct Weight {
    pub age_days: i64,
    pub weight: Decimal,
}

/// How the Automatik works and how its options would have done.
#[derive(Clone, Debug, Serialize)]
pub struct Derivation {
    pub percentile: Decimal,
    pub half_life_days: Decimal,
    pub lookback_days: i64,
    pub method: String,
    pub surcharge_mgdl: Decimal,
    pub fat_surcharge_mgdl: Decimal,
    pub surcharge_full_grams: Decimal,
    pub min_meals: usize,
    pub meal_count: usize,
    pub backtest_days: i64,
    pub variants: Vec<MatrixRow>,
    pub glucose_low_mgdl: Decimal,
    pub glucose_high_mgdl: Decimal,
    pub dose_target_mgdl: Decimal,
    pub tuning: Vec<TuningView>,
    pub active_surcharge_mgdl: Decimal,
    pub tradeoffs: TradeoffsView,
    pub examples: Vec<Explanation>,
    pub weights: Vec<Weight>,
}

/// The period-independent part: suggestions, backtest matrix with the Automatik view, day-type ratio
/// and each meal's miss against its own suggestion.
struct Automatik {
    predictions: HashMap<MealKey, Prediction>,
    derivation: Derivation,
    day_type_ratio: Option<DayTypeRatios>,
    misses: HashMap<String, (Option<Decimal>, Option<Decimal>)>,
}

/// The last Automatik part computed, by its inputs: across the five periods of a background round and
/// across rounds while new readings change no meal factor, it is the same.
static AUTOMATIK_CACHE: Mutex<Option<(String, std::sync::Arc<Automatik>)>> = Mutex::new(None);

fn automatik(today: NaiveDate, points: &[MealPoint], settings: &Value, configured: &Configured) -> std::sync::Arc<Automatik> {
    let key = serde_json::to_string(&json!([today.to_string(), settings, to_value(&points)])).unwrap_or_default();
    if let Some((cached_key, cached)) = AUTOMATIK_CACHE.lock().unwrap_or_else(std::sync::PoisonError::into_inner).as_ref() {
        if *cached_key == key {
            return cached.clone();
        }
    }
    let computed = std::sync::Arc::new(compute_automatik(today, points, configured));
    *AUTOMATIK_CACHE.lock().unwrap_or_else(std::sync::PoisonError::into_inner) = Some((key, computed.clone()));
    computed
}

fn compute_automatik(today: NaiveDate, points: &[MealPoint], c: &Configured) -> Automatik {
    let predictions: HashMap<MealKey, Prediction> = c.keys().into_iter().map(|key| {
        let prediction = forecast::predict_method(points, &key, today, c.percentile, c.half_life, c.lookback, &c.method,
                                                  c.surcharge_mgdl, c.fat_mgdl);
        (key, prediction)
    }).collect();
    // Backtest matrix: every lookback × half-life (plus the configured ones), each at 25 %, the configured
    // percentile and 50 %, plus the configured percentile with the surcharge, on the meals of the last 90 days.
    let percentiles: Vec<Decimal> = BTreeSet::from([dec!(25), c.percentile, dec!(50)]).into_iter().collect();
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
    let mut stats = test.stats.into_iter();
    let columns: Vec<Column> = percentiles.iter().map(|p| Column::Percentile(*p)).chain([Column::Surcharge]).collect();
    let mut matrix: Vec<MatrixRow> = rows.iter().map(|(lookback, half_life)| MatrixRow {
        lookback_days: *lookback, half_life_days: *half_life,
        cells: columns.iter().map(|column| cell(*column, stats.next().unwrap_or_default(), *lookback, *half_life, c)).collect(),
    }).collect();
    mark_best(&mut matrix);
    let active = c.active_surcharge();
    let tuning: Vec<TuningView> = forecast::tuning(points, since, c.lookback, c.half_life, c.percentile, active, c.target, c.fat_mgdl)
        .into_iter().map(|row| TuningView {
            key: row.key,
            meal_count: row.meal_count,
            options: row.options.into_iter().map(|option| TuningOptionView {
                // A number like the page's settings, not text; the lookback in whole days.
                value: if row.key == "lookback" {
                    serde_json::Number::from(i64::try_from(option.value.trunc()).unwrap_or(0))
                } else {
                    option.value.normalize().to_string().parse().unwrap_or_else(|_| serde_json::Number::from(0))
                },
                active: option.active,
                stats: option.stats,
                best: option.best,
                apply: SettingsPatch::default().set(row.key, option.value),
            }).collect(),
            verdict: row.verdict,
        }).collect();
    let tradeoffs = forecast::tradeoffs(points, since, c.lookback, c.half_life, c.percentile, active, c.target, c.fat_mgdl);
    let mut ages: BTreeSet<i64> = BTreeSet::from([1, c.lookback]);
    for k in [1, 2, 4] {
        ages.insert(i64::try_from((c.half_life * Decimal::from(k)).trunc()).unwrap_or(0));
    }
    let derivation = Derivation {
        percentile: c.percentile, half_life_days: c.half_life, lookback_days: c.lookback, method: c.method.clone(),
        surcharge_mgdl: c.surcharge_mgdl, fat_surcharge_mgdl: c.fat_mgdl, surcharge_full_grams: forecast::SURCHARGE_FULL_GRAMS,
        min_meals: forecast::MIN_MEALS, meal_count: test.rows.len(), backtest_days: forecast::BACKTEST_DAYS,
        variants: matrix,
        glucose_low_mgdl: forecast::GLUCOSE_LOW, glucose_high_mgdl: forecast::GLUCOSE_HIGH, dose_target_mgdl: c.target,
        tuning,
        active_surcharge_mgdl: active,
        tradeoffs: TradeoffsView {
            meal_count: tradeoffs.meal_count,
            current: tradeoffs.current.into(),
            line: tradeoffs.line.into_iter().map(TradeoffView::from).collect(),
            cloud: tradeoffs.cloud,
        },
        examples: c.keys().iter().map(|key| forecast::explain(points, key, today, c.lookback, c.half_life, c.percentile, active, c.fat_mgdl)).collect(),
        weights: ages.into_iter().filter(|age| (1..=c.lookback).contains(age))
            .map(|age| Weight { age_days: age, weight: forecast::weight(age, c.half_life) }).collect(),
    };
    let day_type_ratio = c.day_types.then(|| forecast::day_type_ratios(points, today));
    let misses = point_misses(points, c);
    Automatik { predictions, derivation, day_type_ratio, misses }
}

fn point_id(point: &MealPoint) -> String {
    point.id.as_str().map(str::to_string).unwrap_or_else(|| point.id.to_string())
}

/// A backtest cell with its shares below / in / above range and whether it is the configured variant.
fn cell(column: Column, stats: VariantStats, lookback: i64, half_life: Decimal, c: &Configured) -> Cell {
    let share = |count: usize| if stats.glucose_count > 0 {
        Decimal::from(count) * dec!(100) / Decimal::from(stats.glucose_count)
    } else {
        Decimal::ZERO
    };
    let configured_column = if c.surcharge() { column == Column::Surcharge } else { column == Column::Percentile(c.percentile) };
    Cell {
        percentile: column,
        shares: Shares { below: share(stats.below), in_range: share(stats.in_range), above: share(stats.above) },
        active: configured_column && lookback == c.lookback && half_life == c.half_life,
        stats,
        best: None,
    }
}

/// The best cells: most endpoints in range, then fewest below, per lookback period and overall (the
/// first of equal cells wins).
fn mark_best(matrix: &mut [MatrixRow]) {
    let better = |cell: &Cell, top: Option<&Cell>| match top {
        None => true,
        Some(best) => cell.stats.in_range > best.stats.in_range
            || (cell.stats.in_range == best.stats.in_range && cell.stats.below < best.stats.below),
    };
    let at = |matrix: &[MatrixRow], (row, column): (usize, usize)| -> Cell { matrix[row].cells[column].clone() };
    let mut overall: Option<(usize, usize)> = None;
    let mut per_lookback: Vec<(i64, (usize, usize))> = Vec::new();
    for (ri, row) in matrix.iter().enumerate() {
        for (ci, cell) in row.cells.iter().enumerate() {
            if better(cell, overall.map(|place| at(matrix, place)).as_ref()) {
                overall = Some((ri, ci));
            }
            let current = per_lookback.iter().find(|(l, _)| *l == row.lookback_days).map(|(_, place)| *place);
            if better(cell, current.map(|place| at(matrix, place)).as_ref()) {
                match per_lookback.iter_mut().find(|(l, _)| *l == row.lookback_days) {
                    Some(entry) => entry.1 = (ri, ci),
                    None => per_lookback.push((row.lookback_days, (ri, ci))),
                }
            }
        }
    }
    for (_, (row, column)) in per_lookback {
        matrix[row].cells[column].best = Some(Best::Lookback);
    }
    if let Some((row, column)) = overall {
        matrix[row].cells[column].best = Some(Best::Overall);
    }
}

/// The Automatik suggestion of a meal kind as the page shows it.
#[derive(Clone, Debug, Serialize)]
pub struct Suggestion {
    pub auto_units_per_ke: Option<Decimal>,
    pub count: usize,
    pub days: i64,
    pub min_meals: usize,
    pub percentile: Decimal,
    pub half_life_days: Decimal,
    pub surcharge: bool,
    pub surcharge_mgdl: Decimal,
    pub surcharge_full_grams: Decimal,
}

impl Suggestion {
    fn of(prediction: &Prediction, c: &Configured) -> Self {
        Suggestion {
            auto_units_per_ke: prediction.units_per_ke, count: prediction.count, days: c.lookback, min_meals: forecast::MIN_MEALS,
            percentile: c.percentile, half_life_days: c.half_life, surcharge: c.surcharge(), surcharge_mgdl: c.surcharge_mgdl,
            surcharge_full_grams: forecast::SURCHARGE_FULL_GRAMS,
        }
    }
}

/// Mean, median, range and (from five meals) quartiles of the factors.
#[derive(Clone, Debug, Default, Serialize)]
pub struct FactorStats {
    pub mean_units_per_ke: Option<Decimal>,
    pub median_units_per_ke: Option<Decimal>,
    pub min_units_per_ke: Option<Decimal>,
    pub max_units_per_ke: Option<Decimal>,
    pub q1_units_per_ke: Option<Decimal>,
    pub q3_units_per_ke: Option<Decimal>,
}

impl FactorStats {
    /// From sorted values.
    fn of(values: &[Decimal]) -> Self {
        // With fewer than five meals, quartiles imply more precision than the data support.
        let enough = values.len() >= 5;
        FactorStats {
            mean_units_per_ke: (!values.is_empty()).then(|| values.iter().copied().sum::<Decimal>() / Decimal::from(values.len())),
            median_units_per_ke: median(values),
            min_units_per_ke: values.first().copied(),
            max_units_per_ke: values.last().copied(),
            q1_units_per_ke: enough.then(|| quartile(values, dec!(0.25))),
            q3_units_per_ke: enough.then(|| quartile(values, dec!(0.75))),
        }
    }
}

/// One meal in the analysis list.
#[derive(Clone, Debug, Serialize)]
pub struct MealItem {
    pub id: String,
    pub day: String,
    pub at: Option<String>,
    pub day_type: Option<String>,
    pub units_per_ke: Option<Decimal>,
    pub carbs_total: Option<Decimal>,
    pub rapid_units_in_window: Option<Decimal>,
    pub delta_mgdl: Option<Decimal>,
    pub end: Option<String>,
    pub reason: Option<String>,
    pub manual_window: bool,
    pub excluded: bool,
    pub sport: Value,
    pub certainty: Option<String>,
    pub fast: bool,
    pub dish: Option<String>,
    pub expected_delta_mgdl: Option<Decimal>,
    pub deviation_mgdl: Option<Decimal>,
    pub expected_units_per_ke: Option<Decimal>,
    pub strong_outlier: bool,
}

/// Breakfasts of one day type.
#[derive(Clone, Debug, Serialize)]
pub struct DayTypeGroup {
    pub total: usize,
    pub count: usize,
    pub valid_count: usize,
    pub excluded_count: usize,
    pub blocked_count: usize,
    #[serde(flatten)]
    pub stats: FactorStats,
    pub suggestion: Suggestion,
}

/// One meal kind over the period.
#[derive(Clone, Debug, Default, Serialize)]
pub struct MealGroup {
    pub total: usize,
    pub count: usize,
    pub valid_count: usize,
    pub excluded_count: usize,
    pub blocked_count: usize,
    pub multi_carb_count: usize,
    pub shortened_count: usize,
    pub strong_outlier_count: usize,
    #[serde(flatten)]
    pub stats: FactorStats,
    pub median_g_per_unit: Option<Decimal>,
    pub recent_median_units_per_ke: Option<Decimal>,
    pub previous_median_units_per_ke: Option<Decimal>,
    pub trend_units_per_ke: Option<Decimal>,
    #[serde(flatten)]
    pub misses: MissStats,
    pub expected_count: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub suggestion: Option<Suggestion>,
    pub nightscout_units_per_ke: Vec<Decimal>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub by_day_type: Option<BTreeMap<&'static str, DayTypeGroup>>,
    pub calculations: Vec<MealItem>,
}

#[derive(Clone, Debug, Serialize)]
pub struct Misses {
    pub certainty: BTreeMap<String, MissCell>,
    pub fast: BTreeMap<String, MissCell>,
}

/// A kind's sport effect with the settings change "Übernehmen" saves.
#[derive(Clone, Debug, Serialize)]
pub struct KindApply {
    pub standard_drop_mgdl: Option<Decimal>,
    pub apply: Option<Value>,
}

/// The analysis page.
#[derive(Clone, Debug, Serialize)]
pub struct Summary {
    pub days: i64,
    pub groups: BTreeMap<&'static str, MealGroup>,
    pub misses: Misses,
    pub trend: Trend,
    pub profile_sync_error: Option<String>,
    pub suggestion_derivation: Derivation,
    pub day_types: bool,
    pub day_type_ratio: Option<DayTypeRatios>,
    pub sport_analysis: SportAnalysis,
    pub night: NightOverview,
    pub quiet: QuietOverview,
}

/// The analysis page for the last `days` days.
pub fn compute_summary(snapshot: &Snapshot) -> Res<Summary> {
    let today = snapshot.today;
    let days = snapshot.days;
    if !PERIODS.contains(&days) {
        return Err(CoreError::Value(t("Ungültiger Zeitraum")));
    }
    let settings = &snapshot.settings;
    let c = Configured::of(settings)?;
    let day_types = snapshot.day_types.as_ref();
    let mut groups: BTreeMap<&'static str, MealGroup> = MEALS.iter().map(|name| (*name, MealGroup::default())).collect();
    let mut values: HashMap<&str, (Vec<Decimal>, Vec<Decimal>)> = MEALS.iter().map(|name| (*name, (Vec::new(), Vec::new()))).collect();
    let mut calculations: HashMap<&str, Vec<MealItem>> = MEALS.iter().map(|name| (*name, Vec::new())).collect();
    let mut night_records = Vec::new();
    let mut quiet_records = Vec::new();
    let mut sport_meals = Vec::new();
    let mut sport_sessions = Vec::new();
    let empty = StoredDay::default();
    for offset in 0..days {
        let day = today - Duration::days(offset);
        let key = day.to_string();
        let payload = snapshot.payloads.get(&key).unwrap_or(&empty);
        night_records.push(NightRecord {
            day: key.clone(), night: payload.night.clone(),
            basal_units: (!payload.basal.is_empty()).then(|| payload.basal.iter().filter_map(|dose| dose.units).sum()),
        });
        quiet_records.extend(payload.quiet_periods.iter().map(|period| QuietRecord { day: key.clone(), period: period.clone() }));
        let day_type = services::day_type_for(day, day_types, &snapshot.vacation_periods).0;
        sport_sessions.extend(payload.timeline.iter().filter(|row| row.kind == "exercise").map(|row| SportSessionIn {
            id: row.id.clone(), at: row.at.clone(), duration_minutes: dec_of(Some(&row.duration_minutes)),
            kind: row.sport_kind.clone(), notes: row.notes.clone(),
        }));
        for meal in &payload.meals {
            let excluded = snapshot.analysis_exclusions.contains(&meal.id);
            let result = &meal.calculation;
            sport_meals.push(SportMeal {
                id: meal.id.clone(), day: Some(key.clone()), meal: meal.meal.clone(), at: meal.at.clone(), excluded,
                calculation: result.clone(), sport: serde_json::from_value(meal.sport.clone()).unwrap_or_default(),
            });
            let Some(name) = MEALS.iter().find(|name| meal.meal == **name).copied() else { continue };
            let group = groups.get_mut(name).unwrap();
            group.total += 1;
            if result.end_reason.as_deref() == Some("before_next_meal") {
                group.shortened_count += 1;
            }
            if result.extra_carb_count.unwrap_or(0) != 0 {
                group.multi_carb_count += 1;
            }
            match result.factor_units_per_ke {
                None => group.blocked_count += 1,
                Some(value) => {
                    group.valid_count += 1;
                    if excluded {
                        group.excluded_count += 1;
                    } else {
                        group.count += 1;
                        let entry = values.get_mut(name).unwrap();
                        entry.0.push(value);
                        entry.1.push(result.ratio_g_per_unit.unwrap_or_default());
                    }
                }
            }
            calculations.get_mut(name).unwrap().push(MealItem {
                id: meal.id.clone(), day: key.clone(), at: meal.at.clone(), day_type: day_type.clone(),
                units_per_ke: result.factor_units_per_ke, carbs_total: result.carbs_total,
                rapid_units_in_window: result.rapid_units_in_window, delta_mgdl: result.delta_mgdl,
                end: result.end.clone(), reason: result.reason.clone(), manual_window: result.manual_window, excluded,
                sport: meal.sport.clone(), certainty: meal.quality.carbs_certainty.clone(), fast: meal.quality.fast,
                dish: meal.dish.as_ref().and_then(|dish| dish.name.clone()),
                expected_delta_mgdl: None, deviation_mgdl: None, expected_units_per_ke: None, strong_outlier: false,
            });
        }
    }
    for name in MEALS {
        let items = calculations.get_mut(name).unwrap();
        let comparable: Vec<usize> = (0..items.len()).filter(|i| items[*i].units_per_ke.is_some()).collect();
        let sets: Vec<Vec<usize>> = if name == "breakfast" && day_types.is_some() {
            ["work", "free"].iter().map(|kind| comparable.iter().copied().filter(|i| items[*i].day_type.as_deref() == Some(*kind)).collect()).collect()
        } else {
            vec![comparable.clone()]
        };
        for set in sets {
            let factors: Vec<Decimal> = set.iter().filter_map(|i| items[*i].units_per_ke).collect();
            for (index, flag) in set.iter().zip(strong_outlier_flags(&factors)) {
                items[*index].strong_outlier = flag;
            }
        }
        let group = groups.get_mut(name).unwrap();
        group.strong_outlier_count = comparable.iter().filter(|i| items[**i].strong_outlier).count();
        let (own, nightscout) = values.remove(name).unwrap();
        group.stats = FactorStats::of(&sorted(own));
        group.median_g_per_unit = median(&sorted(nightscout));
        let mut chronological: Vec<&MealItem> = items.iter().filter(|item| item.units_per_ke.is_some() && !item.excluded).collect();
        chronological.sort_by(|a, b| b.at.cmp(&a.at));
        if chronological.len() >= 6 {
            let factors = |items: &[&MealItem]| median(&sorted(items.iter().filter_map(|item| item.units_per_ke).collect())).unwrap();
            let (recent, previous) = (factors(&chronological[..3]), factors(&chronological[3..6]));
            group.recent_median_units_per_ke = Some(recent);
            group.previous_median_units_per_ke = Some(previous);
            group.trend_units_per_ke = Some(recent - previous);
        }
    }
    let mode = settings.get("auto_vacation").and_then(Value::as_str).unwrap_or(services::DEFAULT_AUTO_VACATION);
    let points = snapshot.points.clone().unwrap_or_else(|| meal_points(today, history_days(days), snapshot, mode));
    let part = automatik(today, &points, settings, &c);
    let by_day_type: Option<BTreeMap<&'static str, DayTypeGroup>> = day_types.map(|_| ["work", "free"].into_iter().map(|kind| {
        let mine: Vec<&MealItem> = calculations["breakfast"].iter().filter(|item| item.day_type.as_deref() == Some(kind)).collect();
        let kind_values = sorted(mine.iter().filter(|item| !item.excluded).filter_map(|item| item.units_per_ke).collect());
        let key: MealKey = ("breakfast".into(), Some(kind.into()));
        (kind, DayTypeGroup {
            total: mine.len(),
            count: kind_values.len(),
            valid_count: mine.iter().filter(|item| item.units_per_ke.is_some()).count(),
            excluded_count: mine.iter().filter(|item| item.units_per_ke.is_some() && item.excluded).count(),
            blocked_count: mine.iter().filter(|item| item.units_per_ke.is_none()).count(),
            stats: FactorStats::of(&kind_values),
            suggestion: Suggestion::of(&part.predictions[&key], &c),
        })
    }).collect());
    let known: HashSet<String> = points.iter().map(point_id).collect();
    // Erwartet / Delta per meal, the typical miss per meal kind, carb certainty and "Schnell" mark.
    let mut certainty: BTreeMap<String, Vec<Decimal>> = BTreeMap::new();
    let mut fast: BTreeMap<String, Vec<Decimal>> = BTreeMap::new();
    for name in MEALS {
        let items = calculations.get_mut(name).unwrap();
        for item in items.iter_mut() {
            let Some(delta) = item.delta_mgdl else { continue };
            if !known.contains(&item.id) {
                continue;
            }
            let (Some(suggested), Some(deviation)) = part.misses.get(&item.id).copied().unwrap_or((None, None)) else { continue };
            item.expected_units_per_ke = Some(suggested);
            item.expected_delta_mgdl = Some(delta - deviation);
            item.deviation_mgdl = Some(deviation);
            certainty.entry(item.certainty.clone().unwrap_or_else(|| "none".into())).or_default().push(deviation);
            fast.entry(if item.fast { "fast" } else { "other" }.into()).or_default().push(deviation);
        }
        let deviations: Vec<Decimal> = items.iter().filter_map(|item| item.deviation_mgdl).collect();
        let group = groups.get_mut(name).unwrap();
        group.misses = MissStats::of(&deviations);
        group.expected_count = deviations.len();
    }
    let levels = |list: BTreeMap<String, Vec<Decimal>>| list.into_iter().map(|(key, values)| (key, MissCell::of(&values))).collect();
    let misses = Misses { certainty: levels(certainty), fast: levels(fast) };
    let trend = miss_trend(&points, today, &part.misses);
    let named: &[&str] = if day_types.is_some() { &["lunch", "dinner"] } else { &MEALS };
    for name in named {
        let key: MealKey = (name.to_string(), None);
        groups.get_mut(name).unwrap().suggestion = Some(Suggestion::of(&part.predictions[&key], &c));
    }
    let timezone = settings.get("timezone").and_then(Value::as_str).unwrap_or("UTC");
    let ic_schedule: Vec<crate::model::IsfRow> = serde_json::from_value(settings.get("ic_schedule").cloned().unwrap_or(json!([]))).unwrap_or_default();
    for name in MEALS {
        let mut current: Vec<Decimal> = Vec::new();
        if !ic_schedule.is_empty() {
            for item in &calculations[name] {
                if let Some(at) = instant(item.at.as_deref()) {
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
        group.nightscout_units_per_ke = current;
        if name == "breakfast" {
            group.by_day_type = Some(by_day_type.clone().unwrap_or_default());
        }
        group.calculations = calculations.remove(name).unwrap_or_default();
    }
    let names = sport_names(settings);
    let standards: HashMap<String, Decimal> = settings.get("sport_effects_mgdl").and_then(Value::as_object).into_iter().flatten()
        .filter_map(|(kind, value)| decimal_of(value).map(|v| (kind.clone(), v))).collect();
    let lookback_minutes = dec_of(settings.get("sport_lookback_minutes")).and_then(|v| i64::try_from(v.trunc()).ok()).unwrap_or(360);
    let window_minutes = dec_of(settings.get("window_minutes")).and_then(|v| i64::try_from(v.trunc()).ok()).unwrap_or(180);
    let mut sport = sport_analysis(&sport_meals, &sport_sessions, lookback_minutes, window_minutes, &names, &standards, &snapshot.own_sport_sessions);
    for group in sport.kinds.iter_mut() {
        let apply = group.suggested_drop_mgdl.map(|suggested| {
            let mut effects = settings.get("sport_effects_mgdl").cloned().unwrap_or(json!({}));
            if let (Some(map), Some(kind)) = (effects.as_object_mut(), group.kind.clone()) {
                map.insert(kind, Value::String(suggested.to_string()));
            }
            json!({"sport_effects_mgdl": effects})
        });
        group.apply = Some(KindApply { standard_drop_mgdl: group.kind.as_ref().and_then(|k| standards.get(k)).copied(), apply });
    }
    Ok(Summary {
        days,
        groups,
        misses,
        trend,
        profile_sync_error: None,
        suggestion_derivation: part.derivation.clone(),
        day_types: day_types.is_some(),
        day_type_ratio: part.day_type_ratio.clone(),
        sport_analysis: sport,
        night: night_overview(night_records, days),
        quiet: quiet_overview(quiet_records),
    })
}

#[derive(Clone, Debug, Serialize)]
pub struct TrendWeek {
    pub start: String,
    pub end: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct TrendKind {
    pub weeks: Vec<MissCell>,
    /// "up" or "down" when the last weeks all missed the same way.
    pub drift: Option<&'static str>,
}

#[derive(Clone, Debug, Serialize)]
pub struct Trend {
    pub weeks: Vec<TrendWeek>,
    pub kinds: BTreeMap<&'static str, TrendKind>,
}

/// The miss week by week over the last TREND_WEEKS weeks before today, per meal kind; `drift` names a
/// direction when the last DRIFT_WEEKS weeks with meals all missed the same way by at least DRIFT_MGDL.
pub fn miss_trend(points: &[MealPoint], today: NaiveDate, misses: &HashMap<String, (Option<Decimal>, Option<Decimal>)>) -> Trend {
    let weeks: Vec<TrendWeek> = (0..TREND_WEEKS).rev().map(|index| TrendWeek {
        start: (today - Duration::days(7 * (index + 1))).to_string(),
        end: (today - Duration::days(7 * index + 1)).to_string(),
    }).collect();
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
    let kinds = MEALS.iter().map(|meal| {
        let cells: Vec<MissCell> = buckets[meal].iter().map(|values| MissCell::of(values)).collect();
        let medians: Vec<Decimal> = cells.iter().filter(|cell| cell.count > 0).filter_map(|cell| cell.stats.median_deviation_mgdl).collect();
        let recent = &medians[medians.len().saturating_sub(DRIFT_WEEKS)..];
        let drift = if recent.len() == DRIFT_WEEKS && recent.iter().all(|v| *v >= DRIFT_MGDL) { Some("up") }
            else if recent.len() == DRIFT_WEEKS && recent.iter().all(|v| *v <= -DRIFT_MGDL) { Some("down") }
            else { None };
        (*meal, TrendKind { weeks: cells, drift })
    }).collect();
    Trend { weeks, kinds }
}

/// A day's night with that day's basal.
#[derive(Clone, Debug, Serialize)]
pub struct NightRecord {
    pub day: String,
    #[serde(flatten)]
    pub night: StoredNight,
    pub basal_units: Option<Decimal>,
}

#[derive(Clone, Debug, Serialize)]
pub struct NightOverview {
    pub count: usize,
    pub with_carbs_count: usize,
    pub unavailable_count: i64,
    pub median_start_glucose: Option<Decimal>,
    pub median_end_glucose: Option<Decimal>,
    pub median_minimum_glucose: Option<Decimal>,
    pub median_delta_mgdl: Option<Decimal>,
    pub median_drop_mgdl: Option<Decimal>,
    pub median_rise_mgdl: Option<Decimal>,
    pub nights: Vec<NightRecord>,
}

fn night_overview(records: Vec<NightRecord>, days: i64) -> NightOverview {
    let complete: Vec<&StoredNight> = records.iter().map(|record| &record.night)
        .filter(|night| night.minimum_glucose.is_some() && night.start_glucose.is_some()).collect();
    // Nights with carbs in them are shown but left out of the medians.
    let clean: Vec<&&StoredNight> = complete.iter().filter(|night| night.carbs_g.unwrap_or_default().is_zero()).collect();
    let middle = |pick: fn(&StoredNight) -> Option<Decimal>| median(&sorted(clean.iter().filter_map(|night| pick(night)).collect()));
    NightOverview {
        count: clean.len(),
        with_carbs_count: complete.len() - clean.len(),
        unavailable_count: days - complete.len() as i64,
        median_start_glucose: middle(|night| night.start_glucose),
        median_end_glucose: middle(|night| night.end_glucose),
        median_minimum_glucose: middle(|night| night.minimum_glucose),
        median_delta_mgdl: middle(|night| night.delta_mgdl),
        median_drop_mgdl: middle(|night| night.drop_mgdl),
        median_rise_mgdl: middle(|night| night.rise_mgdl),
        nights: records,
    }
}

/// A stretch without bolus and food with its day.
#[derive(Clone, Debug, Serialize)]
pub struct QuietRecord {
    pub day: String,
    #[serde(flatten)]
    pub period: QuietPeriod,
}

#[derive(Clone, Debug, Serialize)]
pub struct QuietOverview {
    pub count: usize,
    pub hours: Decimal,
    pub median_rate_mgdl_per_hour: Option<Decimal>,
    pub periods: Vec<QuietRecord>,
}

fn quiet_overview(mut records: Vec<QuietRecord>) -> QuietOverview {
    let rates = sorted(records.iter().map(|item| item.period.rate_mgdl_per_hour).collect());
    let hours: Decimal = records.iter().map(|item| item.period.hours).sum();
    records.sort_by_key(|record| std::cmp::Reverse(record.period.start));
    QuietOverview { count: records.len(), hours, median_rate_mgdl_per_hour: median(&rates), periods: records }
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
fn raw_factor(result: &StoredCalc) -> Option<Decimal> {
    if !result.blockers.is_empty() || result.start.is_none() {
        return None;
    }
    let carb_effect = result.carbs_times_isf_total?;
    let (delta, insulin) = (result.delta_mgdl?, result.insulin_effect_mgdl?);
    if carb_effect <= Decimal::ZERO {
        return None;
    }
    Some(dec!(10) * (delta + insulin) / carb_effect)
}

/// The kind's standard drop that would have matched what the meals measured (sport_analysis.suggested_drop).
pub fn suggested_drop(standard: Option<Decimal>, ratios: &[Decimal]) -> Option<Decimal> {
    let standard = standard.filter(|v| !v.is_zero())?;
    if ratios.len() < SPORT_SUGGEST_MIN_MEALS {
        return None;
    }
    let ratio = sport_median(ratios)?.clamp(Decimal::ZERO, SPORT_SUGGEST_MAX_RATIO);
    let rounded = (standard * ratio / dec!(5)).round_dp_with_strategy(0, rust_decimal::RoundingStrategy::MidpointNearestEven) * dec!(5);
    let value = rounded.clamp(dec!(-500), dec!(500));
    let value = if value.is_zero() { Decimal::ZERO } else { value.normalize() };
    (value != standard).then_some(value)
}

/// A meal as the sport analysis reads it.
#[derive(Clone, Debug, Default, Deserialize)]
pub struct SportMeal {
    #[serde(default, deserialize_with = "crate::model::text")]
    pub id: String,
    #[serde(default, deserialize_with = "lenient")]
    pub day: Option<String>,
    #[serde(default, deserialize_with = "lenient")]
    pub meal: String,
    #[serde(default, deserialize_with = "lenient")]
    pub at: Option<String>,
    #[serde(default, deserialize_with = "lenient")]
    pub excluded: bool,
    #[serde(default, deserialize_with = "lenient")]
    pub calculation: StoredCalc,
    #[serde(default, deserialize_with = "lenient")]
    pub sport: StoredSport,
}

/// The sport part of a stored meal.
#[derive(Clone, Debug, Default, Deserialize)]
pub struct StoredSport {
    #[serde(default, deserialize_with = "lenient")]
    pub status: Option<String>,
    #[serde(default, deserialize_with = "opt_amount")]
    pub effect_mgdl: Option<Decimal>,
}

/// A sport session as the sport analysis reads it.
#[derive(Clone, Debug, Default, Deserialize)]
pub struct SportSessionIn {
    #[serde(default)]
    pub id: Value,
    #[serde(default, deserialize_with = "lenient")]
    pub at: Option<String>,
    #[serde(default, deserialize_with = "opt_amount")]
    pub duration_minutes: Option<Decimal>,
    #[serde(default, deserialize_with = "lenient")]
    pub kind: Option<String>,
    #[serde(default, deserialize_with = "lenient")]
    pub notes: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct Baseline {
    pub median_units_per_ke: Option<Decimal>,
    pub count: usize,
}

/// A meal near a sport session: what it would have needed without sport against what it measured.
#[derive(Clone, Debug, Serialize)]
pub struct InfluencedMeal {
    pub id: String,
    pub day: Option<String>,
    pub meal: String,
    #[serde(serialize_with = "py_iso")]
    pub start: Instant,
    #[serde(serialize_with = "py_iso")]
    pub end: Instant,
    pub excluded: bool,
    pub carbs_total: Option<Decimal>,
    pub rapid_units_in_window: Option<Decimal>,
    pub delta_mgdl: Option<Decimal>,
    pub expected_delta_mgdl: Option<Decimal>,
    pub effect_mgdl: Option<Decimal>,
    pub factor_without_sport_units_per_ke: Option<Decimal>,
    pub baseline_units_per_ke: Option<Decimal>,
    pub confirmed_effect_mgdl: Option<Decimal>,
    pub applied_mgdl: Option<Decimal>,
    pub shared: bool,
    pub session_ids: Vec<Value>,
    pub reason: Option<String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct SessionView {
    pub id: Value,
    pub kind: Option<String>,
    pub label: String,
    #[serde(serialize_with = "py_iso")]
    pub start: Instant,
    #[serde(serialize_with = "py_iso_opt")]
    pub end: Option<Instant>,
    pub duration_minutes: Option<i64>,
    pub notes: String,
    pub meals: Vec<InfluencedMeal>,
}

/// What the meals measured per sport kind against what the factors assumed.
#[derive(Clone, Debug, Serialize)]
pub struct SportKindView {
    pub kind: Option<String>,
    pub label: String,
    pub session_count: usize,
    pub meal_count: usize,
    pub suggested_drop_mgdl: Option<Decimal>,
    pub median_applied_mgdl: Option<Decimal>,
    pub effect_count: usize,
    pub median_effect_mgdl: Option<Decimal>,
    pub min_effect_mgdl: Option<Decimal>,
    pub max_effect_mgdl: Option<Decimal>,
    /// Only on the analysis page.
    #[serde(flatten, skip_serializing_if = "Option::is_none")]
    pub apply: Option<KindApply>,
}

#[derive(Clone, Debug, Serialize)]
pub struct SportAnalysis {
    pub baselines: BTreeMap<&'static str, Baseline>,
    pub kinds: Vec<SportKindView>,
    pub sessions: Vec<SessionView>,
    pub lookback_minutes: i64,
}

/// Link sport sessions to meals and estimate each meal's sport effect (sport_analysis.analyze).
pub fn sport_analysis(meals: &[SportMeal], exercises: &[SportSessionIn], lookback_minutes: i64, window_minutes: i64,
                      labels: &HashMap<String, String>, standards: &HashMap<String, Decimal>, own_sessions: &HashSet<String>) -> SportAnalysis {
    struct Session<'a> { item: &'a SportSessionIn, start: Instant, end: Instant, duration: i64, meal_ids: Vec<String> }
    let mut sessions: Vec<Session> = exercises.iter().filter_map(|item| {
        let start = instant(item.at.as_deref())?;
        let duration = item.duration_minutes.map(|v| i64::try_from(v.trunc()).unwrap_or(0)).unwrap_or(0);
        Some(Session { item, start, end: start + Duration::minutes(duration), duration, meal_ids: Vec::new() })
    }).collect();
    sessions.sort_by_key(|session| session.start);
    let lookback = Duration::minutes(lookback_minutes);
    struct Prepared<'a> { meal: &'a SportMeal, start: Instant, end: Instant, sessions: Vec<usize>, confirmed: bool, factor: Option<Decimal> }
    let mut prepared = Vec::new();
    for meal in meals {
        let result = &meal.calculation;
        let Some(start) = instant(meal.at.as_deref()) else { continue };
        let end = instant(result.end.as_deref()).unwrap_or(start + Duration::minutes(window_minutes));
        let linked: Vec<usize> = (0..sessions.len()).filter(|i| sessions[*i].end >= start - lookback && sessions[*i].start < end).collect();
        for index in &linked {
            sessions[*index].meal_ids.push(meal.id.clone());
        }
        prepared.push(Prepared { meal, start, end, sessions: linked, confirmed: meal.sport.status.as_deref() == Some("affected"),
                                 factor: raw_factor(result) });
    }
    let baselines: BTreeMap<&'static str, Baseline> = MEALS.iter().map(|kind| {
        let found: Vec<Decimal> = prepared.iter().filter(|item| item.meal.meal == *kind && item.sessions.is_empty() && !item.confirmed
                && !item.meal.excluded).filter_map(|item| item.factor).filter(|f| *f > Decimal::ZERO).collect();
        (*kind, Baseline { median_units_per_ke: sport_median(&found), count: found.len() })
    }).collect();
    let mut influenced: Vec<InfluencedMeal> = Vec::new();
    for item in &prepared {
        if item.sessions.is_empty() && !item.confirmed {
            continue;
        }
        let meal = item.meal;
        let result = &meal.calculation;
        let baseline = baselines.get(meal.meal.as_str()).and_then(|b| b.median_units_per_ke);
        let (mut expected, mut effect, mut reason) = (None, None, None);
        if item.factor.is_none() {
            reason = Some(result.reason.clone().filter(|r| !r.is_empty()).unwrap_or_else(|| t("Mahlzeit nicht berechenbar")));
        } else if let Some(base) = baseline {
            let value = base * result.carbs_times_isf_total.unwrap_or_default() / dec!(10) - result.insulin_effect_mgdl.unwrap_or_default();
            expected = Some(value);
            effect = Some(result.delta_mgdl.unwrap_or_default() - value);
        } else {
            reason = Some(t("Keine Vergleichsmahlzeiten ohne Sport"));
        }
        influenced.push(InfluencedMeal {
            id: meal.id.clone(), day: meal.day.clone(), meal: meal.meal.clone(), start: item.start, end: item.end,
            excluded: meal.excluded, carbs_total: result.carbs_total, rapid_units_in_window: result.rapid_units_in_window,
            delta_mgdl: result.delta_mgdl, expected_delta_mgdl: expected, effect_mgdl: effect,
            factor_without_sport_units_per_ke: item.factor, baseline_units_per_ke: baseline,
            confirmed_effect_mgdl: if item.confirmed { meal.sport.effect_mgdl } else { None },
            applied_mgdl: meal.sport.effect_mgdl.map(|v| -v),
            shared: item.sessions.len() > 1,
            session_ids: item.sessions.iter().map(|i| sessions[*i].item.id.clone()).collect(),
            reason,
        });
    }
    let mut rendered: Vec<SessionView> = sessions.iter().map(|session| SessionView {
        id: session.item.id.clone(),
        kind: session.item.kind.clone(),
        label: session.item.kind.as_ref().and_then(|k| labels.get(k)).cloned().unwrap_or_else(|| t("Sportart unbekannt")),
        start: session.start,
        end: Some(session.end),
        duration_minutes: Some(session.duration),
        notes: session.item.notes.clone(),
        meals: session.meal_ids.iter().filter_map(|id| influenced.iter().find(|meal| meal.id == *id)).cloned().collect(),
    }).collect();
    for meal in &influenced {
        if meal.session_ids.is_empty() {
            rendered.push(SessionView { id: Value::Null, kind: None, label: t("Sport ohne Eintrag"), start: meal.start, end: None,
                                        duration_minutes: None, notes: String::new(), meals: vec![meal.clone()] });
        }
    }
    rendered.sort_by_key(|session| std::cmp::Reverse(session.start));
    struct Kind { kind: Option<String>, label: String, sessions: usize, meals: usize, effects: Vec<Decimal>, applied: Vec<Decimal>, ratios: Vec<Decimal> }
    let mut by_kind: Vec<(String, Kind)> = Vec::new();
    for session in &rendered {
        let key = session.kind.clone().unwrap_or_default();
        let index = match by_kind.iter().position(|(k, _)| *k == key) {
            Some(index) => index,
            None => {
                by_kind.push((key, Kind { kind: session.kind.clone(), label: session.label.clone(), sessions: 0, meals: 0,
                                          effects: Vec::new(), applied: Vec::new(), ratios: Vec::new() }));
                by_kind.len() - 1
            }
        };
        let group = &mut by_kind[index].1;
        group.sessions += 1;
        for meal in &session.meals {
            group.meals += 1;
            // Shared meals cannot be attributed to one session; excluded meals stay out of medians.
            let Some(effect) = meal.effect_mgdl.filter(|_| !meal.shared && !meal.excluded) else { continue };
            group.effects.push(effect);
            if let Some(applied) = meal.applied_mgdl {
                group.applied.push(applied);
                let own = session.id.as_str().is_some_and(|id| own_sessions.contains(id));
                if !applied.is_zero() && !own {
                    group.ratios.push(effect / applied);
                }
            }
        }
    }
    let mut kinds: Vec<SportKindView> = by_kind.into_iter().map(|(_, group)| {
        let standard = group.kind.as_ref().and_then(|k| standards.get(k)).copied();
        SportKindView {
            suggested_drop_mgdl: suggested_drop(standard, &group.ratios),
            median_applied_mgdl: sport_median(&group.applied),
            effect_count: group.effects.len(),
            median_effect_mgdl: sport_median(&group.effects),
            min_effect_mgdl: group.effects.iter().min().copied(),
            max_effect_mgdl: group.effects.iter().max().copied(),
            kind: group.kind, label: group.label, session_count: group.sessions, meal_count: group.meals,
            apply: None,
        }
    }).collect();
    kinds.sort_by(|a, b| a.label.cmp(&b.label));
    SportAnalysis { baselines, kinds, sessions: rendered, lookback_minutes }
}
