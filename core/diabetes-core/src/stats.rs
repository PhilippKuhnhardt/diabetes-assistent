//! Order statistics on sorted values (app/analysis/stats.py).

use rust_decimal::Decimal;
use rust_decimal_macros::dec;

pub fn median(values: &[Decimal]) -> Option<Decimal> {
    crate::forecast::median(values)
}

/// Linear interpolation at (n − 1) × fraction in sorted values.
pub fn quartile(values: &[Decimal], fraction: Decimal) -> Decimal {
    let position = Decimal::from(values.len() - 1) * fraction;
    let lower = position.trunc().try_into().unwrap_or(0usize);
    let share = position - Decimal::from(lower);
    values[lower] + (values[(lower + 1).min(values.len() - 1)] - values[lower]) * share
}

/// Flag unusually distant values without claiming statistical certainty.
pub fn strong_outlier_flags(values: &[Decimal]) -> Vec<bool> {
    if values.len() < 3 {
        return vec![false; values.len()];
    }
    let mut ordered = values.to_vec();
    ordered.sort();
    let center = median(&ordered).unwrap();
    let mut distances: Vec<Decimal> = values.iter().map(|v| (v - center).abs()).collect();
    distances.sort();
    let threshold = (dec!(3) * median(&distances).unwrap()).max(center * dec!(0.5));
    values.iter().map(|v| (v - center).abs() > threshold).collect()
}
