# Rust calculation core

The one implementation of the app's calculation rules. The server calls it through the PyO3 module
`diabetes-core-py` (`app/core.py` is the only bridge), and a native client (iPhone app, widget, Watch
through the phone) can link the same crate to compute offline. Python in `app/` is the outer layer
(web, APIs, sync, storage, LLMs) and holds no rule of its own for the modules below. The target picture is in
[docs/architecture.svg](../docs/architecture.svg).

![Target architecture](../docs/architecture.svg)

## What lives here

| Rust module | Python wrapper (names and signatures only) |
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

The day payload and the analysis page take a whole snapshot each (`day_view.day_snapshot`,
`summary.summary_snapshot`): Python reads the store, the core computes the page. Coarse calls matter, since a
call per reading would cost more in JSON than it saves.

Still in Python and due to move here: the rest of `services/factors.py` (carb factor, place scales, breakfast
median), the review overlay (`day_view.with_checks`), the entry form's rules (`services/entry.py`), diary
edits, the window editor, reminders and the dish outcome rules. Storage, Nightscout sync, the places
catalogue and the meal agent stay outside.

## Differences to the former Python reference

Typed structs replace the former dicts and `rust_decimal` Python's `Decimal` (28 digits). Values agree
with the former reference within the last digits; every rule decision and every dose in 0.5 IE steps agrees
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

## Building for the server

```
python3 core/build.py              # cargo build -p diabetes-core-py, copies it to app/diabetes_core.abi3.so
```

The Dockerfile builds it in a Rust stage. `app/core_types.json` tells `app/core.py` which result paths
are Decimal, datetime, date, int, float or tuple; a new core function gets its entry there by hand.

## Golden calls

`golden/calls.jsonl.gz` holds about 10,000 calls recorded once from the former Python reference while the
test suite ran (synthetic test data only; never record from a real database). CI replays them:

```
cargo build --release --manifest-path core/Cargo.toml
python3 core/golden/compare.py
```

Numbers agree within 1e-9 relative, doses and rule decisions exactly. A deliberate rule change updates
the affected expectations in the file together with the Rust change.
