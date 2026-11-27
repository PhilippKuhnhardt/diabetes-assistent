//! Line protocol for the parity check and scripts: each stdin line is
//! {"fn": "module.function", "args": {...}, "context": {...}?}; each stdout line is
//! {"ok": result} or {"error": message, "kind": "value" | "arithmetic" | "panic"}.

use diabetes_core::{api, CoreError};
use serde_json::{json, Value};
use std::io::{self, BufRead, Write};

fn main() {
    std::panic::set_hook(Box::new(|_| {}));
    let stdin = io::stdin();
    let mut stdout = io::BufWriter::new(io::stdout());
    for line in stdin.lock().lines() {
        let line = line.expect("stdin");
        if line.trim().is_empty() {
            continue;
        }
        let request: Value = match serde_json::from_str(&line) {
            Ok(value) => value,
            Err(error) => {
                writeln!(stdout, "{}", json!({"error": error.to_string(), "kind": "request"})).unwrap();
                continue;
            }
        };
        if let Some(context) = request.get("context") {
            if let Err(error) = api::configure(context) {
                writeln!(stdout, "{}", json!({"error": error.to_string(), "kind": "context"})).unwrap();
                continue;
            }
        }
        let name = request["fn"].as_str().unwrap_or_default().to_string();
        let args = request.get("args").cloned().unwrap_or(json!({}));
        let answer = match std::panic::catch_unwind(|| api::call(&name, &args)) {
            Ok(Ok(value)) => json!({"ok": value}),
            Ok(Err(CoreError::Value(message))) => json!({"error": message, "kind": "value"}),
            Ok(Err(CoreError::Arithmetic(message))) => json!({"error": message, "kind": "arithmetic"}),
            Err(panic) => json!({"error": panic.downcast_ref::<String>().cloned()
                .or_else(|| panic.downcast_ref::<&str>().map(|s| s.to_string())).unwrap_or_default(), "kind": "panic"}),
        };
        writeln!(stdout, "{answer}").unwrap();
        stdout.flush().unwrap();
    }
}
