//! C interface for native apps: `core_call(name, args, context)` takes JSON arguments and a JSON context
//! (`{"language": "de"|"en", "insulin": "fiasp", "switches": [[instant, zone], ...]}`, every field
//! optional) and returns `{"ok": …}` or `{"error": …}` as JSON; free the returned string with `core_free`. Build for iPhone with `cargo build --release --target aarch64-apple-ios`.

use std::ffi::{c_char, CStr, CString};

fn text(pointer: *const c_char) -> String {
    if pointer.is_null() {
        return String::new();
    }
    unsafe { CStr::from_ptr(pointer) }.to_string_lossy().into_owned()
}

fn answer(value: serde_json::Value) -> *mut c_char {
    CString::new(value.to_string()).unwrap_or_default().into_raw()
}

/// # Safety
/// `name`, `args` and `context` must be valid NUL-terminated strings (or a null `context` for the
/// default one).
#[no_mangle]
pub unsafe extern "C" fn core_call(name: *const c_char, args: *const c_char, context: *const c_char) -> *mut c_char {
    let name = text(name);
    let args = serde_json::from_str(&text(args)).unwrap_or_default();
    let context = match diabetes_core::context::Context::from_value(&serde_json::from_str(&text(context)).unwrap_or_default()) {
        Ok(context) => context,
        Err(error) => return answer(serde_json::json!({"error": error.to_string()})),
    };
    let result = std::panic::catch_unwind(|| diabetes_core::api::call(&name, &args, &context));
    answer(match result {
        Ok(Ok(value)) => serde_json::json!({"ok": value}),
        Ok(Err(error)) => serde_json::json!({"error": error.to_string()}),
        Err(_) => serde_json::json!({"error": "internal error"}),
    })
}

/// # Safety
/// `value` must come from `core_call` and be freed once.
#[no_mangle]
pub unsafe extern "C" fn core_free(value: *mut c_char) {
    if !value.is_null() {
        drop(CString::from_raw(value));
    }
}
