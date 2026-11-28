//! What a call runs with besides its arguments: the language of the texts (APP_LANGUAGE, English from
//! i18n_en.json), the rapid-insulin profile (APP_INSULIN) and the time-zone switches while travelling.
//! Every `api::call` gets its own `Context`; `scope` makes it the one the rules read for the length of
//! that call, on the calling thread only, so calls with different contexts never see each other's. Outside
//! a scope (unit tests) the rules read the default: German, Fiasp, no switches.

use crate::insulin::{InsulinProfile, FIASP, PROFILES};
use crate::model::Instant;
use crate::zone::Zone;
use crate::{CoreError, Res};
use chrono::{DateTime, Utc};
use serde_json::Value;
use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;
use std::sync::OnceLock;

#[derive(Clone, Debug)]
pub struct Context {
    pub english: bool,
    pub insulin: &'static InsulinProfile,
    /// Travel switches (instant, zone name or "" for home), oldest first.
    pub switches: Vec<(Instant, String)>,
}

impl Default for Context {
    fn default() -> Self {
        Context { english: false, insulin: &FIASP, switches: Vec::new() }
    }
}

impl Context {
    /// From {"language": "de"|"en", "insulin": "fiasp", "switches": [[instant, zone], ...]}; a missing
    /// field keeps its default, an unknown insulin profile is an error.
    pub fn from_value(value: &Value) -> Res<Context> {
        let mut context = Context::default();
        if let Some(key) = value.get("insulin").and_then(Value::as_str) {
            context.insulin = profile(key).map_err(CoreError::Value)?;
        }
        if let Some(language) = value.get("language").and_then(Value::as_str) {
            context.english = language.starts_with("en");
        }
        if let Some(switches) = value.get("switches").and_then(Value::as_array) {
            context.switches = switches.iter().filter_map(|pair| {
                let at = DateTime::parse_from_rfc3339(pair.get(0)?.as_str()?).ok()?.with_timezone(&Utc);
                Some((at, pair.get(1)?.as_str().unwrap_or_default().to_string()))
            }).collect();
        }
        Ok(context)
    }

    /// A text that differs whenever the context does (for caches across calls).
    pub fn fingerprint(&self) -> String {
        let switches: Vec<String> = self.switches.iter().map(|(at, zone)| format!("{}={zone}", at.to_rfc3339())).collect();
        format!("{}|{}|{}", self.english, self.insulin.key, switches.join(","))
    }
}

/// The rapid-insulin profile by key (insulin::PROFILES); an unknown key is an error.
pub fn profile(key: &str) -> Result<&'static InsulinProfile, String> {
    PROFILES.iter().copied().find(|profile| profile.key == key)
        .ok_or_else(|| format!("unknown insulin profile {key:?}; known: {}", PROFILES.iter().map(|p| p.key).collect::<Vec<_>>().join(", ")))
}

/// The context of the running call plus the zones it built from it.
struct Scope {
    context: Context,
    zones: RefCell<HashMap<String, Zone>>,
}

thread_local! {
    static CURRENT: RefCell<Option<Rc<Scope>>> = const { RefCell::new(None) };
    static DEFAULT: Rc<Scope> = Rc::new(Scope { context: Context::default(), zones: RefCell::new(HashMap::new()) });
}

/// Run `body` with `context` as the one the rules read; the previous one comes back afterwards, also
/// when `body` panics.
pub fn scope<R>(context: &Context, body: impl FnOnce() -> R) -> R {
    struct Restore(Option<Rc<Scope>>);
    impl Drop for Restore {
        fn drop(&mut self) {
            let previous = self.0.take();
            CURRENT.with(|current| *current.borrow_mut() = previous);
        }
    }
    let scope = Rc::new(Scope { context: context.clone(), zones: RefCell::new(HashMap::new()) });
    let _restore = Restore(CURRENT.with(|current| current.borrow_mut().replace(scope)));
    body()
}

fn current() -> Rc<Scope> {
    CURRENT.with(|current| current.borrow().clone()).unwrap_or_else(|| DEFAULT.with(Rc::clone))
}

/// The context of the running call.
pub fn get() -> Context {
    current().context.clone()
}

fn english_texts() -> &'static HashMap<String, String> {
    static TEXTS: OnceLock<HashMap<String, String>> = OnceLock::new();
    TEXTS.get_or_init(|| serde_json::from_str(include_str!("i18n_en.json")).expect("i18n_en.json"))
}

pub fn insulin() -> &'static InsulinProfile {
    current().context.insulin
}

pub fn english() -> bool {
    current().context.english
}

/// The app's local zone for the home zone name, with the call's travel switches.
pub fn local_zone(name: &str) -> Zone {
    let scope = current();
    if let Some(zone) = scope.zones.borrow().get(name) {
        return zone.clone();
    }
    let zone = Zone::new(name, &scope.context.switches);
    scope.zones.borrow_mut().insert(name.to_string(), zone.clone());
    zone
}

/// The text in the call's language with {placeholders} filled in.
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

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_scope_holds_only_for_its_call() {
        let english = Context::from_value(&json!({"language": "en"})).unwrap();
        assert!(!self::english());
        let inner = scope(&english, || (self::english(), scope(&Context::default(), self::english), self::english()));
        assert_eq!(inner, (true, false, true));
        assert!(!self::english());
        let other_thread = scope(&english, || std::thread::spawn(self::english).join().unwrap());
        assert!(!other_thread);
    }

    #[test]
    fn a_scope_is_left_also_on_a_panic() {
        let english = Context::from_value(&json!({"language": "en"})).unwrap();
        assert!(std::panic::catch_unwind(|| scope(&english, || panic!("bug"))).is_err());
        assert!(!self::english());
    }

    #[test]
    fn unknown_profiles_are_refused() {
        assert!(Context::from_value(&json!({"insulin": "nope"})).is_err());
        assert_eq!(Context::from_value(&json!({})).unwrap().insulin.key, "fiasp");
    }
}
