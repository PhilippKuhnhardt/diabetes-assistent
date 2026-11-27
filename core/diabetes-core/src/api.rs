//! JSON entry points: `call("module.function", args)` with the reference's parameter names.
//!
//! This is the interface a native app binds (one function, JSON in and out) and the one the parity
//! check (core/parity) replays recorded Python calls against. Callables of the reference are passed
//! as data: `carb_factors` (IE/KE per carb entry time) and `scales` (place factor per treatment)
//! for on_board.sources, the computed `surcharge_share` for dosing.suggest.

use crate::calculation::{self, Episode, ManualWindow, ScheduleKey};
use crate::model::{decimal_of, Instant, IsfRow, Reading, Settings, Treatment};
use crate::{context, correction_check, correction_scale, dosing, episodes, forecast, fpe, glucose, glucose_alerts, on_board, split, stats};
use crate::{CoreError, Res};
use chrono::{DateTime, Duration, NaiveDate, Utc};
use rust_decimal::Decimal;
use serde::de::DeserializeOwned;
use serde::Serialize;
use serde_json::{json, Value};
use std::collections::HashMap;

struct Args<'a>(&'a Value);

impl Args<'_> {
    fn get<T: DeserializeOwned>(&self, name: &str) -> Res<T> {
        let value = self.0.get(name).cloned().unwrap_or(Value::Null);
        serde_json::from_value(value).map_err(|error| CoreError::Value(format!("argument {name}: {error}")))
    }

    fn opt<T: DeserializeOwned>(&self, name: &str) -> Res<Option<T>> {
        match self.0.get(name) {
            None | Some(Value::Null) => Ok(None),
            Some(_) => self.get(name).map(Some),
        }
    }

    fn dec(&self, name: &str) -> Option<Decimal> {
        self.0.get(name).and_then(decimal_of)
    }

    fn dec_or(&self, name: &str, default: Decimal) -> Decimal {
        self.dec(name).unwrap_or(default)
    }

    fn int_or(&self, name: &str, default: i64) -> i64 {
        self.dec(name).map(|v| v.trunc().try_into().unwrap_or(default)).unwrap_or(default)
    }

    fn bool(&self, name: &str) -> bool {
        self.0.get(name).and_then(Value::as_bool).unwrap_or(false)
    }

    fn str(&self, name: &str) -> String {
        self.0.get(name).and_then(Value::as_str).unwrap_or_default().to_string()
    }

    fn raw(&self, name: &str) -> Value {
        self.0.get(name).cloned().unwrap_or(Value::Null)
    }
}

fn out<T: Serialize>(value: T) -> Res<Value> {
    serde_json::to_value(value).map_err(|error| CoreError::Value(error.to_string()))
}

fn day(args: &Args, name: &str) -> Res<NaiveDate> {
    let text: String = args.get(name)?;
    NaiveDate::parse_from_str(&text[..10.min(text.len())], "%Y-%m-%d").map_err(|e| CoreError::Value(e.to_string()))
}

/// Callables of on_board.sources as data.
fn source_options<'a>(args: &Args, carb_factor: &'a dyn Fn(Instant) -> Option<Decimal>,
                      scale: Option<&'a dyn Fn(&Treatment) -> Option<Decimal>>) -> Res<on_board::SourceOptions<'a>> {
    Ok(on_board::SourceOptions {
        carb_factor, scale,
        surcharge_mgdl: args.dec("surcharge_mgdl"),
        fat_surcharge_mgdl: args.dec_or("fat_surcharge_mgdl", Decimal::ZERO),
        keep_all: args.bool("keep_all"),
        sport_overrides: args.opt::<HashMap<String, Value>>("sport_overrides")?.unwrap_or_default().into_iter()
            .filter_map(|(k, v)| decimal_of(&v).map(|v| (k, v))).collect(),
        sport_minutes_overrides: args.opt::<HashMap<String, Value>>("sport_minutes_overrides")?.unwrap_or_default().into_iter()
            .filter_map(|(k, v)| decimal_of(&v).map(|v| (k, v.trunc().try_into().unwrap_or(0)))).collect(),
    })
}

fn variants(raw: &Value, surcharge_percentile: Decimal, surcharge_mgdl: Decimal) -> Res<Vec<forecast::Variant>> {
    raw.as_array().ok_or_else(|| CoreError::Value("variants".into()))?.iter().map(|item| {
        let parts = item.as_array().ok_or_else(|| CoreError::Value("variant".into()))?;
        let lookback = parts.first().and_then(decimal_of).unwrap_or_default().trunc().try_into().unwrap_or(0);
        let half_life = parts.get(1).and_then(decimal_of).unwrap_or_default();
        let mut surcharge = parts.get(3).and_then(decimal_of);
        let percentile = if parts.get(2).and_then(Value::as_str) == Some(forecast::SURCHARGE) {
            surcharge = Some(surcharge_mgdl);
            surcharge_percentile
        } else {
            parts.get(2).and_then(decimal_of).unwrap_or_default()
        };
        Ok(forecast::Variant { lookback, half_life, percentile, surcharge_mgdl: surcharge })
    }).collect()
}

fn key(args: &Args, name: &str) -> Res<forecast::MealKey> {
    let parts: Vec<Option<String>> = args.get(name)?;
    Ok((parts.first().cloned().flatten().unwrap_or_default(), parts.get(1).cloned().flatten()))
}

/// Set the process context: {"language": "de"|"en", "switches": [[instant, zone], ...]}.
pub fn configure(context_value: &Value) {
    if let Some(language) = context_value.get("language").and_then(Value::as_str) {
        context::set_language(language);
    }
    if let Some(switches) = context_value.get("switches").and_then(Value::as_array) {
        let parsed = switches.iter().filter_map(|pair| {
            let at = DateTime::parse_from_rfc3339(pair.get(0)?.as_str()?).ok()?.with_timezone(&Utc);
            Some((at, pair.get(1)?.as_str().unwrap_or_default().to_string()))
        }).collect();
        context::set_switches(parsed);
    }
}

/// Run one core function on JSON arguments.
pub fn call(name: &str, args: &Value) -> Res<Value> {
    let a = Args(args);
    match name {
        "calculation.minutes_between" => out(calculation::minutes_between(a.get("later")?, a.get("earlier")?)),
        "calculation.insulin_remaining" => out(calculation::insulin_remaining(a.dec_or("minutes", Decimal::ZERO))),
        "calculation.schedule_value_at" => {
            let key = if a.str("key") == "grams_per_unit" { ScheduleKey::GramsPerUnit } else { ScheduleKey::MgdlPerUnit };
            out(calculation::schedule_value_at(a.get("moment")?, &a.get::<Vec<IsfRow>>("schedule")?, &a.str("timezone"), key)?)
        }
        "calculation.isf_at" => out(calculation::isf_at(a.get("moment")?, &a.get::<Vec<IsfRow>>("schedule")?, &a.str("timezone"))?),
        "calculation.interpolate_glucose" => out(calculation::Interpolator::new(&a.get::<Vec<Reading>>("entries")?,
            a.int_or("max_gap_minutes", 15)).at(a.get("moment")?)?),
        "calculation.finger_glucose" => out(calculation::finger_glucose(&a.get::<Vec<Treatment>>("treatments")?, a.get("moment")?,
            a.int_or("max_gap_minutes", 15))),
        "calculation.prebolus_start" => out(calculation::prebolus_start(&a.get::<Episode>("episode")?,
            &a.get::<Vec<Treatment>>("all_treatments")?, &a.get::<Vec<Reading>>("entries")?, &a.get::<Settings>("settings")?)),
        "calculation.automatic_window" => out(calculation::automatic_window(&a.get::<Episode>("episode")?,
            &a.get::<Vec<Treatment>>("all_treatments")?, &a.get::<Vec<Reading>>("entries")?, &a.get::<Settings>("settings")?,
            a.0.get("prebolus").and_then(Value::as_bool).unwrap_or(true))),
        "calculation.window_totals" => out(calculation::window_totals(&a.get::<Episode>("episode")?,
            &a.get::<Vec<Treatment>>("all_treatments")?, a.get("start")?, a.get("end")?)),
        "calculation.calculate_episode" => out(calculation::calculate_episode(&a.get::<Episode>("episode")?,
            &a.get::<Vec<Treatment>>("all_treatments")?, &a.get::<Vec<Reading>>("entries")?, &a.get::<Settings>("settings")?,
            a.dec_or("sport_effect_mgdl", Decimal::ZERO), a.opt::<ManualWindow>("manual_window")?.as_ref())?),

        "episodes.normalize_entries" => out(episodes::normalize_entries(&a.get::<Vec<Value>>("raw")?)),
        "episodes.normalize_treatments" => out(episodes::normalize_treatments(&a.get::<Vec<Value>>("raw")?)),
        "episodes.group_diary_followups" => {
            let mut treatments: Vec<Treatment> = a.get("treatments")?;
            episodes::group_diary_followups(&mut treatments, &a.get::<Settings>("settings")?);
            out(treatments)
        }
        "episodes.meal_kind_at" => out(episodes::meal_kind_at(a.get("at")?, &a.get::<Settings>("settings")?)),
        "episodes.preceding_meal" => out(episodes::preceding_meal(a.get("at")?, a.get::<Vec<Treatment>>("treatments")?.iter(), &a.get::<Settings>("settings")?)),
        "episodes.taken_meal" => out(episodes::taken_meal(a.get("at")?, a.get::<Vec<Treatment>>("treatments")?.iter(), &a.get::<Settings>("settings")?)),
        "episodes.meals_for_day" => {
            let mut settings: Value = a.opt("settings")?.unwrap_or(json!({}));
            settings["timezone"] = json!(a.str("timezone_name"));
            let settings: Settings = serde_json::from_value(settings).map_err(|e| CoreError::Value(e.to_string()))?;
            out(episodes::meals_for_day(&a.get::<Vec<Treatment>>("treatments")?, day(&a, "day")?, &settings))
        }

        "on_board.stomach" => {
            let rows: Vec<(Instant, Value)> = a.get("entries")?;
            out(on_board::stomach(&rows.into_iter().map(|(at, g)| (at, decimal_of(&g).unwrap_or_default())).collect::<Vec<_>>()))
        }
        "on_board.stomach_grams" => out(on_board::stomach_grams(&a.get::<Vec<Treatment>>("treatments")?, a.get("now")?)),
        "on_board.surcharge_remaining" => out(on_board::surcharge_remaining(a.dec_or("minutes", Decimal::ZERO),
            a.dec_or("duration", on_board::SURCHARGE_MINUTES))),
        "on_board.sport_remaining" => out(on_board::sport_remaining(a.dec_or("minutes_after_end", Decimal::ZERO),
            a.dec_or("duration", on_board::SPORT_MINUTES))),
        "on_board.forecast_band" => out(on_board::forecast_band(a.dec_or("lowering_mgdl", Decimal::ZERO), a.dec_or("raising_mgdl", Decimal::ZERO))),
        "on_board.sources" => {
            let treatments: Vec<Treatment> = a.get("treatments")?;
            // carb_factors: [[instant, IE/KE or null], ...]; scales: per treatment (same order), null = 1.
            let factors: Vec<(Instant, Value)> = a.opt("carb_factors")?.unwrap_or_default();
            let factors: HashMap<Instant, Option<Decimal>> = factors.into_iter().map(|(at, v)| (at, decimal_of(&v))).collect();
            let carb_factor = move |at: Instant| factors.get(&at).copied().flatten();
            let scales: Option<Vec<Value>> = a.opt("scales")?;
            let by_item: HashMap<(String, Instant), Option<Decimal>> = treatments.iter().zip(scales.clone().unwrap_or_default())
                .map(|(item, v)| ((item.id.clone(), item.at), decimal_of(&v))).collect();
            let scale = move |item: &Treatment| by_item.get(&(item.id.clone(), item.at)).copied().flatten();
            let options = source_options(&a, &carb_factor, if scales.is_some() { Some(&scale) } else { None })?;
            out(on_board::sources(&treatments, a.get("now")?, &a.get::<Settings>("settings")?, &options)?)
        }
        "on_board.effects_at" => out(on_board::effects_at(&a.get::<on_board::Found>("found")?, a.get("moment")?, a.opt("cutoff")?)),
        "on_board.summary" => out(on_board::summary(&a.get::<on_board::Found>("found")?, a.get("now")?)),
        "on_board.timeline" => out(on_board::timeline(&a.get::<on_board::Found>("found")?, &a.get::<Vec<Reading>>("entries")?,
            a.get("start")?, a.get("end")?, Duration::seconds(a.int_or("horizon_seconds", 3 * 3600)))),

        "dosing.half_units" => out(dosing::half_units(a.dec_or("units", Decimal::ZERO))),
        "dosing.suggest" => {
            let on_board_value = a.raw("on_board");
            let activity = a.raw("activity_change");
            if !(activity.is_null() || activity == json!("")) && decimal_of(&activity).is_none() {
                // Not a finite number (NaN, infinity): out of range, as in the reference.
                return Err(CoreError::Value(context::t("Sporteinfluss muss zwischen -500 und 500 mg/dL liegen")));
            }
            let options = dosing::DoseOptions {
                activity_change: a.dec_or("activity_change", Decimal::ZERO),
                units_per_ke: a.dec("units_per_ke"), auto_units_per_ke: a.dec("auto_units_per_ke"),
                auto_surcharge_mgdl: a.dec("auto_surcharge_mgdl"), surcharge_share: a.dec("surcharge_share"),
                on_board: (!on_board_value.is_null()).then(|| (
                    on_board_value.get("lowering_mgdl").and_then(decimal_of).unwrap_or_default(),
                    on_board_value.get("raising_mgdl").and_then(decimal_of).unwrap_or_default())),
                finger: a.opt("finger")?.unwrap_or_default(), hold_rise: a.bool("hold_rise"),
            };
            let carbs = a.dec("carbs").ok_or_else(|| CoreError::Arithmetic("[<class 'decimal.ConversionSyntax'>]".into()))?;
            out(dosing::suggest(carbs, &a.get::<Settings>("settings")?, &a.get::<Vec<Reading>>("entries")?,
                &a.get::<Vec<Treatment>>("treatments")?, a.get("now")?, &options)?)
        }

        "split.offer" => out(split::offer(a.dec_or("carbs", Decimal::ZERO), a.bool("snack"), &a.get::<Settings>("settings")?, a.bool("fat"))),
        "split.worth_it" => out(split::worth_it(a.dec("full_units"), &a.get::<Settings>("settings")?)),
        "split.parts" => out(split::parts(a.dec_or("full_units", Decimal::ZERO), a.int_or("first_percent", 60), a.dec("later_units"))),
        "split.open_plan" => out(split::open_plan(&a.get::<Vec<Treatment>>("treatments")?, a.get("now")?)),
        "split.timing" => {
            let plan: Value = a.get("plan")?;
            let plan = split::Plan { meal_at: serde_json::from_value(plan["meal_at"].clone()).map_err(|e| CoreError::Value(e.to_string()))?,
                units: decimal_of(&plan["units"]).unwrap_or_default(),
                due_at: serde_json::from_value(plan["due_at"].clone()).map_err(|e| CoreError::Value(e.to_string()))? };
            out(split::timing(&plan, &a.get::<Vec<Reading>>("entries")?, &a.get::<Settings>("settings")?, a.get("now")?))
        }

        "correction_scale.meal_rise" => out(correction_scale::meal_rise(&a.get::<Vec<Reading>>("entries")?,
            &a.get::<Vec<Treatment>>("treatments")?, a.get("now")?)),
        "correction_scale.scale" => out(correction_scale::scale(&a.get("result")?, a.opt::<correction_scale::Rise>("rise")?.as_ref())),
        "correction_scale.dose_range" => out(correction_scale::dose_range(a.dec_or("normal_units", Decimal::ZERO),
            a.bool("correction"), a.opt::<String>("tempo")?.as_deref())),

        "glucose.limits" => out(glucose::limits(&a.get::<Settings>("settings").unwrap_or_else(|_| default_settings()))),
        "glucose.shown" => out(glucose::shown(a.dec_or("mgdl", Decimal::ZERO), &a.get::<Settings>("settings").unwrap_or_else(|_| default_settings()),
            a.bool("signed"))),
        "glucose.target_at" => out(glucose::target_at(a.get("at")?, &a.get::<Settings>("settings")?)),
        "glucose.tone" => out(glucose::tone(a.dec("value"), a.get("at")?, &a.get::<Settings>("settings")?)),
        "glucose.target_bands" => out(glucose::target_bands(a.get("start")?, a.get("end")?, &a.get::<Settings>("settings")?)),
        "glucose.trend" => {
            let points: Vec<(Instant, Value)> = a.get("points")?;
            out(glucose::trend(&points.into_iter().map(|(at, v)| (at, decimal_of(&v).unwrap_or_default())).collect::<Vec<_>>()))
        }
        "glucose.current_status" => out(glucose::current_status(&a.get::<Vec<Reading>>("entries")?, &a.get::<Settings>("settings")?,
            a.get("now")?)),

        "fpe.units" => out(fpe::units(&a.raw("fat_g"), &a.raw("protein_g"))),
        "fpe.minutes" => out(fpe::minutes(a.dec_or("fpe", Decimal::ZERO))),
        "fpe.estimate" => out(fpe::estimate(&a.raw("values"), &a.get::<Settings>("settings")?)),

        "forecast.weighted_quantile" => {
            let pairs: Vec<(Value, Value)> = a.get("pairs")?;
            let pairs: Vec<(Decimal, Decimal)> = pairs.iter().map(|(v, w)| (decimal_of(v).unwrap_or_default(), decimal_of(w).unwrap_or_default())).collect();
            out(forecast::weighted_quantile(&pairs, a.dec_or("fraction", Decimal::ZERO)))
        }
        "forecast.weight" => out(forecast::weight(a.int_or("age", 0), a.dec_or("half_life", Decimal::ONE))),
        "forecast.surcharge_share" => out(forecast::surcharge_share(a.dec_or("grams", Decimal::ZERO))),
        "forecast.history" => out(forecast::history(&a.get::<Vec<forecast::MealPoint>>("points")?, &key(&a, "key")?, day(&a, "day")?,
            a.int_or("lookback", 30))),
        "forecast.predict" => out(forecast::predict(&a.get::<Vec<forecast::MealPoint>>("points")?, &key(&a, "key")?, day(&a, "day")?,
            a.dec_or("percentile", Decimal::ZERO), a.dec_or("half_life", Decimal::ONE), a.int_or("lookback", 30))),
        "forecast.predict_method" => out(forecast::predict_method(&a.get::<Vec<forecast::MealPoint>>("points")?, &key(&a, "key")?,
            day(&a, "day")?, a.dec_or("percentile", Decimal::ZERO), a.dec_or("half_life", Decimal::ONE), a.int_or("lookback", 30),
            &a.str("method"), a.dec_or("mgdl", forecast::SURCHARGE_MGDL), a.dec_or("fat_mgdl", Decimal::ZERO))),
        "forecast.place_scale" => out(forecast::place_scale(&a.get::<Vec<forecast::MealPoint>>("points")?, a.int_or("place_id", 0),
            day(&a, "day")?, a.dec_or("percentile", Decimal::ZERO), a.dec_or("half_life", Decimal::ONE), a.int_or("lookback", 30),
            &a.str("method"), a.dec_or("mgdl", forecast::SURCHARGE_MGDL), a.dec_or("fat_mgdl", Decimal::ZERO))),
        "forecast.median" => {
            let values: Vec<Value> = a.get("values")?;
            out(forecast::median(&values.iter().map(|v| decimal_of(v).unwrap_or_default()).collect::<Vec<_>>()))
        }
        "forecast.day_type_ratios" => out(forecast::day_type_ratios(&a.get::<Vec<forecast::MealPoint>>("points")?, day(&a, "today")?)),
        "forecast.backtest" => {
            let variants = variants(&a.raw("variants"), a.dec_or("surcharge_percentile", rust_decimal_macros::dec!(37.5)),
                a.dec_or("surcharge_mgdl", forecast::SURCHARGE_MGDL))?;
            out(forecast::backtest(&a.get::<Vec<forecast::MealPoint>>("points")?, day(&a, "since")?, &variants,
                a.dec_or("target_glucose", Decimal::from(120)), a.dec_or("fat_mgdl", Decimal::ZERO)))
        }
        "forecast.tuning" => out(forecast::tuning(&a.get::<Vec<forecast::MealPoint>>("points")?, day(&a, "since")?, a.int_or("lookback", 30),
            a.dec_or("half_life", Decimal::ONE), a.dec_or("percentile", Decimal::ZERO), a.dec_or("surcharge_mgdl", Decimal::ZERO),
            a.dec_or("target_glucose", Decimal::from(120)), a.dec_or("fat_mgdl", Decimal::ZERO))),
        "forecast.tradeoffs" => out(forecast::tradeoffs(&a.get::<Vec<forecast::MealPoint>>("points")?, day(&a, "since")?, a.int_or("lookback", 30),
            a.dec_or("half_life", Decimal::ONE), a.dec_or("percentile", Decimal::ZERO), a.dec_or("surcharge_mgdl", Decimal::ZERO),
            a.dec_or("target_glucose", Decimal::from(120)), a.dec_or("fat_mgdl", Decimal::ZERO))),
        "forecast.explain" => out(forecast::explain(&a.get::<Vec<forecast::MealPoint>>("points")?, &key(&a, "key")?, day(&a, "day")?,
            a.int_or("lookback", 30), a.dec_or("half_life", Decimal::ONE), a.dec_or("percentile", Decimal::ZERO),
            a.dec_or("surcharge_mgdl", Decimal::ZERO), a.dec_or("fat_mgdl", Decimal::ZERO))),

        "glucose_alerts.forecast" => out(glucose_alerts::forecast(&a.get::<Vec<Reading>>("entries")?, &a.get("found")?, a.get("now")?,
            a.dec_or("fade", glucose_alerts::FADE_MINUTES))),
        "glucose_alerts.warnings" => {
            let limits = glucose_alerts::Limits {
                low_pause_minutes: a.int_or("low_pause_minutes", glucose_alerts::LOW_PAUSE_MINUTES),
                high_pause_minutes: a.int_or("high_pause_minutes", glucose_alerts::HIGH_PAUSE_MINUTES),
                low_mgdl: a.dec_or("low_mgdl", glucose_alerts::LOW_MGDL), high_mgdl: a.dec_or("high_mgdl", glucose_alerts::HIGH_MGDL),
            };
            out(glucose_alerts::warnings(&a.get::<Vec<Reading>>("entries")?, &a.get("found")?, a.get("now")?, &a.str("timezone_name"), &limits))
        }
        "correction_check.due" => out(correction_check::due(&a.get::<Vec<Reading>>("entries")?, &a.get("found")?,
            &a.get::<Vec<Treatment>>("treatments")?, a.get("now")?)),

        "stats.median" => {
            let values: Vec<Value> = a.get("values")?;
            out(stats::median(&values.iter().map(|v| decimal_of(v).unwrap_or_default()).collect::<Vec<_>>()))
        }
        "stats.quartile" => {
            let values: Vec<Value> = a.get("values")?;
            out(stats::quartile(&values.iter().map(|v| decimal_of(v).unwrap_or_default()).collect::<Vec<_>>(), a.dec_or("fraction", Decimal::ZERO)))
        }
        "stats.strong_outlier_flags" => {
            let values: Vec<Value> = a.get("values")?;
            out(stats::strong_outlier_flags(&values.iter().map(|v| decimal_of(v).unwrap_or_default()).collect::<Vec<_>>()))
        }
        "services.dose_help" => crate::services::dose_help(&serde_json::from_value(args.clone())
            .map_err(|error| CoreError::Value(format!("snapshot: {error}")))?),
        "day.compute_day" => crate::day::compute_day(&serde_json::from_value(args.clone())
            .map_err(|error| CoreError::Value(format!("snapshot: {error}")))?),
        other => Err(CoreError::Value(format!("unknown function {other}"))),
    }
}

fn default_settings() -> Settings {
    serde_json::from_value(json!({"timezone": "UTC"})).unwrap()
}
