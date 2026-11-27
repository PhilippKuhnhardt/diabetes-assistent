//! What still acts on glucose right now (app/on_board.py): rapid insulin, carbs in one shared
//! stomach, the meal surcharge still to come and sport sessions. A rough forecast model, not
//! physiological facts or dosing advice.

use crate::calculation::{insulin_remaining, isf_at, minutes_between, Interpolator};
use crate::forecast::surcharge_share;
use crate::model::{decimal_of, Instant, Reading, Settings, Treatment};
use chrono::Duration;
use rust_decimal::prelude::{FromPrimitive, ToPrimitive};
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};

/// Every carb entry reaches the stomach this many minutes after it is entered.
pub const STOMACH_DELAY: i64 = 15;
/// The stomach passes on STOMACH_RATE × S / (S + STOMACH_HALF_GRAMS) g/min for S g inside, shared by all entries.
pub const STOMACH_RATE: f64 = 1.0;
pub const STOMACH_HALF_GRAMS: f64 = 40.0;
pub const CARBS_LOOKBACK: Decimal = dec!(720);
pub const NEGLIGIBLE_GRAMS: Decimal = dec!(0.5);
pub const SURCHARGE_MINUTES: Decimal = dec!(300);
pub const SURCHARGE_MINUTES_FAT: Decimal = dec!(360);
pub const SPORT_MINUTES: Decimal = dec!(300);
pub const SPORT_AHEAD_HOURS: i64 = 6;
/// Typical forecast miss after a post-meal correction.
pub const FORECAST_SPREAD_MGDL: Decimal = dec!(60);
/// Likely range of the expected value by how much still acts: (from, below, above).
pub const FORECAST_BAND: [(Decimal, Decimal, Decimal); 3] =
    [(dec!(150), dec!(75), dec!(90)), (dec!(40), dec!(50), dec!(60)), (dec!(0), dec!(30), dec!(30))];
pub const FRESH_MEAL_MINUTES: Decimal = dec!(90);
pub const NEGLIGIBLE_MGDL: Decimal = dec!(5);
pub const STEP_MINUTES: i64 = 5;
pub const HORIZON_HOURS: i64 = 8;

/// Absorbed share of each (time, grams) entry, minute by minute from the first entry's time.
pub fn stomach(entries: &[(Instant, Decimal)]) -> Vec<Vec<f64>> {
    if entries.is_empty() {
        return Vec::new();
    }
    let first = entries.iter().map(|(at, _)| *at).min().unwrap();
    let starts: Vec<i64> = entries.iter().map(|(at, _)| (*at - first).num_minutes() + STOMACH_DELAY).collect();
    let grams: Vec<f64> = entries.iter().map(|(_, amount)| amount.to_f64().unwrap_or(0.0)).collect();
    let (mut inside, mut taken) = (vec![0.0; entries.len()], vec![0.0; entries.len()]);
    let mut curves: Vec<Vec<f64>> = vec![Vec::new(); entries.len()];
    let last = *starts.iter().max().unwrap();
    let mut minute = 0;
    loop {
        for (index, begin) in starts.iter().enumerate() {
            if *begin == minute {
                inside[index] += grams[index];
            }
        }
        let total: f64 = inside.iter().sum();
        if total > 1e-9 {
            let share = total.min(STOMACH_RATE * total / (total + STOMACH_HALF_GRAMS)) / total;
            for index in 0..entries.len() {
                let part = inside[index] * share;
                inside[index] -= part;
                taken[index] += part;
            }
        }
        for index in 0..entries.len() {
            curves[index].push(if grams[index] > 0.0 { taken[index] / grams[index] } else { 1.0 });
        }
        minute += 1;
        if minute > last && (inside.iter().sum::<f64>() < 0.01 || minute > last + 24 * 60) {
            return curves;
        }
    }
}

/// A carb entry with its absorption curve from the stomach it shares with the others.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CarbRow {
    pub at: Instant,
    #[serde(deserialize_with = "crate::model::amount")]
    pub grams: Decimal,
    /// Absorbed share minute by minute from `origin`; empty = digests alone.
    #[serde(default)]
    pub absorbed: Vec<f64>,
    #[serde(default)]
    pub origin: Option<Instant>,
    #[serde(default, skip_serializing_if = "Option::is_none", deserialize_with = "crate::model::opt_amount")]
    pub mgdl_per_gram: Option<Decimal>,
}

impl CarbRow {
    /// Share not yet absorbed at `moment`.
    pub fn left(&self, moment: Instant) -> Decimal {
        if moment <= self.at {
            return Decimal::ONE;
        }
        if self.absorbed.is_empty() {
            let mut alone = self.clone();
            attach_stomach(std::slice::from_mut(&mut alone));
            return alone.left(moment);
        }
        let position = (moment - self.origin.unwrap_or(self.at)).num_microseconds().unwrap_or(0) as f64 / 60e6;
        let index = position as usize;
        let curve = &self.absorbed;
        let done = if index + 1 >= curve.len() {
            curve[curve.len() - 1]
        } else {
            curve[index] + (curve[index + 1] - curve[index]) * (position - index as f64)
        };
        (Decimal::ONE - Decimal::from_f64(done).unwrap_or(Decimal::ONE).round_dp(6)).max(Decimal::ZERO)
    }
}

/// Curves for rows sharing one stomach.
pub fn attach_stomach(rows: &mut [CarbRow]) {
    if rows.is_empty() {
        return;
    }
    let first = rows.iter().map(|row| row.at).min().unwrap();
    let curves = stomach(&rows.iter().map(|row| (row.at, row.grams)).collect::<Vec<_>>());
    for (row, curve) in rows.iter_mut().zip(curves) {
        row.absorbed = curve;
        row.origin = Some(first);
    }
}

fn carb_row(at: Instant, grams: Decimal) -> CarbRow {
    CarbRow { at, grams, absorbed: Vec::new(), origin: None, mgdl_per_gram: None }
}

/// Carbs still in the stomach (or on their way to it) at `now`: what a new entry joins.
pub fn stomach_grams(treatments: &[Treatment], now: Instant) -> Decimal {
    let mut rows: Vec<CarbRow> = treatments.iter()
        .filter(|item| !item.is_exercise() && item.at <= now && item.carbs > Decimal::ZERO
            && minutes_between(now, item.at) < CARBS_LOOKBACK)
        .map(|item| carb_row(item.at, item.carbs))
        .collect();
    attach_stomach(&mut rows);
    rows.iter().map(|row| row.grams * row.left(now)).sum()
}

/// Share of an entry's surcharge still to come: linear from the entry over `duration` minutes.
pub fn surcharge_remaining(minutes: Decimal, duration: Decimal) -> Decimal {
    if minutes <= Decimal::ZERO {
        return Decimal::ONE;
    }
    ((duration - minutes) / duration).max(Decimal::ZERO)
}

pub fn sport_remaining(minutes_after_end: Decimal, duration: Decimal) -> Decimal {
    if minutes_after_end <= Decimal::ZERO {
        return Decimal::ONE;
    }
    (Decimal::ONE - minutes_after_end / duration).max(Decimal::ZERO)
}

pub fn sport_end(item: &Treatment) -> Instant {
    item.at + Duration::minutes(item.duration.trunc().to_i64().unwrap_or(0))
}

/// Minutes after its end over which a session acts: the user's value for it, else its kind's setting.
pub fn sport_minutes(item: &Treatment, settings: &Settings, overrides: &HashMap<String, i64>) -> Decimal {
    if let Some(value) = overrides.get(&item.id) {
        return Decimal::from(*value);
    }
    item.sport_type.as_ref().and_then(|kind| settings.sport_durations_minutes.get(kind)).and_then(decimal_of)
        .map(|value| value.trunc()).unwrap_or(SPORT_MINUTES)
}

/// A session's glucose drop in mg/dL (positive lowers): the user's value for it, else its kind's setting.
pub fn sport_effect(item: &Treatment, settings: &Settings, overrides: &HashMap<String, Decimal>) -> Decimal {
    if let Some(value) = overrides.get(&item.id) {
        return *value;
    }
    item.sport_type.as_ref().and_then(|kind| settings.sport_effects_mgdl.get(kind)).and_then(decimal_of).unwrap_or(Decimal::ZERO)
}

/// Share of a session's effect that acts between start and end.
pub fn sport_share(item: &Treatment, start: Instant, end: Instant, duration: Decimal) -> Decimal {
    let finish = sport_end(item);
    sport_remaining(minutes_between(start, finish), duration) - sport_remaining(minutes_between(end, finish), duration)
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct DoseRow {
    pub at: Instant,
    #[serde(deserialize_with = "crate::model::amount")]
    pub units: Decimal,
    #[serde(deserialize_with = "crate::model::amount")]
    pub mgdl_per_unit: Decimal,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SurchargeRow {
    pub at: Instant,
    #[serde(default)]
    pub fat: bool,
    #[serde(default = "default_surcharge_minutes", deserialize_with = "crate::model::amount")]
    pub minutes: Decimal,
    #[serde(deserialize_with = "crate::model::amount")]
    pub mgdl: Decimal,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SportRow {
    #[serde(default, deserialize_with = "crate::model::text")]
    pub id: String,
    pub at: Instant,
    pub end: Instant,
    pub kind: String,
    #[serde(deserialize_with = "crate::model::amount")]
    pub mgdl: Decimal,
    #[serde(deserialize_with = "crate::model::amount")]
    pub minutes: Decimal,
}

/// The doses, carbs, surcharges and sport sessions that may still act, with their full effect.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct Found {
    pub doses: Vec<DoseRow>,
    pub carbs: Vec<CarbRow>,
    pub surcharges: Vec<SurchargeRow>,
    pub fresh_meals: Vec<Instant>,
    pub sports: Vec<SportRow>,
}

/// Inputs of `sources` besides the treatments.
pub struct SourceOptions<'a> {
    /// IE/KE that values a carb entry made at an instant.
    pub carb_factor: &'a dyn Fn(Instant) -> Option<Decimal>,
    /// The meal surcharge (surcharge method only).
    pub surcharge_mgdl: Option<Decimal>,
    pub fat_surcharge_mgdl: Decimal,
    /// Keep everything up to now however old (a whole-day timeline).
    pub keep_all: bool,
    pub sport_overrides: HashMap<String, Decimal>,
    pub sport_minutes_overrides: HashMap<String, i64>,
    /// The place factor of an entry (None = 1), for its carbs and surcharge.
    pub scale: Option<&'a dyn Fn(&Treatment) -> Option<Decimal>>,
}

fn key_of(item: &Treatment) -> String {
    if item.id.is_empty() { format!("{}|{}|{}", item.at.timestamp_micros(), item.insulin, item.carbs) } else { item.id.clone() }
}

/// The surcharge of each carb entry: the share its carbs add to what is still in the stomach,
/// share(in stomach + carbs) − share(in stomach), of the full surcharge (with the fat surcharge or
/// the entry's calculated fat/protein surcharge), acting linearly from the entry.
fn entry_surcharges(rows: &[(CarbRow, &Treatment)], now: Instant, options: &SourceOptions, surcharge: Decimal) -> Vec<SurchargeRow> {
    let mut result = Vec::new();
    for (index, (row, item)) in rows.iter().enumerate() {
        let before: Decimal = rows[..index].iter().map(|(other, _)| other.grams * other.left(row.at)).sum();
        let fat = item.fat_protein;
        let (full, duration) = match &item.surcharge {
            Some(own) => (own.mgdl, Decimal::from(own.minutes)),
            None => (surcharge + if fat { options.fat_surcharge_mgdl } else { Decimal::ZERO },
                     if fat { SURCHARGE_MINUTES_FAT } else { SURCHARGE_MINUTES }),
        };
        let part = full * (surcharge_share(before + row.grams) - surcharge_share(before));
        let place = options.scale.and_then(|scale| scale(item)).unwrap_or(Decimal::ONE);
        let age = if options.keep_all { Decimal::ZERO } else { minutes_between(now, row.at) };
        if part > Decimal::ZERO && age < duration {
            result.push(SurchargeRow { at: row.at, fat, minutes: duration, mgdl: part * place });
        }
    }
    result
}

fn default_surcharge_minutes() -> Decimal {
    SURCHARGE_MINUTES
}

/// Everything that may still act at `now`.
pub fn sources(treatments: &[Treatment], now: Instant, settings: &Settings, options: &SourceOptions) -> Found {
    let isf = |at: Instant| isf_at(at, &settings.isf_schedule, &settings.timezone).unwrap_or(Decimal::ZERO);
    let mut ordered: Vec<&Treatment> = treatments.iter().collect();
    ordered.sort_by_key(|item| item.at);
    let mut seen = HashSet::new();
    let mut found = Found::default();
    let mut stomach_rows: Vec<(CarbRow, &Treatment)> = Vec::new();
    let insulin_total = crate::insulin::active().total_minutes();
    for item in ordered {
        let key = key_of(item);
        if seen.contains(&key) {
            continue;
        }
        if item.is_exercise() && item.sport_type.is_some() {
            seen.insert(key);
            let effect = sport_effect(item, settings, &options.sport_overrides);
            let duration = sport_minutes(item, settings, &options.sport_minutes_overrides);
            let ahead = if options.keep_all { Duration::zero() } else { Duration::hours(SPORT_AHEAD_HOURS) };
            if !effect.is_zero() && item.at <= now + ahead
                && (options.keep_all || minutes_between(now, sport_end(item)) < duration) {
                found.sports.push(SportRow { id: item.id.clone(), at: item.at, end: sport_end(item),
                                             kind: item.sport_type.clone().unwrap(), mgdl: effect, minutes: duration });
            }
            continue;
        }
        if item.at > now {
            continue;
        }
        seen.insert(key);
        let age = if options.keep_all { Decimal::ZERO } else { minutes_between(now, item.at) };
        if item.has_rapid_dose() && age < insulin_total {
            found.doses.push(DoseRow { at: item.at, units: item.insulin, mgdl_per_unit: isf(item.at) });
        }
        if item.carbs > Decimal::ZERO && (options.keep_all || minutes_between(now, item.at) < CARBS_LOOKBACK) {
            stomach_rows.push((carb_row(item.at, item.carbs), item));
        }
        if item.is_main_meal() && age < FRESH_MEAL_MINUTES {
            found.fresh_meals.push(item.at);
        }
    }
    // All carb entries share one stomach; the curves count every entry, also one without a factor.
    let mut rows: Vec<CarbRow> = stomach_rows.iter().map(|(row, _)| row.clone()).collect();
    attach_stomach(&mut rows);
    for ((row, _), curve) in stomach_rows.iter_mut().zip(rows) {
        *row = curve;
    }
    for (row, item) in &stomach_rows {
        if !options.keep_all && row.grams * row.left(now) < NEGLIGIBLE_GRAMS {
            continue;
        }
        if let Some(factor) = (options.carb_factor)(item.at) {
            let place = options.scale.and_then(|scale| scale(item)).unwrap_or(Decimal::ONE);
            found.carbs.push(CarbRow { mgdl_per_gram: Some(factor * place * isf(item.at) / dec!(10)), ..row.clone() });
        }
    }
    if let Some(surcharge) = options.surcharge_mgdl {
        found.surcharges = entry_surcharges(&stomach_rows, now, options, surcharge);
    }
    found
}

/// Effect still to come at a moment (mg/dL, positive) and the active insulin in IE.
#[derive(Clone, Debug, Default, Serialize)]
pub struct Effects {
    pub insulin_units: Decimal,
    pub lowering_mgdl: Decimal,
    pub carbs_mgdl: Decimal,
    pub surcharge_mgdl: Decimal,
    /// Signed: positive lowers glucose.
    pub sport_mgdl: Decimal,
    pub raising_mgdl: Decimal,
}

/// Effects at `moment` of the entries up to `cutoff` (default `moment`); sport counts whatever the cutoff.
pub fn effects_at(found: &Found, moment: Instant, cutoff: Option<Instant>) -> Effects {
    let cutoff = cutoff.unwrap_or(moment);
    let doses = found.doses.iter().filter(|row| row.at <= cutoff);
    let mut units = Decimal::ZERO;
    let mut lowering = Decimal::ZERO;
    for dose in doses {
        let left = dose.units * insulin_remaining(minutes_between(moment, dose.at));
        units += left;
        lowering += left * dose.mgdl_per_unit;
    }
    let carbs: Decimal = found.carbs.iter().filter(|row| row.at <= cutoff)
        .map(|row| row.grams * row.left(moment) * row.mgdl_per_gram.unwrap_or(Decimal::ZERO)).sum();
    let surcharge: Decimal = found.surcharges.iter().filter(|row| row.at <= cutoff)
        .map(|row| row.mgdl * surcharge_remaining(minutes_between(moment, row.at), row.minutes)).sum();
    let sport: Decimal = found.sports.iter()
        .map(|row| row.mgdl * sport_remaining(minutes_between(moment, row.end), row.minutes)).sum();
    Effects {
        insulin_units: units, lowering_mgdl: lowering + sport.max(Decimal::ZERO), carbs_mgdl: carbs,
        surcharge_mgdl: surcharge, sport_mgdl: sport, raising_mgdl: carbs + surcharge + (-sport).max(Decimal::ZERO),
    }
}

/// How far below and above the expected value glucose likely ends (8 of 10 cases).
pub fn forecast_band(lowering: Decimal, raising: Decimal) -> (Decimal, Decimal) {
    let acting = lowering + raising;
    FORECAST_BAND.iter().find(|(start, _, _)| acting >= *start)
        .map(|(_, below, above)| (*below, *above)).unwrap_or((FORECAST_BAND[2].1, FORECAST_BAND[2].2))
}

/// First 5-minute step from now at which less than NEGLIGIBLE_MGDL of one side is left.
fn until(found: &Found, now: Instant, side: fn(&Effects) -> Decimal) -> Option<Instant> {
    if side(&effects_at(found, now, None)) < NEGLIGIBLE_MGDL {
        return None;
    }
    let mut moment = now;
    while moment < now + Duration::hours(HORIZON_HOURS) {
        moment += Duration::minutes(STEP_MINUTES);
        if side(&effects_at(found, moment, None)) < NEGLIGIBLE_MGDL {
            return Some(moment);
        }
    }
    Some(moment)
}

#[derive(Clone, Debug, Serialize)]
pub struct DoseLeft {
    pub at: Instant,
    pub units: Decimal,
    pub remaining_units: Decimal,
}

#[derive(Clone, Debug, Serialize)]
pub struct CarbsLeft {
    pub at: Instant,
    pub grams: Decimal,
    pub remaining_grams: Decimal,
}

#[derive(Clone, Debug, Serialize)]
pub struct SportLeft {
    #[serde(flatten)]
    pub session: SportRow,
    pub remaining_mgdl: Decimal,
}

/// The "Wirkt noch" summary: current effects plus when each side has finished acting.
#[derive(Clone, Debug, Serialize)]
pub struct Summary {
    #[serde(flatten)]
    pub effects: Effects,
    pub lowering_until: Option<Instant>,
    pub raising_until: Option<Instant>,
    pub settled_at: Option<Instant>,
    pub fresh_meal_at: Option<Instant>,
    pub doses: Vec<DoseLeft>,
    pub carbs: Vec<CarbsLeft>,
    pub sports: Vec<SportLeft>,
}

pub fn summary(found: &Found, now: Instant) -> Summary {
    let lowering_until = until(found, now, |e| e.lowering_mgdl);
    let raising_until = until(found, now, |e| e.raising_mgdl);
    Summary {
        effects: effects_at(found, now, None), lowering_until, raising_until,
        settled_at: lowering_until.max(raising_until),
        fresh_meal_at: found.fresh_meals.iter().max().copied(),
        doses: found.doses.iter().map(|dose| DoseLeft { at: dose.at, units: dose.units,
            remaining_units: dose.units * insulin_remaining(minutes_between(now, dose.at)) }).collect(),
        carbs: found.carbs.iter().map(|row| CarbsLeft { at: row.at, grams: row.grams, remaining_grams: row.grams * row.left(now) }).collect(),
        sports: found.sports.iter().map(|row| SportLeft { session: row.clone(),
            remaining_mgdl: row.mgdl * sport_remaining(minutes_between(now, row.end), row.minutes) }).collect(),
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct TimelineRow {
    pub at: Instant,
    pub glucose_mgdl: Option<Decimal>,
    pub model_mgdl: Option<Decimal>,
    pub lowering_per_hour: Decimal,
    pub raising_per_hour: Decimal,
    pub insulin_units: Decimal,
}

/// Every step from start to end: how fast insulin lowers and carbs plus surcharge raise glucose,
/// the measured glucose and the model's value from the glucose `horizon` earlier.
pub fn timeline(found: &Found, entries: &[Reading], start: Instant, end: Instant, horizon: Duration) -> Vec<TimelineRow> {
    let glucose = Interpolator::new(entries, 15);
    let step = Duration::minutes(STEP_MINUTES);
    let per_hour = Decimal::from(60 / STEP_MINUTES);
    let mut rows = Vec::new();
    let mut moment = start;
    while moment <= end {
        let now = effects_at(found, moment, None);
        let after = effects_at(found, moment + step, None);
        let step_start = effects_at(found, moment, Some(moment + step));
        let before = effects_at(found, moment - horizon, Some(moment));
        let model = glucose.value(moment - horizon).map(|earlier| earlier - (before.lowering_mgdl - now.lowering_mgdl)
            + (before.raising_mgdl - now.raising_mgdl));
        rows.push(TimelineRow {
            at: moment, glucose_mgdl: glucose.value(moment), model_mgdl: model,
            lowering_per_hour: (step_start.lowering_mgdl - after.lowering_mgdl) * per_hour,
            raising_per_hour: (step_start.raising_mgdl - after.raising_mgdl) * per_hour,
            insulin_units: now.insulin_units,
        });
        moment += step;
    }
    rows
}
