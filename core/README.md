# Rust calculation core (experiment)

A port of the app's calculation core to Rust, so a native client (iPhone app, widget, Watch through the
phone) can compute everything offline with the same rules as the server. The Python code in `app/`
stays the reference; nothing in the server uses this crate yet.

## What is ported

| Rust module | Python reference |
| --- | --- |
| `calculation` | `app/calculation.py`: meal window, pre-bolus, endpoint, meal factor |
| `episodes` | `app/episodes.py`: normalizing Nightscout records, snack grouping, meals of a day |
| `on_board` | `app/on_board.py`: "Wirkt noch" (insulin, stomach, surcharge, sport) |
| `dosing` | `app/dosing.py`: dose options |
| `split`, `correction_scale`, `fpe` | split bolus, correction scale and dose slider, fat/protein surcharge |
| `glucose`, `glucose_alerts`, `correction_check` | CGM status and trend, early warnings, "BZ prüfen" |
| `forecast`, `stats` | Automatik incl. place factor, backtest, tuning, trade-offs |
| `services` | `app/services/dose_help.py` with the parts of `factors.py` and `store/marks.py` it reads |
| `zone`, `context` | travel time zones (`common.TravelZone`), language of the texts (`app/i18n.py`) |

Not ported: storage, Nightscout sync, the day payload assembly (`day_view.compute_day`), analysis pages,
dishes, places catalogue, the meal agent. On a phone those stay with the server or the app itself.

## Differences to the reference

Typed structs replace the reference's dicts and `rust_decimal` its `Decimal` (28 digits). Values agree
with the reference within the last digits; every rule decision and every dose in 0.5 IE steps agrees
exactly on the recorded calls. Numbers in JSON results are strings in their natural form (`"2.5"`, not
`"2.50"`), instants are RFC 3339 in UTC.

## Interface

One function, JSON in and out: `api::call("module.function", args)` with the reference's parameter
names; `services.dose_help` takes a whole snapshot (see `services::Snapshot`). `core-call` exposes it
as a line protocol on stdin/stdout:

```
{"fn": "dosing.half_units", "args": {"units": "2.26"}}
{"ok":"2.5"}
```

## Checking against the reference

```
cargo build --release --manifest-path core/Cargo.toml
python3 core/parity/record.py      # runs the Python test suite and records every distinct core call
python3 core/parity/compare.py     # replays them against the Rust core
```

The recording holds synthetic test data only (`calls.jsonl`, ignored by git); never record from a real
database.
