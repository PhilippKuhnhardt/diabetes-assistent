# Contributing

Thanks for your interest. This app was built around one person's diabetes therapy, so please read this before you open a pull request.

## Scope

Upstream stays small on purpose:

- **Nightscout is the only data source.** Adapters for other CGM or pump APIs will not be merged; feed your data into Nightscout instead.
- **Multiple daily injections with one rapid insulin.** The insulin action curve is a profile in `app/insulin.py`, selected with `APP_INSULIN`. Adding curves for other insulins is welcome in forks; upstream keeps the profile it is used with.
- **One instance per person.** No multi-user or multi-tenant features.

Bug fixes, tests, documentation, translations and things that make the app easier to run for others are very welcome. For larger changes, open an issue first so we can agree on the approach.

**Safety first.** The app suggests insulin doses. Any change to a calculation needs a test that shows the old and the new result, and a sentence in the PR on why the new one is right. Never loosen a safety check (lows, stale CGM data, insulin still acting) without discussing it in an issue first.

## Development setup

Python 3.14 and Node 22 (only for the syntax check and the click test).

```sh
pip install -r requirements.txt
python3 -m unittest discover -s tests -v
for file in web/app.js web/ui/*.js web/i18n/*.js; do node --check "$file"; done
```

Run the app locally against the fake Nightscout used by the click test:

```sh
python tests/e2e/server.py      # serves the app on http://127.0.0.1:8765/ with synthetic data
npm install --no-save playwright@1.63.0 && npx playwright install chromium
node tests/e2e/smoke.js         # in a second shell
```

## Tests

- `tests/test_*.py`: unit and integration tests (the integration tests start a fake Nightscout).
- `tests/test_golden.py`: golden-master snapshots of every response for 40 days of synthetic data. A deliberate change regenerates them with `GOLDEN_UPDATE=1 python3 -m unittest tests.test_golden`; the diff of `tests/golden/` is part of the review.
- `tests/e2e/smoke.js`: click test in Chromium (navigation, entry form, cards, settings).

CI runs all three on every push and pull request.

## Code layout

- `app/`: Python standard-library HTTP server (`app/server.py`), domain rules and calculations (`calculation.py`, `on_board.py`, `forecast.py`, `dosing.py`, ...), services behind the endpoints (`app/services/`), SQLite storage (`app/store/`), Nightscout and LLM clients (`app/ingestion/`).
- `web/`: Preact + htm as ES modules without a build step (`web/ui/`), vendored in `web/vendor/`. All domain rules live on the server; the browser only draws what it receives.
- `docs/rechenwege.md`: the formulas and model assumptions (German). `AGENTS.md`: detailed notes on behaviour and design decisions, written for AI coding agents but useful for humans too.

## Style

- Keep dependencies minimal; prefer the standard library.
- Money-like precision: calculations use `Decimal`.
- UI texts go through `web/i18n/de.js` and `web/i18n/en.js`; server texts are written in German and translated in `app/i18n_en.py` (`tests/test_i18n.py` checks that none is missing).
- Comments explain why, in English.

## License

By contributing you agree that your contribution is licensed under the [AGPL-3.0](LICENSE).
