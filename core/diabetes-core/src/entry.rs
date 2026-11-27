//! The diary entry form: what a new entry would mean (snack or meal, closing a meal, the basal that
//! rides along, the split chip, the dose options and the slider) and what saving it stores.
//!
//! The adapter (app/services/entry.py) reads the day, the settings, the place chips and, for today, the
//! dose help's snapshot; it hashes the dose check, writes to Nightscout and stores the local marks.

use crate::context::{local_zone, t};
use crate::correction_scale;
use crate::wire::iso;
use crate::dosing::half_units;
use crate::episodes::{self, Root};
use crate::fpe::{self, Estimate};
use crate::model::{decimal_of, Instant, Settings, Treatment};
use crate::services::{self, DoseHelp, Snapshot};
use crate::split;
use crate::window;
use crate::{CoreError, Res};
use chrono::Duration;
use rust_decimal::prelude::ToPrimitive;
use rust_decimal::{Decimal, RoundingStrategy};
use rust_decimal_macros::dec;
use serde::Deserialize;
use serde_json::{json, Map, Value};

pub const MODES: [&str; 3] = ["meal", "exercise", "basal"];
/// fast / slow: the ends of a correction without food after a recent meal; they take the cautious option's place there.
pub const DOSE_VARIANTS: [&str; 5] = ["fast", "normal", "slow", "cautious", "carbs"];
/// The planned second part of a split bolus, offered as its own option while it is open.
pub const SPLIT_VARIANT: &str = "split";
/// The fields a shown suggestion depends on; saving a tapped suggestion re-checks them together with the shown
/// option units. Raw totals and expected values are left out: what still acts decays every second, so they
/// never match between showing and saving, while the rounded doses only change for a real reason.
pub const DOSE_CHECK_KEYS: [&str; 12] = [
    "carbs_g", "glucose_at", "glucose_mgdl", "isf_mgdl_per_unit", "target_mgdl", "cautious_target_mgdl",
    "units_per_ke", "factor_source", "day_type", "activity_change_mgdl", "auto_units_per_ke", "recent_rapid_doses"];
use crate::limits::{FINGER_MGDL, MAX_CARBS_G as MAX_CARBS, MAX_UNITS, SPORT_EFFECT_MAX_MGDL, SPORT_EFFECT_MINUTES};

fn invalid(message: String) -> CoreError {
    CoreError::Value(message)
}

/// The meal of the stored day the form reads: id, start, source entry and window.
#[derive(Clone, Debug, Deserialize)]
pub struct DayMeal {
    pub id: String,
    pub at: Instant,
    #[serde(default, deserialize_with = "crate::model::opt_text")]
    pub source_id: Option<String>,
    #[serde(default)]
    pub window: Value,
}

/// Everything the form's rules read.
#[derive(Clone, Debug, Deserialize)]
pub struct Inputs {
    /// What the form holds (carbs, insulin, finger_mgdl, as, close_meal, split, fat_protein, fat_g, ...).
    pub payload: Value,
    pub settings: Settings,
    #[serde(default)]
    pub at: Option<Instant>,
    /// The entry's day and today, local (YYYY-MM-DD).
    pub day: String,
    pub today: String,
    /// The day's treatments and meals as stored.
    #[serde(default)]
    pub treatments: Vec<Treatment>,
    #[serde(default)]
    pub meals: Vec<DayMeal>,
    /// Whether the day already has a basal dose.
    #[serde(default)]
    pub basal_today: bool,
    #[serde(default)]
    pub day_type: Option<String>,
    #[serde(default)]
    pub day_type_source: Option<String>,
    /// The place chips (places.chips) for a carb entry, and the place the entry will be saved with.
    #[serde(default)]
    pub place: Value,
    #[serde(default)]
    pub place_id: Value,
    /// The dose help's snapshot of now; only for today.
    #[serde(default)]
    pub snapshot: Option<Snapshot>,
}

fn text_of(value: Option<&Value>) -> Option<String> {
    match value? {
        Value::Null => None,
        Value::String(text) => Some(text.trim().to_string()),
        Value::Number(number) => Some(number.to_string()),
        Value::Bool(flag) => Some(if *flag { "True" } else { "False" }.to_string()),
        other => Some(other.to_string()),
    }
}

/// A typed amount: empty is None, text that is no finite number is Err.
fn amount(value: Option<&Value>) -> Result<Option<Decimal>, ()> {
    match text_of(value) {
        None => Ok(None),
        Some(text) if text.is_empty() => Ok(None),
        Some(text) => decimal_of(&Value::String(text)).map(Some).ok_or(()),
    }
}

/// Empty carbohydrates mean a correction without food; invalid input yields None.
pub fn carbs(payload: &Value) -> Option<Decimal> {
    match amount(payload.get("carbs")) {
        Ok(None) => Some(Decimal::ZERO),
        Ok(Some(value)) if Decimal::ZERO <= value && value <= MAX_CARBS => Some(value),
        _ => None,
    }
}

/// The glucose drop typed for a sport session (the form shows the signed change, −70 lowers); None when left
/// empty, so the session follows the setting of its kind.
pub fn sport_drop(payload: &Value) -> Res<Option<Decimal>> {
    match amount(payload.get("sport_effect_mgdl")) {
        Ok(None) => Ok(None),
        Ok(Some(change)) if change.abs() <= SPORT_EFFECT_MAX_MGDL => Ok(Some(-change)),
        Ok(Some(_)) => Err(invalid(t("Sporteinfluss muss zwischen −500 und 500 mg/dL liegen"))),
        Err(()) => Err(invalid(t("Ungültiger Sporteinfluss"))),
    }
}

/// The session's own effect duration in minutes (the same field as on the entry card); None follows its kind.
pub fn sport_minutes(payload: &Value) -> Res<Option<i64>> {
    match payload.get("sport_minutes") {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(text)) if text.is_empty() => Ok(None),
        Some(Value::Number(number)) => match number.as_f64() {
            Some(minutes) if (*SPORT_EFFECT_MINUTES.start() as f64..=*SPORT_EFFECT_MINUTES.end() as f64).contains(&minutes) => Ok(Some(minutes.round_ties_even() as i64)),
            _ => Err(invalid(t("Die Wirkdauer muss zwischen 0,5 und 24 h liegen."))),
        },
        _ => Err(invalid(t("Die Wirkdauer muss zwischen 0,5 und 24 h liegen."))),
    }
}

/// Blood glucose measured by hand in the form; empty means none, out of range is an error.
pub fn finger(payload: &Value) -> Res<Option<Decimal>> {
    match amount(payload.get("finger_mgdl")) {
        Ok(None) => Ok(None),
        Ok(Some(value)) if FINGER_MGDL.contains(&value) => Ok(Some(value)),
        _ => Err(invalid(t("Blutig gemessener BZ muss zwischen 20 und 600 mg/dL liegen"))),
    }
}

struct Form<'a> {
    inputs: &'a Inputs,
    mode: &'a str,
    carbs: Option<Decimal>,
    finger: Option<Decimal>,
}

impl<'a> Form<'a> {
    fn new(inputs: &'a Inputs) -> Res<Form<'a>> {
        let mode = inputs.payload.get("mode").and_then(Value::as_str).unwrap_or("meal");
        let mode = MODES.iter().find(|m| **m == mode).copied().ok_or_else(|| invalid(t("Ungültiger Eintragstyp")))?;
        let finger = if mode == "meal" { finger(&inputs.payload)? } else { None };
        Ok(Form { inputs, mode, carbs: carbs(&inputs.payload), finger })
    }

    fn payload(&self, key: &str) -> Option<&Value> {
        self.inputs.payload.get(key)
    }

    fn flag(&self, key: &str) -> bool {
        self.payload(key) == Some(&Value::Bool(true))
    }

    fn settings(&self) -> &Settings {
        &self.inputs.settings
    }

    fn is_today(&self) -> bool {
        self.inputs.day == self.inputs.today
    }

    fn before_breakfast_end(&self) -> bool {
        self.inputs.at.is_some_and(|at| local_zone(&self.settings().timezone).minute_of_day(&at) < self.settings().breakfast_end_minute)
    }
}

/// How a new carb entry is assigned: snack of a preceding main meal (unless flipped to its own meal), and
/// whether it can end that meal instead.
#[derive(Clone, Debug)]
struct Assignment {
    meal: Option<String>,
    at: Option<Instant>,
    own_meal: &'static str,
    snack: bool,
    own_meal_taken: bool,
    close: Option<Close>,
}

#[derive(Clone, Debug)]
struct Close {
    meal: String,
    at: Instant,
    meal_id: String,
    start: Instant,
    end: Instant,
    active: bool,
}

impl Assignment {
    fn view(&self) -> Value {
        json!({
            "meal": self.meal, "at": self.at.map(|at| iso(&at)), "own_meal": self.own_meal, "snack": self.snack,
            "own_meal_taken": self.own_meal_taken,
            "close": self.close.as_ref().map(|c| json!({"meal": c.meal, "at": iso(&c.at), "end": iso(&c.end), "active": c.active})),
        })
    }
}

fn instant(value: Option<&Value>) -> Option<Instant> {
    value.and_then(Value::as_str).and_then(|text| chrono::DateTime::parse_from_rfc3339(text).ok()).map(|at| at.with_timezone(&chrono::Utc))
}

/// A new carb entry right after a main meal counts as its snack unless flipped to its own meal.
///
/// When the day already has a main meal of the entry's own kind, it can only be a snack (of the preceding
/// meal, or of no meal), so each kind counts once a day. An entry below SMALL_SNACK_GRAMS that would start
/// a main meal is a snack of no meal until flipped, so a bite before sport does not take the day's meal.
fn assignment(form: &Form) -> Option<Assignment> {
    let (at, carbs) = (form.inputs.at?, form.carbs?);
    if form.mode != "meal" || carbs <= Decimal::ZERO {
        return None;
    }
    let settings = form.settings();
    let treatments = &form.inputs.treatments;
    let own_meal = episodes::meal_kind_at(at, settings).1;
    let taken = episodes::taken_meal(at, treatments.iter(), settings);
    let root = episodes::preceding_meal(at, treatments.iter(), settings);
    let as_meal = form.payload("as").and_then(Value::as_str) == Some("meal");
    if root.is_none() && taken.is_none() {
        if carbs >= episodes::SMALL_SNACK_GRAMS {
            return None;
        }
        return Some(Assignment { meal: None, at: None, own_meal, snack: !as_meal, own_meal_taken: false, close: None });
    }
    let result = if taken.is_some() {
        Assignment { meal: root.as_ref().map(|r| r.meal.clone()), at: root.as_ref().map(|r| r.at), own_meal, snack: true,
                     own_meal_taken: true, close: None }
    } else {
        let root = root.as_ref().unwrap();
        Assignment { meal: Some(root.meal.clone()), at: Some(root.at), own_meal, snack: !as_meal, own_meal_taken: false, close: None }
    };
    Some(closing(form, root.as_ref(), result))
}

/// A snack after a main meal can end that meal instead: the meal's window then ends at the entry, the same
/// manual window "Zeitraum anpassen" sets, so the snack and anything later no longer count in its factor. A
/// meal whose manual window already ended keeps no snacks.
fn closing(form: &Form, root: Option<&Root>, mut result: Assignment) -> Assignment {
    let Some(root) = root.filter(|_| result.snack) else { return result };
    let at = form.inputs.at.unwrap();
    let Some(meal) = form.inputs.meals.iter().find(|m| m.source_id.is_some() && m.source_id == root.id) else { return result };
    let (Some(start), Some(end)) = (instant(meal.window.get("start")), instant(meal.window.get("end"))) else { return result };
    if meal.window.get("manual").and_then(Value::as_bool).unwrap_or(false) && end <= at {
        return Assignment { meal: None, at: None, ..result };
    }
    if end <= at || window::problem(meal.at, start, at).is_some() {
        return result; // Too soon after the meal to end it here.
    }
    let active = form.flag("close_meal");
    result.close = Some(Close { meal: root.meal.clone(), at: root.at, meal_id: meal.id.clone(), start, end: at, active });
    if active {
        result.meal = None;
        result.at = None;
    }
    result
}

/// Before breakfast end, while the day has no basal yet, the usual dose rides along.
fn basal_units(form: &Form) -> Decimal {
    let units = form.settings().basal_default_units;
    if form.mode != "meal" || units <= Decimal::ZERO || form.inputs.basal_today || !form.before_breakfast_end() {
        return Decimal::ZERO;
    }
    units
}

/// The calculated fat/protein surcharge (fpe.rs) when the form chose it. Snacks carry it like meals.
fn fpe_surcharge(form: &Form) -> Option<Estimate> {
    if form.mode != "meal" || form.carbs.is_none_or(|c| c.is_zero()) {
        return None;
    }
    let found = fpe::estimate(&form.inputs.payload, form.settings());
    found.filter(|f| fpe::chosen(Some(f), &form.inputs.payload))
}

/// The split-bolus chip: offered and preselected for a large or fat/protein main meal; the form can switch it off.
fn split_chip(form: &Form, assignment: Option<&Assignment>) -> Option<Map<String, Value>> {
    let (carbs, at) = (form.carbs.filter(|c| !c.is_zero())?, form.inputs.at?);
    if form.mode != "meal" {
        return None;
    }
    let calculated = fpe_surcharge(form);
    // With the calculated surcharge every main meal may split ("carbs now, surcharge later"); the dose drops the
    // split again when the surcharge part stays small.
    let offer = split::offer(carbs, assignment.is_some_and(|a| a.snack), form.settings(), form.flag("fat_protein") || calculated.is_some())?;
    let mut chip = Map::new();
    chip.insert("first_percent".into(), json!(offer.first_percent));
    chip.insert("delay_minutes".into(), json!(offer.delay_minutes));
    if calculated.is_some() {
        chip.insert("fpe".into(), json!(true));
    }
    chip.insert("active".into(), json!(form.payload("split") != Some(&Value::Bool(false))));
    chip.insert("due_at".into(), json!(iso(&(at + Duration::minutes(offer.delay_minutes)))));
    Some(chip)
}

fn number(value: Option<&Value>) -> Option<Decimal> {
    value.and_then(decimal_of)
}

fn dose_help(snapshot: &Snapshot, payload: Value) -> Res<DoseHelp> {
    let mut snapshot = snapshot.clone();
    snapshot.payload = payload;
    services::dose_help(&snapshot)
}

fn opt_text(value: Option<Decimal>) -> Value {
    value.map(|v| json!(v.to_string())).unwrap_or(Value::Null)
}

/// The suggestion for today, with its options already resolved for display; `split` is the chip as the dose
/// leaves it (dropped when the dose is too small to split, re-divided for the calculated surcharge).
fn dose(form: &Form, assignment: Option<&Assignment>, split: Option<Map<String, Value>>) -> Res<(Map<String, Value>, Option<Map<String, Value>>)> {
    let message = |text: String| { let mut map = Map::new(); map.insert("message".into(), json!(text)); map };
    let Some(carbs) = form.carbs.filter(|_| form.mode == "meal") else { return Ok((message(String::new()), split)) };
    let Some(snapshot) = form.inputs.snapshot.as_ref().filter(|_| form.is_today()) else {
        let text = if carbs > Decimal::ZERO { t("Eine aktuelle Empfehlung gibt es nur für den heutigen Tag.") } else { String::new() };
        return Ok((message(text), split));
    };
    let has_carbs = carbs > Decimal::ZERO;
    let mut request = json!({
        "carbs": carbs.to_string(), "snack": assignment.is_some_and(|a| a.snack),
        "fat_protein": has_carbs && form.flag("fat_protein"),
        "place_id": if has_carbs { form.inputs.place_id.clone() } else { Value::Null },
        "finger_mgdl": form.finger.map(|f| f.to_string()), "finger_at": form.inputs.at.map(|at| iso(&at)),
    });
    if has_carbs {
        for key in ["fat_g", "protein_g", "surcharge_mode", "fpe_level"] {
            request[key] = form.payload(key).cloned().unwrap_or(Value::Null);
        }
    }
    let help = dose_help(snapshot, request)?;
    // With carbs every option uses the calculated factor; until it has enough meals the breakfast day-type
    // median or Nightscout's I:C stands in (DoseHelp::normal, carbs_only).
    let (normal_units, normal_expected) = help.normal();
    let settings = form.settings();
    let mut split = split;
    if split.is_some() && !split::worth_it(normal_units, settings) {
        // The normal dose is too small to split (e.g. shrunk by a low glucose): all of it now, no chip.
        split = None;
    }
    if let Some(chip) = split.clone().filter(|chip| chip.contains_key("fpe")) {
        // Calculated surcharge: the carbs (and correction) now, the surcharge later.
        let chosen = fpe_surcharge(form).is_some();
        let minimum = Decimal::from(settings.split_min_grams);
        if !chosen {
            // The static surcharge was used after all: the usual rule (carbs, fat mark) decides.
            if carbs < minimum && !form.flag("fat_protein") {
                split = None;
            }
        } else {
            let later = half_units(help.suggestion.auto_surcharge_units.unwrap_or_default());
            if later < fpe::SPLIT_LATER_MIN_UNITS && carbs < minimum {
                split = None;
            } else {
                let full = normal_units;
                let share = match full { Some(full) if full > Decimal::ZERO => dec!(100) * (full - later) / full, _ => dec!(100) };
                let mut chip = chip;
                chip.insert("later_units".into(), json!(later.to_string()));
                chip.insert("first_percent".into(), json!(share.clamp(Decimal::ZERO, dec!(100))
                    .round_dp_with_strategy(0, RoundingStrategy::MidpointNearestEven).to_i64().unwrap_or(100)));
                split = Some(chip);
            }
        }
    }
    let scale = help.scale().filter(|_| carbs.is_zero());
    let active = split.as_ref().filter(|chip| chip.get("active") == Some(&Value::Bool(true)));
    let mut options: Vec<Map<String, Value>> = Vec::new();
    let savable = |units: &Value| decimal_of(units).is_some_and(|u| u <= MAX_UNITS);
    for variant in DOSE_VARIANTS {
        if variant == "carbs" && !has_carbs {
            continue;
        }
        if variant == "fast" || variant == "slow" {
            if let Some(scale) = scale {
                let (units, expected) = if variant == "fast" { (scale.fast_entry_units, scale.fast_expected_glucose_mgdl) }
                                        else { (scale.slow_entry_units, scale.slow_expected_glucose_mgdl) };
                let units = json!(units.to_string());
                let mut option = Map::new();
                option.insert("variant".into(), json!(variant));
                option.insert("savable".into(), json!(savable(&units)));
                option.insert("units".into(), units);
                option.insert("expected_mgdl".into(), json!(expected.to_string()));
                option.insert("suggested".into(), json!(scale.tempo.as_deref() == Some(variant)));
                options.push(option);
            }
            continue;
        }
        if variant == "cautious" {
            continue; // The slider replaces the cautious target.
        }
        let (units, expected) = if variant == "normal" { (normal_units, normal_expected) } else { help.carbs_only() };
        let Some(units) = units.map(|u| json!(u.to_string())) else { continue };
        let mut option = Map::new();
        option.insert("variant".into(), json!(variant));
        option.insert("units".into(), units.clone());
        option.insert("expected_mgdl".into(), opt_text(expected));
        if let Some(chip) = active {
            // The shown units are the first part; the expected value assumes the planned second part too.
            let percent = chip["first_percent"].as_i64().unwrap_or(100);
            let (first, later) = split::parts(decimal_of(&units).unwrap_or_default(), percent, number(chip.get("later_units")));
            option.insert("units".into(), json!(first.to_string()));
            option.insert("full_units".into(), units);
            option.insert("later_units".into(), json!(later.to_string()));
            option.insert("later_at".into(), chip["due_at"].clone());
        }
        let ok = savable(&option["units"]);
        option.insert("savable".into(), json!(ok));
        if let (Some(scale), "normal") = (scale, variant) {
            option.insert("suggested".into(), json!(scale.tempo.as_deref() == Some("normal")));
        }
        options.push(option);
    }
    if let Some(plan) = help.split_plan.as_ref().filter(|_| carbs.is_zero()) {
        // The second part is what the current forecast asks for; the planned IE only stand in without a fresh
        // glucose value.
        let s = &help.suggestion;
        let forecast_units = s.entry_units;
        let units = forecast_units.unwrap_or(plan.units);
        let expected = opt_text(s.forecast_mgdl.zip(s.isf_mgdl_per_unit).map(|(forecast, isf)| forecast - units * isf));
        let mut option = Map::new();
        option.insert("variant".into(), json!(SPLIT_VARIANT));
        option.insert("units".into(), json!(units.to_string()));
        option.insert("due_at".into(), json!(plan.due_at));
        option.insert("planned_units".into(), json!(plan.units.to_string()));
        option.insert("state".into(), json!(plan.state));
        option.insert("latest_at".into(), json!(plan.latest_at));
        option.insert("from_forecast".into(), json!(forecast_units.is_some()));
        option.insert("expected_mgdl".into(), expected);
        option.insert("savable".into(), json!(Decimal::ZERO < units && units <= MAX_UNITS));
        options.insert(0, option);
    }
    // Without a fresh CGM value the form offers its field for blood glucose measured by hand.
    let finger_offer = !help.suggestion.glucose_fresh || help.suggestion.glucose_source == Some("finger");
    let result = match serde_json::to_value(&help).map_err(|error| CoreError::Value(error.to_string()))? {
        Value::Object(map) => map,
        _ => Map::new(),
    };
    let shown: Vec<Value> = options.iter().map(|o| json!([o.get("variant"), o.get("units"), o.get("later_units")])).collect();
    let material = json!([DOSE_CHECK_KEYS.iter().map(|key| result.get(*key).cloned().unwrap_or(Value::Null)).collect::<Vec<_>>(), shown]);
    let slider = slider(help.suggestion.isf_mgdl_per_unit, &options, carbs, scale, split.as_ref());
    let mut out = result;
    out.insert("options".into(), Value::Array(options.into_iter().map(Value::Object).collect()));
    out.insert("check_material".into(), material);
    out.insert("finger_offer".into(), json!(finger_offer));
    out.insert("slider".into(), slider);
    Ok((out, split))
}

/// The dose slider around the normal dose (correction_scale.dose_range): ends, recommendation, where glucose
/// likely ends and, for a split meal, how a total is divided.
fn slider(isf: Option<Decimal>, options: &[Map<String, Value>], carbs: Decimal, scale: Option<&correction_scale::Scale>,
          split: Option<&Map<String, Value>>) -> Value {
    let find = |variant: &str| options.iter().find(|o| o["variant"] == variant);
    let Some(base) = find(SPLIT_VARIANT).or_else(|| find("normal")) else { return Value::Null };
    let normal = number(base.get("full_units").filter(|v| !v.is_null())).or_else(|| number(base.get("units"))).unwrap_or_default();
    let tempo = scale.and_then(|s| s.tempo.as_deref());
    let mut out = serde_json::to_value(correction_scale::dose_range(normal, carbs.is_zero(), tempo)).unwrap();
    let active = split.filter(|chip| chip.get("active") == Some(&Value::Bool(true)));
    out["expected_mgdl"] = base.get("expected_mgdl").cloned().unwrap_or(Value::Null);
    out["isf_mgdl_per_unit"] = opt_text(isf);
    out["first_percent"] = active.map(|c| c["first_percent"].clone()).unwrap_or(Value::Null);
    out["later_at"] = active.map(|c| c["due_at"].clone()).unwrap_or(Value::Null);
    // Calculated surcharge: the later part is that surcharge, whatever total is chosen.
    out["later_units"] = active.and_then(|c| c.get("later_units").cloned()).unwrap_or(Value::Null);
    out
}

/// Today's "Wirkt noch" card, whatever the form currently holds.
fn on_board(form: &Form, dose: &Map<String, Value>) -> Value {
    if !form.is_today() {
        return Value::Null;
    }
    if let Some(found) = dose.get("on_board") {
        return found.clone();
    }
    form.inputs.snapshot.as_ref().and_then(|snapshot| dose_help(snapshot, json!({"carbs": "0"})).ok())
        .and_then(|result| result.on_board).and_then(|card| serde_json::to_value(&card).ok()).unwrap_or(Value::Null)
}

/// Everything the entry form shows for the values typed so far.
/// (`carbs` alone: the parsed carbs the adapter needs to decide whether to look up the place chips.)
pub fn preview(inputs: &Inputs) -> Res<Value> {
    let form = Form::new(inputs)?;
    let assignment = assignment(&form);
    // The day type is asked once, with the day's first meal; later it is changed in "Verlauf".
    let first_meal = !inputs.treatments.iter().any(|item| item.carbs > Decimal::ZERO);
    let day_type_visible = form.mode == "meal" && inputs.day_type.as_deref().is_some_and(|d| !d.is_empty())
        && form.before_breakfast_end() && first_meal;
    let split = split_chip(&form, assignment.as_ref());
    let (dose, split) = dose(&form, assignment.as_ref(), split)?;
    let has_carbs = form.carbs.is_some_and(|c| c > Decimal::ZERO);
    Ok(json!({
        "day": inputs.day,
        "assignment": assignment.as_ref().map(Assignment::view),
        "basal_units": basal_units(&form).to_string(),
        "day_type": if day_type_visible { json!({"value": inputs.day_type, "source": inputs.day_type_source}) } else { Value::Null },
        "split": split,
        "place": if form.mode == "meal" && has_carbs { inputs.place.clone() } else { Value::Null },
        "on_board": on_board(&form, &dose),
        "dose": dose,
    }))
}

/// The tapped option still offers exactly the dose that was shown and is being saved.
fn same_dose(dose: &Map<String, Value>, payload: &Value, insulin: Decimal) -> bool {
    let Some(shown) = payload.get("dose_shown").filter(|s| s.is_object()) else { return false };
    let variant = payload.get("dose_variant");
    let option = dose.get("options").and_then(Value::as_array)
        .and_then(|options| options.iter().find(|o| o.get("variant") == variant));
    let Some(option) = option.filter(|o| o.get("savable") == Some(&Value::Bool(true))) else { return false };
    fn value(v: Option<&Value>) -> Option<&Value> {
        v.filter(|v| !v.is_null())
    }
    let same = |a: Option<&Value>, b: Option<&Value>| match (value(a), value(b)) {
        (None, None) => true,
        (Some(a), Some(b)) => decimal_of(a).is_some() && decimal_of(a) == decimal_of(b),
        _ => false,
    };
    let units = option.get("units");
    same(units, shown.get("units")) && same(units, Some(&json!(insulin.to_string())))
        && same(option.get("later_units"), shown.get("later_units"))
}

/// What saving the entry stores, after the same checks the form shows. The adapter writes it.
pub fn save_plan(inputs: &Inputs) -> Res<Value> {
    let form = Form::new(inputs)?;
    let payload = &inputs.payload;
    let mode = form.mode;
    if mode == "basal" {
        return Err(invalid(t("Basal wird über die Basal-Eingabe gespeichert")));
    }
    let out_of_range = || invalid(t("KH oder Insulin außerhalb des erlaubten Bereichs"));
    let carbs = if mode == "meal" { form.carbs.ok_or_else(out_of_range)? } else { Decimal::ZERO };
    let insulin = if mode == "meal" { amount(payload.get("insulin")).map_err(|_| out_of_range())?.unwrap_or_default() } else { Decimal::ZERO };
    let finger = form.finger;
    if mode == "meal" && carbs <= Decimal::ZERO && insulin <= Decimal::ZERO && finger.is_none() {
        return Err(invalid(t("Bitte Kohlenhydrate, Insulin oder beides eintragen.")));
    }
    let at = inputs.at.ok_or_else(|| invalid(t("Zeit fehlt")))?;
    let base = json!({"carbs": carbs.to_string(), "insulin": insulin.to_string(), "finger_mgdl": finger.map(|f| f.to_string())});
    if finger.is_some() && carbs <= Decimal::ZERO && insulin <= Decimal::ZERO {
        let mut plan = base;
        plan["kind"] = json!("finger");
        plan["day"] = json!(local_zone(&inputs.settings.timezone).local(&at).date().to_string());
        return Ok(plan);
    }
    let assignment = assignment(&form);
    let basal = if payload.get("with_basal").is_some_and(|v| !v.is_null() && v != &json!(false) && v != &json!(0) && v != &json!(""))
        { basal_units(&form) } else { Decimal::ZERO };
    let (sport_drop, sport_minutes) = if mode == "exercise" { (sport_drop(payload)?, sport_minutes(payload)?) } else { (None, None) };
    // A session's own drop is stored only where it differs from its kind's setting.
    let kind_drop = payload.get("sport_kind").and_then(Value::as_str)
        .and_then(|kind| inputs.settings.sport_effects_mgdl.get(kind)).and_then(decimal_of).unwrap_or_default();
    let sport_drop = sport_drop.filter(|drop| *drop != kind_drop);
    let split = if carbs > Decimal::ZERO { split_chip(&form, assignment.as_ref()) } else { None };
    let tapped = payload.get("dose_check").is_some_and(|v| !v.is_null() && v != &json!(""));
    let split_active = split.as_ref().is_some_and(|chip| chip.get("active") == Some(&Value::Bool(true)));
    let (dose, split) = if tapped || split_active { dose(&form, assignment.as_ref(), split)? } else { (Map::new(), split) };
    let mut split_units = None;
    if split.as_ref().is_some_and(|chip| chip.get("active") == Some(&Value::Bool(true))) && form.is_today() {
        // The second part completes the chosen suggestion (normal unless another was tapped).
        let options = dose.get("options").and_then(Value::as_array).cloned().unwrap_or_default();
        let by = |variant: &str| options.iter().find(|o| o["variant"] == variant).cloned();
        let option = payload.get("dose_variant").and_then(Value::as_str).and_then(by).or_else(|| by("normal")).or_else(|| by("carbs"));
        match payload.get("dose_total_units").filter(|v| !v.is_null() && **v != json!("")) {
            Some(total) => {
                // A dose chosen on the slider: its total, of which the saved insulin is the first part.
                let total = decimal_of(total).filter(|t| Decimal::ZERO <= *t && *t <= MAX_UNITS)
                    .ok_or_else(|| invalid(t("Gesamtdosis muss zwischen 0 und 100 IE liegen")))?;
                split_units = Some(total - insulin).filter(|later| *later > Decimal::ZERO);
            }
            None => {
                if let Some(full) = option.as_ref().and_then(|o| number(o.get("full_units").filter(|v| !v.is_null()))) {
                    split_units = Some(full - insulin).filter(|later| *later > Decimal::ZERO);
                }
            }
        }
    }
    let kind = if mode == "exercise" { "exercise" }
        else if carbs > Decimal::ZERO { if assignment.as_ref().is_some_and(|a| a.snack) { "snack" } else { "meal" } }
        else { "correction" };
    let calculated = if carbs > Decimal::ZERO { fpe_surcharge(&form) } else { None };
    let close = assignment.as_ref().and_then(|a| a.close.as_ref()).filter(|c| carbs > Decimal::ZERO && c.active);
    let mut plan = base;
    let extra = json!({
        "kind": kind, "at": iso(&at),
        "main_meal": assignment.as_ref().is_some_and(|a| !a.snack),
        "basal_units": basal.to_string(),
        "sport_drop": sport_drop.map(|d| d.to_string()), "sport_minutes": sport_minutes,
        "split_units": split_units.filter(|_| kind == "meal").map(|u| u.to_string()),
        "split_delay_minutes": split.as_ref().and_then(|chip| chip.get("delay_minutes").cloned()),
        "fpe_surcharge": calculated.map(|c| json!({"mgdl": c.mgdl.to_string(), "minutes": c.minutes})),
        "close": close.map(|c| json!({"meal_id": c.meal_id, "start": iso(&c.start), "end": iso(&c.end)})),
        "tapped": tapped, "same_dose": tapped && same_dose(&dose, payload, insulin),
        "check_material": dose.get("check_material").cloned().unwrap_or(Value::Null),
    });
    if let (Value::Object(plan), Value::Object(extra)) = (&mut plan, extra) {
        plan.extend(extra);
    }
    Ok(plan)
}
