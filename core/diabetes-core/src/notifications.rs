//! Push reminders: which are due now and what they say. A forgotten basal, a due second split part,
//! early warnings of a low or high (glucose_alerts) and "BZ prüfen" after a meal (correction_check).
//!
//! The adapter (app/services/notifications.py) gathers the dose help's snapshot, the switched-on kinds,
//! the warning pauses and when each repeating kind was last sent; it checks which keys were already
//! sent, sends over Pushover and stores what went out.

use crate::context::{english, local_zone, t, tr};
use crate::correction_check;
use crate::wire::iso;
use crate::glucose::{limits, shown};
use crate::glucose_alerts::{self, Warning};
use crate::model::{Instant, Reading, Settings, Treatment};
use crate::services::{self, DoseHelp, Factors, Snapshot};
use crate::split;
use crate::Res;
use chrono::Duration;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::collections::HashMap;

/// Minutes before a low or high warning may come again.
pub const WARNING_REPEAT_MINUTES: i64 = 60;

#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct Reminder {
    pub key: String,
    pub kind: &'static str,
    pub title: String,
    pub message: String,
}

/// A warning as data (glucose_alerts::warnings' result read back).
#[derive(Clone, Debug, Deserialize)]
pub struct WarningInput {
    pub kind: String,
    #[serde(deserialize_with = "crate::model::amount")]
    pub glucose_mgdl: Decimal,
    pub at: Instant,
    #[serde(deserialize_with = "crate::model::amount")]
    pub mgdl: Decimal,
}

impl From<WarningInput> for Warning {
    fn from(input: WarningInput) -> Warning {
        Warning { kind: if input.kind == "low" { "low" } else { "high" }, glucose_mgdl: input.glucose_mgdl, at: input.at, mgdl: input.mgdl }
    }
}

/// The dose help's correction now without food (None without a fresh glucose value).
#[derive(Clone, Debug, Default, Deserialize)]
pub struct Correction {
    #[serde(default, deserialize_with = "crate::model::opt_amount")]
    pub normal: Option<Decimal>,
    #[serde(default, deserialize_with = "crate::model::opt_amount")]
    pub cautious: Option<Decimal>,
    /// The ends of what was right after recent meals (fast = little need, slow = much) and the tempo
    /// the meal's rise leans to.
    #[serde(default, deserialize_with = "crate::model::opt_amount")]
    pub fast: Option<Decimal>,
    #[serde(default, deserialize_with = "crate::model::opt_amount")]
    pub slow: Option<Decimal>,
    #[serde(default)]
    pub tempo: Option<String>,
}

pub fn label(kind: &str) -> String {
    t(match kind {
        "basal" => "Basal vergessen",
        "split" => "2. Split-Teil fällig",
        "low" => "Tief droht",
        "high" => "Hoch droht",
        _ => "BZ prüfen",
    })
}

/// How soon a repeating kind may come again; None for kinds sent once per key.
pub fn repeat_minutes(kind: &str) -> Option<i64> {
    match kind {
        "low" | "high" => Some(WARNING_REPEAT_MINUTES),
        "check" => Some(correction_check::REPEAT_MINUTES),
        _ => None,
    }
}

/// Insulin units without trailing zeros, with a decimal comma in German.
fn units(value: Decimal) -> String {
    let text = if value.is_zero() { "0".to_string() } else { value.normalize().to_string() };
    if english() { text } else { text.replace('.', ",") }
}

fn clock(at: Instant, settings: &Settings) -> String {
    local_zone(&settings.timezone).local(&at).format("%H:%M").to_string()
}

fn on(enabled: &HashMap<String, bool>, kind: &str) -> bool {
    enabled.get(kind).copied().unwrap_or(false)
}

/// Basal and split reminders due now. `second_part` gives the second split part's IE from the current
/// forecast (None without a fresh glucose value); it is asked only when a part is due.
pub fn due_reminders(now: Instant, settings: &Settings, treatments: &[Treatment], basal_today: bool,
                     enabled: &HashMap<String, bool>, entries: &[Reading],
                     second_part: &mut dyn FnMut() -> Option<Decimal>) -> Vec<Reminder> {
    let zone = local_zone(&settings.timezone);
    let local = zone.local(&now);
    let mut reminders = Vec::new();
    if on(enabled, "basal") && zone.minute_of_day(&now) >= settings.breakfast_end_minute && !basal_today {
        let usual = settings.basal_default_units;
        // Only the facts: the user knows the app, the title says what is due.
        let message = if usual > Decimal::ZERO { tr("Üblich {units} IE", &[("units", units(usual))]) } else { t("Heute noch kein Basal") };
        reminders.push(Reminder { key: format!("basal:{}", local.date()), kind: "basal", title: label("basal"), message });
    }
    let plan = if on(enabled, "split") { split::open_plan(treatments, now) } else { None };
    if let Some(plan) = plan {
        let state = split::timing(&plan, entries, settings, now).state;
        if state == "early" || state == "due" {
            // The amount comes from the current forecast, the plan stays as orientation.
            let meal = tr("Mahlzeit um {time}", &[("time", clock(plan.meal_at, settings))]);
            let planned = units(plan.units);
            let message = match second_part() {
                None => tr("{units} IE zur {meal}", &[("units", planned), ("meal", meal)]),
                Some(value) if value > Decimal::ZERO => tr("{units} IE laut Prognose (geplant {planned}) zur {meal}",
                    &[("units", units(value)), ("planned", planned), ("meal", meal)]),
                Some(_) => tr("Laut Prognose nicht nötig (geplant {planned}) zur {meal}", &[("planned", planned), ("meal", meal)]),
            };
            // One reminder per plan: an early one is not repeated at the planned time.
            let title = if state == "early" { t("2. Split-Teil jetzt: BZ steigt schnell") } else { label("split") };
            reminders.push(Reminder { key: format!("split:{}", iso(&plan.meal_at)), kind: "split", title, message });
        }
    }
    reminders
}

/// The push texts of low and high warnings (switched-on kinds only).
pub fn warning_reminders(now: Instant, settings: &Settings, warnings: &[Warning], enabled: &HashMap<String, bool>) -> Vec<Reminder> {
    let (low, high) = limits(settings);
    warnings.iter().filter(|warning| on(enabled, warning.kind)).map(|warning| {
        let minutes = ((warning.at - now).num_milliseconds() as f64 / 60_000.0).round_ties_even().max(0.0) as i64;
        let (crossing, extreme) = if warning.kind == "low" {
            (tr("unter {mgdl}", &[("mgdl", shown(low, settings, false))]), t("Tiefstwert"))
        } else {
            (tr("über {mgdl}", &[("mgdl", shown(high, settings, false))]), t("Höchstwert"))
        };
        let message = tr("BZ {glucose}, {crossing} um {time} (in {minutes} min), {extreme} {mgdl}", &[
            ("glucose", shown(warning.glucose_mgdl, settings, false)), ("crossing", crossing),
            ("time", clock(warning.at, settings)), ("minutes", minutes.to_string()), ("extreme", extreme),
            ("mgdl", shown(warning.mgdl, settings, false))]);
        Reminder { key: format!("{}:{}", warning.kind, iso(&now)), kind: warning.kind, title: label(warning.kind), message }
    }).collect()
}

/// The "BZ prüfen" text for a due check, with the dose help's correction.
pub fn correction_reminder(now: Instant, settings: &Settings, hit: &correction_check::Check, correction: Option<&Correction>) -> Reminder {
    let mut message = tr("BZ {glucose} ({rise} in 30 min), Prognose Ende {end}, Mahlzeit um {time}", &[
        ("glucose", shown(hit.glucose_mgdl, settings, false)), ("rise", shown(hit.rise_mgdl, settings, true)),
        ("end", shown(hit.end_mgdl, settings, false)), ("time", clock(hit.meal_at, settings))]);
    if let Some(correction) = correction {
        match correction.normal {
            Some(normal) if normal > Decimal::ZERO => {
                message += &tr(". Korrektur {units} IE", &[("units", units(normal))]);
                if let (Some(fast), Some(slow)) = (correction.fast, correction.slow) {
                    // The ends of what was right after recent meals; the meal's rise leans to one.
                    message += &tr(" (wenig {fast} · viel {slow} IE", &[("fast", units(fast)), ("slow", units(slow))]);
                    if let Some(tempo) = correction.tempo.as_deref().filter(|tempo| !tempo.is_empty()) {
                        let lean = t(match tempo { "fast" => "wenig", "slow" => "viel", _ => "normal" });
                        message += &tr(", eher {lean}", &[("lean", lean)]);
                    }
                    message += ")";
                } else if let Some(cautious) = correction.cautious.filter(|c| *c != normal) {
                    message += &tr(" (vorsichtig {units} IE)", &[("units", units(cautious))]);
                }
            }
            Some(_) => message += &t(". Laut Dosishilfe keine Korrektur nötig"),
            None => {}
        }
    }
    Reminder { key: format!("check:{}", iso(&now)), kind: "check", title: label("check"), message }
}

/// Everything the background job's check reads.
#[derive(Clone, Debug, Deserialize)]
pub struct Inputs {
    /// The dose help's snapshot of now (its payload is replaced by an entry without food).
    pub snapshot: Snapshot,
    pub enabled: HashMap<String, bool>,
    /// Whether today already has a basal dose.
    pub basal_today: bool,
    /// Minutes without a warning: low after a carb entry, high after rapid insulin.
    #[serde(default)]
    pub pauses: HashMap<String, i64>,
    /// When each repeating kind was last sent.
    #[serde(default)]
    pub last_sent: HashMap<String, Option<Instant>>,
}

fn dose_help_now(snapshot: &Snapshot) -> Option<DoseHelp> {
    let mut without_food = snapshot.clone();
    without_food.payload = json!({"carbs": "0"});
    services::dose_help(&without_food).ok()
}

fn correction_of(result: &DoseHelp) -> Option<Correction> {
    let normal = result.suggestion.entry_units?;
    let scale = result.scale();
    Some(Correction {
        normal: Some(normal),
        cautious: result.suggestion.cautious_entry_units,
        fast: scale.map(|s| s.fast_entry_units),
        slow: scale.map(|s| s.slow_entry_units),
        tempo: scale.and_then(|s| s.tempo.clone()),
    })
}

/// The reminders due now, in sending order, without those whose kind came too recently.
pub fn due(inputs: &Inputs) -> Res<Vec<Reminder>> {
    let snapshot = &inputs.snapshot;
    let (now, settings, enabled) = (snapshot.now, &snapshot.settings, &inputs.enabled);
    if !enabled.values().any(|on| *on) {
        return Ok(Vec::new());
    }
    // The dose help without food: its correction is the second split part's amount and the check's correction.
    let mut help: Option<Option<DoseHelp>> = None;
    let mut help_now = || help.get_or_insert_with(|| dose_help_now(snapshot)).clone();
    let mut reminders = due_reminders(now, settings, &snapshot.treatments, inputs.basal_today, enabled, &snapshot.entries,
        &mut || help_now().and_then(|result| result.suggestion.entry_units));
    let wants_warning = on(enabled, "low") || on(enabled, "high");
    if wants_warning || on(enabled, "check") {
        // What still acts now ("Wirkt noch"), as for the dose help.
        if let Ok(found) = Factors::new(snapshot).sources() {
            if wants_warning {
                let (low, high) = limits(settings);
                let pause = |kind: &str, default: i64| inputs.pauses.get(kind).copied().unwrap_or(default);
                let bounds = glucose_alerts::Limits {
                    low_pause_minutes: pause("low", glucose_alerts::LOW_PAUSE_MINUTES),
                    high_pause_minutes: pause("high", glucose_alerts::HIGH_PAUSE_MINUTES), low_mgdl: low, high_mgdl: high };
                let warnings = glucose_alerts::warnings(&snapshot.entries, &found, now, &settings.timezone, &bounds);
                reminders.extend(warning_reminders(now, settings, &warnings, enabled));
            }
            if on(enabled, "check") {
                if let Some(hit) = correction_check::due(&snapshot.entries, &found, &snapshot.treatments, now) {
                    let correction = help_now().as_ref().and_then(correction_of);
                    reminders.push(correction_reminder(now, settings, &hit, correction.as_ref()));
                }
            }
        }
    }
    Ok(reminders.into_iter().filter(|reminder| {
        let last = inputs.last_sent.get(reminder.kind).copied().flatten();
        match (repeat_minutes(reminder.kind), last) {
            (Some(minutes), Some(last)) => now - last >= Duration::minutes(minutes),
            _ => true,
        }
    }).collect())
}
