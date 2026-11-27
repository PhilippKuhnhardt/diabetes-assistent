//! Calculation core of the diabetes assistant, ported line by line from the Python reference in app/.
//!
//! Every function keeps the name, rules and result fields of its Python original; typed structs
//! replace the reference's dicts and rust_decimal its Decimal (28 significant digits). Results agree
//! with the reference within rounding of the last digits; doses in 0.5 IE steps and every rule
//! decision agree exactly (checked by core/parity against the Python reference).

pub mod api;
pub mod calculation;
pub mod context;
pub mod correction_check;
pub mod day;
pub mod correction_scale;
pub mod dosing;
pub mod episodes;
pub mod forecast;
pub mod fpe;
pub mod glucose;
pub mod glucose_alerts;
pub mod insulin;
pub mod on_board;
pub mod model;
pub mod notifications;
pub mod services;
pub mod split;
pub mod stats;
pub mod summary;
pub mod zone;

/// An error the Python reference raises: ValueError with its (translated) message, or an
/// ArithmeticError from Decimal.
#[derive(Clone, Debug, PartialEq)]
pub enum CoreError {
    Value(String),
    Arithmetic(String),
}

impl std::fmt::Display for CoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CoreError::Value(message) | CoreError::Arithmetic(message) => f.write_str(message),
        }
    }
}

pub type Res<T> = Result<T, CoreError>;

/// Raise ValueError(message), as the reference does.
pub fn value_error<T>(message: String) -> Res<T> {
    Err(CoreError::Value(message))
}
