# Contributing

This is a personal project built around one person's therapy. Bug reports and small fixes are welcome; for larger changes, please open an issue first.

- **Rules live on the server.** Every domain rule belongs in `app/`; `web/` only draws the view data it receives.
- **Texts in both languages.** A new UI text gets a key in `web/i18n/de.js` and `web/i18n/en.js`. A new server text is written in German as `tr("…")` with its English in `app/i18n_en.py`.
- **Dependencies.** Python standard library plus `requirements.txt`; no build step for the web part.
- **Tests.** Run `python3 -m unittest discover -s tests -v` and `node --check` on the changed JavaScript files. Golden test differences must be intended and visible in the review.
- **No personal data.** Never commit `.env`, database files or data fetched from the read API.
- **Calculations.** Changes to how doses or factors are calculated need a reason and, ideally, a backtest against real data; describe them in `docs/rechenwege.md`.

[AGENTS.md](AGENTS.md) describes the project's rules and past decisions in detail.

By contributing you agree that your contribution is licensed under the [AGPL-3.0](LICENSE).
