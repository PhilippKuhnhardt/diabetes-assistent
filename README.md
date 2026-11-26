# Diabetes diary and insulin factor analysis

[Deutsche Fassung (ausführliche Dokumentation)](README.de.md)

A personal web app for living with type 1 diabetes on multiple daily injections. It reads CGM data and manually injected rapid insulin from [Nightscout](https://nightscout.github.io/) (built with a FreeStyle Libre 3 Plus and Fiasp pens), works out how much insulin each meal actually needed, and uses that history to suggest doses. The goal is less mental load: one place to log, review and dose, instead of doing the arithmetic in your head.

The user interface speaks German and English (it follows the browser, with a switch in the settings); messages from the server, push notifications and the meal agent follow `APP_LANGUAGE` (`de` by default, or `en`). Code, comments and documentation are in English; a German version of this README is in [README.de.md](README.de.md).

> **Disclaimer: not a medical device, not medical advice.** This is a private hobby project built around one person's data, settings and habits. It is not a medical device, it is not certified or clinically validated, and nothing it shows is a therapy recommendation. Its calculations rest on simplifying model assumptions and can be wrong. Never base an insulin dose on it alone; discuss any change to your therapy with your diabetes care team. You use it entirely at your own risk; the authors accept no liability for any harm arising from its use (see also sections 15 and 16 of the license). Run it for yourself only; offering a hosted instance that calculates doses for other people may make you the manufacturer of a medical device in the legal sense.

## What it does

- **Diary.** Log carbs, rapid insulin, basal, exercise and finger-stick readings in one form. Every entry is written to Nightscout; meals are detected automatically as breakfast, lunch or dinner, and snacks are attached to the meal they belong to.
- **Meal factors.** For every main meal the app calculates the insulin-to-carb factor that would have been right in hindsight (insulin units per 10 g carbs), from the glucose change over the meal window, all carbs and all rapid insulin in it, and the correction factor from the Nightscout profile. Windows, exercise effects and exclusions can be adjusted per meal.
- **Dose help.** Suggestions for a new meal use a recency-weighted percentile of past factors for the same meal (optionally with a fixed fat/protein surcharge), subtract insulin and carbs still acting, and offer normal, cautious and carbs-only options. Large meals can be split into two boluses.
- **Analysis.** Factor statistics per meal and day type (work vs. free day), and a 90-day backtest that shows how each combination of settings would have landed (time below, in and above range at the meal endpoint), so settings can be chosen from data.
- **Reminders.** Optional Pushover notifications for forgotten basal, a due second split bolus, and low/high early warnings.
- **Agents and scripts.** A read-only JSON API (`/api/v1`) and an MCP server (`/mcp`) so an AI assistant such as Claude can check glucose, ask for a dose suggestion and log what was eaten and injected.

The formulas, assumptions and their limits are documented in [docs/rechenwege.md](docs/rechenwege.md); the MCP setup is in [docs/mcp.md](docs/mcp.md).

## How it is built

- Python standard library for the HTTP server, SQLite and `Decimal` arithmetic; the few packages in `requirements.txt` (pydantic for input validation, the vendor SDKs for the meal agent). Plain HTML/CSS/JavaScript, no build step.
- Nightscout is the source of truth for CGM readings, carbs, injections and the current profile. SQLite mirrors that data and stores local decisions (manual windows, exclusions, settings) and calculated days, which are recomputed when their inputs or the code change.
- All domain rules live on the server (`app/`); the browser (`web/app.js`) only draws the view data it receives.
- Runs as a single Docker container; credentials come from environment variables, never from files in the repository.

## Running it

You need a [Nightscout](https://nightscout.github.io/) site with your CGM data and a profile (correction factor, carb ratio, target). Nightscout is the only data source; the app does not talk to CGM vendors directly. Docker with Compose is all you need on the server.

```sh
git clone https://github.com/PhilippKuhnhardt/diabetes-assistent.git
cd diabetes-assistent
cp .env.example .env      # fill in NIGHTSCOUT_URL, NIGHTSCOUT_CREDENTIAL and APP_PASSWORD
docker compose up -d --build
```

The app then listens on <http://127.0.0.1:8765/>. Put it behind a reverse proxy with HTTPS (Caddy, Traefik, nginx, or a platform such as Coolify) before you reach it from outside; keep `APP_BIND_ADDRESS=127.0.0.1` unless the proxy runs on another host. If you prefer not to keep a `.env` file, export the same variables in the shell or set them in your hosting platform and run `docker compose --env-file /dev/null up -d --build`.

### Configuration

All configuration comes from environment variables; [.env.example](.env.example) lists every one with a short explanation.

| Variable | Required | Meaning |
| --- | --- | --- |
| `NIGHTSCOUT_URL` | yes | HTTPS base URL of Nightscout |
| `NIGHTSCOUT_CREDENTIAL` | yes | Raw `API_SECRET` or an access token |
| `NIGHTSCOUT_AUTH_MODE` | | `secret` (default, sends the SHA-1 of the secret) or `token` |
| `APP_PASSWORD` | yes | Password for the browser login |
| `APP_TIMEZONE` | | Home time zone if the Nightscout profile has none (default `Europe/Berlin`) |
| `APP_LANGUAGE` | | `de` (default) or `en`: server messages, push notifications and the meal agent. The page follows the browser and has its own switch. |
| `APP_INSULIN` | | Rapid-insulin action profile from [app/insulin.py](app/insulin.py) (default and only upstream profile: `fiasp`) |
| `APP_SYNC_MINUTES` | | Background sync interval (default 2, `0` = off) |
| `APP_BIND_ADDRESS`, `APP_PORT` | | Published address and port (default `127.0.0.1:8765`) |
| `APP_DATA_DIR`, `APP_UID`, `APP_GID` | | Host directory for SQLite and photos (default `./data`), writable for this user and group (default 1000) |
| `API_READ_KEY` | | Turns on the read-only JSON API `/api/v1` (at least 32 characters) |
| `MCP_API_KEY`, `MCP_OAUTH_REDIRECTS` | | Turns on the MCP server `/mcp` for an AI agent, see [docs/mcp.md](docs/mcp.md) |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY` | | Meal agent (carb estimates from photos and text). Any one is enough; without a key the agent is simply off. Voice input needs OpenRouter. |
| `PUSHOVER_APP_TOKEN`, `PUSHOVER_USER_KEY`, `APP_PUBLIC_URL` | | Push reminders and low/high early warnings via [Pushover](https://pushover.net/) |
| `APP_HEARTBEAT_URL` | | Pinged after each error-free background round, for an external watchdog |

Back up `APP_DATA_DIR` (the SQLite database with its WAL files and the meal photos). A volume survives a redeploy but is not a backup.

### First steps

- Log in with `APP_PASSWORD`, then open the settings and check that the Nightscout profile (carb ratio, correction factor, target) was loaded.
- Without history, dose suggestions use the carb ratio from the Nightscout profile. Once a few weeks of meals are logged, the app's own meal factors take over.
- The usual basal dose is 0 (off) until you set it in the settings.
- Insulin action follows the Fiasp curve from [app/insulin.py](app/insulin.py). Upstream ships only this profile; a fork can add another there and select it with `APP_INSULIN`. Many constants of the forecast model were estimated on one person's data; treat them as a starting point, not as calibrated for you.

### One instance per person

The app has one login, one database and one Nightscout connection. It is not built for several people on one instance; run one container per person instead.

## Tests

```sh
pip install -r requirements.txt
python3 -m unittest discover -s tests -v
```

The integration tests run against a local fake Nightscout server, and the golden tests use 40 days of synthetic data. CI also runs a click test in Chromium (`tests/e2e/`). See [CONTRIBUTING.md](CONTRIBUTING.md) for details.

## License

[GNU Affero General Public License v3.0](LICENSE). If you run a modified version as a network service, the AGPL requires you to offer its users the source code of that version.
