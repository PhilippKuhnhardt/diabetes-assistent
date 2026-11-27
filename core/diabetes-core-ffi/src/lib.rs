//! C interface for native apps: `core_call(name, json)` returns `{"ok": …}` or `{"error": …}` as JSON;
//! free the returned string with `core_free`. `core_configure(json)` sets the language, the insulin
//! profile and the travel switches and answers like `core_call` (`{"ok": null}` or `{"error": …}`). Build for iPhone with `cargo build --release --target aarch64-apple-ios`.

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
/// `name` and `args` must be valid NUL-terminated strings.
#[no_mangle]
pub unsafe extern "C" fn core_call(name: *const c_char, args: *const c_char) -> *mut c_char {
    let name = text(name);
    let args = serde_json::from_str(&text(args)).unwrap_or_default();
    let result = std::panic::catch_unwind(|| diabetes_core::api::call(&name, &args));
    answer(match result {
        Ok(Ok(value)) => serde_json::json!({"ok": value}),
        Ok(Err(error)) => serde_json::json!({"error": error.to_string()}),
        Err(_) => serde_json::json!({"error": "internal error"}),
    })
}

/// # Safety
/// `context` must be a valid NUL-terminated string.
#[no_mangle]
pub unsafe extern "C" fn core_configure(context: *const c_char) -> *mut c_char {
    answer(match diabetes_core::api::configure(&serde_json::from_str(&text(context)).unwrap_or_default()) {
        Ok(()) => serde_json::json!({"ok": null}),
        Err(error) => serde_json::json!({"error": error.to_string()}),
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
