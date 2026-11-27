//! Reads "op a b" lines on stdin and prints Python-str results, for parity checks against Python's decimal.
use diabetes_core::decimal::{Decimal, Rounding};
use std::io::{self, BufRead, Write};

fn main() {
    let stdin = io::stdin();
    let mut out = io::BufWriter::new(io::stdout());
    for line in stdin.lock().lines() {
        let line = line.unwrap();
        let parts: Vec<&str> = line.split(' ').collect();
        let a = Decimal::parse(parts[1]).unwrap();
        let b = parts.get(2).map(|t| Decimal::parse(t).unwrap());
        let result = match parts[0] {
            "add" => a.add(b.as_ref().unwrap()).to_string(),
            "sub" => a.sub(b.as_ref().unwrap()).to_string(),
            "mul" => a.mul(b.as_ref().unwrap()).to_string(),
            "div" => a.checked_div(b.as_ref().unwrap()).map(|v| v.to_string()).unwrap_or_else(|_| "ERR".into()),
            "pow" => a.pow(b.as_ref().unwrap()).map(|v| v.to_string()).unwrap_or_else(|_| "ERR".into()),
            "exp" => a.exp().to_string(),
            "norm" => a.normalize().to_string(),
            "qhu" => a.quantize(b.as_ref().unwrap(), Rounding::HalfUp).map(|v| v.to_string()).unwrap_or_else(|_| "ERR".into()),
            "qhe" => a.quantize(b.as_ref().unwrap(), Rounding::HalfEven).map(|v| v.to_string()).unwrap_or_else(|_| "ERR".into()),
            "str" => a.to_string(),
            "f2" => a.format_fixed(2),
            "fp" => a.format_plain(),
            "flt" => format!("{:?}", a.to_f64()),
            "cmp" => format!("{:?}", a.partial_cmp(b.as_ref().unwrap())),
            "int" => a.to_int().to_string(),
            "round" => a.round_int().to_string(),
            other => panic!("{other}"),
        };
        writeln!(out, "{result}").unwrap();
    }
}
