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
use crate::model::{decimal_of, AutoMethod, DayType, Instant, MealKind, Reading, Settings, Treatment};
use crate::on_board::{self, SourceOptions, Summary};
use crate::split;
use crate::stats;
use crate::Res;
use chrono::{Datelike, Duration, NaiveDate};
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use serde::{Deserialize, Serialize};
use serde_json::Value;
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
    pub day_types: Option<HashMap<NaiveDate, DayType>>,
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
pub fn day_type_for(day: NaiveDate, overrides: Option<&HashMap<NaiveDate, DayType>>, periods: &[VacationPeriod]) -> (Option<DayType>, &'static str) {
    let Some(overrides) = overrides else { return (None, "off") };
    if let Some(kind) = overrides.get(&day) {
        return (Some(*kind), "manual");
    }
    if in_vacation(day, periods) {
        return (Some(DayType::Free), "vacation");
    }
    (Some(if day.weekday().number_from_monday() >= 6 { DayType::Free } else { DayType::Work }), "weekday")
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
    fn meal_factor_values(&self, kind: MealKind, day_type: Option<DayType>, today: NaiveDate) -> Vec<Decimal> {
        let days = counted_days(today, BREAKFAST_FACTOR_DAYS, &self.vacation_periods, self.mode());
        let mut values = Vec::new();
        for (real, _) in days.into_iter().skip(1) {
            if let Some(wanted) = day_type {
                if day_type_for(real, self.day_types.as_ref(), &self.vacation_periods).0 != Some(wanted) {
                    continue;
                }
            }
            values.extend(self.meal_points.iter().filter(|p| Self::real_day(p) == real && p.key.0 == kind).map(|p| p.value));
        }
        values.sort();
        values
    }
}

fn fat_surcharge(settings: &Settings) -> Decimal {
    settings.auto_fat_surcharge_mgdl
}

/// Surcharge of one main meal as the dose help uses it; None without the surcharge method.
pub fn meal_surcharge_mgdl(settings: &Settings, fat: bool) -> Option<Decimal> {
    settings.surcharge_method().then(|| settings.auto_surcharge_mgdl + if fat { fat_surcharge(settings) } else { Decimal::ZERO })
}

/// The Automatik factors of `today` as "Wirkt noch" values carb entries with them (factors.carb_factor), and
/// the place factor of every place switched on for it (factors.place_scales, factors.entry_scale).
pub(crate) struct Factors<'a> {
    snapshot: &'a Snapshot,
    today: NaiveDate,
    day_type: Option<DayType>,
    points: Vec<MealPoint>,
    predictions: RefCell<HashMap<MealKey, forecast::Prediction>>,
    pub scales: HashMap<i64, (String, PlaceScale)>,
}

impl<'a> Factors<'a> {
    pub fn new(snapshot: &'a Snapshot) -> Factors<'a> {
        let settings = &snapshot.settings;
        let today = local_zone(&settings.timezone).local(&snapshot.now).date();
        let day_type = day_type_for(today, snapshot.day_types.as_ref(), &snapshot.vacation_periods).0;
        let (percentile, half_life, lookback) = (settings.auto_percentile, settings.auto_half_life_days, settings.auto_lookback_days);
        let points = snapshot.points(today, lookback);
        let chosen: Vec<&Place> = snapshot.places.iter().filter(|p| p.adjust).collect();
        let scales = if chosen.is_empty() { HashMap::new() } else {
            let place_points = snapshot.points(today, lookback + forecast::PLACE_LOOKBACK_DAYS);
            chosen.iter().map(|place| (place.id, (place.name.clone(), forecast::place_scale(&place_points, place.id, today, percentile,
                half_life, lookback, settings.auto_method, settings.auto_surcharge_mgdl, fat_surcharge(settings))))).collect()
        };
        Factors { snapshot, today, day_type, points, predictions: RefCell::new(HashMap::new()), scales }
    }

    /// The Automatik suggestion of one meal key today.
    pub fn prediction(&self, key: &MealKey) -> forecast::Prediction {
        let settings = &self.snapshot.settings;
        self.predictions.borrow_mut().entry(*key).or_insert_with(|| forecast::predict_method(
            &self.points, key, self.today, settings.auto_percentile, settings.auto_half_life_days, settings.auto_lookback_days,
            settings.auto_method, settings.auto_surcharge_mgdl, fat_surcharge(settings))).clone()
    }

    /// IE/KE that values a carb entry at `at`: the Automatik factor of its clock-time meal kind, else Nightscout's I:C.
    pub fn carb_factor(&self, at: Instant) -> Option<Decimal> {
        let settings = &self.snapshot.settings;
        let kind = MealKind::at_minute(local_zone(&settings.timezone).minute_of_day(&at), settings);
        let key: MealKey = (kind, if kind == MealKind::Breakfast { self.day_type } else { None });
        self.prediction(&key).units_per_ke
            .or_else(|| schedule_value_at(at, &settings.ic_schedule, &settings.timezone, ScheduleKey::GramsPerUnit).ok().map(|ratio| dec!(10) / ratio))
    }

    /// The place factor of the place recorded for a diary entry.
    pub fn entry_scale(&self, item: &Treatment) -> Option<Decimal> {
        let place = self.snapshot.entry_places.get(item.event_id.as_deref().unwrap_or_default())?;
        self.scales.get(place).and_then(|(_, s)| s.scale)
    }

    /// What still acts now ("Wirkt noch"), as the dose help, warnings and reminders read it.
    pub fn sources(&self) -> Res<on_board::Found> {
        let snapshot = self.snapshot;
        let settings = &snapshot.settings;
        let carb_factor = |at: Instant| self.carb_factor(at);
        let entry_scale = |item: &Treatment| self.entry_scale(item);
        let options = SourceOptions {
            carb_factor: &carb_factor,
            surcharge_mgdl: settings.surcharge_method().then_some(settings.auto_surcharge_mgdl),
            fat_surcharge_mgdl: fat_surcharge(settings), keep_all: false,
            sport_overrides: snapshot.sport_overrides.iter().filter_map(|(k, v)| decimal_of(v).map(|v| (k.clone(), v))).collect(),
            sport_minutes_overrides: snapshot.sport_minutes_overrides.iter()
                .filter_map(|(k, v)| decimal_of(v).map(|v| (k.clone(), v.trunc().try_into().unwrap_or(0)))).collect(),
            scale: if self.scales.values().any(|(_, s)| s.scale.is_some()) { Some(&entry_scale) } else { None },
        };
        on_board::sources(&snapshot.treatments, snapshot.now, settings, &options)
    }
}

/// The place factor of every place switched on for it, by place id (the place settings show it).
pub fn place_scales(snapshot: &Snapshot) -> HashMap<i64, PlaceScale> {
    Factors::new(snapshot).scales.into_iter().map(|(id, (_, scale))| (id, scale)).collect()
}

/// The second part of an open split plan and whether it fits now (split::timing).
#[derive(Clone, Debug, Serialize)]
pub struct SplitPlanView {
    pub meal_at: Instant,
    pub units: Decimal,
    pub due_at: Instant,
    pub state: &'static str,
    pub latest_at: Instant,
}

/// The calculated against the static fat/protein surcharge, as the entry form offers the choice.
#[derive(Clone, Debug, Serialize)]
pub struct SurchargeChoice {
    pub mode: &'static str,
    pub static_mgdl: Option<Decimal>,
    pub static_fat: bool,
    pub fpe: Decimal,
    pub fpe_mgdl: Decimal,
    pub fpe_minutes: i64,
    pub per_fpe_mgdl: Decimal,
    pub fat_g: Value,
    pub protein_g: Value,
}

/// `surcharge_choice`: the choice, `false` when a fat/protein level was picked instead of the agent's grams,
/// null without a calculated surcharge.
#[derive(Clone, Debug, Serialize)]
#[serde(untagged)]
pub enum SurchargeOffer {
    Choice(Box<SurchargeChoice>),
    Level(bool),
}

#[derive(Clone, Debug, Serialize)]
pub struct PlaceFactorView {
    pub name: String,
    pub count: usize,
    pub min_meals: usize,
    pub days: i64,
    pub scale: Option<Decimal>,
}

/// One line of the "Wirkt noch" card: a dose, a carb entry or a sport session still acting.
#[derive(Clone, Debug, Serialize)]
pub struct DoseRest {
    pub at: Instant,
    pub units: Decimal,
    pub remaining_units: Decimal,
}

#[derive(Clone, Debug, Serialize)]
pub struct CarbRest {
    pub at: Instant,
    pub grams: Decimal,
    pub remaining_grams: Decimal,
}

#[derive(Clone, Debug, Serialize)]
pub struct SportRest {
    pub at: Instant,
    pub end: Instant,
    pub kind: String,
    pub mgdl: Decimal,
    pub remaining_mgdl: Decimal,
}

/// The diary's "Wirkt noch" card.
#[derive(Clone, Debug, Serialize)]
pub struct OnBoardView {
    pub glucose_mgdl: Option<Decimal>,
    pub glucose_fresh: bool,
    pub glucose_source: Option<&'static str>,
    pub insulin_units: Decimal,
    pub lowering_mgdl: Decimal,
    pub lowering_until: Option<Instant>,
    pub raising_mgdl: Decimal,
    pub raising_until: Option<Instant>,
    pub carbs_mgdl: Decimal,
    pub surcharge_mgdl: Decimal,
    pub sport_mgdl: Decimal,
    pub sports: Vec<SportRest>,
    pub carbs_remaining_g: Decimal,
    pub expected_mgdl: Option<Decimal>,
    pub expected_low_mgdl: Option<Decimal>,
    pub expected_high_mgdl: Option<Decimal>,
    pub expected_at: Option<Instant>,
    pub fresh_meal_at: Option<Instant>,
    pub split_plan: Option<SplitPlanView>,
    pub doses: Vec<DoseRest>,
    pub carbs: Vec<CarbRest>,
}

/// The dose help for one entry: the dose suggestion with the factor it used and where that came from, the
/// correction scale (without carbs), the open split plan and "Wirkt noch".
#[derive(Clone, Debug, Serialize)]
pub struct DoseHelp {
    #[serde(flatten)]
    pub suggestion: dosing::Suggestion,
    /// Only without carbs (absent with carbs): the ends of what was right after recent meals, null without them.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub correction_scale: Option<Option<correction_scale::Scale>>,
    pub split_plan: Option<SplitPlanView>,
    pub on_board: Option<OnBoardView>,
    pub auto_meal: MealKind,
    pub auto_factor_count: usize,
    pub auto_method: AutoMethod,
    pub auto_fat_surcharge_mgdl: Option<Decimal>,
    pub surcharge_levels: bool,
    pub surcharge_choice: Option<SurchargeOffer>,
    pub place_factor: Option<PlaceFactorView>,
    pub auto_factor_min_meals: usize,
    pub auto_percentile: Decimal,
    pub auto_half_life_days: Decimal,
    pub auto_factor_days: i64,
    pub auto_median_units_per_ke: Option<Decimal>,
    pub auto_q1_units_per_ke: Option<Decimal>,
    pub day_type: Option<DayType>,
    pub day_type_source: &'static str,
    pub breakfast: bool,
    pub factor_source: &'static str,
    pub breakfast_factor_count: Option<usize>,
    pub breakfast_factor_days: Option<i64>,
    pub breakfast_factor_min_meals: usize,
    pub breakfast_factor_units_per_ke: Option<Decimal>,
}

impl DoseHelp {
    /// Whether the options use the Automatik factor: with carbs once it has enough meals; until then the
    /// breakfast day-type median or Nightscout's I:C stands in.
    pub fn uses_auto(&self) -> bool {
        self.suggestion.carbs_g > Decimal::ZERO && self.suggestion.auto_units_per_ke.is_some()
    }

    /// The normal option (to the target) with the factor the options use: IE now and where glucose likely ends.
    pub fn normal(&self) -> (Option<Decimal>, Option<Decimal>) {
        let s = &self.suggestion;
        if self.uses_auto() { (s.auto_entry_units, s.auto_expected_glucose_mgdl) } else { (s.entry_units, s.expected_glucose_mgdl) }
    }

    /// The correction scale of an entry without carbs.
    pub fn scale(&self) -> Option<&correction_scale::Scale> {
        self.correction_scale.as_ref().and_then(Option::as_ref)
    }

    /// The carbs-only option (no correction) with the factor the options use.
    pub fn carbs_only(&self) -> (Option<Decimal>, Option<Decimal>) {
        let s = &self.suggestion;
        if self.uses_auto() { (s.auto_carbs_entry_units, s.auto_carbs_expected_glucose_mgdl) } else { (s.carbs_entry_units, s.carbs_expected_glucose_mgdl) }
    }
}

/// The dose help result for the entry in `snapshot.payload` (the server's /api/entry/preview dose part).
pub fn dose_help(snapshot: &Snapshot) -> Res<DoseHelp> {
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
    let kind = MealKind::at_minute(minute, settings);
    let key: MealKey = (kind, if is_breakfast { day_type } else { None });
    let meal_values = snapshot.meal_factor_values(kind, if is_breakfast { day_type } else { None }, today);
    let breakfast_median = stats::median(&meal_values);
    let use_factor = is_breakfast && meal_values.len() >= BREAKFAST_FACTOR_MIN_MEALS && breakfast_median.is_some_and(|m| m > Decimal::ZERO);
    let (percentile, half_life, lookback) = (settings.auto_percentile, settings.auto_half_life_days, settings.auto_lookback_days);
    let fat_mgdl = fat_surcharge(settings);
    let factors = Factors::new(snapshot);
    let prediction = factors.prediction(&key);
    let auto_value = prediction.units_per_ke;
    let place = payload.get("place_id").and_then(Value::as_i64).and_then(|id| factors.scales.get(&id));
    let place_scale = place.and_then(|(_, s)| s.scale).unwrap_or(Decimal::ONE);
    let treatments = &snapshot.treatments;
    let active: Option<Summary> = factors.sources().ok().map(|found| on_board::summary(&found, now));

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
    // A correction without food also shows the ends of what was right after recent meals.
    let correction_scale = carbs.is_zero().then(|| {
        let normal = correction_scale::Normal { forecast_mgdl: suggestion.forecast_mgdl, correction_units: suggestion.correction_units,
            entry_units: suggestion.entry_units, isf_mgdl_per_unit: suggestion.isf_mgdl_per_unit };
        let rise = correction_scale::meal_rise(&snapshot.entries, treatments, now);
        correction_scale::scale(&normal, rise.as_ref())
    });
    let split_plan = plan.as_ref().map(|plan| {
        let when = split::timing(plan, &snapshot.entries, settings, now);
        SplitPlanView { meal_at: plan.meal_at, units: plan.units, due_at: plan.due_at, state: when.state, latest_at: when.latest_at }
    });
    let on_board = active.as_ref().map(|a| on_board_view(a, &suggestion, split_plan.clone()));
    let q1 = (meal_values.len() >= forecast::MIN_MEALS).then(|| stats::quartile(&meal_values, dec!(0.25)));
    let surcharge_choice = calculated.as_ref().map(|c| match c.level {
        Some(_) => SurchargeOffer::Level(false),
        None => SurchargeOffer::Choice(Box::new(SurchargeChoice {
            mode: if use_fpe { "fpe" } else { "static" },
            static_mgdl: meal_surcharge_mgdl(settings, fat), static_fat: fat,
            fpe: c.fpe.round_dp_with_strategy(1, rust_decimal::RoundingStrategy::MidpointNearestEven),
            fpe_mgdl: c.mgdl, fpe_minutes: c.minutes, per_fpe_mgdl: settings.fpe_surcharge_mgdl,
            fat_g: payload.get("fat_g").cloned().unwrap_or(Value::Null),
            protein_g: payload.get("protein_g").cloned().unwrap_or(Value::Null),
        })),
    });
    Ok(DoseHelp {
        suggestion, correction_scale, split_plan, on_board,
        auto_meal: kind, auto_factor_count: prediction.count, auto_method: settings.auto_method,
        auto_fat_surcharge_mgdl: (surcharge && fat && !use_fpe).then_some(fat_mgdl),
        surcharge_levels: surcharge && meal_surcharge.is_some(),
        surcharge_choice,
        place_factor: place.map(|(name, s)| PlaceFactorView { name: name.clone(), count: s.count, min_meals: s.min_meals, days: s.days, scale: s.scale }),
        auto_factor_min_meals: forecast::MIN_MEALS, auto_percentile: percentile,
        auto_half_life_days: half_life, auto_factor_days: lookback,
        auto_median_units_per_ke: breakfast_median.filter(|_| !meal_values.is_empty()),
        auto_q1_units_per_ke: q1,
        day_type, day_type_source, breakfast: is_breakfast,
        factor_source: if use_factor { "breakfast_day_type" } else { "nightscout" },
        breakfast_factor_count: is_breakfast.then_some(meal_values.len()),
        breakfast_factor_days: is_breakfast.then_some(BREAKFAST_FACTOR_DAYS),
        breakfast_factor_min_meals: BREAKFAST_FACTOR_MIN_MEALS,
        breakfast_factor_units_per_ke: if is_breakfast { breakfast_median } else { None },
    })
}

fn on_board_view(active: &Summary, result: &dosing::Suggestion, split_plan: Option<SplitPlanView>) -> OnBoardView {
    let e = &active.effects;
    let expected = result.glucose_mgdl.filter(|_| result.glucose_fresh).map(|g| g - e.lowering_mgdl + e.raising_mgdl);
    let (below, above) = on_board::forecast_band(e.lowering_mgdl, e.raising_mgdl);
    OnBoardView {
        glucose_mgdl: result.glucose_mgdl, glucose_fresh: result.glucose_fresh, glucose_source: result.glucose_source,
        insulin_units: e.insulin_units,
        lowering_mgdl: e.lowering_mgdl, lowering_until: active.lowering_until,
        raising_mgdl: e.raising_mgdl, raising_until: active.raising_until,
        carbs_mgdl: e.carbs_mgdl, surcharge_mgdl: e.surcharge_mgdl, sport_mgdl: e.sport_mgdl,
        sports: active.sports.iter().map(|row| SportRest { at: row.session.at, end: row.session.end, kind: row.session.kind.clone(),
            mgdl: row.session.mgdl, remaining_mgdl: row.remaining_mgdl }).collect(),
        carbs_remaining_g: active.carbs.iter().map(|row| row.remaining_grams).sum(),
        expected_mgdl: expected,
        expected_low_mgdl: expected.map(|x| x - below),
        expected_high_mgdl: expected.map(|x| x + above),
        expected_at: active.settled_at, fresh_meal_at: active.fresh_meal_at,
        split_plan,
        doses: active.doses.iter().map(|d| DoseRest { at: d.at, units: d.units, remaining_units: d.remaining_units }).collect(),
        carbs: active.carbs.iter().map(|c| CarbRest { at: c.at, grams: c.grams, remaining_grams: c.remaining_grams }).collect(),
    }
}
