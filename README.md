# Diabetes diary and insulin factor analysis

[Deutsche Fassung](README.de.md)

A personal web app for living with type 1 diabetes on multiple daily injections. It reads CGM data (FreeStyle Libre 3 Plus) and manually injected rapid insulin (Fiasp) from [Nightscout](https://nightscout.github.io/), works out how much insulin each meal actually needed, and uses that history to suggest doses. The goal is less mental load: one place to log, review and dose.

> **Disclaimer: not a medical device, not medical advice.** This is a private hobby project built around one person's data, settings and habits. It is not a medical device, it is not certified or clinically validated, and nothing it shows is a therapy recommendation. Its calculations rest on simplifying model assumptions and can be wrong. Never base an insulin dose on it alone; discuss any change to your therapy with your diabetes care team. You use it entirely at your own risk; the authors accept no liability for any harm arising from its use (see also sections 15 and 16 of the license).

## What it does

- **Today.** Current glucose with trend, a chart of the last hours, insulin and carbs still acting, and the latest entries.
- **Logging.** The "+" button logs a meal (described to the meal agent by photo, voice or text), carbs and insulin, exercise or basal. Every entry is written to Nightscout; meals are recognised as breakfast, lunch or dinner, and snacks are attached to their meal.
- **Dose help.** Suggestions for a new meal or a correction use the meal factors of recent weeks, subtract what is still acting and show where glucose is likely to land. Large meals can be split into two boluses.
- **History.** Week by week, each meal with its factor, window and curve, for checking and correcting.
- **Analysis.** Factor statistics per meal and day type, and a 90-day backtest of the settings.
- **Exercise.** Jogging, gym, light cardio and racing come built in; own kinds with their own glucose effect and duration are added under More → Profile & model → Sport.
- **Units.** Glucose in mg/dL or mmol/L, the meal factor as units per 10 g (KE), per 12 g (BE) or grams per unit, set under More → Profile & model. Low and high limits for warnings and charts are settings too.
- **Reminders.** Optional Pushover notifications for forgotten basal, a due second split bolus and low/high early warnings.
- **Agents and scripts.** A read-only JSON API (`/api/v1`) and an MCP server (`/mcp`), so an AI assistant can check glucose, ask for a dose suggestion and log entries.

The formulas and their limits are described in [docs/rechenwege.md](docs/rechenwege.md), the MCP setup in [docs/mcp.md](docs/mcp.md) (both in German).

## Language

The web interface speaks German and English. It follows the browser; a different choice can be made per device under More → Language. Server texts (error messages, push notifications, the meal agent) follow `APP_LANGUAGE`: `de` (default) or `en`.

## Setup

You need Docker with Compose and a Nightscout instance with CGM data.

```sh
cp .env.example .env
# fill in NIGHTSCOUT_URL, NIGHTSCOUT_CREDENTIAL and APP_PASSWORD
docker compose up -d --build
```

The app then listens on <http://127.0.0.1:8765/>. Log in with `APP_PASSWORD`. For access from outside, put a reverse proxy with HTTPS in front of it. One instance serves one person; run a separate instance with its own database for anyone else.

The database lives in `APP_DATA_DIR` (default `./data`) as `app.sqlite3` and survives container restarts. It must be writable for `APP_UID`/`APP_GID`. Back it up; a volume is not a backup.

### Environment variables

| Variable | Meaning |
| --- | --- |
| `NIGHTSCOUT_URL` | Nightscout base URL (HTTPS). Required. |
| `NIGHTSCOUT_CREDENTIAL` | Raw `API_SECRET` or access token. Required. |
| `APP_PASSWORD` | Password for the app login. Required. |
| `NIGHTSCOUT_AUTH_MODE` | `secret` (default, sends the SHA-1 of `API_SECRET`) or `token`. |
| `APP_LANGUAGE` | `de` (default) or `en`: language of server texts, push messages and the meal agent. |
| `APP_TIMEZONE` | Home time zone if the Nightscout profile has none (default `Europe/Berlin`). |
| `APP_INSULIN` | Rapid-insulin action profile from [app/insulin.py](app/insulin.py) (default and only upstream profile: `fiasp`; a fork can add its own there). |
| `APP_SYNC_MINUTES` | Background sync with Nightscout every N minutes (default `2`, `0` = off). |
| `APP_BIND_ADDRESS`, `APP_PORT` | Where Compose publishes the app (default `127.0.0.1:8765`). |
| `APP_DATA_DIR`, `APP_UID`, `APP_GID` | Data directory and the user it belongs to (default `./data`, `1000`). |
| `API_READ_KEY` | Optional key for the read API (at least 32 characters). |
| `MCP_API_KEY` | Optional, turns on the MCP server (at least 32 characters, see `docs/mcp.md`). |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY` | Optional, provider for the meal agent; one is enough. |
| `PUSHOVER_APP_TOKEN`, `PUSHOVER_USER_KEY` | Optional, reminders via Pushover. |
| `APP_PUBLIC_URL` | Optional, HTTPS address of the app that push messages link to. |
| `APP_HEARTBEAT_URL` | Optional, ping URL of an external monitor (e.g. Healthchecks.io) called after every good sync. |

Credentials belong in `.env` or your host's secret settings, never in the repository. The Docker image can also be run without Compose (e.g. on Coolify): it serves on port 8765 and keeps its database in `/data`; see the German README for Coolify notes.

## Read API

With `API_READ_KEY` set, `GET /api/v1/…` with `Authorization: Bearer <API_READ_KEY>` returns the stored data as JSON (meals, window events, CGM entries, treatments, basal, profile). `GET /api/v1` lists the endpoints and columns. Choose the range with `?days=N` (default 30) or `?from=YYYY-MM-DD&to=YYYY-MM-DD`. The key opens nothing else. Fetched data is personal and does not belong in the repository.

## Development

```sh
pip install -r requirements.txt
python3 -m unittest discover -s tests -v
for f in web/app.js web/ui/*.js web/i18n/*.js; do node --check "$f"; done
```

The integration tests use a local fake Nightscout server, the golden tests 40 days of synthetic data. CI also runs a browser click test (`tests/e2e/`). The project's rules in detail are in [AGENTS.md](AGENTS.md).

## License

[GNU Affero General Public License v3.0](LICENSE). If you run a modified version as a network service, the AGPL requires you to offer its users the source code of that version.
