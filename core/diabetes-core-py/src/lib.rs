//! Python binding of the calculation core: the server imports it as `app.diabetes_core` and reaches it
//! only through app/core.py. Arguments and results are JSON text, the same interface as `api::call`;
//! the GIL is released while the core computes, so request threads keep answering during a round.

use pyo3::exceptions::{PyArithmeticError, PyValueError};
use pyo3::prelude::*;

/// Run `module.function` on JSON arguments and return the JSON result. A rule error raises
/// ValueError (ArithmeticError for an arithmetic one), as the former Python modules did.
#[pyfunction]
fn call(py: Python<'_>, name: &str, args: &str) -> PyResult<String> {
    let outcome = py.detach(|| {
        let args: serde_json::Value = serde_json::from_str(args).map_err(|error| core_lib::CoreError::Value(error.to_string()))?;
        core_lib::api::call(name, &args).map(|value| value.to_string())
    });
    outcome.map_err(|error| match error {
        core_lib::CoreError::Arithmetic(message) => PyArithmeticError::new_err(message),
        core_lib::CoreError::Value(message) => PyValueError::new_err(message),
    })
}

/// Set the process context: {"language": "de"|"en", "switches": [[instant, zone], ...]}.
#[pyfunction]
fn configure(context: &str) -> PyResult<()> {
    let value = serde_json::from_str(context).map_err(|error| PyValueError::new_err(error.to_string()))?;
    core_lib::api::configure(&value);
    Ok(())
}

#[pymodule]
fn diabetes_core(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(call, module)?)?;
    module.add_function(wrap_pyfunction!(configure, module)?)?;
    Ok(())
}
