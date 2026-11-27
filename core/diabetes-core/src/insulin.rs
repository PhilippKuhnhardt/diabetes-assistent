//! Rapid-insulin action profile (app/insulin.py): Loop's exponential activity curve.

use rust_decimal::{Decimal, MathematicalOps};
use rust_decimal_macros::dec;

#[derive(Clone, Debug)]
pub struct InsulinProfile {
    pub key: &'static str,
    pub name: &'static str,
    pub delay_minutes: Decimal,
    pub peak_minutes: Decimal,
    pub duration_minutes: Decimal,
}

impl InsulinProfile {
    /// Share of a dose that has not acted yet, `minutes` after the injection.
    pub fn remaining(&self, minutes: Decimal) -> Decimal {
        let t = minutes - self.delay_minutes;
        if t <= Decimal::ZERO {
            return Decimal::ONE;
        }
        let (peak, duration) = (self.peak_minutes, self.duration_minutes);
        if t >= duration {
            return Decimal::ZERO;
        }
        let tau = peak * (Decimal::ONE - peak / duration) / (Decimal::ONE - dec!(2) * peak / duration);
        let a = dec!(2) * tau / duration;
        let scale = Decimal::ONE / (Decimal::ONE - a + (Decimal::ONE + a) * (-duration / tau).exp());
        let used = scale * (Decimal::ONE - a)
            * ((t * t / (tau * duration * (Decimal::ONE - a)) - t / tau - Decimal::ONE) * (-t / tau).exp() + Decimal::ONE);
        (Decimal::ONE - used).clamp(Decimal::ZERO, Decimal::ONE)
    }

    /// From the injection until nothing is left.
    pub fn total_minutes(&self) -> Decimal {
        self.delay_minutes + self.duration_minutes
    }
}

/// Loop's Fiasp preset: peak 55 min after a 10-min delay, 6 h. The only profile upstream.
pub const FIASP: InsulinProfile = InsulinProfile {
    key: "fiasp", name: "Fiasp", delay_minutes: dec!(10), peak_minutes: dec!(55), duration_minutes: dec!(360),
};

pub fn active() -> &'static InsulinProfile {
    &FIASP
}
