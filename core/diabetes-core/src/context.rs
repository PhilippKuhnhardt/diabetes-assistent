//! Process-wide inputs the reference reads from its environment and store: the language of the
//! texts (APP_LANGUAGE, English from app/i18n_en.py) and the time-zone switches while travelling.

use crate::model::Instant;
use crate::zone::Zone;
use std::collections::HashMap;
use std::sync::{LazyLock, OnceLock, RwLock};

struct Context {
    english: bool,
    switches: Vec<(Instant, String)>,
    zones: HashMap<String, Zone>,
}

static CONTEXT: LazyLock<RwLock<Context>> = LazyLock::new(|| RwLock::new(Context { english: false, switches: Vec::new(), zones: HashMap::new() }));

fn english_texts() -> &'static HashMap<String, String> {
    static TEXTS: OnceLock<HashMap<String, String>> = OnceLock::new();
    TEXTS.get_or_init(|| serde_json::from_str(include_str!("i18n_en.json")).expect("i18n_en.json"))
}

pub fn set_language(language: &str) {
    CONTEXT.write().unwrap().english = language.starts_with("en");
}

pub fn english() -> bool {
    CONTEXT.read().unwrap().english
}

/// Set the travel switches (instant, zone name or "" for home), oldest first.
pub fn set_switches(switches: Vec<(Instant, String)>) {
    let mut context = CONTEXT.write().unwrap();
    if context.switches != switches {
        context.switches = switches;
        context.zones.clear();
    }
}

/// The app's local zone for the home zone name.
pub fn local_zone(name: &str) -> Zone {
    if let Some(zone) = CONTEXT.read().unwrap().zones.get(name) {
        return zone.clone();
    }
    let mut context = CONTEXT.write().unwrap();
    let zone = Zone::new(name, &context.switches);
    context.zones.insert(name.to_string(), zone.clone());
    zone
}

/// The text in the configured language with {placeholders} filled in.
pub fn tr(text: &str, params: &[(&str, String)]) -> String {
    let mut text = if english() { english_texts().get(text).cloned().unwrap_or_else(|| text.to_string()) } else { text.to_string() };
    for (name, value) in params {
        text = text.replace(&format!("{{{name}}}"), value);
    }
    text
}

pub fn t(text: &str) -> String {
    tr(text, &[])
}
