//! The dose help for an entry being typed now (app/services/dose_help.py with the parts of
//! app/services/factors.py and app/store/marks.py it reads), from a snapshot instead of the store.
//!
//! This is what a phone runs offline: the snapshot holds the recent treatments and readings, the
//! settings, the day types and vacations, the places and the calculated meal factors (meal points by
//! calendar day). The Automatik, the place factor, "Wirkt noch", the split plan and the dose options
//! are computed here exactly as on the server.

use crate::calculation::{schedule_value_at, ScheduleKey};
use crate::context::local_zone;
use crate::correction_scale;
use crate::dosing::{self, DoseOptions, Finger};
use crate::forecast::{self, MealKey, MealPoint, PlaceScale};
use crate::fpe;
use crate::model::{decimal_of, Instant, Reading, Settings, Treatment};
use crate::on_board::{self, SourceOptions, Summary};
use crate::split;
use crate::stats;
use crate::Res;
use chrono::{Datelike, Duration, NaiveDate};
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use serde::Deserialize;
use serde_json::{json, Map, Value};
use std::cell::RefCell;
use std::collections::HashMap;

pub const BREAKFAST_FACTOR_DAYS: i64 = 28;
pub const BREAKFAST_FACTOR_MIN_MEALS: usize = 3;
pub const DEFAULT_AUTO_VACATION: &str = "separate";

#[derive(Clone, Debug, Deserialize)]
pub struct VacationPeriod {
    pub start_day: NaiveDate,
    #[serde(default)]
    pub end_day: Option<NaiveDate>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct Place {
    pub id: i64,
    pub name: String,
    #[serde(default)]
    pub adjust: bool,
}

/// Everything the dose help reads.
#[derive(Clone, Debug, Deserialize)]
pub struct Snapshot {
    pub now: Instant,
    pub settings: Settings,
    /// The entry form's payload (carbs, finger_mgdl, place_id, fat_g, protein_g, fpe_level, ...).
    #[serde(default)]
    pub payload: Value,
    /// Readings and treatments of the recent span (day_view.recent), grouped as the diary groups them.
    pub entries: Vec<Reading>,
    pub treatments: Vec<Treatment>,
    /// Manual work/free days, or null while day types are off.
    #[serde(default)]
    pub day_types: Option<HashMap<NaiveDate, String>>,
    #[serde(default)]
    pub vacation_periods: Vec<VacationPeriod>,
    /// Valid, non-excluded meal factors; `day` may be the calendar day (it is recounted here).
    pub meal_points: Vec<MealPoint>,
    #[serde(default)]
    pub places: Vec<Place>,
    /// Diary entry event id -> place id.
    #[serde(default)]
    pub entry_places: HashMap<String, i64>,
    #[serde(default)]
    pub sport_overrides: HashMap<String, Value>,
    #[serde(default)]
    pub sport_minutes_overrides: HashMap<String, Value>,
    #[serde(default)]
    pub auto_vacation: Option<String>,
}

pub fn in_vacation(day: NaiveDate, periods: &[VacationPeriod]) -> bool {
    periods.iter().any(|p| p.start_day <= day && p.end_day.is_none_or(|end| day <= end))
}

/// (day type, source): a manual choice wins, then vacation, then Mon–Fri work; (None, "off") while off.
pub fn day_type_for(day: NaiveDate, overrides: Option<&HashMap<NaiveDate, String>>, periods: &[VacationPeriod]) -> (Option<String>, &'static str) {
    let Some(overrides) = overrides else { return (None, "off") };
    if let Some(kind) = overrides.get(&day) {
        return (Some(kind.clone()), "manual");
    }
    if in_vacation(day, periods) {
        return (Some("free".into()), "vacation");
    }
    (Some(if day.weekday().number_from_monday() >= 6 { "free" } else { "work" }.into()), "weekday")
}

/// (calendar day, day as the Automatik counts it) from today back over days_back days, newest
/// first: outside a vacation, "separate" and "vacation_only" skip vacation days and move the days
/// before them up; during one, "vacation_only" counts only vacation days.
pub fn counted_days(today: NaiveDate, days_back: i64, periods: &[VacationPeriod], mode: &str) -> Vec<(NaiveDate, NaiveDate)> {
    let calendar: Vec<NaiveDate> = (0..=days_back).map(|offset| today - Duration::days(offset)).collect();
    if !matches!(mode, "separate" | "vacation_only") || periods.is_empty() {
        return calendar.into_iter().map(|day| (day, day)).collect();
    }
    if in_vacation(today, periods) {
        return calendar.into_iter().filter(|day| mode != "vacation_only" || in_vacation(*day, periods)).map(|day| (day, day)).collect();
    }
    let mut result = Vec::new();
    let mut offset = 0;
    while result.len() as i64 <= days_back && offset <= days_back + 732 {
        let day = today - Duration::days(offset);
        offset += 1;
        if !in_vacation(day, periods) {
            result.push((day, today - Duration::days(result.len() as i64)));
        }
    }
    result
}

impl Snapshot {
    fn mode(&self) -> &str {
        self.auto_vacation.as_deref().unwrap_or(DEFAULT_AUTO_VACATION)
    }

    fn real_day(point: &MealPoint) -> NaiveDate {
        point.real_day.unwrap_or(point.day)
    }

    /// factors.meal_points: the points of the counted days, `day` recounted.
    fn points(&self, today: NaiveDate, days_back: i64) -> Vec<MealPoint> {
        let days = counted_days(today, days_back, &self.vacation_periods, self.mode());
        let mut result = Vec::new();
        for (real, counted) in days {
            for point in self.meal_points.iter().filter(|p| Self::real_day(p) == real) {
                result.push(MealPoint { day: counted, real_day: Some(real), ..point.clone() });
            }
        }
        result
    }

    /// factors.meal_factor_values: sorted IE/KE of one meal kind over the past 28 days.
    fn meal_factor_values(&self, kind: &str, day_type: Option<&str>, today: NaiveDate) -> Vec<Decimal> {
        let days = counted_days(today, BREAKFAST_FACTOR_DAYS, &self.vacation_periods, self.mode());
        let mut values = Vec::new();
        for (real, _) in days.into_iter().skip(1) {
            if let Some(wanted) = day_type {
                if day_type_for(real, self.day_types.as_ref(), &self.vacation_periods).0.as_deref() != Some(wanted) {
                    continue;
                }
            }
            values.extend(self.meal_points.iter().filter(|p| Self::real_day(p) == real && p.key.0 == kind).map(|p| p.value));
        }
        values.sort();
        values
    }
}

fn meal_kind(minute: i64, settings: &Settings) -> &'static str {
    if minute < settings.breakfast_end_minute { "breakfast" } else if minute < settings.lunch_end_minute { "lunch" } else { "dinner" }
}

fn fat_surcharge(settings: &Settings) -> Decimal {
    settings.auto_fat_surcharge_mgdl
}

/// Surcharge of one main meal as the dose help uses it; None without the surcharge method.
fn meal_surcharge_mgdl(settings: &Settings, fat: bool) -> Option<Decimal> {
    settings.surcharge_method().then(|| settings.auto_surcharge_mgdl + if fat { fat_surcharge(settings) } else { Decimal::ZERO })
}

fn text(value: Option<Decimal>) -> Value {
    value.map(|v| json!(v.to_string())).unwrap_or(Value::Null)
}

/// The dose help result for the entry in `snapshot.payload` (the server's /api/entry/preview dose part).
pub fn dose_help(snapshot: &Snapshot) -> Res<Value> {
    let settings = &snapshot.settings;
    let now = snapshot.now;
    let zone = local_zone(&settings.timezone);
    let today = zone.local(&now).date();
    let payload = &snapshot.payload;
    let mut finger: Vec<Finger> = snapshot.treatments.iter().filter_map(|t| t.finger_mgdl.filter(|v| !v.is_zero()).map(|mgdl| Finger { at: t.at, mgdl })).collect();
    if let Some(mgdl) = payload.get("finger_mgdl").and_then(decimal_of) {
        // A value typed in the entry form counts before it is saved, at the entry's time.
        let at = payload.get("finger_at").and_then(|v| serde_json::from_value::<Instant>(v.clone()).ok()).unwrap_or(now);
        finger.push(Finger { at: at.min(now), mgdl });
    }
    let periods = &snapshot.vacation_periods;
    let (day_type, day_type_source) = day_type_for(today, snapshot.day_types.as_ref(), periods);
    let minute = zone.minute_of_day(&now);
    let is_breakfast = minute < settings.breakfast_end_minute;
    let kind = meal_kind(minute, settings);
    let key: MealKey = (kind.to_string(), if is_breakfast { day_type.clone() } else { None });
    let meal_values = snapshot.meal_factor_values(kind, if is_breakfast { day_type.as_deref() } else { None }, today);
    let breakfast_median = stats::median(&meal_values);
    let use_factor = is_breakfast && meal_values.len() >= BREAKFAST_FACTOR_MIN_MEALS && breakfast_median.is_some_and(|m| m > Decimal::ZERO);
    let (percentile, half_life, lookback) = (settings.auto_percentile, settings.auto_half_life_days, settings.auto_lookback_days);
    let points = snapshot.points(today, lookback);
    let method = settings.auto_method.as_str();
    let (surcharge_mgdl, fat_mgdl) = (settings.auto_surcharge_mgdl, fat_surcharge(settings));
    let prediction = forecast::predict_method(&points, &key, today, percentile, half_life, lookback, method, surcharge_mgdl, fat_mgdl);
    let auto_value = prediction.units_per_ke;

    // Place factor of every place switched on for it.
    let chosen: Vec<&Place> = snapshot.places.iter().filter(|p| p.adjust).collect();
    let scales: HashMap<i64, (String, PlaceScale)> = if chosen.is_empty() { HashMap::new() } else {
        let place_points = snapshot.points(today, lookback + forecast::PLACE_LOOKBACK_DAYS);
        chosen.iter().map(|place| (place.id, (place.name.clone(), forecast::place_scale(&place_points, place.id, today, percentile,
            half_life, lookback, method, surcharge_mgdl, fat_mgdl)))).collect()
    };
    let place = payload.get("place_id").and_then(Value::as_i64).and_then(|id| scales.get(&id));
    let place_scale = place.and_then(|(_, s)| s.scale).unwrap_or(Decimal::ONE);

    // factors.carb_factor: the Automatik factor of the entry's clock-time meal kind, else Nightscout's I:C.
    let predictions: RefCell<HashMap<MealKey, Option<Decimal>>> = RefCell::new(HashMap::from([(key.clone(), auto_value)]));
    let carb_factor = |at: Instant| -> Option<Decimal> {
        let kind = meal_kind(zone.minute_of_day(&at), settings);
        let entry_key: MealKey = (kind.to_string(), if kind == "breakfast" { day_type.clone() } else { None });
        let value = *predictions.borrow_mut().entry(entry_key.clone()).or_insert_with(|| forecast::predict_method(
            &points, &entry_key, today, percentile, half_life, lookback, method, surcharge_mgdl, fat_mgdl).units_per_ke);
        value.or_else(|| schedule_value_at(at, &settings.ic_schedule, &settings.timezone, ScheduleKey::GramsPerUnit).ok().map(|ratio| dec!(10) / ratio))
    };
    let any_scale = scales.values().any(|(_, s)| s.scale.is_some());
    let entry_scale = |item: &Treatment| -> Option<Decimal> {
        let place = snapshot.entry_places.get(item.event_id.as_deref().unwrap_or_default())?;
        scales.get(place).and_then(|(_, s)| s.scale)
    };
    let options = SourceOptions {
        carb_factor: &carb_factor,
        surcharge_mgdl: settings.surcharge_method().then_some(surcharge_mgdl),
        fat_surcharge_mgdl: fat_mgdl, keep_all: false,
        sport_overrides: snapshot.sport_overrides.iter().filter_map(|(k, v)| decimal_of(v).map(|v| (k.clone(), v))).collect(),
        sport_minutes_overrides: snapshot.sport_minutes_overrides.iter()
            .filter_map(|(k, v)| decimal_of(v).map(|v| (k.clone(), v.trunc().try_into().unwrap_or(0)))).collect(),
        scale: if any_scale { Some(&entry_scale) } else { None },
    };
    let treatments = &snapshot.treatments;
    let active: Option<Summary> = on_board::sources(treatments, now, settings, &options).ok().map(|found| on_board::summary(&found, now));

    // The surcharge follows the stomach: this entry gets the share its carbs add to what is in it.
    let surcharge = settings.surcharge_method() && auto_value.is_some();
    let carbs = payload.get("carbs").and_then(decimal_of).unwrap_or(Decimal::ZERO);
    let before = on_board::stomach_grams(treatments, now);
    let added_share = forecast::surcharge_share(before + carbs) - forecast::surcharge_share(before);
    let fat = payload.get("fat_protein").and_then(Value::as_bool) == Some(true);
    let mut meal_surcharge = meal_surcharge_mgdl(settings, fat);
    let calculated = if surcharge && meal_surcharge.is_some() { fpe::estimate(payload, settings) } else { None };
    let use_fpe = fpe::chosen(calculated.as_ref(), payload);
    if use_fpe {
        meal_surcharge = calculated.as_ref().map(|c| c.mgdl);
    }
    let plan = split::open_plan(treatments, now);
    let dose = DoseOptions {
        activity_change: payload.get("activity_change_mgdl").and_then(decimal_of).unwrap_or(Decimal::ZERO),
        units_per_ke: if use_factor { breakfast_median } else { None },
        auto_units_per_ke: auto_value.map(|v| v * place_scale),
        auto_surcharge_mgdl: if surcharge { meal_surcharge.map(|m| m * place_scale) } else { None },
        surcharge_share: Some(added_share),
        on_board: active.as_ref().map(|a| (a.effects.lowering_mgdl, a.effects.raising_mgdl)),
        finger, hold_rise: plan.is_some() && carbs > Decimal::ZERO,
    };
    let suggestion = dosing::suggest(carbs, settings, &snapshot.entries, treatments, now, &dose)?;
    let mut result: Map<String, Value> = match serde_json::to_value(&suggestion).unwrap() { Value::Object(map) => map, _ => unreachable!() };
    if carbs.is_zero() {
        // A correction without food also shows the ends of what was right after recent meals.
        let normal = correction_scale::Normal { forecast_mgdl: suggestion.forecast_mgdl, correction_units: suggestion.correction_units,
            entry_units: suggestion.entry_units, isf_mgdl_per_unit: suggestion.isf_mgdl_per_unit };
        let rise = correction_scale::meal_rise(&snapshot.entries, treatments, now);
        result.insert("correction_scale".into(), serde_json::to_value(correction_scale::scale(&normal, rise.as_ref())).unwrap());
    }
    let split_plan = plan.as_ref().map(|plan| {
        let when = split::timing(plan, &snapshot.entries, settings, now);
        json!({"meal_at": plan.meal_at, "units": plan.units.to_string(), "due_at": plan.due_at, "state": when.state, "latest_at": when.latest_at})
    });
    result.insert("split_plan".into(), split_plan.clone().unwrap_or(Value::Null));
    result.insert("on_board".into(), active.as_ref().map(|a| on_board_view(a, &suggestion, split_plan.clone())).unwrap_or(Value::Null));
    let q1 = (meal_values.len() >= forecast::MIN_MEALS).then(|| stats::quartile(&meal_values, dec!(0.25)));
    let extra = json!({
        "auto_meal": kind, "auto_factor_count": prediction.count, "auto_method": method,
        "auto_fat_surcharge_mgdl": if surcharge && fat && !use_fpe { json!(fat_mgdl.to_string()) } else { Value::Null },
        "surcharge_levels": surcharge && meal_surcharge.is_some(),
        "surcharge_choice": match &calculated {
            Some(c) if c.level.is_none() => json!({
                "mode": if use_fpe { "fpe" } else { "static" },
                "static_mgdl": text(meal_surcharge_mgdl(settings, fat)), "static_fat": fat,
                "fpe": c.fpe.round_dp_with_strategy(1, rust_decimal::RoundingStrategy::MidpointNearestEven).to_string(),
                "fpe_mgdl": c.mgdl.to_string(), "fpe_minutes": c.minutes, "per_fpe_mgdl": settings.fpe_surcharge_mgdl.to_string(),
                "fat_g": payload.get("fat_g").cloned().unwrap_or(Value::Null), "protein_g": payload.get("protein_g").cloned().unwrap_or(Value::Null)}),
            Some(_) => json!(false),
            None => Value::Null,
        },
        "place_factor": place.map(|(name, s)| json!({"name": name, "count": s.count, "min_meals": s.min_meals, "days": s.days,
                                                      "scale": text(s.scale)})),
        "auto_factor_min_meals": forecast::MIN_MEALS, "auto_percentile": percentile.to_string(),
        "auto_half_life_days": half_life.to_string(), "auto_factor_days": lookback,
        "auto_median_units_per_ke": text(breakfast_median.filter(|_| !meal_values.is_empty())),
        "auto_q1_units_per_ke": text(q1),
        "day_type": day_type, "day_type_source": day_type_source, "breakfast": is_breakfast,
        "factor_source": if use_factor { "breakfast_day_type" } else { "nightscout" },
        "breakfast_factor_count": if is_breakfast { json!(meal_values.len()) } else { Value::Null },
        "breakfast_factor_days": if is_breakfast { json!(BREAKFAST_FACTOR_DAYS) } else { Value::Null },
        "breakfast_factor_min_meals": BREAKFAST_FACTOR_MIN_MEALS,
        "breakfast_factor_units_per_ke": if is_breakfast { text(breakfast_median) } else { Value::Null },
    });
    if let Value::Object(map) = extra {
        result.extend(map);
    }
    Ok(Value::Object(result))
}

/// The diary's "Wirkt noch" card.
fn on_board_view(active: &Summary, result: &dosing::Suggestion, split_plan: Option<Value>) -> Value {
    let e = &active.effects;
    let expected = result.glucose_mgdl.filter(|_| result.glucose_fresh).map(|g| g - e.lowering_mgdl + e.raising_mgdl);
    let (below, above) = on_board::forecast_band(e.lowering_mgdl, e.raising_mgdl);
    json!({
        "glucose_mgdl": text(result.glucose_mgdl), "glucose_fresh": result.glucose_fresh, "glucose_source": result.glucose_source,
        "insulin_units": e.insulin_units.to_string(),
        "lowering_mgdl": e.lowering_mgdl.to_string(), "lowering_until": active.lowering_until,
        "raising_mgdl": e.raising_mgdl.to_string(), "raising_until": active.raising_until,
        "carbs_mgdl": e.carbs_mgdl.to_string(), "surcharge_mgdl": e.surcharge_mgdl.to_string(), "sport_mgdl": e.sport_mgdl.to_string(),
        "sports": active.sports.iter().map(|row| json!({"at": row.session.at, "end": row.session.end, "kind": row.session.kind,
            "mgdl": row.session.mgdl.to_string(), "remaining_mgdl": row.remaining_mgdl.to_string()})).collect::<Vec<_>>(),
        "carbs_remaining_g": active.carbs.iter().map(|row| row.remaining_grams).sum::<Decimal>().to_string(),
        "expected_mgdl": text(expected),
        "expected_low_mgdl": text(expected.map(|x| x - below)),
        "expected_high_mgdl": text(expected.map(|x| x + above)),
        "expected_at": active.settled_at, "fresh_meal_at": active.fresh_meal_at,
        "split_plan": split_plan,
        "doses": active.doses.iter().map(|d| json!({"at": d.at, "units": d.units.to_string(), "remaining_units": d.remaining_units.to_string()})).collect::<Vec<_>>(),
        "carbs": active.carbs.iter().map(|c| json!({"at": c.at, "grams": c.grams.to_string(), "remaining_grams": c.remaining_grams.to_string()})).collect::<Vec<_>>(),
    })
}
