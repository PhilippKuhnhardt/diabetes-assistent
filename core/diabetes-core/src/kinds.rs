//! The closed sets of names the core works with: meal kind, day type, Automatik method, a diary entry's
//! role in its meal and the carb certainty. On the wire they stay the lowercase names the server and the
//! page use ("breakfast", "work", "surcharge", "meal-dose", "weighed").

use crate::model::Settings;
use serde::{Deserialize, Deserializer, Serialize};
use std::fmt;

macro_rules! names {
    ($name:ident { $($variant:ident = $text:literal),+ $(,)? }) => {
        impl $name {
            pub const ALL: &'static [$name] = &[$($name::$variant),+];

            pub fn as_str(self) -> &'static str {
                match self { $($name::$variant => $text),+ }
            }

            pub fn parse(text: &str) -> Option<Self> {
                match text { $($text => Some($name::$variant),)+ _ => None }
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(self.as_str())
            }
        }

        impl PartialEq<&str> for $name {
            fn eq(&self, other: &&str) -> bool {
                self.as_str() == *other
            }
        }
    };
}

/// Breakfast, lunch or dinner, by the clock or by the diary marker.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum MealKind {
    Breakfast,
    Lunch,
    Dinner,
}

names!(MealKind { Breakfast = "breakfast", Lunch = "lunch", Dinner = "dinner" });

impl MealKind {
    /// The clock-based kind of a local minute of the day.
    pub fn at_minute(minute: i64, settings: &Settings) -> Self {
        if minute < settings.breakfast_end_minute {
            MealKind::Breakfast
        } else if minute < settings.lunch_end_minute {
            MealKind::Lunch
        } else {
            MealKind::Dinner
        }
    }
}

/// Work or free day (breakfast only, while day types are on).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum DayType {
    Work,
    Free,
}

names!(DayType { Work = "work", Free = "free" });

/// How the Automatik derives a factor: a percentile of past factors, or that without each meal's surcharge.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AutoMethod {
    #[default]
    Percentile,
    Surcharge,
}

names!(AutoMethod { Percentile = "percentile", Surcharge = "surcharge" });

/// A diary entry's role in its meal (the [DA1:…] marker).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Role {
    #[serde(rename = "meal")]
    Meal,
    #[serde(rename = "meal-dose")]
    MealDose,
    #[serde(rename = "correction")]
    Correction,
    #[serde(rename = "snack")]
    Snack,
}

names!(Role { Meal = "meal", MealDose = "meal-dose", Correction = "correction", Snack = "snack" });

/// How sure the carbs are, from most to least certain.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Certainty {
    /// Weighed or standardised (package, standard portion).
    Weighed,
    /// All relevant ingredients known, only the weight estimated.
    Known,
    /// Unknown ingredients or a very unclear amount.
    Unsure,
}

names!(Certainty { Weighed = "weighed", Known = "known", Unsure = "unsure" });

/// One of the names, else None: a name the core does not know (typed by hand, from an older version)
/// counts as not set, as an unknown marker always did.
pub fn opt_name<'de, D: Deserializer<'de>, T: serde::de::DeserializeOwned>(deserializer: D) -> Result<Option<T>, D::Error> {
    let value = serde_json::Value::deserialize(deserializer)?;
    Ok(serde_json::from_value(value).ok())
}

/// The least certain of the levels.
pub fn least_certain(levels: impl IntoIterator<Item = Certainty>) -> Option<Certainty> {
    levels.into_iter().max()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_round_trip() {
        for kind in MealKind::ALL {
            assert_eq!(MealKind::parse(kind.as_str()), Some(*kind));
            assert_eq!(serde_json::to_value(kind).unwrap(), kind.as_str());
        }
        assert_eq!(serde_json::to_value(Role::MealDose).unwrap(), "meal-dose");
        assert_eq!(Role::parse("meal-dose"), Some(Role::MealDose));
        assert_eq!(least_certain([Certainty::Known, Certainty::Weighed]), Some(Certainty::Known));
        assert_eq!(least_certain([]), None);
    }
}
