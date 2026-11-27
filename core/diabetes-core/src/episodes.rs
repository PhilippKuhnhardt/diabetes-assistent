//! Normalize Nightscout records and associate app-entered meal episodes (app/episodes.py).

use crate::context::{local_zone, t};
use crate::model::{decimal_of, Instant, Reading, Settings, SplitPlan, SurchargePlan, Treatment};
use crate::{value_error, Res};
use chrono::{DateTime, Duration, NaiveDate, Utc};
use regex::Regex;
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use serde::Serialize;
use serde_json::Value;
use std::collections::HashSet;
use std::sync::OnceLock;

macro_rules! pattern {
    ($name:ident, $text:expr) => {
        fn $name() -> &'static Regex {
            static PATTERN: OnceLock<Regex> = OnceLock::new();
            PATTERN.get_or_init(|| Regex::new($text).unwrap())
        }
    };
}

pattern!(tag_re, r"\[DA1:([a-f0-9-]{36}):(breakfast|lunch|dinner):(meal|meal-dose|correction|snack)\]");
pattern!(event_tag_re, r"\[(?:DAE:[a-f0-9-]{36}|DA2:(?:snack|meal|basal)|DASP:\d+(?:\.\d+)?@\d+|DAZ:\d+(?:\.\d+)?@\d+)\]");
pattern!(split_re, r"\[DASP:(\d+(?:\.\d+)?)@(\d+)\]");
pattern!(surcharge_re, r"\[DAZ:(\d+(?:\.\d+)?)@(\d+)\]");
pattern!(sport_re, r"\[DAS1:([a-z][a-z0-9_]{0,31})\]");
pattern!(quality_re, r"\[DAQ:(weighed|known|unsure|fat|alcohol|fast)\]");
pattern!(certainty_re, r"\[DAQ:(weighed|known|unsure)\]");
pattern!(dae_re, r"\[DAE:([a-f0-9-]{36})\]");
pattern!(snack_mark_re, r"\s*\[DA2:snack\]");
pattern!(episode_id_re, r"^[a-f0-9-]{36}$");

pub const BASAL_MARKER: &str = "[DA2:basal]";
pub const SNACK_GAP_MINUTES: i64 = 120;
/// A carb entry below this that would start a main meal defaults to a snack of no meal.
pub const SMALL_SNACK_GRAMS: Decimal = dec!(20);
/// A history reading (no trend arrow) this close to a live one is dropped.
pub const HISTORY_SECONDS: i64 = 10;
pub const CERTAINTIES: [&str; 3] = ["weighed", "known", "unsure"];
pub const SPORT_LABELS: [(&str, &str); 4] =
    [("jogging", "Joggen"), ("gym", "Gym"), ("light_cardio", "Leichtes Cardio"), ("racing", "Rennsport")];

pub fn event_id(notes: &str) -> Option<String> {
    dae_re().captures(notes).map(|c| c[1].to_string())
}

pub fn carbs_certainty(notes: &str) -> Option<String> {
    certainty_re().captures(notes).map(|c| c[1].to_string())
}

pub fn split_plan(notes: &str) -> Option<SplitPlan> {
    split_re().captures(notes).map(|c| SplitPlan { units: c[1].parse().unwrap(), delay_minutes: c[2].parse().unwrap() })
}

pub fn surcharge_plan(notes: &str) -> Option<SurchargePlan> {
    surcharge_re().captures(notes).map(|c| SurchargePlan { mgdl: c[1].parse().unwrap(), minutes: c[2].parse().unwrap() })
}

pub fn split_tag(units: Decimal, delay_minutes: i64) -> String {
    format!("[DASP:{}@{delay_minutes}]", units.normalize())
}

pub fn surcharge_tag(mgdl: Decimal, minutes: i64) -> String {
    format!("[DAZ:{}@{minutes}]", mgdl.normalize())
}

/// The least certain of the marked carb certainties.
pub fn least_certain<'a>(levels: impl IntoIterator<Item = &'a str>) -> Option<&'a str> {
    levels.into_iter().filter(|level| CERTAINTIES.contains(level))
        .max_by_key(|level| CERTAINTIES.iter().position(|c| c == level))
}

pub fn tag(episode_id: &str, meal: &str, role: &str) -> Res<String> {
    if !["breakfast", "lunch", "dinner"].contains(&meal) || !["meal", "meal-dose", "correction", "snack"].contains(&role) {
        return value_error(t("Ungültiger Ereignistyp"));
    }
    if !episode_id_re().is_match(episode_id) {
        return value_error(t("Ungültige Mahlzeit-ID"));
    }
    Ok(format!("[DA1:{episode_id}:{meal}:{role}]"))
}

fn millis_instant(millis: i64) -> Instant {
    DateTime::<Utc>::from_timestamp_millis(millis).unwrap_or_default()
}

fn iso_instant(text: &str) -> Option<Instant> {
    DateTime::parse_from_rfc3339(&text.replace(' ', "T")).ok().map(|at| at.with_timezone(&Utc))
}

fn text_of(value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::String(text) => text.clone(),
        other => other.to_string(),
    }
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::String(text) => !text.is_empty(),
        Value::Number(number) => number.to_string().parse::<f64>().map(|v| v != 0.0).unwrap_or(true),
        _ => true,
    }
}

/// CGM readings from raw Nightscout entries, sorted, without LibreLinkUp history readings that lie
/// next to a live one.
pub fn normalize_entries(raw: &[Value]) -> Vec<Reading> {
    let mut readings = Vec::new();
    for item in raw {
        let Some(sgv) = item.get("sgv").filter(|v| !v.is_null()) else { continue };
        let at = match item.get("date").filter(|v| truthy(v)).and_then(decimal_of) {
            Some(millis) => millis_instant(millis.trunc().try_into().unwrap_or(0)),
            None => match item.get("dateString").and_then(Value::as_str).and_then(iso_instant) {
                Some(at) => at,
                None => continue,
            },
        };
        let id = item.get("_id").filter(|v| truthy(v)).or(item.get("date")).map(text_of).unwrap_or_default();
        readings.push((Reading { id, at, sgv: decimal_of(sgv) }, item.get("direction").is_some_and(truthy)));
    }
    let mut live: Vec<Instant> = readings.iter().filter(|(_, trend)| *trend).map(|(r, _)| r.at).collect();
    live.sort();
    let window = Duration::seconds(HISTORY_SECONDS);
    let beside_live = |at: Instant| {
        let index = live.partition_point(|x| *x < at - window);
        index < live.len() && live[index] <= at + window
    };
    let mut result: Vec<Reading> = readings.into_iter().filter(|(r, trend)| *trend || !beside_live(r.at)).map(|(r, _)| r).collect();
    result.sort_by_key(|r| r.at);
    result
}

/// A Nightscout BG Check in mg/dL (mmol/L converted), else None.
pub fn finger_mgdl(item: &Value) -> Option<Decimal> {
    if item.get("eventType").and_then(Value::as_str) != Some("BG Check") {
        return None;
    }
    let value = decimal_of(item.get("glucose")?)?;
    if value <= Decimal::ZERO {
        return None;
    }
    let units = item.get("units").map(text_of).unwrap_or_default().to_lowercase();
    Some(if units.contains("mmol") { (value * dec!(18.0182)).round() } else { value })
}

fn parse_at(item: &Value) -> Option<Instant> {
    if let Some(text) = item.get("created_at").and_then(Value::as_str).filter(|t| !t.is_empty()) {
        return iso_instant(text);
    }
    let millis: i64 = decimal_of(item.get("date")?)?.trunc().try_into().ok()?;
    Some(millis_instant(millis))
}

/// Treatments from raw Nightscout records with the app's markers read from their notes, sorted.
pub fn normalize_treatments(raw: &[Value]) -> Vec<Treatment> {
    let mut result = Vec::new();
    for item in raw {
        let Some(at) = parse_at(item) else { continue };
        let notes = item.get("notes").filter(|v| truthy(v)).map(text_of).unwrap_or_default();
        let event_type = item.get("eventType").map(text_of).unwrap_or_default();
        let mut sport_type = if event_type == "Exercise" { sport_re().captures(&notes).map(|c| c[1].to_string()) } else { None };
        if event_type == "Exercise" && sport_type.is_none() {
            // Recover recognizable legacy or manual entries from their note prefix.
            let note = notes.trim().to_lowercase();
            sport_type = SPORT_LABELS.iter().find(|(_, label)| {
                let prefix = label.to_lowercase();
                note == prefix || note.starts_with(&format!("{prefix} —")) || note.starts_with(&format!("{prefix} |"))
            }).map(|(key, _)| key.to_string());
        }
        let marker = tag_re().captures(&notes);
        let amount = |key: &str| item.get(key).and_then(decimal_of).unwrap_or(Decimal::ZERO);
        let id = item.get("_id").filter(|v| truthy(v)).or(item.get("identifier").filter(|v| truthy(v)))
            .map(text_of).unwrap_or_default();
        result.push(Treatment {
            id, at, event_type, carbs: amount("carbs"), insulin: amount("insulin"), basal_units: amount("basal_units"),
            duration: amount("duration"), sport_type, carbs_certainty: carbs_certainty(&notes),
            fat_protein: notes.contains("[DAQ:fat]"), alcohol: notes.contains("[DAQ:alcohol]"), fast: notes.contains("[DAQ:fast]"),
            split: split_plan(&notes), surcharge: surcharge_plan(&notes), finger_mgdl: finger_mgdl(item),
            event_id: event_id(&notes),
            episode_id: marker.as_ref().map(|c| c[1].to_string()),
            meal: marker.as_ref().map(|c| c[2].to_string()),
            role: marker.as_ref().map(|c| c[3].to_string()),
            notes,
        });
    }
    result.sort_by_key(|item| item.at);
    result
}

/// (local date, clock-based meal kind) of an instant.
pub fn meal_kind_at(at: Instant, settings: &Settings) -> (NaiveDate, &'static str) {
    let zone = local_zone(&settings.timezone);
    let minute = zone.minute_of_day(&at);
    let kind = if minute < settings.breakfast_end_minute { "breakfast" }
               else if minute < settings.lunch_end_minute { "lunch" } else { "dinner" };
    (zone.local(&at).date(), kind)
}

/// The main meal a carb entry at `at` would count as a snack for.
#[derive(Clone, Debug, Serialize)]
pub struct Root {
    pub id: String,
    pub at: Instant,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub end: Option<Instant>,
    pub meal: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub same_kind: Option<bool>,
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub free: bool,
}

fn by_time<'a>(treatments: impl Iterator<Item = &'a Treatment>) -> Vec<&'a Treatment> {
    let mut items: Vec<&Treatment> = treatments.collect();
    items.sort_by_key(|item| item.at);
    items
}

fn is_diary_main_meal(item: &Treatment) -> bool {
    item.event_type == "Meal Bolus" && item.carbs > Decimal::ZERO
}

/// The main meal a new carb entry at `at` would count as a snack for: started less than 2 h before,
/// or of the same clock-based kind with `at` inside its (dose-extended) window.
pub fn preceding_meal<'a>(at: Instant, treatments: impl Iterator<Item = &'a Treatment>, settings: &Settings) -> Option<Root> {
    let window = Duration::minutes(settings.window_minutes);
    let kind = meal_kind_at(at, settings);
    let mut root: Option<Root> = None;
    for item in by_time(treatments) {
        if item.at > at {
            break;
        }
        if is_diary_main_meal(item) {
            let own = meal_kind_at(item.at, settings);
            root = Some(Root {
                id: item.id.clone(), at: item.at, end: Some(item.at + window),
                meal: item.meal.clone().unwrap_or_else(|| own.1.to_string()), same_kind: Some(own == kind), free: false,
            });
        } else if let Some(current) = root.as_mut() {
            if item.at < current.end.unwrap() && item.insulin > Decimal::ZERO
                && matches!(item.event_type.as_str(), "Snack Bolus" | "Correction Bolus" | "Bolus") {
                current.end = Some(current.end.unwrap().max(item.at + window));
            }
        }
    }
    let root = root?;
    if root.at == at {
        return None;
    }
    if at - root.at < Duration::minutes(SNACK_GAP_MINUTES) || (root.same_kind == Some(true) && at < root.end.unwrap()) {
        Some(root)
    } else {
        None
    }
}

/// An earlier main meal of the same day and clock-based kind as `at`; each kind counts once a day.
pub fn taken_meal<'a>(at: Instant, treatments: impl Iterator<Item = &'a Treatment>, settings: &Settings) -> Option<Root> {
    let kind = meal_kind_at(at, settings);
    for item in by_time(treatments) {
        if item.at >= at {
            break;
        }
        if is_diary_main_meal(item) && meal_kind_at(item.at, settings) == kind {
            return Some(Root { id: item.id.clone(), at: item.at, end: None,
                               meal: item.meal.clone().unwrap_or_else(|| kind.1.to_string()), same_kind: None, free: false });
        }
    }
    None
}

/// Reclassify one diary main meal in place; return the meal it joins as a snack (`free` for a
/// snack of no meal, whose kind is taken without a meal close before it).
pub fn classify_diary_entry(index: usize, treatments: &mut [Treatment], settings: &Settings) -> Option<Root> {
    let item = &treatments[index];
    if item.event_type != "Meal Bolus" || item.carbs <= Decimal::ZERO {
        return None;
    }
    if !event_tag_re().is_match(&item.notes) || item.episode_id.is_some() {
        return None;
    }
    let others = || treatments.iter().enumerate().filter(move |(i, _)| *i != index).map(|(_, other)| other);
    let regular = if item.notes.contains("[DA2:meal]") { None } else { preceding_meal(item.at, others(), settings) };
    let root = regular.or_else(|| taken_meal(item.at, others(), settings).map(|root| Root { free: true, ..root }))?;
    let item = &mut treatments[index];
    item.event_type = if item.insulin > Decimal::ZERO { "Snack Bolus" } else { "Carb Correction" }.into();
    item.notes = format!("{} [DA2:snack]", item.notes).trim().to_string();
    Some(root)
}

/// Group later diary carb entries as snacks of the preceding main meal (see the reference's
/// docstring): within 2 h, or inside the window of a meal of the same kind; an entry whose kind is
/// taken that day becomes a snack of no meal; a stored snack whose meal was freed so is classified
/// afresh and can become the main meal itself.
pub fn group_diary_followups(treatments: &mut [Treatment], settings: &Settings) {
    let mut free: HashSet<String> = HashSet::new();
    let mut order: Vec<usize> = (0..treatments.len()).collect();
    order.sort_by_key(|&index| treatments[index].at);
    for index in order {
        let notes = treatments[index].notes.clone();
        if notes.contains("[DA2:snack]") {
            let item = &treatments[index];
            if free.is_empty() || item.episode_id.is_some() || item.carbs <= Decimal::ZERO {
                continue;
            }
            // Which meal the snack joined when it was saved: freed entries still count as main meals.
            let view: Vec<Treatment> = treatments.iter().enumerate().filter(|(i, _)| *i != index)
                .map(|(_, other)| if free.contains(&other.id) { Treatment { event_type: "Meal Bolus".into(), ..other.clone() } } else { other.clone() })
                .collect();
            match preceding_meal(item.at, view.iter(), settings) {
                Some(joined) if free.contains(&joined.id) => {}
                _ => continue,
            }
            let item = &mut treatments[index];
            item.event_type = "Meal Bolus".into();
            item.notes = snack_mark_re().replace_all(&notes, "").trim().to_string();
        }
        if let Some(root) = classify_diary_entry(index, treatments, settings) {
            if root.free {
                free.insert(treatments[index].id.clone());
            }
        }
    }
}

/// A meal root of a day: a tagged main meal or an imported Nightscout meal.
#[derive(Clone, Debug, Serialize)]
pub struct MealRoot {
    pub id: String,
    pub source_id: String,
    pub imported: bool,
    pub at: Instant,
    pub meal: String,
    pub carbs: Decimal,
    pub notes: String,
}

pub fn meals_for_day(treatments: &[Treatment], day: NaiveDate, settings: &Settings) -> Vec<MealRoot> {
    let zone = local_zone(&settings.timezone);
    let mut roots = Vec::new();
    for item in treatments {
        let tagged = item.role.as_deref() == Some("meal") && item.episode_id.is_some();
        let imported = item.episode_id.is_none() && item.event_type == "Meal Bolus" && item.carbs > Decimal::ZERO;
        if !(tagged || imported) || zone.local(&item.at).date() != day {
            continue;
        }
        let minute = zone.minute_of_day(&item.at);
        let inferred = if minute < settings.breakfast_end_minute { "breakfast" }
                       else if minute < settings.lunch_end_minute { "lunch" } else { "dinner" };
        let notes = quality_re().replace_all(&event_tag_re().replace_all(&tag_re().replace_all(&item.notes, ""), ""), "").trim().to_string();
        roots.push(MealRoot {
            id: if tagged { item.episode_id.clone().unwrap() } else { item.id.clone() },
            source_id: item.id.clone(), imported,
            at: item.at, meal: if tagged { item.meal.clone().unwrap_or_default() } else { inferred.to_string() },
            carbs: item.carbs, notes,
        });
    }
    roots
}
