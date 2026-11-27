//! The JSON shape the core exchanges with the server: instants as Python's isoformat in UTC and
//! truthiness of loosely typed values (Nightscout records, form payloads). One place for these helpers.

use crate::model::Instant;
use chrono::Timelike;
use serde_json::Value;

/// An instant as Python's datetime.isoformat() in UTC: microseconds only when there are any.
pub fn iso(at: &Instant) -> String {
    let micros = at.nanosecond() / 1000;
    if micros == 0 {
        at.format("%Y-%m-%dT%H:%M:%S+00:00").to_string()
    } else {
        format!("{}.{:06}+00:00", at.format("%Y-%m-%dT%H:%M:%S"), micros)
    }
}

/// Every string chrono wrote as an RFC 3339 instant ("…Z") in Python's isoformat instead. The Nightscout
/// profile is passed through as Nightscout wrote it.
pub fn python_instants(value: &mut Value) {
    match value {
        Value::String(text) if text.len() >= 20 && text.ends_with('Z') && text.as_bytes().get(10) == Some(&b'T') => {
            if let Ok(parsed) = chrono::DateTime::parse_from_rfc3339(text) {
                *text = iso(&parsed.with_timezone(&chrono::Utc));
            }
        }
        Value::Array(items) => items.iter_mut().for_each(python_instants),
        Value::Object(map) => map.iter_mut().filter(|(key, _)| key.as_str() != "nightscout_profile")
            .for_each(|(_, item)| python_instants(item)),
        _ => {}
    }
}

/// A serialized value with its instants in Python's isoformat.
pub fn to_value<T: serde::Serialize>(value: &T) -> Value {
    let mut value = serde_json::to_value(value).unwrap_or(Value::Null);
    python_instants(&mut value);
    value
}

/// Whether a loosely typed value counts as set: not null, false, 0, "" or empty.
pub fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().is_some_and(|v| v != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn instants_read_like_python_isoformat() {
        let at: Instant = "2026-11-27T08:15:00Z".parse().unwrap();
        assert_eq!(iso(&at), "2026-11-27T08:15:00+00:00");
        let mut value = json!({"at": "2026-11-27T08:15:00.250Z", "nightscout_profile": {"at": "2026-11-27T08:15:00Z"}});
        python_instants(&mut value);
        assert_eq!(value, json!({"at": "2026-11-27T08:15:00.250000+00:00", "nightscout_profile": {"at": "2026-11-27T08:15:00Z"}}));
    }

    #[test]
    fn truthiness_follows_python() {
        for (value, set) in [(json!(null), false), (json!(0), false), (json!(0.0), false), (json!(""), false), (json!([]), false),
                             (json!({}), false), (json!(false), false), (json!(1), true), (json!("x"), true), (json!([0]), true)] {
            assert_eq!(truthy(&value), set, "{value}");
        }
    }
}
