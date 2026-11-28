//! Suggested meal factor (Automatik) from recent calculated meal factors (app/forecast.py).
//!
//! The suggestion is a weighted percentile of the meal factors of the same meal kind from the last
//! `lookback` days, each meal weighted 0.5^(age / half-life). The surcharge method subtracts a fixed
//! meal surcharge from past factors and adds the meal's own share to the dose. The backtest replays
//! every option combination on past meals. Model choices for experimentation, not dosing advice.

use crate::model::{opt_amount, AutoMethod, DayType, Instant, MealKind};
use chrono::NaiveDate;
use rust_decimal::{Decimal, MathematicalOps};
use rust_decimal_macros::dec;
use serde::{Deserialize, Deserializer, Serialize};
use std::cell::RefCell;
use std::collections::HashMap;

pub const LOOKBACKS: [i64; 3] = [8, 15, 30];
pub const MIN_MEALS: usize = 4;
pub const HALF: Decimal = dec!(0.5);
pub const HALF_LIVES: [Decimal; 6] = [dec!(1), dec!(2), dec!(3), dec!(5), dec!(7), dec!(14)];
pub const GLUCOSE_LOW: Decimal = dec!(80);
pub const GLUCOSE_HIGH: Decimal = dec!(160);
pub const RATIO_DAYS: i64 = 30;
pub const BACKTEST_DAYS: i64 = 90;
pub const RATIO_NEIGHBOURS: usize = 2;
pub const RATIO_REFERENCE_DAYS: i64 = 7;
pub const SURCHARGE: &str = "surcharge";
pub const SURCHARGE_MGDL: Decimal = dec!(40);
pub const SURCHARGE_FULL_GRAMS: Decimal = dec!(40);
pub const FAT_SURCHARGE_MGDL: Decimal = dec!(120);
pub const PLACE_LOOKBACK_DAYS: i64 = 60;
pub const PLACE_MIN_MEALS: usize = 3;
pub const PLACE_HALF_LIFE: Decimal = dec!(14);
pub const PLACE_SCALE_MIN: Decimal = dec!(0.5);
pub const PLACE_SCALE_MAX: Decimal = dec!(1.5);
pub const TUNING_PERCENTILES: [Decimal; 6] = [dec!(25), dec!(30), dec!(35), dec!(40), dec!(45), dec!(50)];
pub const TUNING_SURCHARGES: [Decimal; 5] = [dec!(0), dec!(20), dec!(40), dec!(60), dec!(80)];
pub const NOISE_MEALS: Decimal = dec!(2);
pub const NOISE_SHARE: Decimal = dec!(0.05);

/// A meal kind, breakfast also by day type: (Breakfast, Some(Work)), (Lunch, None).
pub type MealKey = (MealKind, Option<DayType>);

/// A key as the server writes it: ["breakfast", "work"] or ["lunch", null].
pub fn key_of<'de, D: Deserializer<'de>>(deserializer: D) -> Result<MealKey, D::Error> {
    let parts: Vec<Option<String>> = Vec::deserialize(deserializer)?;
    let kind = parts.first().cloned().flatten().unwrap_or_default();
    let kind = MealKind::parse(&kind).ok_or_else(|| serde::de::Error::custom(format!("unknown meal kind {kind:?}")))?;
    Ok((kind, parts.get(1).cloned().flatten().and_then(|day_type| DayType::parse(&day_type))))
}

/// A valid, non-excluded meal factor (app/services/factors.py meal_points).
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct MealPoint {
    #[serde(default)]
    pub id: serde_json::Value,
    /// The day as the Automatik counts it (vacation days skipped after a vacation).
    pub day: NaiveDate,
    /// The calendar day.
    #[serde(default)]
    pub real_day: Option<NaiveDate>,
    pub at: Instant,
    #[serde(deserialize_with = "key_of")]
    pub key: MealKey,
    #[serde(default, deserialize_with = "crate::kinds::opt_name")]
    pub day_type: Option<DayType>,
    #[serde(deserialize_with = "crate::model::amount")]
    pub value: Decimal,
    #[serde(default, deserialize_with = "opt_amount")]
    pub end_glucose: Option<Decimal>,
    #[serde(default, deserialize_with = "opt_amount")]
    pub carbs_isf: Option<Decimal>,
    #[serde(default, deserialize_with = "opt_amount")]
    pub carbs_g: Option<Decimal>,
    #[serde(default)]
    pub fat: bool,
    /// The calculated fat/protein surcharge the meal was dosed with.
    #[serde(default, deserialize_with = "opt_amount")]
    pub surcharge_mgdl: Option<Decimal>,
    #[serde(default)]
    pub place_id: Option<i64>,
    #[serde(default)]
    pub own_place: bool,
}

fn age(day: NaiveDate, point: &MealPoint) -> i64 {
    (day - point.day).num_days()
}

/// Quantile of (value, weight) pairs; equal weights give linear interpolation at (n − 1) × p.
pub fn weighted_quantile(pairs: &[(Decimal, Decimal)], fraction: Decimal) -> Decimal {
    let mut items = pairs.to_vec();
    items.sort();
    if items.len() == 1 {
        return items[0].0;
    }
    let total: Decimal = items.iter().map(|(_, w)| *w).sum();
    let first_half = items[0].1 / dec!(2);
    let span = total - first_half - items[items.len() - 1].1 / dec!(2);
    let mut positions = Vec::with_capacity(items.len());
    let mut running = Decimal::ZERO;
    for (_, weight) in &items {
        positions.push((running + weight / dec!(2) - first_half) / span);
        running += weight;
    }
    for index in 0..items.len() - 1 {
        let (low, high) = (positions[index], positions[index + 1]);
        if fraction <= high {
            let share = if high > low { (fraction - low) / (high - low) } else { Decimal::ZERO };
            return items[index].0 + (items[index + 1].0 - items[index].0) * share.max(Decimal::ZERO);
        }
    }
    items[items.len() - 1].0
}

thread_local! {
    static WEIGHTS: RefCell<HashMap<(i64, Decimal), Decimal>> = RefCell::new(HashMap::new());
}

/// 0.5^(age / half-life).
pub fn weight(age: i64, half_life: Decimal) -> Decimal {
    WEIGHTS.with(|cache| {
        *cache.borrow_mut().entry((age, half_life.normalize())).or_insert_with(|| {
            let exponent = Decimal::from(age) / half_life;
            if exponent.fract().is_zero() {
                HALF.powi(exponent.trunc().try_into().unwrap_or(i64::MAX))
            } else {
                HALF.powd(exponent)
            }
        })
    })
}

/// (factor, age in days) of the same key from the lookback days before `day`; today is left out.
pub fn history(points: &[MealPoint], key: &MealKey, day: NaiveDate, lookback: i64) -> Vec<(Decimal, i64)> {
    points.iter().filter(|p| p.key == *key && (1..=lookback).contains(&age(day, p)) && !p.own_place)
        .map(|p| (p.value, age(day, p))).collect()
}

#[derive(Clone, Debug, Serialize)]
pub struct Prediction {
    pub count: usize,
    pub units_per_ke: Option<Decimal>,
}

pub fn predict_from(values: &[(Decimal, i64)], percentile: Decimal, half_life: Decimal) -> Prediction {
    let value = if values.len() >= MIN_MEALS {
        let pairs: Vec<(Decimal, Decimal)> = values.iter().map(|(f, a)| (*f, weight(*a, half_life))).collect();
        Some(weighted_quantile(&pairs, percentile / dec!(100)))
    } else {
        None
    };
    Prediction { count: values.len(), units_per_ke: value.filter(|v| *v > Decimal::ZERO) }
}

pub fn predict(points: &[MealPoint], key: &MealKey, day: NaiveDate, percentile: Decimal, half_life: Decimal, lookback: i64) -> Prediction {
    predict_from(&history(points, key, day, lookback), percentile, half_life)
}

/// Share of the full meal surcharge: linear in the carbs up to SURCHARGE_FULL_GRAMS.
pub fn surcharge_share(grams: Decimal) -> Decimal {
    (grams.max(Decimal::ZERO) / SURCHARGE_FULL_GRAMS).min(Decimal::ONE)
}

/// The surcharge in IE/KE of a meal with Σ[g × KF] = carbs_isf.
pub fn surcharge_units_per_ke(grams: Decimal, carbs_isf: Decimal, mgdl: Decimal) -> Decimal {
    dec!(10) * mgdl * surcharge_share(grams) / carbs_isf
}

/// One main meal's surcharge: configured, plus fat for a fat/protein meal, or its calculated one.
pub fn meal_surcharge(point: &MealPoint, mgdl: Decimal, fat_mgdl: Decimal) -> Decimal {
    if !mgdl.is_zero() {
        if let Some(own) = point.surcharge_mgdl {
            return own;
        }
        if point.fat {
            return mgdl + fat_mgdl;
        }
    }
    mgdl
}

/// A meal's factor without its surcharge share; None without carbs or meal size.
pub fn base_factor(point: &MealPoint, mgdl: Decimal, fat_mgdl: Decimal) -> Option<Decimal> {
    let grams = point.carbs_g.filter(|g| !g.is_zero())?;
    let carbs_isf = point.carbs_isf.filter(|c| *c > Decimal::ZERO)?;
    Some(point.value - surcharge_units_per_ke(grams, carbs_isf, meal_surcharge(point, mgdl, fat_mgdl)))
}

pub fn base_history(points: &[MealPoint], key: &MealKey, day: NaiveDate, lookback: i64, mgdl: Decimal, fat_mgdl: Decimal) -> Vec<(Decimal, i64)> {
    points.iter().filter(|p| p.key == *key && (1..=lookback).contains(&age(day, p)) && !p.own_place)
        .filter_map(|p| base_factor(p, mgdl, fat_mgdl).map(|base| (base, age(day, p)))).collect()
}

/// Like predict; with the surcharge method the percentile of factors without surcharge.
#[allow(clippy::too_many_arguments)]
pub fn predict_method(points: &[MealPoint], key: &MealKey, day: NaiveDate, percentile: Decimal, half_life: Decimal,
                      lookback: i64, method: AutoMethod, mgdl: Decimal, fat_mgdl: Decimal) -> Prediction {
    if method != AutoMethod::Surcharge {
        return predict(points, key, day, percentile, half_life, lookback);
    }
    predict_from(&base_history(points, key, day, lookback, mgdl, fat_mgdl), percentile, half_life)
}

/// Full factor for a target meal: percentile without surcharge plus the meal's surcharge share.
pub fn predict_surcharge(values: &[(Decimal, i64)], percentile: Decimal, half_life: Decimal, target: &MealPoint,
                         mgdl: Decimal, fat_mgdl: Decimal) -> Option<Decimal> {
    let base = predict_from(values, percentile, half_life).units_per_ke?;
    let grams = target.carbs_g.filter(|g| !g.is_zero())?;
    let carbs_isf = target.carbs_isf.filter(|c| !c.is_zero())?;
    Some(base + surcharge_units_per_ke(grams, carbs_isf, meal_surcharge(target, mgdl, fat_mgdl)))
}

#[derive(Clone, Debug, Serialize)]
pub struct PlaceScale {
    pub count: usize,
    pub min_meals: usize,
    pub days: i64,
    pub scale: Option<Decimal>,
}

/// How much the main meals at one place needed compared with the Automatik: weighted median of
/// actual ÷ suggested factor over its meals of the last PLACE_LOOKBACK_DAYS, clamped.
#[allow(clippy::too_many_arguments)]
pub fn place_scale(points: &[MealPoint], place_id: i64, day: NaiveDate, percentile: Decimal, half_life: Decimal,
                   lookback: i64, method: AutoMethod, mgdl: Decimal, fat_mgdl: Decimal) -> PlaceScale {
    let mut pairs = Vec::new();
    for point in points {
        let days = age(day, point);
        if point.place_id != Some(place_id) || !(1..=PLACE_LOOKBACK_DAYS).contains(&days) || point.value <= Decimal::ZERO {
            continue;
        }
        let suggested = if method == AutoMethod::Surcharge {
            predict_surcharge(&base_history(points, &point.key, point.day, lookback, mgdl, fat_mgdl), percentile, half_life, point, mgdl, fat_mgdl)
        } else {
            predict(points, &point.key, point.day, percentile, half_life, lookback).units_per_ke
        };
        if let Some(suggested) = suggested.filter(|s| !s.is_zero()) {
            pairs.push((point.value / suggested, weight(days, PLACE_HALF_LIFE)));
        }
    }
    let scale = (pairs.len() >= PLACE_MIN_MEALS).then(|| weighted_quantile(&pairs, HALF).clamp(PLACE_SCALE_MIN, PLACE_SCALE_MAX));
    PlaceScale { count: pairs.len(), min_meals: PLACE_MIN_MEALS, days: PLACE_LOOKBACK_DAYS, scale }
}

/// Median of sorted values; None when empty.
pub fn median(values: &[Decimal]) -> Option<Decimal> {
    if values.is_empty() {
        return None;
    }
    let middle = values.len() / 2;
    Some(if values.len() % 2 == 1 { values[middle] } else { (values[middle - 1] + values[middle]) / dec!(2) })
}

fn sorted(mut values: Vec<Decimal>) -> Vec<Decimal> {
    values.sort();
    values
}

#[derive(Clone, Debug, Serialize)]
pub struct RatioSingle {
    pub day: NaiveDate,
    pub surcharge: Decimal,
    pub before: usize,
    pub after: usize,
}

#[derive(Clone, Debug, Serialize)]
pub struct KindRatio {
    pub work_count: usize,
    pub work_median: Option<Decimal>,
    pub free_count: usize,
    pub free_median: Option<Decimal>,
    pub surcharge: Option<Decimal>,
    pub singles: Vec<RatioSingle>,
    pub single_median: Option<Decimal>,
    pub single_min: Option<Decimal>,
    pub single_max: Option<Decimal>,
}

#[derive(Clone, Debug, Serialize)]
pub struct DayTypeRatios {
    pub days: i64,
    pub reference_days: i64,
    pub neighbours: usize,
    pub meals: HashMap<MealKind, KindRatio>,
}

/// Stress surcharge of work days over free days at breakfast (information only).
pub fn day_type_ratios(points: &[MealPoint], today: NaiveDate) -> DayTypeRatios {
    let (days, reference_days, neighbours) = (RATIO_DAYS, RATIO_REFERENCE_DAYS, RATIO_NEIGHBOURS);
    let mut meals = HashMap::new();
    for kind in [MealKind::Breakfast] {
        let recent: Vec<&MealPoint> = points.iter().filter(|p| p.key.0 == kind && (0..days).contains(&age(today, p))).collect();
        let of_type = |t: DayType| sorted(recent.iter().filter(|p| p.day_type == Some(t)).map(|p| p.value).collect());
        let (work, free) = (of_type(DayType::Work), of_type(DayType::Free));
        let mut work_days: Vec<&MealPoint> = points.iter().filter(|p| p.key.0 == kind && p.day_type == Some(DayType::Work)).collect();
        work_days.sort_by_key(|p| p.at);
        let mut ordered = recent.clone();
        ordered.sort_by_key(|p| p.at);
        let mut singles = Vec::new();
        for point in ordered.iter().filter(|p| p.day_type == Some(DayType::Free)) {
            let before: Vec<&&MealPoint> = work_days.iter().filter(|w| (1..=reference_days).contains(&(point.day - w.day).num_days())).collect();
            let after: Vec<&&MealPoint> = work_days.iter().filter(|w| (1..=reference_days).contains(&(w.day - point.day).num_days())).collect();
            let before = &before[before.len().saturating_sub(neighbours)..];
            let after = &after[..after.len().min(neighbours)];
            let reference = sorted(before.iter().chain(after).map(|w| w.value).collect());
            if reference.len() >= 2 && !point.value.is_zero() {
                singles.push(RatioSingle { day: point.real_day.unwrap_or(point.day),
                    surcharge: median(&reference).unwrap() / point.value - Decimal::ONE, before: before.len(), after: after.len() });
            }
        }
        let surcharges = sorted(singles.iter().map(|s| s.surcharge).collect());
        meals.insert(kind, KindRatio {
            work_count: work.len(), work_median: median(&work), free_count: free.len(), free_median: median(&free),
            surcharge: match (median(&work), median(&free)) {
                (Some(w), Some(f)) if !f.is_zero() => Some(w / f - Decimal::ONE),
                _ => None,
            },
            single_median: median(&surcharges), single_min: surcharges.first().copied(), single_max: surcharges.last().copied(),
            singles,
        });
    }
    DayTypeRatios { days, reference_days, neighbours, meals }
}

/// One backtest variant: lookback, half-life, percentile and meal surcharge (None = plain percentile).
#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq)]
pub struct Variant {
    pub lookback: i64,
    pub half_life: Decimal,
    pub percentile: Decimal,
    pub surcharge_mgdl: Option<Decimal>,
}

#[derive(Clone, Debug, Serialize)]
pub struct BacktestRow {
    pub units_per_ke: Decimal,
    pub predictions: Vec<Decimal>,
    pub glucose: Option<Vec<Decimal>>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct VariantStats {
    pub count: usize,
    pub glucose_count: usize,
    pub below: usize,
    pub in_range: usize,
    pub above: usize,
    pub mean_glucose: Option<Decimal>,
    pub median_glucose: Option<Decimal>,
    pub median_abs_deviation: Option<Decimal>,
}

#[derive(Clone, Debug, Serialize)]
pub struct Backtest {
    pub rows: Vec<BacktestRow>,
    pub stats: Vec<VariantStats>,
}

/// Simulate each variant on the meals since `since`; only meals every variant can predict count.
/// The simulated endpoint is target + (F − P) × Σ[g × KF] / 10 (only the factor error).
pub fn backtest(points: &[MealPoint], since: NaiveDate, variants: &[Variant], target_glucose: Decimal, fat_mgdl: Decimal) -> Backtest {
    let mut by_key: HashMap<&MealKey, Vec<MealPoint>> = HashMap::new();
    for point in points {
        by_key.entry(&point.key).or_default().push(point.clone());
    }
    let specs: Vec<Variant> = variants.iter().map(|v| Variant { surcharge_mgdl: v.surcharge_mgdl.filter(|m| !m.is_zero()), ..*v }).collect();
    let longest = specs.iter().map(|s| s.lookback).max().unwrap_or(0);
    let mut surcharges: Vec<Decimal> = Vec::new();
    for spec in &specs {
        if let Some(mgdl) = spec.surcharge_mgdl {
            if !surcharges.contains(&mgdl) {
                surcharges.push(mgdl);
            }
        }
    }
    let mut ordered: Vec<&MealPoint> = points.iter().collect();
    ordered.sort_by_key(|p| p.at);
    let mut rows = Vec::new();
    for target in ordered {
        if target.day < since || target.value <= Decimal::ZERO {
            continue;
        }
        let same = &by_key[&target.key];
        let known = history(same, &target.key, target.day, longest);
        let known_base: Vec<(Decimal, Vec<(Decimal, i64)>)> = surcharges.iter()
            .map(|mgdl| (*mgdl, base_history(same, &target.key, target.day, longest, *mgdl, fat_mgdl))).collect();
        let mut predictions = Vec::with_capacity(specs.len());
        for spec in &specs {
            let prediction = match spec.surcharge_mgdl {
                Some(mgdl) => {
                    let base = &known_base.iter().find(|(m, _)| *m == mgdl).unwrap().1;
                    let slice: Vec<(Decimal, i64)> = base.iter().filter(|(_, a)| *a <= spec.lookback).copied().collect();
                    predict_surcharge(&slice, spec.percentile, spec.half_life, target, mgdl, fat_mgdl)
                }
                None => {
                    let slice: Vec<(Decimal, i64)> = known.iter().filter(|(_, a)| *a <= spec.lookback).copied().collect();
                    predict_from(&slice, spec.percentile, spec.half_life).units_per_ke
                }
            };
            match prediction {
                Some(value) => predictions.push(value),
                None => break,
            }
        }
        if predictions.len() < specs.len() {
            continue;
        }
        let glucose = target.carbs_isf.filter(|c| !c.is_zero()).map(|carbs_isf| predictions.iter()
            .map(|p| target_glucose + (target.value - p) * carbs_isf / dec!(10)).collect());
        rows.push(BacktestRow { units_per_ke: target.value, predictions, glucose });
    }
    let stats = (0..specs.len()).map(|index| {
        let absolute = sorted(rows.iter().map(|r| (r.predictions[index] - r.units_per_ke).abs() / r.units_per_ke).collect());
        let levels = sorted(rows.iter().filter_map(|r| r.glucose.as_ref().map(|g| g[index])).collect());
        VariantStats {
            count: rows.len(), glucose_count: levels.len(),
            below: levels.iter().filter(|l| **l < GLUCOSE_LOW).count(),
            in_range: levels.iter().filter(|l| GLUCOSE_LOW <= **l && **l <= GLUCOSE_HIGH).count(),
            above: levels.iter().filter(|l| **l > GLUCOSE_HIGH).count(),
            mean_glucose: (!levels.is_empty()).then(|| levels.iter().sum::<Decimal>() / Decimal::from(levels.len())),
            median_glucose: median(&levels), median_abs_deviation: median(&absolute),
        }
    }).collect();
    Backtest { rows, stats }
}

fn options_of(values: &[Decimal], configured: Decimal) -> Vec<Decimal> {
    let mut all: Vec<Decimal> = values.to_vec();
    if !all.contains(&configured) {
        all.push(configured);
    }
    all.sort();
    all
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct TuningOption {
    #[serde(default, deserialize_with = "crate::model::amount")]
    pub value: Decimal,
    #[serde(default)]
    pub active: bool,
    #[serde(flatten)]
    pub stats: VariantStats,
    #[serde(default)]
    pub best: bool,
}

#[derive(Clone, Debug, Serialize)]
pub struct Verdict {
    pub verdict: &'static str,
    pub noise_meals: Decimal,
}

#[derive(Clone, Debug, Serialize)]
pub struct TuningRow {
    pub key: &'static str,
    pub meal_count: usize,
    pub options: Vec<TuningOption>,
    #[serde(flatten)]
    pub verdict: Verdict,
}

/// A tuning row's setting: its name, its options, its configured value and the variant for an option.
type TuningSetting = (&'static str, Vec<Decimal>, Decimal, Box<dyn Fn(Decimal) -> Variant>);

/// One row per setting: every option of it with the others as configured.
pub fn tuning(points: &[MealPoint], since: NaiveDate, configured: Variant, target_glucose: Decimal, fat_mgdl: Decimal) -> Vec<TuningRow> {
    let Variant { lookback, half_life, percentile, .. } = configured;
    let surcharge_mgdl = configured.surcharge_mgdl.unwrap_or_default();
    let variant = |l: i64, h: Decimal, p: Decimal, s: Decimal| Variant { lookback: l, half_life: h, percentile: p, surcharge_mgdl: Some(s) };
    let lookbacks: Vec<Decimal> = options_of(&LOOKBACKS.map(Decimal::from), Decimal::from(lookback));
    let rows: Vec<TuningSetting> = vec![
        ("percentile", options_of(&TUNING_PERCENTILES, percentile), percentile, Box::new(move |v| variant(lookback, half_life, v, surcharge_mgdl))),
        ("surcharge", options_of(&TUNING_SURCHARGES, surcharge_mgdl), surcharge_mgdl, Box::new(move |v| variant(lookback, half_life, percentile, v))),
        ("half_life", options_of(&HALF_LIVES, half_life), half_life, Box::new(move |v| variant(lookback, v, percentile, surcharge_mgdl))),
        ("lookback", lookbacks, Decimal::from(lookback), Box::new(move |v: Decimal| variant(v.try_into().unwrap(), half_life, percentile, surcharge_mgdl))),
    ];
    rows.into_iter().map(|(key, values, configured, make)| {
        let variants: Vec<Variant> = values.iter().map(|v| make(*v)).collect();
        let stats = backtest(points, since, &variants, target_glucose, fat_mgdl).stats;
        let mut options: Vec<TuningOption> = values.iter().zip(stats)
            .map(|(value, stats)| TuningOption { value: *value, active: *value == configured, stats, best: false }).collect();
        let verdict = judge(&mut options);
        TuningRow { key, meal_count: options.first().map(|o| o.stats.glucose_count).unwrap_or(0), options, verdict }
    }).collect()
}

/// Verdict of one tuning row: 'better' beyond chance, 'shift' (trades lows for highs), or 'none'.
pub fn judge(options: &mut [TuningOption]) -> Verdict {
    let total = options.first().map(|o| o.stats.glucose_count).unwrap_or(0);
    let noise = NOISE_MEALS.max(NOISE_SHARE * Decimal::from(total));
    let mut best: Option<usize> = None;
    options.iter_mut().for_each(|option| option.best = false);
    for index in 0..options.len() {
        let rank = |o: &TuningOption| (o.stats.in_range as i64, -(o.stats.below as i64));
        if best.is_none_or(|b| rank(&options[index]) > rank(&options[b])) {
            best = Some(index);
        }
    }
    let active = options.iter().position(|o| o.active);
    let (Some(best), Some(active)) = (best, active) else { return Verdict { verdict: "none", noise_meals: noise } };
    if total == 0 {
        return Verdict { verdict: "none", noise_meals: noise };
    }
    options[best].best = true;
    let spread = |f: fn(&VariantStats) -> usize| {
        let values: Vec<usize> = options.iter().map(|o| f(&o.stats)).collect();
        Decimal::from(values.iter().max().unwrap() - values.iter().min().unwrap())
    };
    let verdict = if Decimal::from(options[best].stats.in_range as i64 - options[active].stats.in_range as i64) > noise {
        "better"
    } else if spread(|s| s.below) > noise || spread(|s| s.above) > noise {
        "shift"
    } else {
        "none"
    };
    Verdict { verdict, noise_meals: noise }
}

#[derive(Clone, Debug, Serialize)]
pub struct Explanation {
    pub meal: MealKind,
    pub day_type: Option<DayType>,
    pub count: usize,
    pub min_meals: usize,
    pub units_per_ke: Option<Decimal>,
    pub carbs_g: Option<Decimal>,
    pub isf: Option<Decimal>,
    pub carb_units: Option<Decimal>,
    pub surcharge_units: Option<Decimal>,
    pub total_units: Option<Decimal>,
}

/// The configured suggestion for a typical meal of `key`, step by step.
#[allow(clippy::too_many_arguments)]
pub fn explain(points: &[MealPoint], key: &MealKey, day: NaiveDate, lookback: i64, half_life: Decimal,
               percentile: Decimal, surcharge_mgdl: Decimal, fat_mgdl: Decimal) -> Explanation {
    let mut used: Vec<&MealPoint> = points.iter().filter(|p| p.key == *key && (1..=lookback).contains(&age(day, p)) && !p.own_place).collect();
    used.sort_by_key(|p| std::cmp::Reverse(p.at));
    if !surcharge_mgdl.is_zero() {
        used.retain(|p| base_factor(p, surcharge_mgdl, fat_mgdl).is_some());
    }
    let values: Vec<(Decimal, i64)> = used.iter().map(|p| (if surcharge_mgdl.is_zero() { p.value } else {
        base_factor(p, surcharge_mgdl, fat_mgdl).unwrap() }, age(day, p))).collect();
    let factor = predict_from(&values, percentile, half_life).units_per_ke;
    let sized: Vec<&&MealPoint> = used.iter().filter(|p| p.carbs_g.is_some_and(|g| !g.is_zero()) && p.carbs_isf.is_some_and(|c| !c.is_zero())).collect();
    let grams = median(&sorted(sized.iter().map(|p| p.carbs_g.unwrap()).collect()));
    let isf = median(&sorted(sized.iter().map(|p| p.carbs_isf.unwrap() / p.carbs_g.unwrap()).collect()));
    let (mut carb_units, mut surcharge_units) = (None, None);
    if let (Some(factor), Some(grams)) = (factor, grams) {
        carb_units = Some(grams / dec!(10) * factor);
        surcharge_units = Some(if surcharge_mgdl.is_zero() { Decimal::ZERO } else { surcharge_mgdl * surcharge_share(grams) / isf.unwrap() });
    }
    Explanation {
        meal: key.0, day_type: key.1, count: values.len(), min_meals: MIN_MEALS, units_per_ke: factor,
        carbs_g: grams, isf, carb_units, surcharge_units, total_units: carb_units.zip(surcharge_units).map(|(c, s)| c + s),
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct TradeoffOption {
    pub lookback_days: i64,
    pub half_life_days: Decimal,
    pub percentile: Decimal,
    pub surcharge_mgdl: Decimal,
    pub below: usize,
    pub in_range: usize,
    pub above: usize,
    pub median_abs_deviation: Option<Decimal>,
    pub median_glucose: Option<Decimal>,
    pub changes: Vec<&'static str>,
    pub variants: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub delta_below: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub delta_above: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub delta_median_glucose: Option<Option<Decimal>>,
}

#[derive(Clone, Debug, Serialize)]
pub struct CloudPoint {
    pub below: usize,
    pub above: usize,
    pub variants: usize,
}

#[derive(Clone, Debug, Serialize)]
pub struct Tradeoffs {
    pub meal_count: usize,
    pub current: TradeoffOption,
    pub line: Vec<TradeoffOption>,
    pub cloud: Vec<CloudPoint>,
}

/// Every combination of the tuning options on the same meals: the trade-off line holds those no
/// other one beats on both fewer endpoints below and fewer above the range.
pub fn tradeoffs(points: &[MealPoint], since: NaiveDate, configured: Variant, target_glucose: Decimal, fat_mgdl: Decimal) -> Tradeoffs {
    let Variant { lookback, half_life, percentile, .. } = configured;
    let surcharge_mgdl = configured.surcharge_mgdl.unwrap_or_default();
    let configured = Variant { surcharge_mgdl: Some(surcharge_mgdl), ..configured };
    let mut variants = Vec::new();
    for days in options_of(&LOOKBACKS.map(Decimal::from), Decimal::from(lookback)) {
        for strength in options_of(&HALF_LIVES, half_life) {
            for value in options_of(&TUNING_PERCENTILES, percentile) {
                for mgdl in options_of(&TUNING_SURCHARGES, surcharge_mgdl) {
                    variants.push(Variant { lookback: days.try_into().unwrap(), half_life: strength, percentile: value, surcharge_mgdl: Some(mgdl) });
                }
            }
        }
    }
    let stats = backtest(points, since, &variants, target_glucose, fat_mgdl).stats;
    let changes = |v: &Variant| {
        let mut names = Vec::new();
        if v.lookback != lookback { names.push("lookback"); }
        if v.half_life != half_life { names.push("half_life"); }
        if v.percentile != percentile { names.push("percentile"); }
        if v.surcharge_mgdl != Some(surcharge_mgdl) { names.push("surcharge"); }
        names
    };
    let mut options: Vec<((usize, usize), TradeoffOption)> = Vec::new();
    for (variant, item) in variants.iter().zip(&stats) {
        let counts = (item.below, item.above);
        let option = TradeoffOption {
            lookback_days: variant.lookback, half_life_days: variant.half_life, percentile: variant.percentile,
            surcharge_mgdl: variant.surcharge_mgdl.unwrap(), below: item.below, in_range: item.in_range, above: item.above,
            median_abs_deviation: item.median_abs_deviation, median_glucose: item.median_glucose,
            changes: changes(variant), variants: 1, delta_below: None, delta_above: None, delta_median_glucose: None,
        };
        match options.iter_mut().find(|(c, _)| *c == counts) {
            None => options.push((counts, option)),
            Some((_, known)) => {
                if option.changes.len() < known.changes.len() {
                    let variants = known.variants + 1;
                    *known = TradeoffOption { variants, ..option };
                } else {
                    known.variants += 1;
                }
            }
        }
    }
    let index = variants.iter().position(|v| *v == configured).unwrap();
    let wanted = (stats[index].below, stats[index].above);
    let mut current = options.iter().find(|(c, _)| *c == wanted).unwrap().1.clone();
    let unchanged = current.changes.is_empty();
    current.changes.clear();
    let mut line: Vec<TradeoffOption> = options.iter().filter(|(_, option)| !options.iter().any(|(_, other)|
        other.below <= option.below && other.above <= option.above && (other.below, other.above) != (option.below, option.above)))
        .map(|(_, option)| option.clone()).collect();
    line.sort_by_key(|o| o.below);
    for option in &mut line {
        option.delta_below = Some(option.below as i64 - current.below as i64);
        option.delta_above = Some(option.above as i64 - current.above as i64);
        option.delta_median_glucose = Some(option.median_glucose.zip(current.median_glucose).map(|(a, b)| a - b));
    }
    // The configured option on the line shows its own (zero) deltas too.
    if unchanged && line.iter().any(|o| (o.below, o.above) == wanted) {
        current.delta_below = Some(0);
        current.delta_above = Some(0);
        current.delta_median_glucose = Some(current.median_glucose.map(|_| Decimal::ZERO));
    }
    Tradeoffs {
        meal_count: stats.first().map(|s| s.glucose_count).unwrap_or(0), current, line,
        cloud: options.iter().map(|(_, o)| CloudPoint { below: o.below, above: o.above, variants: o.variants }).collect(),
    }
}
