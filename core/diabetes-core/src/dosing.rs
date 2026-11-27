//! Carbohydrate and current-glucose arithmetic of the dose help (app/dosing.py).

use crate::calculation::{isf_at, schedule_value_at, ScheduleKey};
use crate::context::t;
use crate::glucose::{self, Trend};
use crate::model::{Instant, Reading, Settings, Treatment};
use crate::on_board::FORECAST_SPREAD_MGDL;
use crate::{value_error, Res};
use chrono::Duration;
use rust_decimal::{Decimal, RoundingStrategy};
use rust_decimal_macros::dec;
use serde::{Deserialize, Serialize};

/// Round to the next injectable 0.5 IE step (halves up).
pub fn half_units(units: Decimal) -> Decimal {
    (units * dec!(2)).round_dp_with_strategy(0, RoundingStrategy::MidpointAwayFromZero) / dec!(2)
}

#[derive(Clone, Debug, Deserialize)]
pub struct Finger {
    pub at: Instant,
    #[serde(deserialize_with = "crate::model::amount")]
    pub mgdl: Decimal,
}

/// Inputs of `suggest` besides glucose and treatments.
#[derive(Clone, Debug, Default)]
pub struct DoseOptions {
    /// Signed sport change in mg/dL (negative lowers glucose and the dose).
    pub activity_change: Decimal,
    /// Replaces Nightscout's I:C, e.g. the breakfast factor of the day type.
    pub units_per_ke: Option<Decimal>,
    /// The Automatik factor.
    pub auto_units_per_ke: Option<Decimal>,
    /// The meal surcharge (surcharge method) and the share these carbs add to it.
    pub auto_surcharge_mgdl: Option<Decimal>,
    pub surcharge_share: Option<Decimal>,
    /// What still acts ("Wirkt noch"): lowering and raising mg/dL.
    pub on_board: Option<(Decimal, Decimal)>,
    pub finger: Vec<Finger>,
    /// An open split plan: the options only correct downwards.
    pub hold_rise: bool,
}

#[derive(Clone, Debug, Serialize)]
pub struct RecentDose {
    pub at: Instant,
    pub units: Decimal,
}

macro_rules! fields {
    ($name:ident { $($field:ident),* $(,)? }) => {
        #[derive(Clone, Debug, Default, Serialize)]
        pub struct $name {
            pub carbs_g: Decimal,
            pub glucose_mgdl: Option<Decimal>,
            pub glucose_at: Option<Instant>,
            pub glucose_fresh: bool,
            pub glucose_source: Option<&'static str>,
            #[serde(flatten)]
            pub trend: Trend,
            pub recent_rapid_doses: Vec<RecentDose>,
            pub activity_change_mgdl: Decimal,
            #[serde(skip_serializing_if = "Option::is_none")]
            pub reason: Option<String>,
            $(#[serde(skip_serializing_if = "Option::is_none")] pub $field: Option<Decimal>,)*
        }
    };
}

fields!(Suggestion {
    nightscout_units_per_ke, units_per_ke, carb_units, isf_mgdl_per_unit, activity_units, target_mgdl, cautious_target_mgdl,
    auto_units_per_ke, auto_surcharge_units, auto_carb_units,
    carbs_total_units, carbs_entry_units, carbs_expected_glucose_mgdl,
    auto_carbs_total_units, auto_carbs_entry_units, auto_carbs_expected_glucose_mgdl,
    forecast_mgdl, cautious_forecast_mgdl, correction_units, total_units, entry_units, expected_glucose_mgdl,
    cautious_correction_units, cautious_total_units, cautious_entry_units, cautious_expected_glucose_mgdl,
    auto_total_units, auto_entry_units, auto_expected_glucose_mgdl,
    auto_cautious_total_units, auto_cautious_entry_units, auto_cautious_expected_glucose_mgdl,
});

/// The dose suggestions for `carbs` g now: normal (to the target), cautious and carbs only, with
/// Nightscout's I:C (or the given factor) and the Automatik factor. Model arithmetic, not advice.
pub fn suggest(carbs: Decimal, settings: &Settings, entries: &[Reading], treatments: &[Treatment], now: Instant,
               options: &DoseOptions) -> Res<Suggestion> {
    let grams = carbs;
    if !(Decimal::ZERO..=dec!(500)).contains(&grams) {
        return value_error(t("Kohlenhydrate müssen zwischen 0 und 500 g liegen"));
    }
    let activity = options.activity_change;
    if !(dec!(-500)..=dec!(500)).contains(&activity) {
        return value_error(t("Sporteinfluss muss zwischen -500 und 500 mg/dL liegen"));
    }
    let mut points: Vec<(Instant, Decimal)> = entries.iter().filter_map(|e| e.sgv.filter(|_| e.at <= now).map(|v| (e.at, v))).collect();
    points.sort();
    let mut latest = points.last().copied();
    let mut checks: Vec<(Instant, Decimal)> = options.finger.iter().filter(|f| f.at <= now).map(|f| (f.at, f.mgdl)).collect();
    checks.sort();
    let by_hand = checks.last().is_some_and(|check| latest.is_none_or(|l| check.0 > l.0));
    if by_hand {
        latest = checks.last().copied();
    }
    let fresh = latest.is_some_and(|l| now - l.0 <= Duration::minutes(15));
    let mut result = Suggestion {
        carbs_g: grams, glucose_mgdl: latest.map(|l| l.1), glucose_at: latest.map(|l| l.0), glucose_fresh: fresh,
        glucose_source: latest.map(|_| if by_hand { "finger" } else { "cgm" }),
        trend: if by_hand { None } else { glucose::trend(&points) }.unwrap_or_default(),
        recent_rapid_doses: treatments.iter()
            .filter(|item| now - Duration::hours(3) <= item.at && item.at <= now && item.has_rapid_dose())
            .map(|item| RecentDose { at: item.at, units: item.insulin }).collect(),
        activity_change_mgdl: activity,
        ..Default::default()
    };
    let profile = schedule_value_at(now, &settings.ic_schedule, &settings.timezone, ScheduleKey::GramsPerUnit)
        .and_then(|ratio| isf_at(now, &settings.isf_schedule, &settings.timezone).map(|isf| (ratio, isf)));
    let Ok((ratio, isf)) = profile else {
        result.reason = Some(t("I:C- oder Korrekturfaktor-Profil aus Nightscout fehlt"));
        return Ok(result);
    };
    result.nightscout_units_per_ke = Some(dec!(10) / ratio);
    let carb_units = match options.units_per_ke { None => grams / ratio, Some(factor) => grams * factor / dec!(10) };
    result.units_per_ke = Some(options.units_per_ke.unwrap_or(dec!(10) / ratio));
    result.carb_units = Some(carb_units);
    result.isf_mgdl_per_unit = Some(isf);
    result.activity_units = Some(activity / isf);
    let target = settings.dose_target_mgdl;
    let cautious_target = settings.dose_cautious_target_mgdl;
    result.target_mgdl = Some(target);
    result.cautious_target_mgdl = Some(cautious_target);
    let mut auto_carb_units = options.auto_units_per_ke.map(|factor| grams * factor / dec!(10));
    if let Some(units) = auto_carb_units.as_mut() {
        result.auto_units_per_ke = options.auto_units_per_ke;
        if let (Some(surcharge), true) = (options.auto_surcharge_mgdl, grams > Decimal::ZERO) {
            let surcharge_units = surcharge * options.surcharge_share.unwrap_or(Decimal::ZERO) / isf;
            result.auto_surcharge_units = Some(surcharge_units);
            *units += surcharge_units;
        }
        result.auto_carb_units = Some(*units);
    }
    let (lowering, raising) = options.on_board.unwrap_or_default();
    let mut starts = None;
    if fresh {
        let glucose = latest.unwrap().1;
        let guarded = glucose + activity - (lowering - raising).max(Decimal::ZERO);
        let full = glucose + activity - lowering + raising;
        // Cautious starts from the lower edge of the forecast's typical miss.
        let mut cautious_start = guarded.max(full - FORECAST_SPREAD_MGDL);
        // The rise an open second part covers is left to it.
        let dose_start = if options.hold_rise { full.min(target) } else { full };
        if options.hold_rise {
            cautious_start = cautious_start.min(cautious_target);
        }
        starts = Some((full, dose_start, cautious_start));
    }
    for (auto, units) in [(false, Some(carb_units)), (true, auto_carb_units)] {
        let Some(units) = units else { continue };
        let total = (units + activity / isf).max(Decimal::ZERO);
        let expected = starts.map(|(_, dose_start, _)| dose_start + (units - half_units(total)) * isf);
        if auto {
            (result.auto_carbs_total_units, result.auto_carbs_entry_units, result.auto_carbs_expected_glucose_mgdl) =
                (Some(total), Some(half_units(total)), expected);
        } else {
            (result.carbs_total_units, result.carbs_entry_units, result.carbs_expected_glucose_mgdl) =
                (Some(total), Some(half_units(total)), expected);
        }
    }
    let Some((start, dose_start, cautious_start)) = starts else {
        result.reason = Some("Kein aktueller CGM-Wert innerhalb von 15 Minuten. Ein blutig gemessener Wert kann ihn ersetzen.".into());
        return Ok(result);
    };
    result.forecast_mgdl = Some(start);
    result.cautious_forecast_mgdl = Some(cautious_start);
    let correction = (dose_start - target) / isf;
    let cautious_correction = (cautious_start - cautious_target) / isf;
    let total = (carb_units + correction).max(Decimal::ZERO);
    let cautious_total = (carb_units + cautious_correction).max(Decimal::ZERO);
    let (entry, cautious_entry) = (half_units(total), half_units(cautious_total));
    result.correction_units = Some(correction);
    result.total_units = Some(total);
    result.entry_units = Some(entry);
    result.expected_glucose_mgdl = Some(dose_start + (carb_units - entry) * isf);
    result.cautious_correction_units = Some(cautious_correction);
    result.cautious_total_units = Some(cautious_total);
    result.cautious_entry_units = Some(cautious_entry);
    result.cautious_expected_glucose_mgdl = Some(cautious_start + (carb_units - cautious_entry) * isf);
    if let Some(auto_units) = auto_carb_units {
        let auto_total = (auto_units + correction).max(Decimal::ZERO);
        let auto_entry = half_units(auto_total);
        result.auto_total_units = Some(auto_total);
        result.auto_entry_units = Some(auto_entry);
        // Expected glucose follows the auto factor's own carb model.
        result.auto_expected_glucose_mgdl = Some(dose_start + (auto_units - auto_entry) * isf);
        let auto_cautious_total = (auto_units + cautious_correction).max(Decimal::ZERO);
        let auto_cautious_entry = half_units(auto_cautious_total);
        result.auto_cautious_total_units = Some(auto_cautious_total);
        result.auto_cautious_entry_units = Some(auto_cautious_entry);
        result.auto_cautious_expected_glucose_mgdl = Some(cautious_start + (auto_units - auto_cautious_entry) * isf);
    }
    Ok(result)
}
