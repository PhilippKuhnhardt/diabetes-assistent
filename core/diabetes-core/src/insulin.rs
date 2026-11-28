//! Rapid-insulin action profiles: Loop's exponential activity curve (delay, peak, duration).
//!
//! The app is built and tested with Fiasp; its curve fits the author's CGM data. A fork adds another
//! profile to PROFILES and picks it with APP_INSULIN (app/insulin.py passes the key to the core's context).
//! Every calculation (meal factors, "Wirkt noch", dose help, warnings) reads the active profile, and stored
//! days are recalculated when it changes (its key is part of every day's fingerprint).

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

/// Every profile a deployment can pick with APP_INSULIN.
pub const PROFILES: [&InsulinProfile; 1] = [&FIASP];

/// The profile the running call uses (its context, Fiasp by default).
pub fn active() -> &'static InsulinProfile {
    crate::context::insulin()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fiasp_matches_the_approved_table() {
        for (minutes, share) in [(0, dec!(1)), (15, dec!(1)), (60, dec!(0.75)), (90, dec!(0.54)), (180, dec!(0.14)), (360, dec!(0))] {
            let remaining = FIASP.remaining(Decimal::from(minutes));
            assert!((remaining - share).abs() < dec!(0.01), "{minutes} min: {remaining}");
        }
    }

    #[test]
    fn a_slower_profile_leaves_more_and_ends_after_its_delay_and_duration() {
        let slower = InsulinProfile { key: "slow", name: "Slow", delay_minutes: dec!(15), peak_minutes: dec!(75), duration_minutes: dec!(360) };
        assert!(slower.remaining(dec!(90)) > FIASP.remaining(dec!(90)));
        assert_eq!(slower.remaining(dec!(375)), Decimal::ZERO);
    }
}
