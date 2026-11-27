//! The manual window of a meal: its limits, the editor's readings and what a candidate window counts.
//!
//! The adapter (app/services/window_editor.py, annotations.py, entry.py) reads the stored day (meal,
//! readings, treatments) and the settings; storing a chosen window stays there.

use crate::calculation::{window_totals, Episode, Interpolator};
use crate::context::t;
use crate::glucose::{tone, trend};
use crate::model::{lenient, Instant, Reading, Settings, Treatment};
use crate::wire::py_iso;
use chrono::{Duration, Timelike};
use rust_decimal::{prelude::ToPrimitive, Decimal, RoundingStrategy};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;

/// A manual window starts at most this long before or after the meal ...
pub const LEAD_MINUTES: i64 = 120;
pub const LAG_MINUTES: i64 = 120;
/// ... ends at least this long after meal and window start, and lasts at most MAX_MINUTES.
pub const MIN_MINUTES: i64 = 30;
pub const MAX_MINUTES: i64 = 600;
/// The editor shows a little more than the handles can reach.
const VIEW_LEAD_MINUTES: i64 = LEAD_MINUTES + 30;
const VIEW_LAG_MINUTES: i64 = MAX_MINUTES + 60;
const MAX_GAP_MINUTES: i64 = 15;

/// The manual-window limits the page draws.
#[derive(Clone, Copy, Debug, Serialize)]
pub struct Limits {
    pub lead_minutes: i64,
    pub lag_minutes: i64,
    pub min_minutes: i64,
    pub max_minutes: i64,
}

pub fn limits() -> Limits {
    Limits { lead_minutes: LEAD_MINUTES, lag_minutes: LAG_MINUTES, min_minutes: MIN_MINUTES, max_minutes: MAX_MINUTES }
}

/// Why a manual window is not allowed, or None.
pub fn problem(meal_at: Instant, start: Instant, end: Instant) -> Option<String> {
    if !(meal_at - Duration::minutes(LEAD_MINUTES) <= start && start <= meal_at + Duration::minutes(LAG_MINUTES)) {
        return Some(t("Beginn muss höchstens zwei Stunden vor oder nach dem Mahlzeitbeginn liegen"));
    }
    if end < start.max(meal_at) + Duration::minutes(MIN_MINUTES) {
        return Some(t("Ende muss mindestens 30 Minuten nach Mahlzeit- und Fensterbeginn liegen"));
    }
    if end - start > Duration::minutes(MAX_MINUTES) {
        return Some(t("Zeitraum darf höchstens zehn Stunden lang sein"));
    }
    None
}

/// The meal as the stored day carries it.
#[derive(Clone, Debug, Deserialize)]
pub struct Meal {
    pub id: String,
    pub at: Instant,
    #[serde(default, deserialize_with = "crate::model::opt_text")]
    pub source_id: Option<String>,
    #[serde(default, deserialize_with = "crate::model::amount")]
    pub carbs: Decimal,
    #[serde(default, deserialize_with = "lenient")]
    pub window: Handles,
}

/// The instants the stored window's handles snap to.
#[derive(Clone, Debug, Default, Deserialize)]
pub struct Handles {
    #[serde(default, deserialize_with = "lenient")]
    pub auto_start: Option<Instant>,
    #[serde(default, deserialize_with = "lenient")]
    pub auto_end: Option<Instant>,
    #[serde(default, deserialize_with = "lenient")]
    pub start: Option<Instant>,
    #[serde(default, deserialize_with = "lenient")]
    pub end: Option<Instant>,
}

/// The CGM value at an instant with its colour and trend, as the editor draws it.
#[derive(Clone, Debug, Serialize)]
pub struct EditorReading {
    #[serde(serialize_with = "py_iso")]
    pub t: Instant,
    pub value: Option<Decimal>,
    pub tone: &'static str,
    pub trend: Option<&'static str>,
}

fn reading(entries: &[Reading], interpolator: &Interpolator, at: Instant, settings: &Settings) -> EditorReading {
    let Ok(point) = interpolator.at(at) else {
        return EditorReading { t: at, value: None, tone: "none", trend: None };
    };
    let value = point.value;
    let mut course: Vec<(Instant, Decimal)> = entries.iter()
        .filter(|row| at - Duration::minutes(16) <= row.at && row.at < at)
        .filter_map(|row| row.sgv.map(|sgv| (row.at, sgv))).collect();
    course.push((at, value));
    // Decimal.quantize(Decimal("0.1")): always one decimal.
    let mut shown = value.round_dp_with_strategy(1, RoundingStrategy::MidpointNearestEven);
    shown.rescale(1);
    EditorReading { t: at, value: Some(shown), tone: tone(Some(value), at, settings), trend: trend(&course).and_then(|t| t.trend_direction) }
}

fn sorted(entries: &[Reading]) -> Vec<Reading> {
    let mut entries = entries.to_vec();
    entries.sort_by_key(|row| row.at);
    entries
}

/// Readings for every minute the handles can reach, plus the instants they snap to (window ends, entries,
/// a sport session's end).
#[derive(Clone, Debug, Serialize)]
pub struct Editor {
    pub meal_id: String,
    pub limits: Limits,
    pub readings: Vec<EditorReading>,
}

pub fn editor(meal: &Meal, entries: &[Reading], treatments: &[Treatment], settings: &Settings) -> Editor {
    let entries = sorted(entries);
    let interpolator = Interpolator::new(&entries, MAX_GAP_MINUTES);
    let lead = meal.at - Duration::minutes(VIEW_LEAD_MINUTES);
    let start = lead.with_second(0).and_then(|at| at.with_nanosecond(0)).unwrap_or(lead);
    let end = meal.at + Duration::minutes(VIEW_LAG_MINUTES);
    let mut moments = BTreeSet::new();
    let mut at = start;
    while at <= end {
        moments.insert(at);
        at += Duration::minutes(1);
    }
    let handles = &meal.window;
    moments.extend([handles.auto_start, handles.auto_end, handles.start, handles.end].into_iter().flatten());
    for item in treatments.iter().filter(|item| start <= item.at && item.at <= end) {
        moments.insert(item.at);
        if item.event_type == "Exercise" {
            moments.insert(item.at + Duration::minutes(item.duration.trunc().to_i64().unwrap_or(0)));
        }
    }
    Editor { meal_id: meal.id.clone(), limits: limits(),
             readings: moments.into_iter().map(|moment| reading(&entries, &interpolator, moment, settings)).collect() }
}

/// What a candidate window counts.
#[derive(Clone, Debug, Serialize)]
pub struct WindowPreview {
    pub start: EditorReading,
    pub end: EditorReading,
    pub delta_mgdl: Option<Decimal>,
    pub minutes: i64,
    pub carbs_g: Decimal,
    pub rapid_units: Decimal,
    pub missing_glucose: bool,
    pub problem: Option<String>,
}

/// What a candidate window counts, with the same half-open rule and full doses as the factor.
pub fn preview(meal: &Meal, entries: &[Reading], treatments: &[Treatment], settings: &Settings, start: Instant, end: Instant) -> WindowPreview {
    let entries = sorted(entries);
    let interpolator = Interpolator::new(&entries, MAX_GAP_MINUTES);
    let episode = Episode { at: meal.at, source_id: meal.source_id.clone(), carbs: meal.carbs };
    let (carbs, insulin) = window_totals(&episode, treatments, start, end);
    let first = reading(&entries, &interpolator, start, settings);
    let last = reading(&entries, &interpolator, end, settings);
    WindowPreview {
        delta_mgdl: first.value.zip(last.value).map(|(a, b)| b - a),
        missing_glucose: first.value.is_none() || last.value.is_none(),
        start: first,
        end: last,
        minutes: (end - start).num_microseconds().unwrap_or(0) / 60_000_000,
        carbs_g: carbs,
        rapid_units: insulin,
        problem: problem(meal.at, start, end),
    }
}
