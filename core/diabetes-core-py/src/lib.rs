//! Python binding of the calculation core: the server imports it as `app.diabetes_core` and reaches it
//! only through app/core.py. Arguments, context and results are JSON text, the same interface as `api::call`;
//! the GIL is released while the core computes, so request threads keep answering during a round.

use pyo3::exceptions::{PyArithmeticError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;

/// Run `module.function` on JSON arguments with a JSON context ({"language", "insulin", "switches"},
/// see context::Context) and return the JSON result. A rule error raises
/// ValueError (ArithmeticError for an arithmetic one), as the former Python modules did. A panic (a bug in
/// the core) raises RuntimeError, so the server's usual `except Exception` handling sees it instead of
/// PyO3's PanicException, which derives from BaseException and would end a request or background thread.
#[pyfunction]
fn call(py: Python<'_>, name: &str, args: &str, context: &str) -> PyResult<String> {
    let outcome = py.detach(|| {
        std::panic::catch_unwind(|| {
            let args: serde_json::Value = serde_json::from_str(args).map_err(|error| core_lib::CoreError::Value(error.to_string()))?;
            let context: serde_json::Value = serde_json::from_str(context).map_err(|error| core_lib::CoreError::Value(error.to_string()))?;
            let context = core_lib::context::Context::from_value(&context)?;
            core_lib::api::call(name, &args, &context).map(|value| value.to_string())
        })
    });
    let outcome = outcome.map_err(|panic| {
        let message = panic.downcast_ref::<String>().cloned()
            .or_else(|| panic.downcast_ref::<&str>().map(|text| text.to_string()))
            .unwrap_or_else(|| "panic".to_string());
        PyRuntimeError::new_err(format!("calculation core failed in {name}: {message}"))
    })?;
    outcome.map_err(|error| match error {
        core_lib::CoreError::Arithmetic(message) => PyArithmeticError::new_err(message),
        core_lib::CoreError::Value(message) => PyValueError::new_err(message),
    })
}

#[pymodule]
fn diabetes_core(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(call, module)?)?;
    Ok(())
}
