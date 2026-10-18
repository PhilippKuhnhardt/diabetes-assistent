# Diabetes diary and insulin factor analysis

[Deutsche Fassung (ausführliche Dokumentation)](README.de.md)

A personal web app for living with type 1 diabetes on multiple daily injections. It reads CGM data (FreeStyle Libre 3 Plus) and manually injected rapid insulin (Fiasp) from [Nightscout](https://nightscout.github.io/), works out how much insulin each meal actually needed, and uses that history to suggest doses. The goal is less mental load: one place to log, review and dose, instead of doing the arithmetic in your head.

The user interface and most of the documentation are in German, because the app was built for one German-speaking user. The code, comments and commit history are in English.

> **Not medical advice.** This is a private hobby project built around one person's data, settings and habits. Its calculations rest on simplifying model assumptions, it is not a medical device, and it is not validated for anyone else. Use it as inspiration, not as a dosing tool.

## What it does

- **Diary.** Log carbs, rapid insulin, basal, exercise and finger-stick readings in one form. Every entry is written to Nightscout; meals are detected automatically as breakfast, lunch or dinner, and snacks are attached to the meal they belong to.
- **Meal factors.** For every main meal the app calculates the insulin-to-carb factor that would have been right in hindsight (insulin units per 10 g carbs), from the glucose change over the meal window, all carbs and all rapid insulin in it, and the correction factor from the Nightscout profile. Windows, exercise effects and exclusions can be adjusted per meal.
- **Dose help.** Suggestions for a new meal use a recency-weighted percentile of past factors for the same meal (optionally with a fixed fat/protein surcharge), subtract insulin and carbs still acting, and offer normal, cautious and carbs-only options. Large meals can be split into two boluses.
- **Analysis.** Factor statistics per meal and day type (work vs. free day), and a 90-day backtest that shows how each combination of settings would have landed (time below, in and above range at the meal endpoint), so settings can be chosen from data.
- **Reminders.** Optional Pushover notifications for forgotten basal, a due second split bolus, and low/high early warnings.
- **Agents and scripts.** A read-only JSON API (`/api/v1`) and an MCP server (`/mcp`) so an AI assistant such as Claude can check glucose, ask for a dose suggestion and log what was eaten and injected.

The formulas, assumptions and their limits are documented in German in [docs/rechenwege.md](docs/rechenwege.md); the MCP setup is in [docs/mcp.md](docs/mcp.md).

## How it is built

- Python standard library only (HTTP server, SQLite, `Decimal` arithmetic), plain HTML/CSS/JavaScript, no build step.
- Nightscout is the source of truth for CGM readings, carbs, injections and the current profile. SQLite mirrors that data and stores local decisions (manual windows, exclusions, settings) and calculated days, which are recomputed when their inputs or the code change.
- All domain rules live on the server (`app/`); the browser (`web/app.js`) only draws the view data it receives.
- Runs as a single Docker container; credentials come from environment variables, never from files in the repository.

## Running it

You need a Nightscout instance with CGM data. Set the required environment variables and start the container:

```sh
export NIGHTSCOUT_URL=https://your-nightscout.example
export NIGHTSCOUT_CREDENTIAL=...   # API_SECRET or access token
export APP_PASSWORD=...            # password for the app login
docker compose --env-file /dev/null up -d --build
```

The app then listens on <http://127.0.0.1:8765/>. Optional variables (read API key, MCP key, Pushover, timezone, sync interval) and deployment notes for Coolify are listed in the [German README](README.de.md#deployment-mit-umgebungsvariablen).

## Tests

```sh
python3 -m unittest discover -s tests -v
node --check web/app.js
```

The integration tests run against a local fake Nightscout server, and the golden tests use 40 days of synthetic data.
