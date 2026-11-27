//! Deterministic, auditable meal-endpoint calculation (app/calculation.py).
//!
//! A mathematical model, not an insulin dosing algorithm: complete carb absorption and full dose
//! effect at the endpoint are explicit assumptions.

use crate::context::{local_zone, t, tr};
use crate::insulin;
use crate::model::{Instant, IsfRow, Reading, Settings, Treatment};
use crate::{value_error, Res};
use chrono::Duration;
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use serde::{Deserialize, Serialize};

/// Minutes from `earlier` to `later` (negative when later is before).
pub fn minutes_between(later: Instant, earlier: Instant) -> Decimal {
    let micros = (later - earlier).num_microseconds().unwrap_or(i64::MAX);
    Decimal::from(micros) / dec!(60000000)
}

pub fn insulin_remaining(minutes: Decimal) -> Decimal {
    insulin::active().remaining(minutes)
}

/// Value of a profile schedule at the local clock time of `moment`.
pub fn schedule_value_at(moment: Instant, schedule: &[IsfRow], timezone: &str, key: ScheduleKey) -> Res<Decimal> {
    let minute = local_zone(timezone).minute_of_day(&moment);
    let mut rows: Vec<&IsfRow> = schedule.iter().collect();
    rows.sort_by_key(|row| row.start_minute);
    let Some(last) = rows.last() else { return value_error(t("Profil-Zeitplan fehlt")) };
    let mut chosen = *last;
    for row in &rows {
        if row.start_minute <= minute {
            chosen = row;
        } else {
            break;
        }
    }
    let value = match key {
        ScheduleKey::MgdlPerUnit => chosen.mgdl_per_unit,
        ScheduleKey::GramsPerUnit => chosen.grams_per_unit,
    }
    .unwrap_or(Decimal::ZERO);
    if value <= Decimal::ZERO {
        return value_error(t("Profilwert muss positiv sein"));
    }
    Ok(value)
}

#[derive(Clone, Copy)]
pub enum ScheduleKey {
    MgdlPerUnit,
    GramsPerUnit,
}

/// Correction factor (mg/dL per IE) at `moment`.
pub fn isf_at(moment: Instant, schedule: &[IsfRow], timezone: &str) -> Res<Decimal> {
    schedule_value_at(moment, schedule, timezone, ScheduleKey::MgdlPerUnit)
}

#[derive(Clone, Debug, Serialize)]
pub struct GlucosePoint {
    pub value: Decimal,
    pub before: Instant,
    pub after: Instant,
}

/// Piecewise linear CGM interpolation over readings sorted once; no nearest-reading substitution.
pub struct Interpolator {
    points: Vec<(Instant, Decimal)>,
    max_gap: Decimal,
}

impl Interpolator {
    pub fn new(entries: &[Reading], max_gap_minutes: i64) -> Interpolator {
        let mut points: Vec<(Instant, Decimal)> = entries.iter().filter_map(|e| e.sgv.map(|v| (e.at, v))).collect();
        points.sort_by(|a, b| a.0.cmp(&b.0).then(a.1.cmp(&b.1)));
        Interpolator { points, max_gap: Decimal::from(max_gap_minutes) }
    }

    pub fn at(&self, moment: Instant) -> Res<GlucosePoint> {
        let index = self.points.partition_point(|(at, _)| *at < moment);
        if index < self.points.len() && self.points[index].0 == moment {
            return Ok(GlucosePoint { value: self.points[index].1, before: moment, after: moment });
        }
        if index == 0 || index == self.points.len() {
            return value_error(t("CGM-Werte um den Messzeitpunkt fehlen"));
        }
        let (before, after) = (self.points[index - 1], self.points[index]);
        let gap = minutes_between(after.0, before.0);
        if gap > self.max_gap {
            return value_error(t("CGM-Lücke am Messzeitpunkt ist zu groß"));
        }
        let fraction = minutes_between(moment, before.0) / gap;
        Ok(GlucosePoint { value: before.1 + (after.1 - before.1) * fraction, before: before.0, after: after.0 })
    }

    pub fn value(&self, moment: Instant) -> Option<Decimal> {
        self.at(moment).ok().map(|point| point.value)
    }
}

pub fn interpolate_glucose(entries: &[Reading], moment: Instant) -> Res<GlucosePoint> {
    Interpolator::new(entries, 15).at(moment)
}

/// The blood glucose measured by hand nearest to `moment`, within the gap.
pub fn finger_glucose(treatments: &[Treatment], moment: Instant, max_gap_minutes: i64) -> Option<GlucosePoint> {
    treatments.iter()
        .filter_map(|item| item.finger_mgdl.filter(|v| !v.is_zero()).map(|v| (minutes_between(item.at, moment).abs(), item, v)))
        .filter(|(gap, _, _)| *gap <= Decimal::from(max_gap_minutes))
        .min_by(|a, b| a.0.cmp(&b.0))
        .map(|(_, item, value)| GlucosePoint { value, before: item.at, after: item.at })
}

/// A rapid dose without carbs at most this long before a main meal whose own entry carries no rapid
/// insulin is its bolus (pre-bolus).
pub const PREBOLUS_LEAD_MINUTES: i64 = 30;

/// The meal an analysis window belongs to.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Episode {
    pub at: Instant,
    #[serde(default, deserialize_with = "crate::model::opt_text")]
    pub source_id: Option<String>,
    #[serde(default, deserialize_with = "crate::model::amount")]
    pub carbs: Decimal,
}

fn own_index(episode: &Episode, treatments: &[Treatment]) -> Option<usize> {
    let source = episode.source_id.as_deref()?;
    treatments.iter().position(|item| item.id == source)
}

fn is_own(episode: &Episode, item: &Treatment) -> bool {
    episode.source_id.as_deref().is_some_and(|source| item.id == source)
}

/// The earliest pre-bolus of the meal, or None.
pub fn prebolus_start(episode: &Episode, treatments: &[Treatment], entries: &[Reading], settings: &Settings) -> Option<Instant> {
    let meal_at = episode.at;
    let own = own_index(episode, treatments);
    if let Some(index) = own {
        if treatments[index].has_rapid_dose() {
            return None;
        }
    }
    let lead = Duration::minutes(PREBOLUS_LEAD_MINUTES);
    let first = treatments.iter().enumerate()
        .filter(|(index, item)| !item.is_exercise() && Some(*index) != own && meal_at - lead <= item.at && item.at < meal_at
            && item.has_rapid_dose() && item.carbs <= Decimal::ZERO)
        .map(|(_, item)| item.at)
        .min()?;
    if treatments.iter().enumerate().any(|(index, item)| !item.is_exercise() && Some(index) != own
        && first <= item.at && item.at < meal_at && item.carbs > Decimal::ZERO) {
        return None;
    }
    let previous = treatments.iter().enumerate()
        .filter(|(index, item)| item.is_main_meal() && item.at < first && Some(*index) != own)
        .map(|(_, item)| item)
        .fold(None::<&Treatment>, |best, item| if best.is_none_or(|b| item.at > b.at) { Some(item) } else { best });
    if let Some(previous) = previous {
        let span = automatic_window(
            &Episode { at: previous.at, source_id: Some(previous.id.clone()).filter(|id| !id.is_empty()), carbs: previous.carbs },
            treatments, entries, settings, false);
        if span.end > first {
            return None;
        }
    }
    Some(first)
}

#[derive(Clone, Debug, Serialize)]
pub struct Window {
    pub start: Instant,
    pub end: Instant,
    pub nominal_end: Instant,
    pub extended_end: Instant,
    pub next_main_meal_at: Option<Instant>,
}

/// Default meal window: from the meal (or its pre-bolus) for a fixed time, extended by doses,
/// bounded by the next main meal.
pub fn automatic_window(episode: &Episode, treatments: &[Treatment], entries: &[Reading], settings: &Settings,
                        prebolus: bool) -> Window {
    let meal_at = episode.at;
    let start = if prebolus { prebolus_start(episode, treatments, entries, settings) } else { None }.unwrap_or(meal_at);
    let window = Duration::minutes(settings.window_minutes);
    let nominal_end = meal_at + window;
    let mut end = nominal_end;
    let next_main_meal_at = treatments.iter()
        .filter(|item| !is_own(episode, item) && item.is_main_meal() && meal_at < item.at)
        .map(|item| item.at)
        .min();
    let mut ordered: Vec<&Treatment> = treatments.iter().collect();
    ordered.sort_by_key(|item| item.at);
    // Each rapid dose reached by the current window starts a full new window; a following main
    // meal bounds the doses that belong to this meal.
    for item in ordered {
        if item.at >= end {
            break;
        }
        if item.at >= start && next_main_meal_at.is_none_or(|next| item.at < next) && item.has_rapid_dose() {
            end = end.max(item.at + window);
        }
    }
    let extended_end = end;
    if let Some(next) = next_main_meal_at {
        if next < end && minutes_between(next, meal_at) > dec!(120) {
            let candidate = entries.iter()
                .filter(|item| item.sgv.is_some() && meal_at + Duration::minutes(120) <= item.at && item.at < next
                    && minutes_between(next, item.at) <= dec!(15))
                .map(|item| item.at)
                .max();
            if let Some(candidate) = candidate {
                end = candidate;
            }
        }
    }
    Window { start, end, nominal_end, extended_end, next_main_meal_at }
}

/// Carbs and rapid insulin a window counts: half-open [start, end), every dose in full, the
/// meal's own entry always.
pub fn window_totals(episode: &Episode, treatments: &[Treatment], start: Instant, end: Instant) -> (Decimal, Decimal) {
    let mut carbs = episode.carbs;
    let mut units = Decimal::ZERO;
    for item in treatments.iter().filter(|item| !item.is_exercise()) {
        let own = is_own(episode, item);
        let inside = start <= item.at && item.at < end;
        if inside && !own && item.carbs > Decimal::ZERO {
            carbs += item.carbs;
        }
        if item.insulin > Decimal::ZERO && (own || inside) && item.is_rapid() {
            units += item.insulin;
        }
    }
    (carbs, units)
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ManualWindow {
    pub start: Instant,
    pub end: Instant,
}

#[derive(Clone, Debug, Serialize)]
pub struct CarbEvent {
    pub id: Option<String>,
    pub at: Instant,
    pub carbs: Decimal,
    pub isf: Decimal,
    pub carbs_times_isf: Decimal,
    pub kind: &'static str,
}

#[derive(Clone, Debug, Serialize)]
pub struct Blocker {
    pub kind: &'static str,
    pub at: Instant,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub carbs: Option<Decimal>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub units: Option<Decimal>,
    pub message: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct DoseEffect {
    pub id: Option<String>,
    pub at: Instant,
    pub units: Decimal,
    pub role: String,
    pub event_type: String,
    pub isf: Decimal,
    pub effect_mgdl: Decimal,
}

#[derive(Clone, Debug, Serialize)]
pub struct PriorDose {
    pub id: Option<String>,
    pub at: Instant,
    pub units: Decimal,
    pub share: Decimal,
    pub isf: Decimal,
    pub effect_mgdl: Decimal,
}

/// One meal's factor with every input it used.
#[derive(Clone, Debug, Serialize)]
pub struct MealCalculation {
    pub algorithm: &'static str,
    pub start: Instant,
    pub end: Instant,
    pub nominal_end: Instant,
    pub window_minutes: i64,
    pub actual_window_minutes: Decimal,
    pub extended_end: Instant,
    pub end_reason: &'static str,
    pub manual_window: bool,
    pub auto_start: Instant,
    pub auto_end: Instant,
    pub prebolus: bool,
    pub next_main_meal_at: Option<Instant>,
    pub carbs: Decimal,
    pub carbs_total: Decimal,
    pub carbs_times_isf_total: Decimal,
    pub extra_carb_count: usize,
    pub g0: Decimal,
    pub g3: Decimal,
    pub g0_sources: [Instant; 2],
    pub g0_source: &'static str,
    pub g3_sources: [Instant; 2],
    pub delta_mgdl: Decimal,
    pub adjusted_delta_mgdl: Decimal,
    pub sport_effect_mgdl: Decimal,
    pub meal_isf: Decimal,
    pub rapid_units_in_window: Decimal,
    pub insulin_effect_mgdl: Decimal,
    pub prior_insulin_effect_mgdl: Decimal,
    pub prior_doses: Vec<PriorDose>,
    pub denominator_mgdl: Decimal,
    pub doses: Vec<DoseEffect>,
    pub carb_events: Vec<CarbEvent>,
    pub blockers: Vec<Blocker>,
    pub ratio_g_per_unit: Option<Decimal>,
    pub factor_units_per_ke: Option<Decimal>,
    pub reason: Option<String>,
    pub assumptions: Vec<String>,
}

/// Solve one endpoint balance for every carb and rapid dose in a meal window:
/// IE/KE = 10 × (ΔG + sport + Σ[IE × KF]) / Σ[g × KF].
pub fn calculate_episode(episode: &Episode, treatments: &[Treatment], entries: &[Reading], settings: &Settings,
                         sport_effect_mgdl: Decimal, manual_window: Option<&ManualWindow>) -> Res<MealCalculation> {
    let meal_at = episode.at;
    let automatic = automatic_window(episode, treatments, entries, settings, true);
    let (auto_start, auto_end) = (automatic.start, automatic.end);
    let (start, end) = match manual_window {
        Some(window) => {
            if !(meal_at < window.end && window.start < window.end) {
                return value_error(t("Manueller Zeitraum muss nach dem Mahlzeitbeginn enden"));
            }
            (window.start, window.end)
        }
        None => (auto_start, auto_end),
    };
    let glucose = Interpolator::new(entries, 15);
    let (g0, g0_source) = match glucose.at(start) {
        Ok(point) => (point, "cgm"),
        // Without CGM at the start (sensor failure or warm-up), a finger measurement close to it stands in.
        Err(error) => match finger_glucose(treatments, start, 15) {
            Some(point) => (point, "finger"),
            None => return Err(error),
        },
    };
    let g1 = glucose.at(end)?;
    let delta = g1.value - g0.value;
    let carbs = episode.carbs;
    if carbs <= Decimal::ZERO {
        return value_error(t("Kohlenhydrate müssen positiv sein"));
    }
    let zone = local_zone(&settings.timezone);
    let isf = |at: Instant| isf_at(at, &settings.isf_schedule, &settings.timezone);
    let meal_isf = isf(meal_at)?;
    let mut carb_events = vec![CarbEvent {
        id: episode.source_id.clone(), at: meal_at, carbs, isf: meal_isf, carbs_times_isf: carbs * meal_isf, kind: "meal",
    }];
    let mut blockers = Vec::new();
    let mut extra_carb_count = 0;
    for item in treatments.iter().filter(|item| !item.is_exercise() && !is_own(episode, item)) {
        if !(start <= item.at && item.at < end && item.carbs > Decimal::ZERO) {
            continue;
        }
        let kind = match item.event_type.as_str() {
            "Snack Bolus" => "snack",
            "Meal Bolus" => "meal",
            _ => "other",
        };
        let sensitivity = isf(item.at)?;
        extra_carb_count += 1;
        carb_events.push(CarbEvent {
            id: Some(item.id.clone()), at: item.at, carbs: item.carbs, isf: sensitivity,
            carbs_times_isf: item.carbs * sensitivity, kind,
        });
        if kind == "meal" {
            let time = zone.local(&item.at).format("%H:%M").to_string();
            blockers.push(Blocker {
                kind, at: item.at, carbs: Some(item.carbs), units: None,
                message: tr("{label} um {time} ({carbs} g KH) im Auswertungsfenster",
                            &[("label", t("Weitere Hauptmahlzeit")), ("time", time), ("carbs", item.carbs.normalize().to_string())]),
            });
        }
    }
    let mut doses = Vec::new();
    let mut insulin_effect = Decimal::ZERO;
    let mut rapid_units = Decimal::ZERO;
    for item in treatments.iter().filter(|item| !item.is_exercise()) {
        let own = is_own(episode, item);
        if item.insulin <= Decimal::ZERO || !(own || (start <= item.at && item.at < end)) {
            continue;
        }
        if !item.is_rapid() {
            let time = zone.local(&item.at).format("%H:%M").to_string();
            blockers.push(Blocker {
                kind: "unknown_insulin", at: item.at, carbs: None, units: Some(item.insulin),
                message: tr("Insulinereignis unbekannten Typs um {time} ({units} IE)",
                            &[("time", time), ("units", item.insulin.normalize().to_string())]),
            });
            continue;
        }
        let sensitivity = isf(item.at)?;
        let effect = item.insulin * sensitivity;
        rapid_units += item.insulin;
        insulin_effect += effect;
        doses.push(DoseEffect {
            id: Some(item.id.clone()), at: item.at, units: item.insulin,
            role: item.role.map(|role| role.to_string())
                .unwrap_or_else(|| if item.event_type.is_empty() { "Bolus".into() } else { item.event_type.clone() }),
            event_type: item.event_type.clone(), isf: sensitivity, effect_mgdl: effect,
        });
    }
    // Rapid insulin from before the window still lowers glucose inside it; the dose help subtracts it
    // as "Wirkt noch", so the factor counts the share that acts between start and end.
    let mut prior_doses = Vec::new();
    let mut prior_effect = Decimal::ZERO;
    for item in treatments.iter().filter(|item| !item.is_exercise()) {
        if item.insulin <= Decimal::ZERO || is_own(episode, item) || item.at >= start || !item.is_rapid() {
            continue;
        }
        let share = insulin_remaining(minutes_between(start, item.at)) - insulin_remaining(minutes_between(end, item.at));
        if share <= Decimal::ZERO {
            continue;
        }
        let sensitivity = isf(item.at)?;
        let effect = item.insulin * share * sensitivity;
        prior_effect += effect;
        prior_doses.push(PriorDose { id: Some(item.id.clone()), at: item.at, units: item.insulin, share, isf: sensitivity, effect_mgdl: effect });
    }
    insulin_effect += prior_effect;
    if !(dec!(-500)..=dec!(500)).contains(&sport_effect_mgdl) {
        return value_error(t("Ungültiger Sporteffekt"));
    }
    let adjusted_delta = delta + sport_effect_mgdl;
    let denominator = adjusted_delta + insulin_effect;
    let carbs_total: Decimal = carb_events.iter().map(|item| item.carbs).sum();
    let carbs_isf_total: Decimal = carb_events.iter().map(|item| item.carbs_times_isf).sum();
    let (ratio, factor, reason) = if !blockers.is_empty() {
        (None, None, Some(blockers.iter().map(|item| item.message.as_str()).collect::<Vec<_>>().join("; ")))
    } else if denominator <= Decimal::ZERO {
        (None, None, Some(t("Kein positiver endlicher Faktor für diese Eingaben")))
    } else {
        (Some(carbs_isf_total / denominator), Some(dec!(10) * denominator / carbs_isf_total), None)
    };
    let manual = manual_window.is_some();
    let prebolus = !manual && auto_start < meal_at;
    let mut assumptions: Vec<String> = [
        "alle KH im Fenster wirken bis zum Endpunkt vollständig",
        "volle IE jeder schnell wirksamen Injektion im Fenster",
        "frühere schnell wirksame Injektionen mit dem Anteil, der im Fenster wirkt",
        "alle KH im Fenster teilen einen Insulinfaktor",
        "kein zusätzlicher Hintergrundtrend",
        "bestätigter Sport- oder Rennsporteffekt (Senkung oder Anstieg) als additive Korrektur der Glukoseänderung",
    ].iter().map(|text| t(text)).collect();
    if manual {
        assumptions.push(t("manuell gewählter Zeitraum statt automatischem Endpunkt"));
    }
    if prebolus {
        assumptions.push(t("Beginn bei der Injektion vor dem Essen (Spritz-Ess-Abstand)"));
    }
    if g0_source == "finger" {
        assumptions.push(t("Startwert blutig gemessen, weil am Beginn kein CGM-Wert vorlag"));
    }
    Ok(MealCalculation {
        algorithm: "endpoint-full-dose-v2", start, end, nominal_end: automatic.nominal_end,
        window_minutes: settings.window_minutes, actual_window_minutes: minutes_between(end, start),
        extended_end: automatic.extended_end,
        end_reason: if manual { "manual" } else if end < automatic.extended_end { "before_next_meal" }
                    else if automatic.extended_end > automatic.nominal_end { "after_last_injection" } else { "fixed_window" },
        manual_window: manual, auto_start, auto_end, prebolus, next_main_meal_at: automatic.next_main_meal_at,
        carbs, carbs_total, carbs_times_isf_total: carbs_isf_total, extra_carb_count,
        g0: g0.value, g3: g1.value, g0_sources: [g0.before, g0.after], g0_source, g3_sources: [g1.before, g1.after],
        delta_mgdl: delta, adjusted_delta_mgdl: adjusted_delta, sport_effect_mgdl, meal_isf,
        rapid_units_in_window: rapid_units, insulin_effect_mgdl: insulin_effect, prior_insulin_effect_mgdl: prior_effect,
        prior_doses, denominator_mgdl: denominator, doses, carb_events, blockers,
        ratio_g_per_unit: ratio, factor_units_per_ke: factor, reason, assumptions,
    })
}
