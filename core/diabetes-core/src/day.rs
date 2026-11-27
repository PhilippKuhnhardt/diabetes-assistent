//! One diary day (app/services/day_view.py compute_day with app/analysis/night.py): the day's meals with
//! their factors, windows, sport shares and marks, the timeline, the night and the quiet stretches, from a
//! snapshot of the store (day_view.day_snapshot) instead of SQLite.
//!
//! The result is the day payload the server stores and the page draws, in the reference's JSON shape:
//! instants as Python's isoformat in UTC, computed amounts as decimal strings, Nightscout's own amounts
//! (carbs, insulin, duration) as they came.

use crate::calculation::{self, Episode, Interpolator, ManualWindow, MealCalculation};
use crate::context::{local_zone, t, tr};
use crate::episodes;
use crate::fpe;
use crate::glucose;
use crate::model::{decimal_of, Instant, Reading, Settings, SurchargePlan, Treatment};
use crate::on_board;
use crate::services::{self, VacationPeriod};
pub use crate::wire::iso;
use crate::wire::{py_iso, py_iso_opt, to_value, Py};
use crate::{CoreError, Res};
use chrono::{Duration, NaiveDate, NaiveTime};
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::collections::{HashMap, HashSet};

/// After a rapid dose before its effect no longer dominates the night curve.
pub const NIGHT_GAP_MINUTES: i64 = 180;
/// A rapid dose this close before breakfast ends the night.
pub const MORNING_MINUTES: i64 = 120;
/// After a rapid dose or carbs before glucose shows only the basal; shorter stretches say too little.
pub const QUIET_AFTER_MINUTES: i64 = 180;
pub const QUIET_MIN_MINUTES: i64 = 120;

#[derive(Clone, Debug, Deserialize)]
pub struct SettingsVersion {
    pub id: i64,
    pub effective_at: Instant,
    pub settings: Value,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Named {
    pub id: i64,
    pub name: String,
}

#[derive(Clone, Debug, Deserialize)]
pub struct MealRecord {
    #[serde(default)]
    pub dish_id: Option<i64>,
    #[serde(default)]
    pub description: String,
}

/// Everything compute_day reads (day_view.day_snapshot).
#[derive(Clone, Debug, Deserialize)]
pub struct Snapshot {
    pub day: NaiveDate,
    #[serde(default)]
    pub connected: bool,
    #[serde(default)]
    pub synced_at: Option<String>,
    pub settings_id: i64,
    pub settings: Value,
    #[serde(default)]
    pub settings_versions: Vec<SettingsVersion>,
    #[serde(default)]
    pub raw_entries: Vec<Value>,
    #[serde(default)]
    pub raw_treatments: Vec<Value>,
    #[serde(default)]
    pub basal_doses: Vec<Value>,
    #[serde(default)]
    pub sport_session_effects: HashMap<String, Value>,
    #[serde(default)]
    pub sport_session_minutes: HashMap<String, Value>,
    #[serde(default)]
    pub analysis_windows: HashMap<String, ManualWindow>,
    #[serde(default)]
    pub analysis_exclusions: HashSet<String>,
    #[serde(default)]
    pub day_types: Option<HashMap<NaiveDate, String>>,
    #[serde(default)]
    pub vacation_periods: Vec<VacationPeriod>,
    #[serde(default)]
    pub places: Vec<Named>,
    #[serde(default)]
    pub entry_places: HashMap<String, i64>,
    #[serde(default)]
    pub dishes: Vec<Named>,
    #[serde(default)]
    pub meal_records: HashMap<String, MealRecord>,
}

fn settings_of(value: &Value) -> Res<Settings> {
    serde_json::from_value(value.clone()).map_err(|error| CoreError::Value(format!("settings: {error}")))
}

fn minutes(value: Decimal) -> Duration {
    Duration::microseconds((value * dec!(60000000)).trunc().try_into().unwrap_or(0))
}

/// The raw Nightscout amounts of a treatment (carbs, insulin, basal units, duration) as they came: the
/// reference keeps `item.get(key) or 0`.
struct Raw(HashMap<String, Map<String, Value>>);

impl Raw {
    fn new(raw: &[Value]) -> Raw {
        let mut found = HashMap::new();
        for item in raw {
            let id = item.get("_id").or_else(|| item.get("identifier")).map(|v| match v {
                Value::String(text) => text.clone(),
                other => other.to_string(),
            }).unwrap_or_default();
            let mut fields = Map::new();
            for key in ["carbs", "insulin", "basal_units", "duration"] {
                fields.insert(key.into(), match item.get(key) {
                    None | Some(Value::Null) | Some(Value::Bool(false)) => json!(0),
                    Some(Value::String(text)) if text.is_empty() => json!(0),
                    Some(Value::Number(number)) if number.as_f64() == Some(0.0) => json!(0),
                    Some(other) => other.clone(),
                });
            }
            found.insert(id, fields);
        }
        Raw(found)
    }

    fn get(&self, item: &Treatment, key: &str) -> Value {
        self.0.get(&item.id).and_then(|fields| fields.get(key)).cloned().unwrap_or_else(|| {
            let value = match key {
                "carbs" => item.carbs,
                "insulin" => item.insulin,
                "basal_units" => item.basal_units,
                _ => item.duration,
            };
            if value.is_zero() { json!(0) } else { Value::String(value.to_string()) }
        })
    }
}

/// episodes.serialize_treatment of a normalized treatment: its fields with Nightscout's own amounts.
fn serialize_treatment(item: &Treatment, raw: &Raw) -> Map<String, Value> {
    let mut out = match to_value(item) {
        Value::Object(map) => map,
        _ => Map::new(),
    };
    for key in ["carbs", "insulin", "basal_units", "duration"] {
        out.insert(key.into(), raw.get(item, key));
    }
    out
}

/// The sport kind names (episodes.sport_names): built-in kinds in the configured language, then the user's own.
pub(crate) fn sport_names(settings: &Value) -> Vec<(String, String)> {
    let mut names: Vec<(String, String)> = episodes::SPORT_LABELS.iter().map(|(kind, label)| (kind.to_string(), t(label))).collect();
    if let Some(Value::Object(custom)) = settings.get("sport_custom") {
        for (kind, name) in custom {
            let name = name.as_str().unwrap_or_default().to_string();
            match names.iter_mut().find(|(known, _)| known == kind) {
                Some(entry) => entry.1 = name,
                None => names.push((kind.clone(), name)),
            }
        }
    }
    names
}

/// The labels a sport note may start with (episodes.note_labels): built-in kinds in either language and own names.
fn note_labels(settings: &Value) -> Vec<String> {
    let mut labels: Vec<String> = Vec::new();
    let built_in = episodes::SPORT_LABELS.iter().map(|(_, label)| label.to_string())
        .chain(episodes::SPORT_LABELS.iter().map(|(_, label)| t(label)));
    let custom = settings.get("sport_custom").and_then(Value::as_object).into_iter()
        .flat_map(|map| map.values().map(|name| name.as_str().unwrap_or_default().to_string()));
    for label in built_in.chain(custom) {
        if !labels.contains(&label) {
            labels.push(label);
        }
    }
    labels
}

fn strip_tags(notes: &str) -> String {
    episodes::quality_re().replace_all(&episodes::event_tag_re().replace_all(&episodes::tag_re().replace_all(notes, ""), ""), "").to_string()
}

struct Marks {
    effects: HashMap<String, Decimal>,
    minutes: HashMap<String, i64>,
}

fn int(value: Decimal) -> i64 {
    value.trunc().try_into().unwrap_or(0)
}

fn instant_of(value: Option<&str>) -> Option<Instant> {
    value.and_then(|text| chrono::DateTime::parse_from_rfc3339(text).ok()).map(|at| at.with_timezone(&chrono::Utc))
}

/// A calculated fat/protein surcharge as the page shows it.
#[derive(Clone, Debug, Serialize)]
pub struct Surcharge {
    pub mgdl: Decimal,
    pub minutes: i64,
}

impl From<&SurchargePlan> for Surcharge {
    fn from(plan: &SurchargePlan) -> Self {
        Surcharge { mgdl: plan.mgdl, minutes: plan.minutes }
    }
}

/// A sport session's effect and how long it acts, with the defaults of its kind.
#[derive(Clone, Debug, Serialize)]
pub struct SportSession {
    pub sport_effect_mgdl: Decimal,
    pub sport_effect_default_mgdl: Decimal,
    pub sport_effect_custom: bool,
    pub sport_minutes: i64,
    pub sport_minutes_default: i64,
    pub sport_minutes_custom: bool,
}

impl SportSession {
    fn of(item: &Treatment, settings: &Settings, marks: &Marks) -> Self {
        SportSession {
            sport_effect_mgdl: on_board::sport_effect(item, settings, &marks.effects),
            sport_effect_default_mgdl: on_board::sport_effect(item, settings, &HashMap::new()),
            sport_effect_custom: marks.effects.contains_key(&item.id),
            sport_minutes: int(on_board::sport_minutes(item, settings, &marks.minutes)),
            sport_minutes_default: int(on_board::sport_minutes(item, settings, &HashMap::new())),
            sport_minutes_custom: marks.minutes.contains_key(&item.id),
        }
    }
}

fn effect_span(item: &Treatment, settings: &Settings, marks: &Marks) -> (Instant, Instant) {
    let end = item.at + minutes(item.duration);
    (end, end + minutes(on_board::sport_minutes(item, settings, &marks.minutes)))
}

/// What is still open on a meal before its day counts as clean.
#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct MealReview {
    #[serde(default)]
    pub open: Vec<String>,
    #[serde(default)]
    pub calculated: bool,
    #[serde(default)]
    pub excludable: bool,
}

impl MealReview {
    /// Only a meal without a factor is open (blocked); a calculated one can be excluded.
    pub fn new(calculated: bool) -> Self {
        MealReview { open: if calculated { Vec::new() } else { vec!["blocked".into()] }, calculated, excludable: calculated }
    }
}

/// The review of a stored meal's calculation.
pub fn meal_review(calculation: &Value) -> MealReview {
    MealReview::new(calculation.get("factor_units_per_ke").is_some_and(|v| !v.is_null()))
}

/// A meal as the day's review reads it.
#[derive(Clone, Debug, Deserialize)]
pub struct ReviewedMeal {
    #[serde(default)]
    pub id: String,
    #[serde(default)]
    pub review: MealReview,
    #[serde(default)]
    pub excluded: bool,
}

#[derive(Clone, Debug, Serialize)]
pub struct DayReview {
    pub blocked: usize,
    pub calculated: usize,
    pub excluded: usize,
    pub first_open: Option<String>,
}

pub fn day_review(meals: &[ReviewedMeal]) -> DayReview {
    DayReview {
        blocked: meals.iter().filter(|m| m.review.open.iter().any(|item| item == "blocked")).count(),
        calculated: meals.iter().filter(|m| m.review.calculated && !m.excluded).count(),
        excluded: meals.iter().filter(|m| m.review.calculated && m.excluded).count(),
        first_open: meals.iter().find(|m| !m.review.open.is_empty()).map(|m| m.id.clone()),
    }
}

/// A stored meal as the checks read it (payloads stored by older versions may lack fields).
#[derive(Clone, Debug, Deserialize)]
pub struct StoredMeal {
    #[serde(default)]
    pub id: Option<String>,
    #[serde(default)]
    pub window: Option<StoredWindow>,
    #[serde(default)]
    pub review: Option<Map<String, Value>>,
    #[serde(default)]
    pub excluded: Option<bool>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct StoredWindow {
    #[serde(default)]
    pub start: Option<String>,
    #[serde(default)]
    pub end: Option<String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct Checked {
    pub meals: Vec<Map<String, Value>>,
    pub review: CheckedDay,
}

#[derive(Clone, Debug, Serialize)]
pub struct CheckedDay {
    pub checked: usize,
    pub tasks: usize,
    pub total: usize,
    /// What still waits for a look.
    pub unchecked: usize,
    /// Whether "Tag prüfen" can check it all (every meal finished).
    pub checkable: bool,
    pub done: bool,
}

/// The user's checks laid over a day at request time, so a check never recalculates a stored day:
/// "Passt" on a meal holds only for the window it was given for, and only a finished meal can be checked.
/// Only main meals need a check (reviewing is about the factor), and the day is done once every meal is
/// checked or excluded. `checks` maps a meal id to the (start, end) it was checked for. Returns each
/// meal's review and the day's review fields.
pub fn with_checks(meals: &[StoredMeal], checks: &HashMap<String, (String, String)>, now: Instant) -> Checked {
    struct Look { checked: bool, checkable: bool, open: bool, excluded: bool }
    let mut reviews = Vec::new();
    let mut looks = Vec::new();
    for meal in meals {
        let start = instant_of(meal.window.as_ref().and_then(|w| w.start.as_deref()));
        let end = instant_of(meal.window.as_ref().and_then(|w| w.end.as_deref()));
        let stored = meal.id.as_ref().and_then(|id| checks.get(id));
        let checked = match (stored, start, end) {
            (Some((from, to)), Some(start), Some(end)) => instant_of(Some(from)) == Some(start) && instant_of(Some(to)) == Some(end),
            _ => false,
        };
        let checkable = end.is_some_and(|end| end <= now);
        let mut review = meal.review.clone().unwrap_or_default();
        let open = review.get("open").is_some_and(|o| o.as_array().is_some_and(|a| !a.is_empty()) || o.as_bool() == Some(true));
        review.insert("checked".into(), json!(checked));
        review.insert("checkable".into(), json!(checkable));
        reviews.push(review);
        looks.push(Look { checked, checkable, open, excluded: meal.excluded.unwrap_or(false) });
    }
    let unchecked = looks.iter().filter(|look| !look.checked && !look.excluded).count();
    Checked {
        meals: reviews,
        review: CheckedDay {
            checked: looks.iter().filter(|look| look.checked).count(),
            tasks: looks.iter().filter(|look| look.open && !look.checked && !look.excluded).count(),
            total: meals.len(),
            unchecked,
            checkable: looks.iter().all(|look| look.checkable),
            done: !meals.is_empty() && unchecked == 0,
        },
    }
}

/// The calculation of a meal: the factor, or why there is none with the window it would use.
#[derive(Clone, Debug, Serialize)]
#[serde(untagged)]
pub enum MealCalc {
    Done {
        #[serde(flatten)]
        result: Py<Box<MealCalculation>>,
        settings_version: i64,
        /// The Nightscout profile in force, as Nightscout wrote it.
        nightscout_profile: Value,
    },
    /// Blocked: the window stays editable.
    Blocked {
        reason: String,
        #[serde(serialize_with = "py_iso")]
        auto_start: Instant,
        #[serde(serialize_with = "py_iso")]
        auto_end: Instant,
        manual_window: bool,
        #[serde(serialize_with = "py_iso")]
        window_start: Instant,
        #[serde(serialize_with = "py_iso")]
        window_end: Instant,
    },
}

impl MealCalc {
    fn result(&self) -> Option<&MealCalculation> {
        match self {
            MealCalc::Done { result, .. } => Some(&result.0),
            MealCalc::Blocked { .. } => None,
        }
    }

    /// The counted window, the automatic one and whether the user set it.
    fn window(&self) -> (Instant, Instant, Instant, Instant, bool) {
        match self {
            MealCalc::Done { result, .. } => (result.0.start, result.0.end, result.0.auto_start, result.0.auto_end, result.0.manual_window),
            MealCalc::Blocked { auto_start, auto_end, manual_window, window_start, window_end, .. } =>
                (*window_start, *window_end, *auto_start, *auto_end, *manual_window),
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct SportEvent {
    pub id: String,
    #[serde(serialize_with = "py_iso")]
    pub at: Instant,
    pub kind: Option<String>,
    pub effect_mgdl: Decimal,
    pub previous_day: bool,
    #[serde(serialize_with = "py_iso")]
    pub effect_from: Instant,
    #[serde(serialize_with = "py_iso")]
    pub effect_until: Instant,
}

/// The sport a meal's factor counts (always automatic).
#[derive(Clone, Debug, Serialize)]
pub struct MealSport {
    pub status: &'static str,
    pub suggested: bool,
    pub suggested_effect_mgdl: Decimal,
    pub effect_mgdl: Decimal,
    pub events: Vec<SportEvent>,
}

/// The marks of a meal's counted carb entries: the least certain level and each factor any entry has.
#[derive(Clone, Debug, Serialize)]
pub struct Quality {
    pub carbs_certainty: Option<String>,
    pub fat_protein: bool,
    pub alcohol: bool,
    pub fast: bool,
}

#[derive(Clone, Debug, Serialize)]
pub struct WindowReading {
    pub value: Decimal,
    pub tone: &'static str,
}

/// The meal's counted window as the diary draws it, with the CGM value at both handles.
#[derive(Clone, Debug, Serialize)]
pub struct MealWindow {
    #[serde(serialize_with = "py_iso")]
    pub start: Instant,
    #[serde(serialize_with = "py_iso")]
    pub end: Instant,
    #[serde(serialize_with = "py_iso")]
    pub auto_start: Instant,
    #[serde(serialize_with = "py_iso")]
    pub auto_end: Instant,
    pub manual: bool,
    pub start_glucose: Option<WindowReading>,
    pub end_glucose: Option<WindowReading>,
}

impl MealWindow {
    fn of(calc: &MealCalc, reading_at: &Interpolator, settings: &Settings) -> Self {
        let (start, end, auto_start, auto_end, manual) = calc.window();
        let reading = |moment: Instant| reading_at.value(moment)
            .map(|value| WindowReading { value, tone: glucose::tone(Some(value), moment, settings) });
        MealWindow { start, end, auto_start, auto_end, manual, start_glucose: reading(start), end_glucose: reading(end) }
    }

    fn holds(&self, at: Instant) -> bool {
        self.start <= at && at < self.end
    }
}

/// One main meal of the day.
#[derive(Clone, Debug, Serialize)]
pub struct MealView {
    pub id: String,
    pub source_id: String,
    pub imported: bool,
    #[serde(serialize_with = "py_iso")]
    pub at: Instant,
    pub meal: String,
    /// Nightscout's amount as it came.
    pub carbs: Value,
    pub notes: String,
    pub review: MealReview,
    pub calculation: MealCalc,
    pub sport: MealSport,
    pub quality: Quality,
    pub fpe_surcharge: Option<Surcharge>,
    pub place: Option<Named>,
    pub dish: Option<Named>,
    pub excluded: bool,
    /// The meal's entries (serialized treatments).
    pub events: Vec<Map<String, Value>>,
    pub window: MealWindow,
    pub vacation: bool,
}

/// A sport session outside the meals.
#[derive(Clone, Debug, Serialize)]
pub struct Exercise {
    pub id: String,
    #[serde(serialize_with = "py_iso")]
    pub at: Instant,
    pub kind: Option<String>,
    pub label: String,
    pub duration_minutes: Value,
    pub notes: String,
    #[serde(flatten)]
    pub session: SportSession,
}

/// Any other entry of the day outside the meals.
#[derive(Clone, Debug, Serialize)]
pub struct OtherEvent {
    pub id: String,
    #[serde(serialize_with = "py_iso")]
    pub at: Instant,
    #[serde(rename = "eventType")]
    pub event_type: String,
    pub carbs: Value,
    pub insulin: Value,
    pub basal_units: Value,
    pub notes: String,
}

/// A basal dose from the local record, as stored.
#[derive(Clone, Debug, Serialize)]
pub struct BasalRow {
    pub id: Value,
    pub at: Value,
    pub units: Value,
    pub notes: Value,
    pub nightscout_synced: Value,
}

/// A CGM reading for the charts.
#[derive(Clone, Debug, Serialize)]
pub struct EntryPoint {
    #[serde(serialize_with = "py_iso")]
    pub at: Instant,
    pub sgv: Value,
    pub tone: &'static str,
}

#[derive(Clone, Debug, Serialize)]
pub struct SportShare {
    pub meal_id: String,
    pub meal: String,
    pub effect_mgdl: Decimal,
}

/// Where a session's effect lands.
#[derive(Clone, Debug, Serialize)]
pub struct SportShares {
    pub sport_shares: Vec<SportShare>,
    pub sport_rest_mgdl: String,
    #[serde(serialize_with = "py_iso_opt")]
    pub sport_effect_until: Option<Instant>,
}

/// The kind of a timeline row.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum RowKind {
    Basal,
    Exercise,
    Finger,
    Meal,
    Snack,
    Carbs,
    Correction,
    Other,
}

impl RowKind {
    fn of(item: &Treatment) -> Self {
        let event_type = item.event_type.as_str();
        if event_type == "Basal" { RowKind::Basal }
        else if event_type == "Exercise" { RowKind::Exercise }
        else if item.finger_mgdl.is_some() { RowKind::Finger }
        else if event_type == "Meal Bolus" && item.carbs > Decimal::ZERO { RowKind::Meal }
        else if event_type == "Snack Bolus" || item.notes.contains("[DA2:snack]") { RowKind::Snack }
        else if event_type == "Carb Correction" { RowKind::Carbs }
        else if matches!(event_type, "Correction Bolus" | "Meal Bolus" | "Bolus") && item.insulin > Decimal::ZERO { RowKind::Correction }
        else { RowKind::Other }
    }

    fn has_carbs(self) -> bool {
        matches!(self, RowKind::Meal | RowKind::Snack | RowKind::Carbs)
    }
}

/// One entry of the day in time order.
#[derive(Clone, Debug, Serialize)]
pub struct TimelineRow {
    pub id: String,
    #[serde(serialize_with = "py_iso")]
    pub at: Instant,
    pub kind: RowKind,
    #[serde(rename = "eventType")]
    pub event_type: String,
    pub carbs: Value,
    pub insulin: Value,
    pub basal_units: Value,
    pub duration_minutes: Value,
    pub sport_kind: Option<String>,
    pub notes: String,
    #[serde(flatten)]
    pub session: Option<SportSession>,
    pub carbs_certainty: Option<String>,
    pub fat_protein: bool,
    pub alcohol: bool,
    pub fast: bool,
    pub place: Option<Named>,
    pub dish: Option<Named>,
    pub event_id: Option<String>,
    pub surcharge: Option<Surcharge>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fpe_level: Option<&'static str>,
    pub description: String,
    pub glucose_mgdl: Option<Decimal>,
    #[serde(serialize_with = "py_iso_opt")]
    pub glucose_at: Option<Instant>,
    /// Only for basal doses.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub nightscout_synced: Option<Value>,
    pub meal_id: Option<String>,
    #[serde(flatten)]
    pub shares: Option<SportShares>,
}

/// A serialized treatment with whether it is rapid insulin.
#[derive(Clone, Debug, Serialize)]
pub struct TreatmentRow {
    #[serde(flatten)]
    pub treatment: Map<String, Value>,
    pub rapid: bool,
}

/// The day payload the server stores and the page draws.
#[derive(Clone, Debug, Serialize)]
pub struct DayPayload {
    pub day: String,
    pub connected: bool,
    #[serde(serialize_with = "py_iso")]
    pub day_start: Instant,
    #[serde(serialize_with = "py_iso")]
    pub day_end: Instant,
    pub timezone: String,
    pub day_type: Option<String>,
    pub day_type_source: &'static str,
    pub vacation: bool,
    pub review: DayReview,
    pub synced_at: Option<String>,
    pub sync_error: Option<String>,
    pub profile_sync_error: Option<String>,
    pub settings_version: i64,
    pub meals: Vec<MealView>,
    pub exercises: Vec<Exercise>,
    pub other_events: Vec<OtherEvent>,
    pub timeline: Vec<TimelineRow>,
    pub basal: Vec<BasalRow>,
    pub basal_sync_error: Option<String>,
    pub night: Night,
    pub quiet_periods: Vec<QuietPeriod>,
    pub entries: Vec<EntryPoint>,
    pub target_bands: Py<Vec<glucose::Band>>,
    pub treatments: Vec<TreatmentRow>,
    pub window_limits: Value,
    pub place_options: Vec<Named>,
    pub dish_options: Vec<Named>,
}

/// One diary day from its snapshot.
pub fn compute_day(snapshot: &Snapshot) -> Res<DayPayload> {
    let day = snapshot.day;
    let raw_settings = &snapshot.settings;
    let settings = settings_of(raw_settings)?;
    let zone = local_zone(&settings.timezone);
    let raw = Raw::new(&snapshot.raw_treatments);
    let entries = episodes::normalize_entries(&snapshot.raw_entries);
    let mut treatments = episodes::normalize_treatments(&snapshot.raw_treatments);
    episodes::group_diary_followups(&mut treatments, &settings);
    let meals = episodes::meals_for_day(&treatments, day, &settings);
    let marks = Marks {
        effects: snapshot.sport_session_effects.iter().filter_map(|(k, v)| decimal_of(v).map(|v| (k.clone(), v))).collect(),
        minutes: snapshot.sport_session_minutes.iter().filter_map(|(k, v)| decimal_of(v).map(|v| (k.clone(), int(v)))).collect(),
    };
    let reading_at = Interpolator::new(&entries, 15);
    let versions: Vec<(i64, Instant, Settings, &Value)> = snapshot.settings_versions.iter()
        .map(|version| Ok((version.id, version.effective_at, settings_of(&version.settings)?, &version.settings)))
        .collect::<Res<_>>()?;
    // config.get_settings_at: the newest version in force at the moment, else the current one.
    let settings_at = |moment: Instant| -> (i64, &Settings, &Value) {
        versions.iter().filter(|(_, effective, _, _)| *effective <= moment)
            .max_by(|a, b| a.1.cmp(&b.1).then(a.0.cmp(&b.0)))
            .map(|(id, _, parsed, raw)| (*id, parsed, *raw))
            .unwrap_or((snapshot.settings_id, &settings, raw_settings))
    };
    let place_names: HashMap<i64, &Named> = snapshot.places.iter().map(|place| (place.id, place)).collect();
    let dish_names: HashMap<i64, &Named> = snapshot.dishes.iter().map(|dish| (dish.id, dish)).collect();
    let place_of = |item: &Treatment| -> Option<Named> {
        item.event_id.as_ref().and_then(|id| snapshot.entry_places.get(id)).and_then(|id| place_names.get(id)).map(|p| (*p).clone())
    };
    let dish_of = |item: &Treatment| -> Option<Named> {
        item.event_id.as_ref().and_then(|id| snapshot.meal_records.get(id)).and_then(|record| record.dish_id)
            .and_then(|id| dish_names.get(&id)).map(|d| (*d).clone())
    };
    let vacation = services::in_vacation(day, &snapshot.vacation_periods);
    let main_meal_ids: HashSet<&str> = treatments.iter().filter(|item| item.is_main_meal()).map(|item| item.id.as_str()).collect();
    let mut shown_in_meals: HashSet<String> = HashSet::new();
    let mut rendered: Vec<MealView> = Vec::new();
    for meal in &meals {
        let (meal_settings_id, meal_settings, meal_raw) = settings_at(meal.at);
        let episode = Episode { at: meal.at, source_id: Some(meal.source_id.clone()), carbs: meal.carbs };
        let associated: Vec<&Treatment> = treatments.iter()
            .filter(|item| item.episode_id.as_deref() == Some(meal.id.as_str()) || item.id == meal.source_id).collect();
        let manual = snapshot.analysis_windows.get(&meal.id);
        let automatic = calculation::automatic_window(&episode, &treatments, &entries, meal_settings, true);
        let (span_start, span_end) = manual.map(|w| (w.start, w.end)).unwrap_or((automatic.start, automatic.end));
        let sport_parts: Vec<(&Treatment, Decimal)> = treatments.iter()
            .filter(|item| item.is_exercise() && item.sport_type.is_some() && item.at < span_end)
            .map(|item| (item, on_board::sport_effect(item, meal_settings, &marks.effects)
                * on_board::sport_share(item, span_start, span_end, on_board::sport_minutes(item, meal_settings, &marks.minutes))))
            .filter(|(_, effect)| !effect.is_zero()).collect();
        let auto_effect: Decimal = sport_parts.iter().map(|(_, effect)| *effect).sum();
        let blocked = |reason: String| MealCalc::Blocked {
            reason, auto_start: automatic.start, auto_end: automatic.end, manual_window: manual.is_some(),
            window_start: span_start, window_end: span_end,
        };
        let calc = if meal_settings.isf_schedule.is_empty() {
            blocked(t("Korrekturfaktor-Profil aus Nightscout fehlt"))
        } else {
            match calculation::calculate_episode(&episode, &treatments, &entries, meal_settings, auto_effect, manual) {
                Ok(result) => MealCalc::Done {
                    result: Py(Box::new(result)), settings_version: meal_settings_id,
                    nightscout_profile: meal_raw.get("nightscout_profile").cloned().unwrap_or(Value::Null),
                },
                Err(CoreError::Value(message)) => blocked(message),
                Err(error) => return Err(error),
            }
        };
        let events: Vec<SportEvent> = sport_parts.iter().map(|(item, effect)| {
            let (effect_from, effect_until) = effect_span(item, meal_settings, &marks);
            SportEvent { id: item.id.clone(), at: item.at, kind: item.sport_type.clone(), effect_mgdl: *effect,
                         previous_day: zone.local(&item.at).date() < day, effect_from, effect_until }
        }).collect();
        let result = calc.result();
        let carb_event_ids = result.into_iter().flat_map(|r| r.carb_events.iter().filter_map(|e| e.id.clone()));
        let dose_ids = result.into_iter().flat_map(|r| r.doses.iter().filter_map(|d| d.id.clone()));
        let mut carb_ids: HashSet<String> = carb_event_ids.clone().collect();
        carb_ids.insert(meal.source_id.clone());
        carb_ids.extend(associated.iter().filter(|item| item.carbs > Decimal::ZERO).map(|item| item.id.clone()));
        let counted: Vec<&Treatment> = treatments.iter().filter(|item| carb_ids.contains(&item.id)).collect();
        let quality = Quality {
            carbs_certainty: episodes::least_certain(counted.iter().filter_map(|item| item.carbs_certainty.as_deref())).map(str::to_string),
            fat_protein: counted.iter().any(|item| item.fat_protein),
            alcohol: counted.iter().any(|item| item.alcohol),
            fast: counted.iter().any(|item| item.fast),
        };
        let source = treatments.iter().find(|item| item.id == meal.source_id);
        let mut carb_entries = counted.clone();
        carb_entries.sort_by(|a, b| (a.id != meal.source_id).cmp(&(b.id != meal.source_id)).then(a.at.cmp(&b.at)));
        let included: HashSet<String> = carb_event_ids.chain(dose_ids)
            .filter(|id| !id.is_empty() && !main_meal_ids.contains(id.as_str())).collect();
        shown_in_meals.extend(included.iter().cloned());
        let event_ids: HashSet<&str> = associated.iter().map(|item| item.id.as_str()).collect();
        let mut listed: Vec<&Treatment> = associated.clone();
        listed.extend(treatments.iter().filter(|item| included.contains(&item.id) && !event_ids.contains(item.id.as_str())));
        listed.sort_by_key(|item| item.at);
        let window = MealWindow::of(&calc, &reading_at, &settings);
        rendered.push(MealView {
            id: meal.id.clone(),
            source_id: meal.source_id.clone(),
            imported: meal.imported,
            at: meal.at,
            meal: meal.meal.clone(),
            carbs: source.map(|item| raw.get(item, "carbs")).unwrap_or_else(|| Value::String(meal.carbs.to_string())),
            notes: meal.notes.clone(),
            review: MealReview::new(result.is_some_and(|r| r.factor_units_per_ke.is_some())),
            sport: MealSport { status: "auto", suggested: !sport_parts.is_empty(), suggested_effect_mgdl: auto_effect,
                               effect_mgdl: auto_effect, events },
            quality,
            fpe_surcharge: source.and_then(|item| item.surcharge.as_ref()).map(Surcharge::from),
            place: carb_entries.iter().find_map(|item| place_of(item)),
            dish: carb_entries.iter().find_map(|item| dish_of(item)),
            excluded: snapshot.analysis_exclusions.contains(&meal.id),
            events: listed.iter().map(|item| serialize_treatment(item, &raw)).collect(),
            window,
            vacation,
            calculation: calc,
        });
    }
    let dinner_at = meals.iter().filter(|meal| meal.meal == "dinner").map(|meal| meal.at).max();
    let night = night_analysis(day, &treatments, &entries, &reading_at, dinner_at, &settings);
    let meal_sources: HashSet<&str> = meals.iter().map(|meal| meal.source_id.as_str()).collect();
    let episode_ids: HashSet<&str> = meals.iter().filter(|meal| !meal.imported).map(|meal| meal.id.as_str()).collect();
    let labels = note_labels(raw_settings);
    let names = sport_names(raw_settings);
    let mut other_events = Vec::new();
    let mut exercises = Vec::new();
    for item in &treatments {
        if zone.local(&item.at).date() != day || meal_sources.contains(item.id.as_str()) || shown_in_meals.contains(&item.id)
            || item.episode_id.as_deref().is_some_and(|id| episode_ids.contains(id)) || item.notes.contains(episodes::BASAL_MARKER) {
            continue;
        }
        if item.is_exercise() {
            let mut note = episodes::event_tag_re().replace_all(&episodes::sport_re().replace_all(&item.notes, ""), "").trim().to_string();
            for label in &labels {
                if note == *label || note.starts_with(&format!("{label} —")) || note.starts_with(&format!("{label} |")) {
                    note = note[label.len()..].trim_start_matches([' ', '—', '|']).to_string();
                    break;
                }
            }
            let label = item.sport_type.as_ref().and_then(|kind| names.iter().find(|(k, _)| k == kind)).map(|(_, name)| name.clone())
                .unwrap_or_else(|| t("Sportart unbekannt"));
            exercises.push(Exercise {
                id: item.id.clone(), at: item.at, kind: item.sport_type.clone(), label,
                duration_minutes: raw.get(item, "duration"), notes: note, session: SportSession::of(item, &settings, &marks),
            });
            continue;
        }
        other_events.push(OtherEvent {
            id: item.id.clone(), at: item.at, event_type: item.event_type.clone(), carbs: raw.get(item, "carbs"),
            insulin: raw.get(item, "insulin"), basal_units: raw.get(item, "basal_units"),
            notes: strip_tags(&item.notes).trim().to_string(),
        });
    }
    let timeline = timeline(snapshot, &treatments, &entries, &reading_at, &raw, &settings, &marks, &labels, &place_of, &dish_of, &rendered);
    let (day_type, day_type_source) = services::day_type_for(day, snapshot.day_types.as_ref(), &snapshot.vacation_periods);
    let day_start = zone.from_local(day.and_time(NaiveTime::MIN));
    let day_end = zone.from_local((day + Duration::days(1)).and_time(NaiveTime::MIN));
    let quiet = quiet_periods(day_start, day_end, &treatments, &entries, &reading_at, &settings, &marks);
    let (range_start, range_end) = day_range(day, &settings);
    let basal = snapshot.basal_doses.iter().map(|dose| {
        let field = |key: &str| dose.get(key).cloned().unwrap_or(Value::Null);
        BasalRow { id: field("id"), at: normalized_instant(dose.get("at")), units: field("units"), notes: field("notes"),
                   nightscout_synced: field("nightscout_synced") }
    }).collect();
    let reviewed: Vec<ReviewedMeal> = rendered.iter()
        .map(|meal| ReviewedMeal { id: meal.id.clone(), review: meal.review.clone(), excluded: meal.excluded }).collect();
    Ok(DayPayload {
        day: day.to_string(),
        connected: snapshot.connected,
        day_start,
        day_end,
        timezone: zone.name_at(&(day_start + (day_end - day_start) / 2)),
        day_type,
        day_type_source,
        vacation,
        review: day_review(&reviewed),
        synced_at: snapshot.synced_at.clone(),
        sync_error: None,
        profile_sync_error: None,
        settings_version: snapshot.settings_id,
        meals: rendered,
        exercises,
        other_events,
        timeline,
        basal,
        basal_sync_error: None,
        night,
        quiet_periods: quiet,
        entries: entries.iter().map(|item| EntryPoint { at: item.at, sgv: raw_sgv(item), tone: glucose::tone(item.sgv, item.at, &settings) }).collect(),
        target_bands: Py(glucose::target_bands(range_start, range_end, &settings)),
        treatments: treatments.iter().map(|item| TreatmentRow { treatment: serialize_treatment(item, &raw), rapid: item.is_rapid() }).collect(),
        window_limits: crate::window::limits(),
        place_options: snapshot.places.clone(),
        dish_options: snapshot.dishes.clone(),
    })
}

fn normalized_instant(value: Option<&Value>) -> Value {
    match value.and_then(Value::as_str).and_then(|text| chrono::DateTime::parse_from_rfc3339(text).ok()) {
        Some(at) => json!(iso(&at.with_timezone(&chrono::Utc))),
        None => value.cloned().unwrap_or(Value::Null),
    }
}

/// The reading's sgv: whole numbers as numbers, the rest as decimal text.
fn raw_sgv(item: &Reading) -> Value {
    match item.sgv {
        Some(value) if value.fract().is_zero() => json!(int(value)),
        Some(value) => Value::String(value.to_string()),
        None => Value::Null,
    }
}

/// common.local_day_range: from 10 h before local midnight to 13:00 the next day.
pub fn day_range(day: NaiveDate, settings: &Settings) -> (Instant, Instant) {
    let zone = local_zone(&settings.timezone);
    let start = zone.from_local(day.and_time(NaiveTime::MIN));
    let end = zone.from_local((day + Duration::days(1)).and_time(NaiveTime::from_hms_opt(13, 0, 0).unwrap()));
    (start - Duration::hours(10), end)
}

#[allow(clippy::too_many_arguments)]
fn timeline(snapshot: &Snapshot, treatments: &[Treatment], entries: &[Reading], reading_at: &Interpolator, raw: &Raw,
            settings: &Settings, marks: &Marks, labels: &[String], place_of: &dyn Fn(&Treatment) -> Option<Named>,
            dish_of: &dyn Fn(&Treatment) -> Option<Named>, meals: &[MealView]) -> Vec<TimelineRow> {
    let zone = local_zone(&settings.timezone);
    let day = snapshot.day;
    // Basal notes in Nightscout are shown from the local record, which also holds unsent doses.
    struct Row<'a> { item: Treatment, basal: Option<&'a Value> }
    let mut rows: Vec<Row> = treatments.iter().filter(|item| !item.notes.contains(episodes::BASAL_MARKER))
        .map(|item| Row { item: item.clone(), basal: None }).collect();
    for dose in &snapshot.basal_doses {
        let Some(at) = instant_of(dose.get("at").and_then(Value::as_str)) else { continue };
        rows.push(Row { item: Treatment {
            id: dose.get("id").and_then(Value::as_str).unwrap_or_default().to_string(), at, event_type: "Basal".into(),
            basal_units: dose.get("units").and_then(decimal_of).unwrap_or_default(),
            notes: dose.get("notes").and_then(Value::as_str).unwrap_or_default().to_string(), ..Treatment::default()
        }, basal: Some(dose) });
    }
    rows.sort_by_key(|row| row.item.at);
    let surcharge_method = settings.surcharge_method();
    let mut timeline = Vec::new();
    for Row { item, basal } in rows {
        if zone.local(&item.at).date() != day {
            continue;
        }
        let kind = RowKind::of(&item);
        let mut note = episodes::quality_re().replace_all(&episodes::event_tag_re().replace_all(
            &episodes::sport_re().replace_all(&episodes::tag_re().replace_all(&item.notes, ""), ""), ""), "")
            .replace("[DA2:snack]", "").trim().to_string();
        if kind == RowKind::Exercise {
            for label in labels {
                if note == *label || note.starts_with(&format!("{label} — ")) {
                    note = note[label.len()..].trim_start_matches([' ', '—']).to_string();
                    break;
                }
            }
        }
        let (glucose, glucose_at) = if kind == RowKind::Finger {
            (item.finger_mgdl, Some(item.at))
        } else {
            match reading_at.at(item.at) {
                Ok(point) => (Some(point.value), Some(item.at)),
                Err(_) => {
                    let previous = entries.iter().filter(|point| point.at <= item.at && item.at - point.at <= Duration::minutes(15)).last();
                    match previous {
                        Some(point) => (point.sgv, Some(point.at)),
                        None => match calculation::finger_glucose(treatments, item.at, 15) {
                            Some(finger) => (Some(finger.value), Some(finger.before)),
                            None => (None, None),
                        },
                    }
                }
            }
        };
        let amounts = match basal {
            Some(dose) => [json!(0), json!(0), dose.get("units").cloned().unwrap_or(Value::Null), json!(0)],
            None => ["carbs", "insulin", "basal_units", "duration"].map(|key| raw.get(&item, key)),
        };
        let [carbs, insulin, basal_units, duration_minutes] = amounts;
        let session = (kind == RowKind::Exercise).then(|| SportSession::of(&item, settings, marks));
        let shares = session.as_ref().map(|session| sport_shares(&item, session, meals));
        timeline.push(TimelineRow {
            id: item.id.clone(),
            at: item.at,
            kind,
            event_type: item.event_type.clone(),
            carbs,
            insulin,
            basal_units,
            duration_minutes,
            sport_kind: item.sport_type.clone(),
            notes: note,
            session,
            carbs_certainty: item.carbs_certainty.clone(),
            fat_protein: item.fat_protein,
            alcohol: item.alcohol,
            fast: item.fast,
            place: place_of(&item),
            dish: dish_of(&item),
            event_id: item.event_id.clone(),
            surcharge: item.surcharge.as_ref().map(Surcharge::from),
            fpe_level: (kind.has_carbs() && surcharge_method)
                .then(|| fpe::level_of(item.surcharge.as_ref().map(|plan| plan.minutes), item.fat_protein)),
            description: item.event_id.as_ref().and_then(|id| snapshot.meal_records.get(id))
                .map(|record| record.description.clone()).unwrap_or_default(),
            glucose_mgdl: glucose,
            glucose_at,
            nightscout_synced: basal.map(|dose| dose.get("nightscout_synced").cloned().unwrap_or(Value::Null)),
            meal_id: block_meal_id(&item, meals),
            shares,
        });
    }
    timeline
}

/// The diary groups an entry under its own main meal, else under the first meal whose window holds it.
fn block_meal_id(item: &Treatment, meals: &[MealView]) -> Option<String> {
    meals.iter().find(|meal| meal.source_id == item.id).or_else(|| meals.iter().find(|meal| meal.window.holds(item.at)))
        .map(|meal| meal.id.clone())
}

/// Where a session's effect lands: each meal of the day gets the part acting inside its window; the rest
/// acts between or after the meals.
fn sport_shares(item: &Treatment, session: &SportSession, meals: &[MealView]) -> SportShares {
    let shares: Vec<SportShare> = meals.iter().flat_map(|meal| meal.sport.events.iter().filter(|event| event.id == item.id)
        .map(|event| SportShare { meal_id: meal.id.clone(), meal: meal.meal.clone(), effect_mgdl: event.effect_mgdl })).collect();
    let taken: Decimal = shares.iter().map(|share| share.effect_mgdl).sum();
    let total = session.sport_effect_mgdl;
    let rest = (total - taken).round_dp_with_strategy(1, rust_decimal::RoundingStrategy::MidpointNearestEven);
    let until = item.at + minutes(item.duration + Decimal::from(session.sport_minutes));
    SportShares {
        sport_shares: shares,
        sport_rest_mgdl: format!("{rest:.1}"),
        sport_effect_until: (!total.is_zero()).then_some(until),
    }
}

/// The night of a day, or why there is none.
#[derive(Clone, Debug, Serialize)]
#[serde(untagged)]
pub enum Night {
    Measured(NightResult),
    /// The glucose at the night's start or end is missing.
    NoGlucose {
        reason: String,
        #[serde(serialize_with = "py_iso")]
        dinner_at: Instant,
        #[serde(serialize_with = "py_iso")]
        breakfast_at: Instant,
        #[serde(serialize_with = "py_iso")]
        last_injection_at: Instant,
        #[serde(serialize_with = "py_iso")]
        start: Instant,
        #[serde(serialize_with = "py_iso")]
        end: Instant,
    },
    /// Breakfast or the morning dose come before the night starts.
    TooShort {
        reason: String,
        #[serde(serialize_with = "py_iso")]
        dinner_at: Instant,
        #[serde(serialize_with = "py_iso")]
        breakfast_at: Instant,
        #[serde(serialize_with = "py_iso")]
        last_injection_at: Instant,
        #[serde(serialize_with = "py_iso_opt")]
        morning_dose_at: Option<Instant>,
        #[serde(serialize_with = "py_iso")]
        start: Instant,
        #[serde(serialize_with = "py_iso")]
        end: Instant,
    },
    /// No rapid dose in the evening.
    NoInjection {
        reason: String,
        #[serde(serialize_with = "py_iso")]
        dinner_at: Instant,
        #[serde(serialize_with = "py_iso")]
        breakfast_at: Instant,
    },
    /// No dinner or no breakfast the next day.
    Missing { reason: String },
}

#[derive(Clone, Debug, Serialize)]
pub struct NightResult {
    #[serde(serialize_with = "py_iso")]
    pub dinner_at: Instant,
    #[serde(serialize_with = "py_iso")]
    pub breakfast_at: Instant,
    #[serde(serialize_with = "py_iso")]
    pub last_injection_at: Instant,
    #[serde(serialize_with = "py_iso_opt")]
    pub morning_dose_at: Option<Instant>,
    #[serde(serialize_with = "py_iso")]
    pub start: Instant,
    #[serde(serialize_with = "py_iso")]
    pub end: Instant,
    pub start_glucose: Decimal,
    pub end_glucose: Decimal,
    pub delta_mgdl: Decimal,
    pub minimum_glucose: Decimal,
    #[serde(serialize_with = "py_iso")]
    pub minimum_at: Instant,
    pub drop_mgdl: Decimal,
    pub rise_mgdl: Decimal,
    pub carbs_g: Decimal,
}

/// Night from three hours after the last rapid dose to breakfast (app/analysis/night.py night_analysis).
pub fn night_analysis(day: NaiveDate, treatments: &[Treatment], entries: &[Reading], reading_at: &Interpolator,
                      dinner_at: Option<Instant>, settings: &Settings) -> Night {
    let Some(dinner_at) = dinner_at else {
        return Night::Missing { reason: t("Für diesen Tag ist kein Abendessen erfasst.") };
    };
    let next_meals = episodes::meals_for_day(treatments, day + Duration::days(1), settings);
    let Some(breakfast) = next_meals.iter().filter(|meal| meal.meal == "breakfast" && meal.at > dinner_at).min_by_key(|meal| meal.at) else {
        return Night::Missing { reason: t("Für den folgenden Tag ist kein Frühstück erfasst.") };
    };
    let breakfast_at = breakfast.at;
    let rapid: Vec<Instant> = treatments.iter().filter(|item| dinner_at <= item.at && item.at < breakfast_at && item.has_rapid_dose())
        .map(|item| item.at).collect();
    let morning_from = breakfast_at - Duration::minutes(MORNING_MINUTES);
    let morning_dose_at = rapid.iter().copied().filter(|at| *at >= morning_from).min();
    let Some(last_injection_at) = rapid.iter().copied().filter(|at| *at < morning_from).max() else {
        return Night::NoInjection { reason: t("Zwischen Abendessen und Frühstück ist keine schnell wirksame Injektion erfasst."),
                                    dinner_at, breakfast_at };
    };
    let end = morning_dose_at.unwrap_or(breakfast_at);
    let start = last_injection_at + Duration::minutes(NIGHT_GAP_MINUTES);
    if end <= start {
        return Night::TooShort { reason: t("Frühstück oder Morgendosis kommen vor dem Nachtstart drei Stunden nach der letzten Injektion."),
                                 dinner_at, breakfast_at, last_injection_at, morning_dose_at, start, end };
    }
    let (start_glucose, end_glucose) = match (reading_at.at(start), reading_at.at(end)) {
        (Ok(a), Ok(b)) => (a.value, b.value),
        (Err(error), _) | (_, Err(error)) => {
            return Night::NoGlucose { reason: error.to_string(), dinner_at, breakfast_at, last_injection_at, start, end };
        }
    };
    let mut minimum = (start_glucose, start);
    for candidate in [(end_glucose, end)].into_iter()
        .chain(entries.iter().filter(|item| start < item.at && item.at < end).filter_map(|item| item.sgv.map(|v| (v, item.at)))) {
        if candidate.0 < minimum.0 {
            minimum = candidate;
        }
    }
    Night::Measured(NightResult {
        dinner_at, breakfast_at, last_injection_at, morning_dose_at, start, end, start_glucose, end_glucose,
        delta_mgdl: end_glucose - start_glucose,
        minimum_glucose: minimum.0,
        minimum_at: minimum.1,
        drop_mgdl: minimum.0 - start_glucose,
        rise_mgdl: end_glucose - minimum.0,
        carbs_g: treatments.iter().filter(|item| start <= item.at && item.at < end).map(|item| item.carbs).sum(),
    })
}

/// A stretch without bolus and food: how glucose moved on basal alone.
#[derive(Clone, Debug, Serialize)]
pub struct QuietPeriod {
    #[serde(serialize_with = "py_iso")]
    pub start: Instant,
    #[serde(serialize_with = "py_iso")]
    pub end: Instant,
    pub hours: Decimal,
    pub start_glucose: Decimal,
    pub end_glucose: Decimal,
    pub minimum_glucose: Decimal,
    #[serde(serialize_with = "py_iso")]
    pub minimum_at: Instant,
    pub delta_mgdl: Decimal,
    pub rate_mgdl_per_hour: Decimal,
}

/// Stretches without bolus and food that start on this day (night.quiet_periods).
fn quiet_periods(day_start: Instant, day_end: Instant, treatments: &[Treatment], entries: &[Reading],
                 reading_at: &Interpolator, settings: &Settings, marks: &Marks) -> Vec<QuietPeriod> {
    let mut blocks: Vec<(Instant, Instant)> = Vec::new();
    for item in treatments {
        if item.is_exercise() {
            let sport = on_board::sport_minutes(item, settings, &marks.minutes);
            blocks.push((item.at, item.at + Duration::minutes(int(item.duration)) + Duration::minutes(int(sport))));
        } else if item.has_rapid_dose() || item.carbs > Decimal::ZERO {
            blocks.push((item.at, item.at + Duration::minutes(QUIET_AFTER_MINUTES)));
        }
    }
    blocks.sort();
    let mut readings: Vec<&Reading> = entries.iter().filter(|item| item.sgv.is_some()).collect();
    readings.sort_by_key(|item| item.at);
    let (Some(first), Some(last)) = (readings.first(), readings.last()) else { return Vec::new() };
    let mut stretches = Vec::new();
    let mut cursor = first.at.max(day_start - Duration::hours(10));
    for (begin, until) in blocks {
        if begin > cursor {
            stretches.push((cursor, begin));
        }
        cursor = cursor.max(until);
    }
    stretches.push((cursor, last.at));
    let mut periods = Vec::new();
    for (start, end) in stretches {
        if !(day_start <= start && start < day_end) || end - start < Duration::minutes(QUIET_MIN_MINUTES) {
            continue;
        }
        let (Some(start_glucose), Some(end_glucose)) = (reading_at.value(start), reading_at.value(end)) else { continue };
        let inside: Vec<&&Reading> = readings.iter().filter(|item| start < item.at && item.at < end).collect();
        let mut times = vec![start];
        times.extend(inside.iter().map(|item| item.at));
        times.push(end);
        if times.windows(2).any(|pair| pair[1] - pair[0] > Duration::minutes(30)) {
            continue;
        }
        let mut minimum = (start_glucose, start);
        for candidate in [(end_glucose, end)].into_iter().chain(inside.iter().map(|item| (item.sgv.unwrap(), item.at))) {
            if candidate.0 < minimum.0 {
                minimum = candidate;
            }
        }
        let hours = Decimal::from((end - start).num_microseconds().unwrap_or(0)) / dec!(1000000) / dec!(3600);
        periods.push(QuietPeriod {
            start, end, hours, start_glucose, end_glucose, minimum_glucose: minimum.0, minimum_at: minimum.1,
            delta_mgdl: end_glucose - start_glucose, rate_mgdl_per_hour: (end_glucose - start_glucose) / hours,
        });
    }
    periods
}

/// Text helpers re-exported for the API's error messages.
pub fn reason(text: &str) -> String {
    tr(text, &[])
}
