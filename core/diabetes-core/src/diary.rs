//! Diary entries as Nightscout stores them: which event type and note markers a carb, correction or sport
//! entry gets, the checks before writing it, and how a saved entry's card edit maps onto it.
//!
//! The adapter (app/services/diary.py) fetches the entry being edited and the day's treatments, resolves the
//! agent's calculated surcharge, writes the record to Nightscout and keeps the local records (place, dish).

use crate::limits::{MAX_CARBS_G, MAX_UNITS, SPORT_DURATION_MINUTES, SPORT_EFFECT_MAX_MGDL, SPORT_EFFECT_MINUTES};
use crate::wire::truthy;
use crate::context::{local_zone, t};
use crate::day::{iso, sport_names};
use crate::episodes;
use crate::model::Certainty;
use crate::fpe;
use crate::model::{decimal_of, Instant, Settings};
use crate::{CoreError, Res};
use rust_decimal::prelude::ToPrimitive;
use rust_decimal::{Decimal, RoundingStrategy};
use rust_decimal_macros::dec;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::collections::HashMap;

pub const KINDS: [&str; 5] = ["meal", "snack", "carbs", "correction", "exercise"];
pub const EDITABLE_KINDS: [&str; 6] = ["meal", "snack", "carbs", "correction", "exercise", "basal"];
/// Event types a diary entry may have when it is edited here.
const EDITABLE_TYPES: [&str; 6] = ["Meal Bolus", "Snack Bolus", "Carb Correction", "Correction Bolus", "Bolus", "Exercise"];
/// The quality marks set by chips: payload flag -> note marker.
const QUALITY_MARKERS: [(&str, &str); 3] = [("fat_protein", "[DAQ:fat]"), ("alcohol", "[DAQ:alcohol]"), ("fast", "[DAQ:fast]")];
const NOTE_MAX: usize = 500;

fn invalid(message: String) -> CoreError {
    CoreError::Value(message)
}

/// Python's Decimal(str(value or 0)): None, "" and 0 are zero; text that is no number is None.
fn amount(value: Option<&Value>) -> Option<Decimal> {
    match value {
        None | Some(Value::Null) | Some(Value::Bool(false)) => Some(Decimal::ZERO),
        Some(Value::String(text)) if text.is_empty() => Some(Decimal::ZERO),
        Some(other) => decimal_of(other),
    }
}

/// Insulin units without trailing zeros: 20, 17.5.
fn unit_text(units: Decimal) -> Decimal {
    units.normalize()
}

#[derive(Clone, Debug, Deserialize)]
pub struct Inputs {
    /// What the form or card sends (kind, at, carbs, insulin, notes, sport_kind, duration_minutes, main_meal,
    /// carbs_certainty, quality flags, split_units, split_delay_minutes, fpe_surcharge, fpe_level, surcharge_mode).
    pub payload: Value,
    /// The settings as stored (sport_custom is read as written) and typed.
    pub settings: Value,
    pub at: Instant,
    /// The entry's event id (kept from the edited entry, else the form's, else new).
    pub event_id: String,
    /// The Nightscout record being edited.
    #[serde(default)]
    pub existing: Option<Map<String, Value>>,
    /// The day's Nightscout treatments, for a new main meal (it may become a snack).
    #[serde(default)]
    pub nearby: Vec<Value>,
}

/// A diary entry's Nightscout record, checked. `record` stays in Nightscout's own shape: the edited record
/// with its other fields kept.
#[derive(Clone, Debug, Serialize)]
pub struct DiaryRecord {
    pub record: Map<String, Value>,
    pub event_type: String,
    pub notes: String,
    pub carbs: Decimal,
    pub insulin: Decimal,
    /// The entry's local day (YYYY-MM-DD).
    pub day: String,
}

/// The Nightscout record of a diary entry, checked: `record` (the edited one updated), its `event_type`,
/// `notes`, `carbs`, `insulin` and local `day`.
pub fn record(inputs: &Inputs) -> Res<DiaryRecord> {
    let payload = &inputs.payload;
    let settings: Settings = serde_json::from_value(inputs.settings.clone()).map_err(|e| invalid(format!("settings: {e}")))?;
    let kind = payload.get("kind").and_then(Value::as_str).unwrap_or_default();
    if !KINDS.contains(&kind) {
        return Err(invalid(t("Ungültiger Eintragstyp")));
    }
    let at = inputs.at;
    let day = local_zone(&settings.timezone).local(&at).date().to_string();
    let out_of_range = || invalid(t("KH oder Insulin außerhalb des erlaubten Bereichs"));
    let carbs = amount(payload.get("carbs")).ok_or_else(out_of_range)?;
    let units = amount(payload.get("insulin")).ok_or_else(out_of_range)?;
    if !(Decimal::ZERO <= carbs && carbs <= MAX_CARBS_G && Decimal::ZERO <= units && units <= MAX_UNITS) {
        return Err(out_of_range());
    }
    if ["meal", "snack", "carbs"].contains(&kind) && carbs <= Decimal::ZERO {
        return Err(invalid(t("Kohlenhydrate müssen positiv sein")));
    }
    if ["correction", "exercise"].contains(&kind) && !carbs.is_zero() {
        return Err(invalid(t("Dieser Eintrag hat keine Kohlenhydrate")));
    }
    if (["carbs", "exercise"].contains(&kind) && !units.is_zero()) || (kind == "correction" && units <= Decimal::ZERO) {
        return Err(invalid(t("Ungültige Insulinmenge")));
    }
    let names = sport_names(&inputs.settings);
    let mut duration = 0;
    let mut sport_kind = String::new();
    if kind == "exercise" {
        sport_kind = match payload.get("sport_kind") { Some(Value::String(text)) => text.clone(), None => String::new(),
                                                         Some(other) => other.to_string() };
        if !names.iter().any(|(known, _)| *known == sport_kind) {
            return Err(invalid(t("Ungültige Sportart")));
        }
        duration = match payload.get("duration_minutes") {
            None => 0,
            Some(value) => decimal_of(value).and_then(|d| d.trunc().to_i64()).ok_or_else(|| invalid(t("Sportdauer muss zwischen 1 und 1440 Minuten liegen")))?,
        };
        if !SPORT_DURATION_MINUTES.contains(&duration) {
            return Err(invalid(t("Sportdauer muss zwischen 1 und 1440 Minuten liegen")));
        }
    }
    let note: String = match payload.get("notes") { Some(Value::String(text)) => text.trim().to_string(), Some(other) if truthy(other) => other.to_string(),
                                                     _ => String::new() }.chars().take(NOTE_MAX).collect();
    let existing = inputs.existing.as_ref();
    if let Some(existing) = existing {
        if !EDITABLE_TYPES.contains(&existing.get("eventType").and_then(Value::as_str).unwrap_or_default()) {
            return Err(invalid(t("Dieser Eintragstyp kann hier nicht bearbeitet werden")));
        }
    }
    let old_notes = existing.and_then(|e| e.get("notes")).map(|n| match n { Value::String(s) => s.clone(), Value::Null => String::new(), o => o.to_string() })
        .unwrap_or_default();
    let event_id = &inputs.event_id;
    let mut quality = String::new();
    let (mut event_type, mut notes) = if kind == "exercise" {
        let label = names.iter().find(|(known, _)| *known == sport_kind).map(|(_, name)| name.clone()).unwrap_or_default();
        ("Exercise".to_string(), format!("{label}{} [DAS1:{sport_kind}] [DAE:{event_id}]", if note.is_empty() { String::new() } else { format!(" — {note}") }))
    } else {
        let event_type = match kind { "meal" => "Meal Bolus", "snack" if units > Decimal::ZERO => "Snack Bolus", "correction" => "Correction Bolus",
                                  _ => "Carb Correction" }.to_string();
        let main_meal = kind == "meal" && (payload.get("main_meal").is_some_and(truthy) || old_notes.contains("[DA2:meal]"));
        let marker = if kind == "snack" { " [DA2:snack]" } else if main_meal { " [DA2:meal]" } else { "" };
        let mut certainty = payload.get("carbs_certainty").cloned().unwrap_or(Value::Null);
        if payload.get("carbs_unsure") == Some(&Value::Bool(true)) && !truthy(&certainty) {
            certainty = json!("unsure"); // older clients
        }
        let certainty = match &certainty {
            Value::Null => None,
            Value::String(text) if text.is_empty() => None,
            Value::String(text) if Certainty::parse(text).is_some() => Some(text.clone()),
            _ => return Err(invalid(t("Unbekannte KH-Sicherheit"))),
        };
        // "keep" leaves a mark as the edited entry has it (the Alkohol chip is gone since 2026-11-15, its old marks stay).
        let eats = carbs > Decimal::ZERO;
        if eats {
            if let Some(level) = &certainty {
                quality += &format!(" [DAQ:{level}]");
            }
            for (flag, tag) in QUALITY_MARKERS {
                let set = if payload.get(flag) == Some(&json!("keep")) { old_notes.contains(tag) } else { payload.get(flag) == Some(&Value::Bool(true)) };
                if set {
                    quality += &format!(" {tag}");
                }
            }
        }
        let mut split = String::new();
        let split_units = payload.get("split_units").filter(|v| !v.is_null());
        if let Some(split_units) = split_units.filter(|_| kind == "meal") {
            let later = decimal_of(split_units);
            let delay = amount(payload.get("split_delay_minutes")).and_then(|d| d.trunc().to_i64()).unwrap_or(-1);
            match later {
                Some(later) if Decimal::ZERO < later && later <= dec!(100) && (30..=300).contains(&delay) =>
                    split = format!(" {}", episodes::split_tag(unit_text(later), delay)),
                _ => return Err(invalid(t("Ungültiger Split-Bolus"))),
            }
        } else if kind == "meal" && existing.is_some() {
            if let Some(kept) = episodes::split_re().find(&old_notes).map(|m| m.as_str().to_string()) {
                split = format!(" {kept}");
            }
        }
        // The surcharge belongs to each carb entry, so any of them can carry the calculated one.
        let mut surcharge_plan = payload.get("fpe_surcharge").cloned();
        let level = payload.get("fpe_level").and_then(Value::as_str);
        if eats && surcharge_plan.is_none() && level.is_some_and(|l| fpe::LEVELS.iter().any(|(name, _)| *name == l))
            && payload.get("surcharge_mode").is_none_or(Value::is_null) {
            // A carb entry without the agent's estimate sets its level on the card (2026-11-26).
            if let Some(found) = fpe::estimate(&json!({"fpe_level": level}), &settings) {
                surcharge_plan = Some(json!({"mgdl": found.mgdl.to_string(), "minutes": found.minutes}));
            }
        }
        let mut surcharge = String::new();
        match surcharge_plan.as_ref().filter(|plan| eats && truthy(plan)) {
            Some(plan) => {
                let mgdl = plan.get("mgdl").and_then(decimal_of);
                let minutes = plan.get("minutes").and_then(decimal_of).and_then(|m| m.trunc().to_i64());
                match (mgdl, minutes) {
                    (Some(mgdl), Some(minutes)) if Decimal::ZERO <= mgdl && mgdl <= dec!(200) && (60..=600).contains(&minutes) => {
                        let whole = mgdl.round_dp_with_strategy(0, RoundingStrategy::MidpointNearestEven);
                        surcharge = format!(" {}", episodes::surcharge_tag(unit_text(whole), minutes));
                    }
                    _ => return Err(invalid(t("Ungültiger Fett/Eiweiß-Zuschlag"))),
                }
            }
            None if eats && existing.is_some() && payload.get("fpe_surcharge").is_none() => {
                if let Some(kept) = episodes::surcharge_re().find(&old_notes).map(|m| m.as_str().to_string()) {
                    surcharge = format!(" {kept}");
                }
            }
            None => {}
        }
        let mut notes = format!("{note}{marker}{quality}{split}{surcharge} [DAE:{event_id}]").trim().to_string();
        if kind == "meal" {
            if let Some(legacy) = episodes::tag_re().captures(&old_notes).filter(|c| &c[3] == "meal") {
                notes += &format!(" {}", &legacy[0]);
            }
        }
        (event_type, notes)
    };
    let mut record = existing.cloned().unwrap_or_default();
    record.insert("eventType".into(), json!(event_type));
    record.insert("created_at".into(), json!(iso(&at).replace("+00:00", "Z")));
    record.insert("notes".into(), json!(notes));
    record.insert("enteredBy".into(), json!("Diabetes Diary"));
    record.insert("identifier".into(), json!(event_id));
    record.insert("carbs".into(), json!(carbs.to_f64().unwrap_or_default()));
    record.insert("insulin".into(), json!(units.to_f64().unwrap_or_default()));
    if kind == "meal" && existing.is_none() {
        // Also a [DA2:meal] entry when its kind is taken that day.
        let mut raw = inputs.nearby.clone();
        raw.push(Value::Object(record.clone()));
        let mut classified = episodes::normalize_treatments(&raw);
        episodes::group_diary_followups(&mut classified, &settings);
        let marker = format!("[DAE:{event_id}]");
        if let Some(candidate) = classified.iter().find(|item| item.notes.contains(&marker)) {
            if candidate.event_type != "Meal Bolus" {
                event_type = candidate.event_type.clone();
                notes = format!("{note} [DA2:snack]{quality} [DAE:{event_id}]").trim().to_string();
                record.insert("eventType".into(), json!(event_type));
                record.insert("notes".into(), json!(notes));
            }
        }
    }
    if record.contains_key("date") {
        record.insert("date".into(), json!(at.timestamp_millis()));
    }
    if kind == "exercise" {
        record.insert("duration".into(), json!(duration));
    } else {
        record.remove("duration");
    }
    Ok(DiaryRecord { record, event_type, notes, carbs, insulin: units, day })
}

/// What a card edit becomes: basal (replaced elsewhere, only its kind) or an entry with its new kind and amounts.
#[derive(Clone, Debug, Serialize)]
#[serde(untagged)]
pub enum EditKind {
    Basal { kind: &'static str },
    Entry { kind: &'static str, carbs: Decimal, insulin: Decimal },
}

/// A saved entry changed on its card: the kind follows from the entry's kind (`original_kind`) and the new
/// values. Returns kind, carbs and insulin (basal is replaced elsewhere and returns only its kind).
pub fn edit_kind(payload: &Value) -> Res<EditKind> {
    let original = payload.get("original_kind").and_then(Value::as_str).unwrap_or_default();
    if !EDITABLE_KINDS.contains(&original) {
        return Err(invalid(t("Dieser Eintragstyp kann hier nicht bearbeitet werden")));
    }
    if !payload.get("id").and_then(Value::as_str).is_some_and(|id| !id.is_empty() && id.chars().count() <= 200) {
        return Err(invalid(t("Ungültiger Eintrag")));
    }
    if original == "basal" {
        return Ok(EditKind::Basal { kind: "basal" });
    }
    let sport = original == "exercise";
    let bad = || invalid(t("KH oder Insulin außerhalb des erlaubten Bereichs"));
    let carbs = if sport { Decimal::ZERO } else { amount(payload.get("carbs")).ok_or_else(bad)? };
    let units = if sport { Decimal::ZERO } else { amount(payload.get("insulin")).ok_or_else(bad)? };
    if !sport && carbs <= Decimal::ZERO && units <= Decimal::ZERO {
        return Err(invalid(t("Bitte Kohlenhydrate, Insulin oder beides eintragen.")));
    }
    let kind = if sport { "exercise" }
        else if carbs > Decimal::ZERO { if original == "snack" { "snack" } else if original == "carbs" && units.is_zero() { "carbs" } else { "meal" } }
        else { "correction" };
    Ok(EditKind::Entry { kind, carbs, insulin: units })
}

/// Changes to a sport session's own effect and duration; only what changed is set.
#[derive(Clone, Debug, Default, Serialize)]
pub struct SportChanges {
    /// Back to the effect of its kind.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reset: Option<bool>,
    /// The session's own glucose drop in mg/dL.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub effect_mgdl: Option<i64>,
    /// The session's own effect duration as the card sent it (null: its kind's).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub minutes: Option<Value>,
}

/// The session's own effect and duration from the card (null = its kind's setting), as changes to the stored
/// ones: `reset`, `effect_mgdl` (a drop) and `minutes`. Only a change is listed.
pub fn sport_changes(payload: &Value, effects: &HashMap<String, Value>, durations: &HashMap<String, Value>, record_id: &str) -> Res<SportChanges> {
    let duration = payload.get("duration_minutes").filter(|v| v.is_number()).and_then(Value::as_f64);
    if !duration.is_some_and(|d| (*SPORT_DURATION_MINUTES.start() as f64..=*SPORT_DURATION_MINUTES.end() as f64).contains(&d)) {
        return Err(invalid(t("Bitte eine Dauer zwischen 1 und 1440 Minuten eintragen.")));
    }
    let change = payload.get("sport_effect_change_mgdl").filter(|v| !v.is_null());
    let change = match change {
        None => None,
        Some(Value::Number(n)) if n.as_i64().is_some_and(|v| Decimal::from(v.abs()) <= SPORT_EFFECT_MAX_MGDL) => n.as_i64(),
        Some(_) => return Err(invalid(t("Bitte eine BZ-Wirkung zwischen −500 und +500 mg/dL eintragen."))),
    };
    let minutes = payload.get("sport_minutes").filter(|v| !v.is_null());
    if let Some(value) = minutes {
        if !value.as_f64().is_some_and(|m| value.is_number() && (*SPORT_EFFECT_MINUTES.start() as f64..=*SPORT_EFFECT_MINUTES.end() as f64).contains(&m)) {
            return Err(invalid(t("Die Wirkdauer muss zwischen 0,5 und 24 h liegen.")));
        }
    }
    let mut changes = SportChanges::default();
    // The API stores a session's effect as a glucose drop; the card shows the signed change.
    let stored = effects.get(record_id);
    match change {
        None if stored.is_some() => changes.reset = Some(true),
        Some(change) if stored.and_then(decimal_of) != Some(Decimal::from(-change)) => changes.effect_mgdl = Some(-change),
        _ => {}
    }
    let current = durations.get(record_id).and_then(Value::as_f64);
    if minutes.and_then(Value::as_f64) != current {
        changes.minutes = Some(minutes.cloned().unwrap_or(Value::Null));
    }
    Ok(changes)
}
