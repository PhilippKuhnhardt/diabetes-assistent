//! Decimal arithmetic with the exact behaviour of Python's `decimal` module in its default context
//! (28 significant digits, ROUND_HALF_EVEN, Emax 999999, Emin -999999, clamp 0, capital `E`).
//!
//! The server's reference implementation computes every glucose, factor and dose value with Python's
//! `Decimal` and stores them as strings, so the Rust core has to produce the very same digits, not only
//! close numbers. This is a port of CPython's `_pydecimal` for the operations the core uses: parsing,
//! `str()`, `+ - * /`, comparison, `quantize`, `normalize`, `exp` and `**`. The coefficient is kept as
//! a digit string as in `_pydecimal`, which keeps the port line by line comparable.

use num_bigint::BigInt;
use num_integer::Integer;
use num_traits::{One, Signed, ToPrimitive, Zero};
use std::cmp::Ordering;
use std::fmt;

const PREC: i64 = 28;
const EMAX: i64 = 999_999;
const EMIN: i64 = -999_999;
const ETINY: i64 = EMIN - PREC + 1;
const ETOP: i64 = EMAX - PREC + 1;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Rounding {
    HalfEven,
    HalfUp,
    Down,
    Up,
    Floor,
    Ceiling,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Kind {
    Finite,
    Infinity,
    NaN,
}

/// A Python-compatible decimal number.
#[derive(Clone, Debug)]
pub struct Decimal {
    sign: u8,
    int: String,
    exp: i64,
    kind: Kind,
}

/// An operation Python's decimal would refuse (it raises an ArithmeticError subclass).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DecimalError {
    InvalidOperation(&'static str),
    DivisionByZero,
    ConversionSyntax,
}

impl fmt::Display for DecimalError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            DecimalError::InvalidOperation(what) => write!(f, "[<class 'decimal.InvalidOperation'>] {what}"),
            DecimalError::DivisionByZero => write!(f, "[<class 'decimal.DivisionByZero'>]"),
            DecimalError::ConversionSyntax => write!(f, "[<class 'decimal.ConversionSyntax'>]"),
        }
    }
}

fn big(digits: &str) -> BigInt {
    digits.parse::<BigInt>().expect("digit string")
}

fn pow10(n: i64) -> BigInt {
    num_traits::pow(BigInt::from(10), n as usize)
}

fn digits_len(n: &BigInt) -> i64 {
    n.abs().to_string().len() as i64
}

fn triple(sign: u8, coefficient: &str, exponent: i64) -> Decimal {
    Decimal { sign, int: coefficient.to_string(), exp: exponent, kind: Kind::Finite }
}

fn all_zeros(s: &str, from: usize) -> bool {
    s[from..].bytes().all(|b| b == b'0')
}

fn exact_half(s: &str, from: usize) -> bool {
    let rest = &s.as_bytes()[from..];
    !rest.is_empty() && rest[0] == b'5' && rest[1..].iter().all(|&b| b == b'0')
}

impl Decimal {
    pub fn zero() -> Decimal {
        triple(0, "0", 0)
    }

    pub fn one() -> Decimal {
        triple(0, "1", 0)
    }

    pub fn nan() -> Decimal {
        Decimal { sign: 0, int: String::new(), exp: 0, kind: Kind::NaN }
    }

    /// `Decimal(str)`: leading/trailing whitespace and underscores are ignored, as in Python.
    pub fn parse(value: &str) -> Result<Decimal, DecimalError> {
        let text: String = value.trim().replace('_', "");
        let bytes = text.as_bytes();
        let mut index = 0;
        let mut sign = 0u8;
        if index < bytes.len() && (bytes[index] == b'+' || bytes[index] == b'-') {
            sign = (bytes[index] == b'-') as u8;
            index += 1;
        }
        let rest = &text[index..];
        let lower = rest.to_ascii_lowercase();
        if lower == "inf" || lower == "infinity" {
            return Ok(Decimal { sign, int: "0".into(), exp: 0, kind: Kind::Infinity });
        }
        if lower.starts_with("nan") || lower.starts_with("snan") {
            let digits = if lower.starts_with("nan") { &lower[3..] } else { &lower[4..] };
            if digits.bytes().all(|b| b.is_ascii_digit()) {
                return Ok(Decimal { sign, int: String::new(), exp: 0, kind: Kind::NaN });
            }
            return Err(DecimalError::ConversionSyntax);
        }
        let mut int_part = String::new();
        let mut frac_part = String::new();
        let mut pos = 0;
        let rb = rest.as_bytes();
        while pos < rb.len() && rb[pos].is_ascii_digit() {
            int_part.push(rb[pos] as char);
            pos += 1;
        }
        if pos < rb.len() && rb[pos] == b'.' {
            pos += 1;
            while pos < rb.len() && rb[pos].is_ascii_digit() {
                frac_part.push(rb[pos] as char);
                pos += 1;
            }
        }
        if int_part.is_empty() && frac_part.is_empty() {
            return Err(DecimalError::ConversionSyntax);
        }
        let mut exp: i64 = 0;
        if pos < rb.len() && (rb[pos] == b'e' || rb[pos] == b'E') {
            pos += 1;
            let start = pos;
            if pos < rb.len() && (rb[pos] == b'+' || rb[pos] == b'-') {
                pos += 1;
            }
            let digits_start = pos;
            while pos < rb.len() && rb[pos].is_ascii_digit() {
                pos += 1;
            }
            if digits_start == pos {
                return Err(DecimalError::ConversionSyntax);
            }
            exp = rest[start..pos].parse::<i64>().map_err(|_| DecimalError::ConversionSyntax)?;
        }
        if pos != rb.len() {
            return Err(DecimalError::ConversionSyntax);
        }
        let all = format!("{int_part}{frac_part}");
        let trimmed = all.trim_start_matches('0');
        let int = if trimmed.is_empty() { "0".to_string() } else { trimmed.to_string() };
        Ok(Decimal { sign, int, exp: exp - frac_part.len() as i64, kind: Kind::Finite })
    }

    pub fn from_i64(value: i64) -> Decimal {
        triple((value < 0) as u8, &value.unsigned_abs().to_string(), 0)
    }

    pub fn is_finite(&self) -> bool {
        self.kind == Kind::Finite
    }

    pub fn is_nan(&self) -> bool {
        self.kind == Kind::NaN
    }

    /// Python's truth value: everything but zero is true (NaN and infinity too).
    pub fn is_truthy(&self) -> bool {
        self.kind != Kind::Finite || self.int != "0"
    }

    pub fn is_zero(&self) -> bool {
        self.kind == Kind::Finite && self.int == "0"
    }

    pub fn is_negative(&self) -> bool {
        self.sign == 1
    }

    pub fn exponent(&self) -> i64 {
        self.exp
    }

    fn nonzero(&self) -> bool {
        self.is_truthy()
    }

    pub fn adjusted(&self) -> i64 {
        if self.kind != Kind::Finite {
            return 0;
        }
        self.exp + self.int.len() as i64 - 1
    }

    fn copy_negate(&self) -> Decimal {
        Decimal { sign: 1 - self.sign, ..self.clone() }
    }

    fn copy_abs(&self) -> Decimal {
        Decimal { sign: 0, ..self.clone() }
    }

    fn rounding_fn(&self, rounding: Rounding, prec: usize) -> i32 {
        let round_down = || if all_zeros(&self.int, prec) { 0 } else { -1 };
        let half_up = || {
            if b"56789".contains(&self.int.as_bytes()[prec]) {
                1
            } else if all_zeros(&self.int, prec) {
                0
            } else {
                -1
            }
        };
        match rounding {
            Rounding::Down => round_down(),
            Rounding::Up => -round_down(),
            Rounding::HalfUp => half_up(),
            Rounding::HalfEven => {
                if exact_half(&self.int, prec) && (prec == 0 || b"02468".contains(&self.int.as_bytes()[prec - 1])) {
                    -1
                } else {
                    half_up()
                }
            }
            Rounding::Ceiling => {
                if self.sign == 1 {
                    round_down()
                } else {
                    -round_down()
                }
            }
            Rounding::Floor => {
                if self.sign == 0 {
                    round_down()
                } else {
                    -round_down()
                }
            }
        }
    }

    /// `_fix` in the default context (half even).
    fn fix(&self) -> Decimal {
        self.fix_with(Rounding::HalfEven)
    }

    fn fix_with(&self, rounding: Rounding) -> Decimal {
        if self.kind != Kind::Finite {
            return self.clone();
        }
        if !self.nonzero() {
            let new_exp = self.exp.max(ETINY).min(EMAX);
            if new_exp != self.exp {
                return triple(self.sign, "0", new_exp);
            }
            return self.clone();
        }
        let mut exp_min = self.int.len() as i64 + self.exp - PREC;
        if exp_min > ETOP {
            return Decimal { sign: self.sign, int: "0".into(), exp: 0, kind: Kind::Infinity };
        }
        let subnormal = exp_min < ETINY;
        if subnormal {
            exp_min = ETINY;
        }
        if self.exp < exp_min {
            let mut digits = self.int.len() as i64 + self.exp - exp_min;
            let mut this = self.clone();
            if digits < 0 {
                this = triple(self.sign, "1", exp_min - 1);
                digits = 0;
            }
            let changed = this.rounding_fn(rounding, digits as usize);
            let mut coeff = if digits == 0 { "0".to_string() } else { this.int[..digits as usize].to_string() };
            if changed > 0 {
                coeff = (big(&coeff) + BigInt::from(1)).to_string();
                if coeff.len() as i64 > PREC {
                    coeff.pop();
                    exp_min += 1;
                }
            }
            if exp_min > ETOP {
                return Decimal { sign: self.sign, int: "0".into(), exp: 0, kind: Kind::Infinity };
            }
            return triple(self.sign, &coeff, exp_min);
        }
        self.clone()
    }

    fn rescale(&self, exp: i64, rounding: Rounding) -> Decimal {
        if self.kind != Kind::Finite {
            return self.clone();
        }
        if !self.nonzero() {
            return triple(self.sign, "0", exp);
        }
        if self.exp >= exp {
            let padded = format!("{}{}", self.int, "0".repeat((self.exp - exp) as usize));
            return triple(self.sign, &padded, exp);
        }
        let mut digits = self.int.len() as i64 + self.exp - exp;
        let mut this = self.clone();
        if digits < 0 {
            this = triple(self.sign, "1", exp - 1);
            digits = 0;
        }
        let changed = this.rounding_fn(rounding, digits as usize);
        let mut coeff = if digits == 0 { "0".to_string() } else { this.int[..digits as usize].to_string() };
        if changed == 1 {
            coeff = (big(&coeff) + BigInt::from(1)).to_string();
        }
        triple(self.sign, &coeff, exp)
    }

    /// `Decimal.quantize(exp, rounding)`.
    pub fn quantize(&self, exp: &Decimal, rounding: Rounding) -> Result<Decimal, DecimalError> {
        if self.kind != Kind::Finite || exp.kind != Kind::Finite {
            if self.is_nan() || exp.is_nan() {
                return Ok(Decimal::nan());
            }
            if exp.kind == Kind::Infinity && self.kind == Kind::Infinity {
                return Ok(self.clone());
            }
            return Err(DecimalError::InvalidOperation("quantize with one INF"));
        }
        if !(ETINY <= exp.exp && exp.exp <= EMAX) {
            return Err(DecimalError::InvalidOperation("target exponent out of bounds in quantize"));
        }
        if !self.nonzero() {
            return Ok(triple(self.sign, "0", exp.exp).fix());
        }
        let adjusted = self.adjusted();
        if adjusted > EMAX {
            return Err(DecimalError::InvalidOperation("exponent of quantize result too large"));
        }
        if adjusted - exp.exp + 1 > PREC {
            return Err(DecimalError::InvalidOperation("quantize result has too many digits"));
        }
        let ans = self.rescale(exp.exp, rounding);
        if ans.adjusted() > EMAX || ans.int.len() as i64 > PREC {
            return Err(DecimalError::InvalidOperation("quantize result has too many digits"));
        }
        Ok(ans.fix())
    }

    /// `Decimal.normalize()`: strip trailing zeros.
    pub fn normalize(&self) -> Decimal {
        if self.kind != Kind::Finite {
            return self.clone();
        }
        let dup = self.fix();
        if dup.kind == Kind::Infinity {
            return dup;
        }
        if !dup.nonzero() {
            return triple(dup.sign, "0", 0);
        }
        let mut end = dup.int.len();
        let mut exp = dup.exp;
        while dup.int.as_bytes()[end - 1] == b'0' && exp < EMAX {
            exp += 1;
            end -= 1;
        }
        triple(dup.sign, &dup.int[..end], exp)
    }

    fn cmp_finite(&self, other: &Decimal) -> Ordering {
        if self.kind != Kind::Finite || other.kind != Kind::Finite {
            let a = if self.kind == Kind::Infinity { if self.sign == 1 { -1 } else { 1 } } else { 0 };
            let b = if other.kind == Kind::Infinity { if other.sign == 1 { -1 } else { 1 } } else { 0 };
            return a.cmp(&b);
        }
        if !self.nonzero() {
            if !other.nonzero() {
                return Ordering::Equal;
            }
            return if other.sign == 1 { Ordering::Greater } else { Ordering::Less };
        }
        if !other.nonzero() {
            return if self.sign == 1 { Ordering::Less } else { Ordering::Greater };
        }
        if other.sign < self.sign {
            return Ordering::Less;
        }
        if self.sign < other.sign {
            return Ordering::Greater;
        }
        let flip = |o: Ordering| if self.sign == 1 { o.reverse() } else { o };
        let (a, b) = (self.adjusted(), other.adjusted());
        if a == b {
            let pa = format!("{}{}", self.int, "0".repeat((self.exp - other.exp).max(0) as usize));
            let pb = format!("{}{}", other.int, "0".repeat((other.exp - self.exp).max(0) as usize));
            flip(pa.cmp(&pb))
        } else {
            flip(a.cmp(&b))
        }
    }

    pub fn add(&self, other: &Decimal) -> Decimal {
        if self.kind != Kind::Finite || other.kind != Kind::Finite {
            if self.is_nan() || other.is_nan() {
                return Decimal::nan();
            }
            if self.kind == Kind::Infinity {
                if other.kind == Kind::Infinity && self.sign != other.sign {
                    return Decimal::nan();
                }
                return self.clone();
            }
            return other.clone();
        }
        let mut exp = self.exp.min(other.exp);
        if !self.nonzero() && !other.nonzero() {
            let sign = self.sign.min(other.sign);
            return triple(sign, "0", exp).fix();
        }
        if !self.nonzero() {
            exp = exp.max(other.exp - PREC - 1);
            return other.rescale(exp, Rounding::HalfEven).fix();
        }
        if !other.nonzero() {
            exp = exp.max(self.exp - PREC - 1);
            return self.rescale(exp, Rounding::HalfEven).fix();
        }
        // _normalize
        let (mut op1, mut op2) = ((self.sign, big(&self.int), self.exp), (other.sign, big(&other.int), other.exp));
        {
            let (tmp, oth) = if op1.2 < op2.2 { (&mut op2, &mut op1) } else { (&mut op1, &mut op2) };
            let tmp_len = digits_len(&tmp.1);
            let other_len = digits_len(&oth.1);
            let e = tmp.2 + (-1i64).min(tmp_len - PREC - 2);
            if other_len + oth.2 - 1 < e {
                oth.1 = BigInt::one();
                oth.2 = e;
            }
            tmp.1 = &tmp.1 * pow10(tmp.2 - oth.2);
            tmp.2 = oth.2;
        }
        let result_sign;
        if op1.0 != op2.0 {
            if op1.1 == op2.1 {
                return triple(0, "0", exp).fix();
            }
            if op1.1 < op2.1 {
                std::mem::swap(&mut op1, &mut op2);
            }
            if op1.0 == 1 {
                result_sign = 1;
                let s = op1.0;
                op1.0 = op2.0;
                op2.0 = s;
            } else {
                result_sign = 0;
            }
        } else if op1.0 == 1 {
            result_sign = 1;
            op1.0 = 0;
            op2.0 = 0;
        } else {
            result_sign = 0;
        }
        let int = if op2.0 == 0 { &op1.1 + &op2.1 } else { &op1.1 - &op2.1 };
        triple(result_sign, &int.to_string(), op1.2).fix()
    }

    pub fn sub(&self, other: &Decimal) -> Decimal {
        self.add(&other.copy_negate())
    }

    pub fn mul(&self, other: &Decimal) -> Decimal {
        let sign = self.sign ^ other.sign;
        if self.kind != Kind::Finite || other.kind != Kind::Finite {
            if self.is_nan() || other.is_nan() {
                return Decimal::nan();
            }
            if (self.kind == Kind::Infinity && !other.nonzero()) || (other.kind == Kind::Infinity && !self.nonzero()) {
                return Decimal::nan();
            }
            return Decimal { sign, int: "0".into(), exp: 0, kind: Kind::Infinity };
        }
        let exp = self.exp + other.exp;
        if !self.nonzero() || !other.nonzero() {
            return triple(sign, "0", exp).fix();
        }
        if self.int == "1" {
            return triple(sign, &other.int, exp).fix();
        }
        if other.int == "1" {
            return triple(sign, &self.int, exp).fix();
        }
        let product = big(&self.int) * big(&other.int);
        triple(sign, &product.to_string(), exp).fix()
    }

    /// `self / other`; Python raises DivisionByZero (or InvalidOperation for 0/0).
    pub fn checked_div(&self, other: &Decimal) -> Result<Decimal, DecimalError> {
        let sign = self.sign ^ other.sign;
        if self.kind != Kind::Finite || other.kind != Kind::Finite {
            if self.is_nan() || other.is_nan() {
                return Ok(Decimal::nan());
            }
            if self.kind == Kind::Infinity && other.kind == Kind::Infinity {
                return Err(DecimalError::InvalidOperation("(+-)INF/(+-)INF"));
            }
            if self.kind == Kind::Infinity {
                return Ok(Decimal { sign, int: "0".into(), exp: 0, kind: Kind::Infinity });
            }
            return Ok(triple(sign, "0", ETINY));
        }
        if !other.nonzero() {
            if !self.nonzero() {
                return Err(DecimalError::InvalidOperation("0 / 0"));
            }
            return Err(DecimalError::DivisionByZero);
        }
        let (coeff, exp);
        if !self.nonzero() {
            exp = self.exp - other.exp;
            coeff = BigInt::zero();
        } else {
            let shift = other.int.len() as i64 - self.int.len() as i64 + PREC + 1;
            let mut e = self.exp - other.exp - shift;
            let (op1, op2) = (big(&self.int), big(&other.int));
            let (mut c, remainder) = if shift >= 0 {
                (&op1 * pow10(shift)).div_rem(&op2)
            } else {
                op1.div_rem(&(&op2 * pow10(-shift)))
            };
            if !remainder.is_zero() {
                if (&c % 5u32).is_zero() {
                    c += 1;
                }
            } else {
                let ideal = self.exp - other.exp;
                while e < ideal && (&c % 10u32).is_zero() {
                    c /= 10u32;
                    e += 1;
                }
            }
            coeff = c;
            exp = e;
        }
        Ok(triple(sign, &coeff.to_string(), exp).fix())
    }

    pub fn neg(&self) -> Decimal {
        if self.is_nan() {
            return self.clone();
        }
        if !self.nonzero() {
            return self.copy_abs().fix();
        }
        self.copy_negate().fix()
    }

    pub fn abs(&self) -> Decimal {
        if self.is_nan() {
            return self.clone();
        }
        if self.sign == 1 {
            self.neg()
        } else if !self.nonzero() {
            self.copy_abs().fix()
        } else {
            self.fix()
        }
    }

    fn is_integer(&self) -> bool {
        if self.kind != Kind::Finite {
            return false;
        }
        if self.exp >= 0 {
            return true;
        }
        let cut = self.int.len() as i64 + self.exp;
        if cut <= 0 {
            return self.int.bytes().all(|b| b == b'0');
        }
        all_zeros(&self.int, cut as usize)
    }

    fn is_even(&self) -> bool {
        if !self.nonzero() || self.exp > 0 {
            return true;
        }
        let index = self.int.len() as i64 - 1 + self.exp;
        if index < 0 {
            return true;
        }
        b"02468".contains(&self.int.as_bytes()[index as usize])
    }

    /// `int(Decimal)`: truncation towards zero.
    pub fn to_int(&self) -> BigInt {
        let s: BigInt = if self.sign == 1 { BigInt::from(-1) } else { BigInt::one() };
        if self.exp >= 0 {
            s * big(&self.int) * pow10(self.exp)
        } else {
            let cut = self.int.len() as i64 + self.exp;
            if cut <= 0 {
                BigInt::zero()
            } else {
                s * big(&self.int[..cut as usize])
            }
        }
    }

    pub fn to_i64(&self) -> i64 {
        self.to_int().to_i64().expect("integer fits")
    }

    /// `float(Decimal)`, which Python computes from the string form.
    pub fn to_f64(&self) -> f64 {
        match self.kind {
            Kind::NaN => f64::NAN,
            Kind::Infinity => {
                if self.sign == 1 {
                    f64::NEG_INFINITY
                } else {
                    f64::INFINITY
                }
            }
            Kind::Finite => self.to_string().parse::<f64>().expect("float"),
        }
    }

    fn log10_exp_bound(&self) -> i64 {
        let adj = self.exp + self.int.len() as i64 - 1;
        if adj >= 1 {
            return adj.to_string().len() as i64 - 1;
        }
        if adj <= -2 {
            return (-1 - adj).to_string().len() as i64 - 1;
        }
        let c = big(&self.int);
        let e = self.exp;
        if adj == 0 {
            let num = (&c - pow10(-e)).to_string();
            let den = (BigInt::from(231) * &c).to_string();
            return num.len() as i64 - den.len() as i64 - (num < den) as i64 + 2;
        }
        let num = (pow10(-e) - &c).to_string();
        num.len() as i64 + e - (num.as_str() < "231") as i64 - 1
    }

    /// `Decimal.exp()`, correctly rounded.
    pub fn exp(&self) -> Decimal {
        if self.is_nan() {
            return self.clone();
        }
        if self.kind == Kind::Infinity {
            return if self.sign == 1 { Decimal::zero() } else { self.clone() };
        }
        if !self.nonzero() {
            return Decimal::one();
        }
        let p = PREC;
        let adj = self.adjusted();
        let ans = if self.sign == 0 && adj > ((EMAX + 1) * 3).to_string().len() as i64 {
            triple(0, "1", EMAX + 1)
        } else if self.sign == 1 && adj > ((-ETINY + 1) * 3).to_string().len() as i64 {
            triple(0, "1", ETINY - 1)
        } else if self.sign == 0 && adj < -p {
            triple(0, &format!("1{}1", "0".repeat((p - 1) as usize)), -p)
        } else if self.sign == 1 && adj < -p - 1 {
            triple(0, &"9".repeat((p + 1) as usize), -p - 1)
        } else {
            let mut c = big(&self.int);
            if self.sign == 1 {
                c = -c;
            }
            let e = self.exp;
            let mut extra = 3;
            loop {
                let (coeff, exp) = dexp(&c, e, p + extra);
                let len = coeff.to_string().len() as i64;
                if !(&coeff % (BigInt::from(5) * pow10(len - p - 1))).is_zero() {
                    break triple(0, &coeff.to_string(), exp);
                }
                extra += 3;
            }
        };
        ans.fix()
    }

    /// `self ** other`.
    pub fn pow(&self, other: &Decimal) -> Result<Decimal, DecimalError> {
        if self.is_nan() || other.is_nan() {
            return Ok(Decimal::nan());
        }
        if !other.nonzero() {
            if !self.nonzero() {
                return Err(DecimalError::InvalidOperation("0 ** 0"));
            }
            return Ok(Decimal::one());
        }
        let mut result_sign = 0u8;
        let mut base = self.clone();
        if self.sign == 1 {
            if other.is_integer() {
                if !other.is_even() {
                    result_sign = 1;
                }
            } else if self.nonzero() {
                return Err(DecimalError::InvalidOperation("x ** y with x negative and y not an integer"));
            }
            base = self.copy_negate();
        }
        if !base.nonzero() {
            if other.sign == 0 {
                return Ok(triple(result_sign, "0", 0));
            }
            return Ok(Decimal { sign: result_sign, int: "0".into(), exp: 0, kind: Kind::Infinity });
        }
        if base.kind == Kind::Infinity {
            if other.sign == 0 {
                return Ok(Decimal { sign: result_sign, int: "0".into(), exp: 0, kind: Kind::Infinity });
            }
            return Ok(triple(result_sign, "0", 0));
        }
        if base.cmp_finite(&Decimal::one()) == Ordering::Equal {
            let exp = if other.is_integer() {
                let multiplier = if other.sign == 1 {
                    0
                } else if other.cmp_finite(&Decimal::from_i64(PREC)) == Ordering::Greater {
                    PREC
                } else {
                    other.to_i64()
                };
                (base.exp * multiplier).max(1 - PREC)
            } else {
                1 - PREC
            };
            return Ok(triple(result_sign, &format!("1{}", "0".repeat((-exp).max(0) as usize)), exp));
        }
        let self_adj = base.adjusted();
        if other.kind == Kind::Infinity {
            if (other.sign == 0) == (self_adj < 0) {
                return Ok(triple(result_sign, "0", 0));
            }
            return Ok(Decimal { sign: result_sign, int: "0".into(), exp: 0, kind: Kind::Infinity });
        }
        let mut ans: Option<Decimal> = None;
        let mut exact = false;
        let bound = base.log10_exp_bound() + other.adjusted();
        if (self_adj >= 0) == (other.sign == 0) {
            if bound >= EMAX.to_string().len() as i64 {
                ans = Some(triple(result_sign, "1", EMAX + 1));
            }
        } else if bound >= (-ETINY).to_string().len() as i64 {
            ans = Some(triple(result_sign, "1", ETINY - 1));
        }
        if ans.is_none() {
            if let Some(found) = power_exact(&base, other, PREC + 1) {
                let found = if result_sign == 1 { triple(1, &found.int, found.exp) } else { found };
                ans = Some(found);
                exact = true;
            }
        }
        let mut ans = match ans {
            Some(found) => found,
            None => {
                let p = PREC;
                let xc = big(&base.int);
                let xe = base.exp;
                let mut yc = big(&other.int);
                let ye = other.exp;
                if other.sign == 1 {
                    yc = -yc;
                }
                let mut extra = 3;
                loop {
                    let (coeff, exp) = dpower(&xc, xe, &yc, ye, p + extra);
                    let len = coeff.to_string().len() as i64;
                    if !(&coeff % (BigInt::from(5) * pow10(len - p - 1))).is_zero() {
                        break triple(result_sign, &coeff.to_string(), exp);
                    }
                    extra += 3;
                }
            }
        };
        if exact && !other.is_integer() && ans.int.len() as i64 <= PREC {
            let expdiff = PREC + 1 - ans.int.len() as i64;
            ans = triple(ans.sign, &format!("{}{}", ans.int, "0".repeat(expdiff as usize)), ans.exp - expdiff);
        }
        Ok(ans.fix())
    }

    /// Python's `round(Decimal)`: nearest integer, ties to even.
    pub fn round_int(&self) -> BigInt {
        self.rescale(0, Rounding::HalfEven).to_int()
    }

    /// `format(value, ".Nf")`: fixed point with N places, half even.
    pub fn format_fixed(&self, places: i64) -> String {
        let rounded = self.rescale(-places, Rounding::HalfEven);
        let digits = rounded.int.clone();
        let sign = if rounded.sign == 1 { "-" } else { "" };
        if places <= 0 {
            return format!("{sign}{digits}");
        }
        let width = places as usize + 1;
        let padded = if digits.len() < width { format!("{}{}", "0".repeat(width - digits.len()), digits) } else { digits };
        let split = padded.len() - places as usize;
        format!("{sign}{}.{}", &padded[..split], &padded[split..])
    }

    /// `format(value, "f")`: plain notation without exponent.
    pub fn format_plain(&self) -> String {
        if self.kind != Kind::Finite {
            return self.to_string();
        }
        let sign = if self.sign == 1 { "-" } else { "" };
        if self.exp >= 0 {
            return format!("{sign}{}{}", self.int, "0".repeat(self.exp as usize));
        }
        let places = (-self.exp) as usize;
        let padded = if self.int.len() <= places {
            format!("{}{}", "0".repeat(places + 1 - self.int.len()), self.int)
        } else {
            self.int.clone()
        };
        let split = padded.len() - places;
        format!("{sign}{}.{}", &padded[..split], &padded[split..])
    }
}

fn nbits(n: &BigInt) -> i64 {
    n.abs().bits() as i64
}

fn decimal_lshift_exact(n: &BigInt, e: i64) -> Option<BigInt> {
    if n.is_zero() {
        return Some(BigInt::zero());
    }
    if e >= 0 {
        return Some(n * pow10(e));
    }
    let s = n.abs().to_string();
    let val_n = (s.len() - s.trim_end_matches('0').len()) as i64;
    if val_n < -e {
        None
    } else {
        Some(n / pow10(-e))
    }
}

fn sqrt_nearest(n: &BigInt, a: &BigInt) -> BigInt {
    let mut a = a.clone();
    let mut b = BigInt::zero();
    while a != b {
        b = a.clone();
        // a = a - -n//a >> 1  (Python: (a - (-n // a)) >> 1, floor division)
        let q = (-n).div_floor(&a);
        a = (&a - q) >> 1usize;
    }
    a
}

fn rshift_nearest(x: &BigInt, shift: i64) -> BigInt {
    let shift = shift as usize;
    let b = BigInt::one() << shift;
    let q = x >> shift; // floor shift for negatives, as in Python
    let low = x & (&b - 1u32);
    let bump = (BigInt::from(2) * low + (&q & BigInt::one())) > b;
    q + if bump { 1 } else { 0 }
}

fn div_nearest(a: &BigInt, b: &BigInt) -> BigInt {
    let (q, r) = a.div_mod_floor(b);
    let bump = (BigInt::from(2) * r + (&q & BigInt::one())) > *b;
    q + if bump { 1 } else { 0 }
}

/// Python's `-int(-x // y)` for positive y: ceiling division of small integers.
fn ceil_div(x: i64, y: i64) -> i64 {
    -((-x).div_euclid(y))
}

fn ilog(x: &BigInt, m: &BigInt, l: i64) -> BigInt {
    let mut y = x - m;
    let mut r: i64 = 0;
    loop {
        let cond = if r <= l {
            (y.abs() << ((l - r) as usize)) >= *m
        } else {
            (y.abs() >> ((r - l) as usize)) >= *m
        };
        if !cond {
            break;
        }
        let inner = m * (m + rshift_nearest(&y, r));
        y = div_nearest(&((m * &y) << 1usize), &(m + sqrt_nearest(&inner, m)));
        r += 1;
    }
    let t = ceil_div(10 * m.to_string().len() as i64, 3 * l);
    let yshift = rshift_nearest(&y, r);
    let mut w = div_nearest(m, &BigInt::from(t));
    for k in (1..t).rev() {
        w = div_nearest(m, &BigInt::from(k)) - div_nearest(&(&yshift * &w), m);
    }
    div_nearest(&(w * y), m)
}

const LOG10_DIGITS: &str = "23025850929940456840179914546843642076011014886";

fn log10_digits(p: i64) -> BigInt {
    if (p as usize) < LOG10_DIGITS.len() {
        return big(&LOG10_DIGITS[..(p as usize + 1)]);
    }
    let mut extra = 3;
    let digits = loop {
        let m = pow10(p + extra + 2);
        let digits = div_nearest(&ilog(&(BigInt::from(10) * &m), &m, 8), &BigInt::from(100)).to_string();
        if !digits[digits.len() - extra as usize..].bytes().all(|b| b == b'0') {
            break digits;
        }
        extra += 3;
    };
    let trimmed = digits.trim_end_matches('0');
    let trimmed = &trimmed[..trimmed.len() - 1];
    big(&trimmed[..(p as usize + 1)])
}

fn dlog(c: &BigInt, e: i64, p: i64) -> BigInt {
    let p = p + 2;
    let l = c.to_string().len() as i64;
    let f = e + l - ((e + l >= 1) as i64);
    let log_d = if p > 0 {
        let k = e + p - f;
        let c = if k >= 0 { c * pow10(k) } else { div_nearest(c, &pow10(-k)) };
        ilog(&c, &pow10(p), 8)
    } else {
        BigInt::zero()
    };
    let f_log_ten = if f != 0 {
        let extra = f.abs().to_string().len() as i64 - 1;
        if p + extra >= 0 {
            div_nearest(&(BigInt::from(f) * log10_digits(p + extra)), &pow10(extra))
        } else {
            BigInt::zero()
        }
    } else {
        BigInt::zero()
    };
    div_nearest(&(f_log_ten + log_d), &BigInt::from(100))
}

fn iexp(x: &BigInt, m: &BigInt, l: i64) -> BigInt {
    let r = nbits(&((x << (l as usize)).div_floor(m)));
    let t = ceil_div(10 * m.to_string().len() as i64, 3 * l);
    let mut y = div_nearest(x, &BigInt::from(t));
    let mshift = m << (r as usize);
    for i in (1..t).rev() {
        y = div_nearest(&(x * (&mshift + &y)), &(&mshift * BigInt::from(i)));
    }
    for k in (0..r).rev() {
        let mshift = m << ((k + 2) as usize);
        y = div_nearest(&(&y * (&y + &mshift)), &mshift);
    }
    m + y
}

fn dexp(c: &BigInt, e: i64, p: i64) -> (BigInt, i64) {
    let p = p + 2;
    let extra = (e + c.abs().to_string().len() as i64 - 1).max(0);
    let q = p + extra;
    let shift = e + q;
    let cshift = if shift >= 0 { c * pow10(shift) } else { c.div_floor(&pow10(-shift)) };
    let (quot, rem) = cshift.div_mod_floor(&log10_digits(q));
    let rem = div_nearest(&rem, &pow10(extra));
    let quot = quot.to_i64().expect("exponent fits");
    (div_nearest(&iexp(&rem, &pow10(p), 8), &BigInt::from(1000)), quot - p + 3)
}

fn dpower(xc: &BigInt, xe: i64, yc: &BigInt, ye: i64, p: i64) -> (BigInt, i64) {
    let b = yc.abs().to_string().len() as i64 + ye;
    let lxc = dlog(xc, xe, p + b + 1);
    let shift = ye - b;
    let pc = if shift >= 0 { &lxc * yc * pow10(shift) } else { div_nearest(&(&lxc * yc), &pow10(-shift)) };
    if pc.is_zero() {
        let above_one = (xc.to_string().len() as i64 + xe >= 1) == yc.is_positive();
        if above_one {
            (pow10(p - 1) + 1, 1 - p)
        } else {
            (pow10(p) - 1, -p)
        }
    } else {
        let (coeff, exp) = dexp(&pc, -(p + 1), p + 1);
        (div_nearest(&coeff, &BigInt::from(10)), exp + 1)
    }
}

fn log10_lb(c: &BigInt) -> i64 {
    let s = c.to_string();
    let correction = match s.as_bytes()[0] {
        b'1' => 100,
        b'2' => 70,
        b'3' => 53,
        b'4' => 40,
        b'5' => 31,
        b'6' => 23,
        b'7' => 16,
        b'8' => 10,
        _ => 5,
    };
    100 * s.len() as i64 - correction
}

fn power_exact(base: &Decimal, other: &Decimal, p: i64) -> Option<Decimal> {
    let mut xc = big(&base.int);
    let mut xe = base.exp;
    while (&xc % 10u32).is_zero() {
        xc /= 10u32;
        xe += 1;
    }
    let mut yc = big(&other.int);
    let mut ye = other.exp;
    while (&yc % 10u32).is_zero() {
        yc /= 10u32;
        ye += 1;
    }
    let y_sign = other.sign;
    if xc.is_one() {
        let mut xe_b = BigInt::from(xe) * &yc;
        while (&xe_b % 10u32).is_zero() && !xe_b.is_zero() {
            xe_b /= 10u32;
            ye += 1;
        }
        if ye < 0 {
            return None;
        }
        let mut exponent = (xe_b * pow10(ye)).to_i64()?;
        if y_sign == 1 {
            exponent = -exponent;
        }
        let zeros = if other.is_integer() && other.sign == 0 {
            let ideal = base.exp * other.to_i64();
            (exponent - ideal).min(p - 1)
        } else {
            0
        };
        return Some(triple(0, &format!("1{}", "0".repeat(zeros.max(0) as usize)), exponent - zeros));
    }
    if y_sign == 1 {
        let last = (&xc % 10u32).to_i64().unwrap();
        let e;
        let new_xc;
        if [2, 4, 6, 8].contains(&last) {
            if (&xc & (-&xc)) != xc {
                return None;
            }
            let e0 = nbits(&xc) - 1;
            let emax = p * 93 / 65;
            if ye >= emax.to_string().len() as i64 {
                return None;
            }
            let e1 = decimal_lshift_exact(&(BigInt::from(e0) * &yc), ye)?;
            let xe1 = decimal_lshift_exact(&(BigInt::from(xe) * &yc), ye)?;
            let e1 = e1.to_i64()?;
            if e1 > emax {
                return None;
            }
            new_xc = num_traits::pow(BigInt::from(5), e1 as usize);
            e = e1;
            xe = xe1.to_i64()?;
        } else if last == 5 {
            let mut e0 = nbits(&xc) * 28 / 65;
            let (mut q, remainder) = num_traits::pow(BigInt::from(5), e0 as usize).div_rem(&xc);
            if !remainder.is_zero() {
                return None;
            }
            while (&q % 5u32).is_zero() {
                q /= 5u32;
                e0 -= 1;
            }
            let emax = p * 10 / 3;
            if ye >= emax.to_string().len() as i64 {
                return None;
            }
            let e1 = decimal_lshift_exact(&(BigInt::from(e0) * &yc), ye)?.to_i64()?;
            let xe1 = decimal_lshift_exact(&(BigInt::from(xe) * &yc), ye)?.to_i64()?;
            if e1 > emax {
                return None;
            }
            new_xc = num_traits::pow(BigInt::from(2), e1 as usize);
            e = e1;
            xe = xe1;
        } else {
            return None;
        }
        let strxc = new_xc.to_string();
        if strxc.len() as i64 > p {
            return None;
        }
        return Some(triple(0, &strxc, -e - xe));
    }
    let (m, n): (BigInt, BigInt);
    let xc_bits = nbits(&xc);
    if ye >= 0 {
        m = &yc * pow10(ye);
        n = BigInt::one();
    } else {
        if xe != 0 && (&yc * BigInt::from(xe)).abs().to_string().len() as i64 <= -ye {
            return None;
        }
        if (yc.abs() * BigInt::from(xc_bits)).to_string().len() as i64 <= -ye {
            return None;
        }
        let mut mm = yc.clone();
        let mut nn = pow10(-ye);
        while (&mm % 2u32).is_zero() && (&nn % 2u32).is_zero() {
            mm /= 2u32;
            nn /= 2u32;
        }
        while (&mm % 5u32).is_zero() && (&nn % 5u32).is_zero() {
            mm /= 5u32;
            nn /= 5u32;
        }
        m = mm;
        n = nn;
    }
    if n > BigInt::one() {
        let n_i = n.to_i64()?;
        if xc_bits <= n_i {
            return None;
        }
        let (q, rem) = xe.div_mod_floor(&n_i);
        if rem != 0 {
            return None;
        }
        xe = q;
        let mut a = BigInt::one() << (ceil_div(nbits(&xc), n_i) as usize);
        let (q, r) = loop {
            let (q, r) = xc.div_mod_floor(&num_traits::pow(a.clone(), (n_i - 1) as usize));
            if a <= q {
                break (q, r);
            }
            a = (&a * BigInt::from(n_i - 1) + q) / BigInt::from(n_i);
        };
        if !(a == q && r.is_zero()) {
            return None;
        }
        xc = a;
    }
    let m_i = m.to_i64()?;
    if xc > BigInt::one() && m_i > p * 100 / log10_lb(&xc) {
        return None;
    }
    xc = num_traits::pow(xc, m_i as usize);
    xe *= m_i;
    let str_xc = xc.to_string();
    if str_xc.len() as i64 > p {
        return None;
    }
    let zeros = if other.is_integer() && other.sign == 0 {
        let ideal = base.exp * other.to_i64();
        (xe - ideal).min(p - str_xc.len() as i64)
    } else {
        0
    };
    Some(triple(0, &format!("{}{}", str_xc, "0".repeat(zeros.max(0) as usize)), xe - zeros))
}

impl fmt::Display for Decimal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let sign = if self.sign == 1 { "-" } else { "" };
        match self.kind {
            Kind::Infinity => return write!(f, "{sign}Infinity"),
            Kind::NaN => return write!(f, "{sign}NaN"),
            Kind::Finite => {}
        }
        let leftdigits = self.exp + self.int.len() as i64;
        let dotplace = if self.exp <= 0 && leftdigits > -6 { leftdigits } else { 1 };
        let (intpart, fracpart) = if dotplace <= 0 {
            ("0".to_string(), format!(".{}{}", "0".repeat((-dotplace) as usize), self.int))
        } else if dotplace >= self.int.len() as i64 {
            (format!("{}{}", self.int, "0".repeat((dotplace - self.int.len() as i64) as usize)), String::new())
        } else {
            (self.int[..dotplace as usize].to_string(), format!(".{}", &self.int[dotplace as usize..]))
        };
        let exp = if leftdigits == dotplace { String::new() } else { format!("E{:+}", leftdigits - dotplace) };
        write!(f, "{sign}{intpart}{fracpart}{exp}")
    }
}

impl PartialEq for Decimal {
    fn eq(&self, other: &Decimal) -> bool {
        if self.is_nan() || other.is_nan() {
            return false;
        }
        self.cmp_finite(other) == Ordering::Equal
    }
}

impl PartialOrd for Decimal {
    fn partial_cmp(&self, other: &Decimal) -> Option<Ordering> {
        if self.is_nan() || other.is_nan() {
            return None;
        }
        Some(self.cmp_finite(other))
    }
}

impl std::ops::Add for &Decimal {
    type Output = Decimal;
    fn add(self, other: &Decimal) -> Decimal {
        Decimal::add(self, other)
    }
}

impl std::ops::Sub for &Decimal {
    type Output = Decimal;
    fn sub(self, other: &Decimal) -> Decimal {
        Decimal::sub(self, other)
    }
}

impl std::ops::Mul for &Decimal {
    type Output = Decimal;
    fn mul(self, other: &Decimal) -> Decimal {
        Decimal::mul(self, other)
    }
}

impl std::ops::Div for &Decimal {
    type Output = Decimal;
    /// Panics like Python raises on division by zero; use `checked_div` where Python code catches it.
    fn div(self, other: &Decimal) -> Decimal {
        match self.checked_div(other) {
            Ok(value) => value,
            Err(error) => std::panic::panic_any(error),
        }
    }
}

impl std::ops::Neg for &Decimal {
    type Output = Decimal;
    fn neg(self) -> Decimal {
        Decimal::neg(self)
    }
}

macro_rules! owned_ops {
    ($($trait:ident $method:ident),*) => {$(
        impl std::ops::$trait for Decimal {
            type Output = Decimal;
            fn $method(self, other: Decimal) -> Decimal { (&self).$method(&other) }
        }
        impl std::ops::$trait<&Decimal> for Decimal {
            type Output = Decimal;
            fn $method(self, other: &Decimal) -> Decimal { (&self).$method(other) }
        }
        impl std::ops::$trait<Decimal> for &Decimal {
            type Output = Decimal;
            fn $method(self, other: Decimal) -> Decimal { self.$method(&other) }
        }
        impl std::ops::$trait<i64> for Decimal {
            type Output = Decimal;
            fn $method(self, other: i64) -> Decimal { (&self).$method(&Decimal::from_i64(other)) }
        }
        impl std::ops::$trait<i64> for &Decimal {
            type Output = Decimal;
            fn $method(self, other: i64) -> Decimal { self.$method(&Decimal::from_i64(other)) }
        }
        impl std::ops::$trait<Decimal> for i64 {
            type Output = Decimal;
            fn $method(self, other: Decimal) -> Decimal { (&Decimal::from_i64(self)).$method(&other) }
        }
        impl std::ops::$trait<&Decimal> for i64 {
            type Output = Decimal;
            fn $method(self, other: &Decimal) -> Decimal { (&Decimal::from_i64(self)).$method(other) }
        }
    )*};
}
owned_ops!(Add add, Sub sub, Mul mul, Div div);

impl std::ops::Neg for Decimal {
    type Output = Decimal;
    fn neg(self) -> Decimal {
        Decimal::neg(&self)
    }
}

impl From<i64> for Decimal {
    fn from(value: i64) -> Decimal {
        Decimal::from_i64(value)
    }
}

impl std::str::FromStr for Decimal {
    type Err = DecimalError;
    fn from_str(value: &str) -> Result<Decimal, DecimalError> {
        Decimal::parse(value)
    }
}

impl std::iter::Sum for Decimal {
    /// Python's `sum(values, Decimal(0))`.
    fn sum<I: Iterator<Item = Decimal>>(iter: I) -> Decimal {
        iter.fold(Decimal::zero(), |acc, value| acc + value)
    }
}

/// `Decimal(text)` for a literal known to be valid.
pub fn d(text: &str) -> Decimal {
    Decimal::parse(text).expect("decimal literal")
}

/// Python's `max(a, b)`: the first argument unless the second is greater.
pub fn max(a: Decimal, b: Decimal) -> Decimal {
    if b > a {
        b
    } else {
        a
    }
}

/// Python's `min(a, b)`: the first argument unless the second is smaller.
pub fn min(a: Decimal, b: Decimal) -> Decimal {
    if b < a {
        b
    } else {
        a
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strings() {
        for (text, shown) in [("1.20", "1.20"), ("0.0000001", "1E-7"), ("1E+2", "1E+2"), ("-0", "-0"), ("100", "100"),
                              ("0.000001", "0.000001"), ("12.3e-10", "1.23E-9")] {
            assert_eq!(d(text).to_string(), shown);
        }
    }

    #[test]
    fn arithmetic() {
        assert_eq!((d("1") / d("3")).to_string(), "0.3333333333333333333333333333");
        assert_eq!((d("10") / d("5")).to_string(), "2");
        assert_eq!((d("1") / d("2")).to_string(), "0.5");
        assert_eq!((d("1.20") * d("2")).to_string(), "2.40");
        assert_eq!((d("0.5").pow(&d("2")).unwrap()).to_string(), "0.25");
        assert_eq!((d("0.5").pow(&d("0.5")).unwrap()).to_string(), "0.7071067811865475244008443621");
        assert_eq!(d("-1").exp().to_string(), "0.3678794411714423215955237702");
    }
}
