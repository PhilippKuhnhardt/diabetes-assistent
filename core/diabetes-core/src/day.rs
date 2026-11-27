//! One diary day (app/services/day_view.py compute_day with app/analysis/night.py): the day's meals with
//! their factors, windows, sport shares and marks, the timeline, the night and the quiet stretches, from a
//! snapshot of the store (day_view.day_snapshot) instead of SQLite.
//!
//! The result is the day payload the server stores and the page draws, in the reference's JSON shape:
//! instants as Python's isoformat in UTC, computed amounts as decimal strings, Nightscout's own amounts
//! (carbs, insulin, duration) as they came.

use crate::calculation::{self, Episode, Interpolator, ManualWindow};
use crate::context::{local_zone, t, tr};
use crate::episodes::{self, MealRoot};
use crate::fpe;
use crate::glucose;
use crate::model::{decimal_of, Instant, Reading, Settings, Treatment};
use crate::on_board;
use crate::services::{self, VacationPeriod};
use crate::{CoreError, Res};
use chrono::{Duration, NaiveDate, NaiveTime, Timelike};
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use serde::Deserialize;
use serde_json::{json, Map, Value};
use std::collections::{HashMap, HashSet};

/// Manual-window limits (app/services/settings.py): start within this long before or after the meal,
/// at least MIN and at most MAX long.
pub const MANUAL_WINDOW_LEAD_MINUTES: i64 = 120;
pub const MANUAL_WINDOW_LAG_MINUTES: i64 = 120;
pub const MANUAL_WINDOW_MIN_MINUTES: i64 = 30;
pub const MANUAL_WINDOW_MAX_MINUTES: i64 = 600;
/// After a rapid dose before its effect no longer dominates the night curve.
pub const NIGHT_GAP_MINUTES: i64 = 180;
/// A rapid dose this close before breakfast ends the night.
pub const MORNING_MINUTES: i64 = 120;
/// After a rapid dose or carbs before glucose shows only the basal; shorter stretches say too little.
pub const QUIET_AFTER_MINUTES: i64 = 180;
pub const QUIET_MIN_MINUTES: i64 = 120;
const QUALITY_MARKERS: [&str; 3] = ["fat_protein", "alcohol", "fast"];

#[derive(Clone, Debug, Deserialize)]
pub struct SettingsVersion {
    pub id: i64,
    pub effective_at: Instant,
    pub settings: Value,
}

#[derive(Clone, Debug, Deserialize)]
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

/// An instant as Python's datetime.isoformat() in UTC: microseconds only when there are any.
pub fn iso(at: &Instant) -> String {
    let micros = at.nanosecond() / 1000;
    if micros == 0 {
        at.format("%Y-%m-%dT%H:%M:%S+00:00").to_string()
    } else {
        format!("{}.{:06}+00:00", at.format("%Y-%m-%dT%H:%M:%S"), micros)
    }
}

fn text(value: Decimal) -> Value {
    Value::String(value.to_string())
}

fn settings_of(value: &Value) -> Res<Settings> {
    serde_json::from_value(value.clone()).map_err(|error| CoreError::Value(format!("settings: {error}")))
}

/// Every string chrono wrote as an RFC 3339 instant ("…Z") in Python's isoformat instead.
fn python_instants(value: &mut Value) {
    match value {
        Value::String(text) if text.len() >= 20 && text.ends_with('Z') && text.as_bytes().get(10) == Some(&b'T') => {
            if let Ok(parsed) = chrono::DateTime::parse_from_rfc3339(text) {
                *text = iso(&parsed.with_timezone(&chrono::Utc));
            }
        }
        Value::Array(items) => items.iter_mut().for_each(python_instants),
        // The Nightscout profile is passed through as Nightscout wrote it.
        Value::Object(map) => map.iter_mut().filter(|(key, _)| key.as_str() != "nightscout_profile")
            .for_each(|(_, item)| python_instants(item)),
        _ => {}
    }
}

fn to_value<T: serde::Serialize>(value: &T) -> Value {
    let mut value = serde_json::to_value(value).unwrap_or(Value::Null);
    python_instants(&mut value);
    value
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
            if value.is_zero() { json!(0) } else { text(value) }
        })
    }
}

/// episodes.serialize_treatment of a normalized treatment.
fn serialize_treatment(item: &Treatment, raw: &Raw) -> Map<String, Value> {
    let mut out = match to_value(item) {
        Value::Object(map) => map,
        _ => Map::new(),
    };
    for key in ["carbs", "insulin", "basal_units", "duration"] {
        out.insert(key.into(), raw.get(item, key));
    }
    if let Some(Value::Object(plan)) = out.get_mut("split") {
        if let Some(units) = plan.get("units").and_then(decimal_of) {
            plan.insert("units".into(), Value::String(units.to_string()));
        }
    }
    if let Some(Value::Object(plan)) = out.get_mut("surcharge") {
        if let Some(mgdl) = plan.get("mgdl").and_then(decimal_of) {
            plan.insert("mgdl".into(), Value::String(mgdl.to_string()));
        }
    }
    out
}

/// The sport kind names (episodes.sport_names): built-in kinds in the configured language, then the user's own.
fn sport_names(settings: &Value) -> Vec<(String, String)> {
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

fn sport_session(item: &Treatment, settings: &Settings, marks: &Marks) -> Map<String, Value> {
    let none_effects = HashMap::new();
    let none_minutes = HashMap::new();
    let mut out = Map::new();
    out.insert("sport_effect_mgdl".into(), text(on_board::sport_effect(item, settings, &marks.effects)));
    out.insert("sport_effect_default_mgdl".into(), text(on_board::sport_effect(item, settings, &none_effects)));
    out.insert("sport_effect_custom".into(), json!(marks.effects.contains_key(&item.id)));
    out.insert("sport_minutes".into(), json!(int(on_board::sport_minutes(item, settings, &marks.minutes))));
    out.insert("sport_minutes_default".into(), json!(int(on_board::sport_minutes(item, settings, &none_minutes))));
    out.insert("sport_minutes_custom".into(), json!(marks.minutes.contains_key(&item.id)));
    out
}

fn int(value: Decimal) -> i64 {
    value.trunc().try_into().unwrap_or(0)
}

fn effect_span(item: &Treatment, settings: &Settings, marks: &Marks) -> (Instant, Instant) {
    let end = item.at + minutes(item.duration);
    (end, end + minutes(on_board::sport_minutes(item, settings, &marks.minutes)))
}

fn instant_of(value: Option<&Value>) -> Option<Instant> {
    value.and_then(Value::as_str).and_then(|text| chrono::DateTime::parse_from_rfc3339(text).ok())
        .map(|at| at.with_timezone(&chrono::Utc))
}

pub fn meal_review(calculation: &Value) -> Value {
    let calculated = calculation.get("factor_units_per_ke").is_some_and(|v| !v.is_null());
    json!({"open": if calculated { json!([]) } else { json!(["blocked"]) }, "calculated": calculated, "excludable": calculated})
}

pub fn day_review(meals: &[Map<String, Value>]) -> Value {
    let open = |meal: &Map<String, Value>| meal["review"]["open"].as_array().is_some_and(|items| !items.is_empty());
    let calculated = |meal: &Map<String, Value>| meal["review"]["calculated"].as_bool().unwrap_or(false);
    let excluded = |meal: &Map<String, Value>| meal["excluded"].as_bool().unwrap_or(false);
    json!({
        "blocked": meals.iter().filter(|m| m["review"]["open"].as_array().is_some_and(|items| items.iter().any(|i| i == "blocked"))).count(),
        "calculated": meals.iter().filter(|m| calculated(m) && !excluded(m)).count(),
        "excluded": meals.iter().filter(|m| calculated(m) && excluded(m)).count(),
        "first_open": meals.iter().find(|m| open(m)).map(|m| m["id"].clone()).unwrap_or(Value::Null),
    })
}

/// One diary day from its snapshot.
pub fn compute_day(snapshot: &Snapshot) -> Res<Value> {
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
    let place_names: HashMap<i64, &str> = snapshot.places.iter().map(|place| (place.id, place.name.as_str())).collect();
    let dish_names: HashMap<i64, &str> = snapshot.dishes.iter().map(|dish| (dish.id, dish.name.as_str())).collect();
    let place_of = |item: &Treatment| -> Value {
        item.event_id.as_ref().and_then(|id| snapshot.entry_places.get(id))
            .and_then(|id| place_names.get(id).map(|name| json!({"id": id, "name": name}))).unwrap_or(Value::Null)
    };
    let dish_of = |item: &Treatment| -> Value {
        item.event_id.as_ref().and_then(|id| snapshot.meal_records.get(id)).and_then(|record| record.dish_id)
            .and_then(|id| dish_names.get(&id).map(|name| json!({"id": id, "name": name}))).unwrap_or(Value::Null)
    };
    let main_meal_ids: HashSet<&str> = treatments.iter().filter(|item| item.is_main_meal()).map(|item| item.id.as_str()).collect();
    let mut shown_in_meals: HashSet<String> = HashSet::new();
    let mut rendered: Vec<Map<String, Value>> = Vec::new();
    let mut meal_roots: Vec<&MealRoot> = Vec::new();
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
        let mut calc: Map<String, Value> = if meal_settings.isf_schedule.is_empty() {
            Map::from_iter([("reason".to_string(), json!(t("Korrekturfaktor-Profil aus Nightscout fehlt")))])
        } else {
            match calculation::calculate_episode(&episode, &treatments, &entries, meal_settings, auto_effect, manual) {
                Ok(result) => {
                    let mut map = match to_value(&result) { Value::Object(map) => map, _ => Map::new() };
                    map.insert("settings_version".into(), json!(meal_settings_id));
                    map.insert("nightscout_profile".into(), meal_raw.get("nightscout_profile").cloned().unwrap_or(Value::Null));
                    map
                }
                Err(CoreError::Value(message)) => Map::from_iter([("reason".to_string(), json!(message))]),
                Err(error) => return Err(error),
            }
        };
        if !calc.contains_key("auto_end") {
            // Keep the window editable when the calculation itself is blocked.
            let (start, end) = manual.map(|w| (w.start, w.end)).unwrap_or((automatic.start, automatic.end));
            calc.insert("auto_start".into(), json!(iso(&automatic.start)));
            calc.insert("auto_end".into(), json!(iso(&automatic.end)));
            calc.insert("manual_window".into(), json!(manual.is_some()));
            calc.insert("window_start".into(), json!(iso(&start)));
            calc.insert("window_end".into(), json!(iso(&end)));
        }
        let events: Vec<Value> = sport_parts.iter().map(|(item, effect)| {
            let (from, until) = effect_span(item, meal_settings, &marks);
            json!({"id": item.id, "at": iso(&item.at), "kind": item.sport_type, "effect_mgdl": effect.to_string(),
                   "previous_day": zone.local(&item.at).date() < day,
                   "effect_from": iso(&from), "effect_until": iso(&until)})
        }).collect();
        let calc_ids = |key: &str| -> Vec<Option<String>> {
            calc.get(key).and_then(Value::as_array).map(|items| items.iter()
                .map(|item| item.get("id").and_then(Value::as_str).map(str::to_string)).collect()).unwrap_or_default()
        };
        let mut carb_ids: HashSet<String> = calc_ids("carb_events").into_iter().flatten().collect();
        carb_ids.insert(meal.source_id.clone());
        carb_ids.extend(associated.iter().filter(|item| item.carbs > Decimal::ZERO).map(|item| item.id.clone()));
        let counted: Vec<&Treatment> = treatments.iter().filter(|item| carb_ids.contains(&item.id)).collect();
        let mut quality = Map::new();
        quality.insert("carbs_certainty".into(), json!(episodes::least_certain(counted.iter().filter_map(|item| item.carbs_certainty.as_deref()))));
        for flag in QUALITY_MARKERS {
            let any = counted.iter().any(|item| match flag { "fat_protein" => item.fat_protein, "alcohol" => item.alcohol, _ => item.fast });
            quality.insert(flag.into(), json!(any));
        }
        let fpe_surcharge = treatments.iter().find(|item| item.id == meal.source_id)
            .and_then(|item| item.surcharge.as_ref()).map(|plan| json!({"mgdl": plan.mgdl.to_string(), "minutes": plan.minutes}))
            .unwrap_or(Value::Null);
        let mut carb_entries = counted.clone();
        carb_entries.sort_by(|a, b| (a.id != meal.source_id).cmp(&(b.id != meal.source_id)).then(a.at.cmp(&b.at)));
        let place = carb_entries.iter().map(|item| place_of(item)).find(|v| !v.is_null()).unwrap_or(Value::Null);
        let dish = carb_entries.iter().map(|item| dish_of(item)).find(|v| !v.is_null()).unwrap_or(Value::Null);
        let included: HashSet<String> = calc_ids("carb_events").into_iter().chain(calc_ids("doses")).flatten()
            .filter(|id| !id.is_empty() && !main_meal_ids.contains(id.as_str())).collect();
        shown_in_meals.extend(included.iter().cloned());
        let event_ids: HashSet<&str> = associated.iter().map(|item| item.id.as_str()).collect();
        let mut listed: Vec<&Treatment> = associated.clone();
        listed.extend(treatments.iter().filter(|item| included.contains(&item.id) && !event_ids.contains(item.id.as_str())));
        listed.sort_by_key(|item| item.at);
        let calc_value = Value::Object(calc);
        let mut output = Map::new();
        output.insert("id".into(), json!(meal.id));
        output.insert("source_id".into(), json!(meal.source_id));
        output.insert("imported".into(), json!(meal.imported));
        output.insert("at".into(), json!(iso(&meal.at)));
        output.insert("meal".into(), json!(meal.meal));
        output.insert("carbs".into(), treatments.iter().find(|item| item.id == meal.source_id).map(|item| raw.get(item, "carbs")).unwrap_or(text(meal.carbs)));
        output.insert("notes".into(), json!(meal.notes));
        output.insert("review".into(), meal_review(&calc_value));
        output.insert("calculation".into(), calc_value);
        output.insert("sport".into(), json!({"status": "auto", "suggested": !sport_parts.is_empty(),
            "suggested_effect_mgdl": auto_effect.to_string(), "effect_mgdl": auto_effect.to_string(), "events": events}));
        output.insert("quality".into(), Value::Object(quality));
        output.insert("fpe_surcharge".into(), fpe_surcharge);
        output.insert("place".into(), place);
        output.insert("dish".into(), dish);
        output.insert("excluded".into(), json!(snapshot.analysis_exclusions.contains(&meal.id)));
        output.insert("events".into(), Value::Array(listed.iter().map(|item| Value::Object(serialize_treatment(item, &raw))).collect()));
        rendered.push(output);
        meal_roots.push(meal);
    }
    for output in rendered.iter_mut() {
        let window = meal_window(output, &reading_at, &settings);
        output.insert("window".into(), window);
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
            let mut row = Map::new();
            row.insert("id".into(), json!(item.id));
            row.insert("at".into(), json!(iso(&item.at)));
            row.insert("kind".into(), json!(item.sport_type));
            row.insert("label".into(), json!(label));
            row.insert("duration_minutes".into(), raw.get(item, "duration"));
            row.insert("notes".into(), json!(note));
            row.extend(sport_session(item, &settings, &marks));
            exercises.push(Value::Object(row));
            continue;
        }
        other_events.push(json!({
            "id": item.id, "at": iso(&item.at), "eventType": item.event_type, "carbs": raw.get(item, "carbs"),
            "insulin": raw.get(item, "insulin"), "basal_units": raw.get(item, "basal_units"),
            "notes": strip_tags(&item.notes).trim(),
        }));
    }
    let timeline = timeline(snapshot, &treatments, &entries, &reading_at, &raw, &settings, &marks, &labels, &place_of, &dish_of, &rendered);
    let (day_type, day_type_source) = services::day_type_for(day, snapshot.day_types.as_ref(), &snapshot.vacation_periods);
    let vacation = services::in_vacation(day, &snapshot.vacation_periods);
    for meal in rendered.iter_mut() {
        meal.insert("vacation".into(), json!(vacation));
    }
    let day_start = zone.from_local(day.and_time(NaiveTime::MIN));
    let day_end = zone.from_local((day + Duration::days(1)).and_time(NaiveTime::MIN));
    let quiet = quiet_periods(day_start, day_end, &treatments, &entries, &reading_at, &settings, &marks);
    let (range_start, range_end) = day_range(day, &settings);
    let basal: Vec<Value> = snapshot.basal_doses.iter().map(|dose| json!({
        "id": dose.get("id"), "at": normalized_instant(dose.get("at")), "units": dose.get("units"),
        "notes": dose.get("notes"), "nightscout_synced": dose.get("nightscout_synced")})).collect();
    let review = day_review(&rendered);
    let mut payload = Map::new();
    payload.insert("day".into(), json!(day.to_string()));
    payload.insert("connected".into(), json!(snapshot.connected));
    payload.insert("day_start".into(), json!(iso(&day_start)));
    payload.insert("day_end".into(), json!(iso(&day_end)));
    payload.insert("timezone".into(), json!(zone.name_at(&(day_start + (day_end - day_start) / 2))));
    payload.insert("day_type".into(), json!(day_type));
    payload.insert("day_type_source".into(), json!(day_type_source));
    payload.insert("vacation".into(), json!(vacation));
    payload.insert("review".into(), review);
    payload.insert("synced_at".into(), json!(snapshot.synced_at));
    payload.insert("sync_error".into(), Value::Null);
    payload.insert("profile_sync_error".into(), Value::Null);
    payload.insert("settings_version".into(), json!(snapshot.settings_id));
    payload.insert("meals".into(), Value::Array(rendered.into_iter().map(Value::Object).collect()));
    payload.insert("exercises".into(), Value::Array(exercises));
    payload.insert("other_events".into(), Value::Array(other_events));
    payload.insert("timeline".into(), Value::Array(timeline));
    payload.insert("basal".into(), Value::Array(basal));
    payload.insert("basal_sync_error".into(), Value::Null);
    payload.insert("night".into(), night);
    payload.insert("quiet_periods".into(), Value::Array(quiet));
    payload.insert("entries".into(), Value::Array(entries.iter().map(|item| json!({
        "at": iso(&item.at), "sgv": raw_sgv(&snapshot.raw_entries, item), "tone": glucose::tone(item.sgv, item.at, &settings)})).collect()));
    payload.insert("target_bands".into(), to_value(&glucose::target_bands(range_start, range_end, &settings)));
    payload.insert("treatments".into(), Value::Array(treatments.iter().map(|item| {
        let mut row = serialize_treatment(item, &raw);
        row.insert("rapid".into(), json!(item.is_rapid()));
        Value::Object(row)
    }).collect()));
    payload.insert("window_limits".into(), json!({"lead_minutes": MANUAL_WINDOW_LEAD_MINUTES, "lag_minutes": MANUAL_WINDOW_LAG_MINUTES,
        "min_minutes": MANUAL_WINDOW_MIN_MINUTES, "max_minutes": MANUAL_WINDOW_MAX_MINUTES}));
    payload.insert("place_options".into(), Value::Array(snapshot.places.iter().map(|p| json!({"id": p.id, "name": p.name})).collect()));
    payload.insert("dish_options".into(), Value::Array(snapshot.dishes.iter().map(|d| json!({"id": d.id, "name": d.name})).collect()));
    let _ = meal_roots;
    Ok(Value::Object(payload))
}

fn normalized_instant(value: Option<&Value>) -> Value {
    match value.and_then(Value::as_str).and_then(|text| chrono::DateTime::parse_from_rfc3339(text).ok()) {
        Some(at) => json!(iso(&at.with_timezone(&chrono::Utc))),
        None => value.cloned().unwrap_or(Value::Null),
    }
}

/// The reading's sgv as Nightscout sent it.
fn raw_sgv(_raw: &[Value], item: &Reading) -> Value {
    match item.sgv {
        Some(value) if value.fract().is_zero() => json!(int(value)),
        Some(value) => text(value),
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

/// The meal's counted window as the diary draws it, with the CGM value at both handles.
fn meal_window(meal: &Map<String, Value>, reading_at: &Interpolator, settings: &Settings) -> Value {
    let calc = &meal["calculation"];
    let at = instant_of(meal.get("at")).unwrap_or_default();
    let fallback = at + Duration::minutes(settings.window_minutes);
    let start = instant_of(calc.get("start")).or_else(|| instant_of(calc.get("window_start"))).unwrap_or(at);
    let end = instant_of(calc.get("end")).or_else(|| instant_of(calc.get("window_end"))).unwrap_or(fallback);
    let reading = |moment: Instant| match reading_at.value(moment) {
        Some(value) => json!({"value": value.to_string(), "tone": glucose::tone(Some(value), moment, settings)}),
        None => Value::Null,
    };
    json!({"start": iso(&start), "end": iso(&end),
           "auto_start": iso(&instant_of(calc.get("auto_start")).unwrap_or(at)),
           "auto_end": iso(&instant_of(calc.get("auto_end")).unwrap_or(fallback)),
           "manual": calc.get("manual_window").and_then(Value::as_bool).unwrap_or(false),
           "start_glucose": reading(start), "end_glucose": reading(end)})
}

#[allow(clippy::too_many_arguments)]
fn timeline(snapshot: &Snapshot, treatments: &[Treatment], entries: &[Reading], reading_at: &Interpolator, raw: &Raw,
            settings: &Settings, marks: &Marks, labels: &[String], place_of: &dyn Fn(&Treatment) -> Value,
            dish_of: &dyn Fn(&Treatment) -> Value, meals: &[Map<String, Value>]) -> Vec<Value> {
    let zone = local_zone(&settings.timezone);
    let day = snapshot.day;
    // Basal notes in Nightscout are shown from the local record, which also holds unsent doses.
    struct Row<'a> { item: Treatment, basal: Option<&'a Value> }
    let mut rows: Vec<Row> = treatments.iter().filter(|item| !item.notes.contains(episodes::BASAL_MARKER))
        .map(|item| Row { item: item.clone(), basal: None }).collect();
    for dose in &snapshot.basal_doses {
        let Some(at) = instant_of(dose.get("at")) else { continue };
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
        let event_type = item.event_type.as_str();
        let kind = if event_type == "Basal" { "basal" }
            else if event_type == "Exercise" { "exercise" }
            else if item.finger_mgdl.is_some() { "finger" }
            else if event_type == "Meal Bolus" && item.carbs > Decimal::ZERO { "meal" }
            else if event_type == "Snack Bolus" || item.notes.contains("[DA2:snack]") { "snack" }
            else if event_type == "Carb Correction" { "carbs" }
            else if matches!(event_type, "Correction Bolus" | "Meal Bolus" | "Bolus") && item.insulin > Decimal::ZERO { "correction" }
            else { "other" };
        let mut note = episodes::quality_re().replace_all(&episodes::event_tag_re().replace_all(
            &episodes::sport_re().replace_all(&episodes::tag_re().replace_all(&item.notes, ""), ""), ""), "")
            .replace("[DA2:snack]", "").trim().to_string();
        if kind == "exercise" {
            for label in labels {
                if note == *label || note.starts_with(&format!("{label} — ")) {
                    note = note[label.len()..].trim_start_matches([' ', '—']).to_string();
                    break;
                }
            }
        }
        let (glucose, glucose_at) = if kind == "finger" {
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
        let mut row = Map::new();
        row.insert("id".into(), json!(item.id));
        row.insert("at".into(), json!(iso(&item.at)));
        row.insert("kind".into(), json!(kind));
        row.insert("eventType".into(), json!(item.event_type));
        if let Some(dose) = basal {
            row.insert("carbs".into(), json!(0));
            row.insert("insulin".into(), json!(0));
            row.insert("basal_units".into(), dose.get("units").cloned().unwrap_or(Value::Null));
            row.insert("duration_minutes".into(), json!(0));
        } else {
            row.insert("carbs".into(), raw.get(&item, "carbs"));
            row.insert("insulin".into(), raw.get(&item, "insulin"));
            row.insert("basal_units".into(), raw.get(&item, "basal_units"));
            row.insert("duration_minutes".into(), raw.get(&item, "duration"));
        }
        row.insert("sport_kind".into(), json!(item.sport_type));
        row.insert("notes".into(), json!(note));
        if kind == "exercise" {
            row.extend(sport_session(&item, settings, marks));
        }
        row.insert("carbs_certainty".into(), json!(item.carbs_certainty));
        row.insert("fat_protein".into(), json!(item.fat_protein));
        row.insert("alcohol".into(), json!(item.alcohol));
        row.insert("fast".into(), json!(item.fast));
        row.insert("place".into(), place_of(&item));
        row.insert("dish".into(), dish_of(&item));
        row.insert("event_id".into(), json!(item.event_id));
        row.insert("surcharge".into(), item.surcharge.as_ref().map(|plan| json!({"mgdl": plan.mgdl.to_string(), "minutes": plan.minutes})).unwrap_or(Value::Null));
        if matches!(kind, "meal" | "snack" | "carbs") && surcharge_method {
            row.insert("fpe_level".into(), json!(fpe::level_of(item.surcharge.as_ref().map(|plan| plan.minutes), item.fat_protein)));
        }
        row.insert("description".into(), json!(item.event_id.as_ref().and_then(|id| snapshot.meal_records.get(id))
            .map(|record| record.description.clone()).unwrap_or_default()));
        row.insert("glucose_mgdl".into(), glucose.map(text).unwrap_or(Value::Null));
        row.insert("glucose_at".into(), glucose_at.map(|at| json!(iso(&at))).unwrap_or(Value::Null));
        if let Some(dose) = basal {
            row.insert("nightscout_synced".into(), dose.get("nightscout_synced").cloned().unwrap_or(Value::Null));
        }
        row.insert("meal_id".into(), block_meal_id(&item, meals));
        if kind == "exercise" {
            row.extend(sport_shares(&item, &row, meals));
        }
        timeline.push(Value::Object(row));
    }
    timeline
}

/// The diary groups an entry under its own main meal, else under the first meal whose window holds it.
fn block_meal_id(item: &Treatment, meals: &[Map<String, Value>]) -> Value {
    let own = meals.iter().find(|meal| meal["source_id"] == item.id.as_str()).or_else(|| meals.iter().find(|meal| {
        let window = &meal["window"];
        matches!((instant_of(window.get("start")), instant_of(window.get("end"))), (Some(start), Some(end)) if start <= item.at && item.at < end)
    }));
    own.map(|meal| meal["id"].clone()).unwrap_or(Value::Null)
}

/// Where a session's effect lands: each meal of the day gets the part acting inside its window; the rest
/// acts between or after the meals.
fn sport_shares(item: &Treatment, row: &Map<String, Value>, meals: &[Map<String, Value>]) -> Map<String, Value> {
    let mut shares = Vec::new();
    let mut taken = Decimal::ZERO;
    for meal in meals {
        for event in meal["sport"]["events"].as_array().into_iter().flatten() {
            if event["id"] == item.id.as_str() {
                taken += decimal_of(&event["effect_mgdl"]).unwrap_or_default();
                shares.push(json!({"meal_id": meal["id"], "meal": meal["meal"], "effect_mgdl": event["effect_mgdl"]}));
            }
        }
    }
    let total = decimal_of(&row["sport_effect_mgdl"]).unwrap_or_default();
    let rest = (total - taken).round_dp_with_strategy(1, rust_decimal::RoundingStrategy::MidpointNearestEven);
    let until = item.at + minutes(item.duration + Decimal::from(row["sport_minutes"].as_i64().unwrap_or(0)));
    let mut out = Map::new();
    out.insert("sport_shares".into(), Value::Array(shares));
    out.insert("sport_rest_mgdl".into(), json!(format!("{rest:.1}")));
    out.insert("sport_effect_until".into(), if total.is_zero() { Value::Null } else { json!(iso(&until)) });
    out
}

/// Night from three hours after the last rapid dose to breakfast (app/analysis/night.py night_analysis).
pub fn night_analysis(day: NaiveDate, treatments: &[Treatment], entries: &[Reading], reading_at: &Interpolator,
                      dinner_at: Option<Instant>, settings: &Settings) -> Value {
    let Some(dinner_at) = dinner_at else {
        return json!({"reason": t("Für diesen Tag ist kein Abendessen erfasst.")});
    };
    let next_meals = episodes::meals_for_day(treatments, day + Duration::days(1), settings);
    let Some(breakfast) = next_meals.iter().filter(|meal| meal.meal == "breakfast" && meal.at > dinner_at).min_by_key(|meal| meal.at) else {
        return json!({"reason": t("Für den folgenden Tag ist kein Frühstück erfasst.")});
    };
    let rapid: Vec<Instant> = treatments.iter().filter(|item| dinner_at <= item.at && item.at < breakfast.at && item.has_rapid_dose())
        .map(|item| item.at).collect();
    let morning_from = breakfast.at - Duration::minutes(MORNING_MINUTES);
    let morning: Vec<Instant> = rapid.iter().copied().filter(|at| *at >= morning_from).collect();
    let evening: Vec<Instant> = rapid.iter().copied().filter(|at| *at < morning_from).collect();
    let (dinner_at, breakfast_at) = (iso(&dinner_at), iso(&breakfast.at));
    let Some(last_injection_at) = evening.iter().max().copied() else {
        return json!({"reason": t("Zwischen Abendessen und Frühstück ist keine schnell wirksame Injektion erfasst."),
                      "dinner_at": dinner_at, "breakfast_at": breakfast_at});
    };
    let morning_dose_at = morning.iter().min().copied();
    let end = morning_dose_at.unwrap_or(breakfast.at);
    let start = last_injection_at + Duration::minutes(NIGHT_GAP_MINUTES);
    let morning_value = morning_dose_at.map(|at| json!(iso(&at))).unwrap_or(Value::Null);
    if end <= start {
        return json!({"reason": t("Frühstück oder Morgendosis kommen vor dem Nachtstart drei Stunden nach der letzten Injektion."),
                      "dinner_at": dinner_at, "breakfast_at": breakfast_at, "last_injection_at": iso(&last_injection_at),
                      "morning_dose_at": morning_value, "start": iso(&start), "end": iso(&end)});
    }
    let (start_glucose, end_glucose) = match (reading_at.at(start), reading_at.at(end)) {
        (Ok(a), Ok(b)) => (a.value, b.value),
        (Err(error), _) | (_, Err(error)) => {
            return json!({"reason": error.to_string(), "start": iso(&start), "end": iso(&end), "dinner_at": dinner_at,
                          "breakfast_at": breakfast_at, "last_injection_at": iso(&last_injection_at)});
        }
    };
    let mut minimum = (start_glucose, start);
    for candidate in [(end_glucose, end)].into_iter()
        .chain(entries.iter().filter(|item| start < item.at && item.at < end).filter_map(|item| item.sgv.map(|v| (v, item.at)))) {
        if candidate.0 < minimum.0 {
            minimum = candidate;
        }
    }
    let night_carbs: Decimal = treatments.iter().filter(|item| start <= item.at && item.at < end).map(|item| item.carbs).sum();
    json!({"dinner_at": dinner_at, "breakfast_at": breakfast_at, "last_injection_at": iso(&last_injection_at),
           "morning_dose_at": morning_value, "start": iso(&start), "end": iso(&end),
           "start_glucose": start_glucose.to_string(), "end_glucose": end_glucose.to_string(),
           "delta_mgdl": (end_glucose - start_glucose).to_string(),
           "minimum_glucose": minimum.0.to_string(), "minimum_at": iso(&minimum.1),
           "drop_mgdl": (minimum.0 - start_glucose).to_string(), "rise_mgdl": (end_glucose - minimum.0).to_string(),
           "carbs_g": night_carbs.to_string()})
}

/// Stretches without bolus and food that start on this day (night.quiet_periods).
fn quiet_periods(day_start: Instant, day_end: Instant, treatments: &[Treatment], entries: &[Reading],
                 reading_at: &Interpolator, settings: &Settings, marks: &Marks) -> Vec<Value> {
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
        periods.push(json!({"start": iso(&start), "end": iso(&end), "hours": hours.to_string(),
            "start_glucose": start_glucose.to_string(), "end_glucose": end_glucose.to_string(),
            "minimum_glucose": minimum.0.to_string(), "minimum_at": iso(&minimum.1),
            "delta_mgdl": (end_glucose - start_glucose).to_string(),
            "rate_mgdl_per_hour": ((end_glucose - start_glucose) / hours).to_string()}));
    }
    periods
}

/// Text helpers re-exported for the API's error messages.
pub fn reason(text: &str) -> String {
    tr(text, &[])
}
