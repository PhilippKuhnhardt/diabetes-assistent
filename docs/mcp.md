# MCP connection for an agent

Goal: Philipp talks to an agent (the Claude app, also by voice, or Claude Code) about his blood glucose, asks it for a recommendation for a meal and finally has the agent record what he ate and injected. For that the agent gets the same inputs as the entry form and saves the same way.

## Principles

- **One calculation.** The MCP server calculates nothing itself. The tools only translate agent arguments into the calls of the entry form (`entry.preview`, `entry.save`), the basal input and the diary. Snack assignment, sport, the basal chip, split, recommendation, "Wirkt noch" (still acting) and all checks are the same as in the UI.
- **Writing only with its own key.** `MCP_API_KEY` (at least 32 characters) is separate from `API_READ_KEY`. Without `MCP_API_KEY`, `/mcp` and its sign-in are off. The key opens only `/mcp`, no other pages.
- **Little prompt.** The server instructions only explain the terms (KH, KE, IE, Faktor, KF, Split, KH-Sicherheit, options). When to save is stated in the tool descriptions: `save_entry` only with values Philipp confirmed, time = now unless he deliberately names another.
- **No duplicate on retry.** Every entry has an `event_id`; if the agent retries with the same one after a timeout, no second entry is created (no second basal either).

## Tools

| Tool | Purpose | Writes? |
| --- | --- | --- |
| `get_status` | Current CGM value with trend, "Wirkt noch" (insulin, carbs, surcharge, expected value), open second split part, correction suggestion without food, basal today, all of today's entries and meal factors | no |
| `preview_entry` | What the form shows for these carbs: snack or main meal, basal missing, day type, split, options normal / cautious / carbs only with factor, with split the part now + the second part with its time | no |
| `save_entry` | Save a meal, snack, correction or sport, with carb certainty (`weighed`/`known`/`unsure`), fat/protein-rich, the chosen option, split and basal, for carbs optionally a dish (`dish`) and description (`description`); sport with type, duration and optionally its own effect (`sport_change_mgdl`, else the sport type's default) | yes |
| `save_basal` | Basal injection of any amount | yes |
| `delete_entry` | Delete a misunderstood entry (only when explicitly asked) | yes |
| `get_day` | One day: entries with id, meals with factor or blocking reason, glucose metrics | no |
| `list_dishes` | Dish catalogue with count, usual carbs and result (needed more or less insulin than usual); with `dish` one dish with its latest meals | no |
| `read_data` | Read API (`/api/v1`) for longer periods | no |

A conversation goes like this:

1. "How is it going?" → `get_status`: "132, rising slightly, 1.5 IE still acting, about 110 expected at 21:40."
2. "I'm eating 45 g now." → `preview_entry(carbs_g=45)`: options and factor; hints when the app classifies it as a snack or basal is missing. Sport that still acts is automatically part of `on_board` and the dose.
3. "I'm injecting 5 units, fairly sure of the estimate, fat-rich." → `save_entry(carbs_g=45, insulin_units=5, carbs_certainty="known", fat_protein=true, dose_variant="normal")`.
4. From 80 g the app suggests a split: a part now, the rest after 120 min. The part injected now is saved; the rest of the chosen option is noted as a planned second part as in the UI and later appears in `get_status` and as option `split` in `preview_entry` without carbs; its amount comes from the current forecast (`planned_units` = planned, `state` = early/due/wait/later).

Difference from the UI: the check "tapped suggestion still current" (`dose_check`) is skipped. In conversation Philipp names the amount he actually injected; that is recorded even if the suggestion has changed slightly in the meantime.

## Transport and sign-in

- `POST /mcp`: MCP "Streamable HTTP", one JSON-RPC message per request, answer as JSON (no event stream, no server session). Supports `initialize`, `ping`, `tools/list`, `tools/call`. `GET /mcp` = 405. A foreign `Origin` = 403.
- **Claude Code, Agent SDK, API:** `Authorization: Bearer <MCP_API_KEY>`.
- **Claude app (web, desktop, iOS, Android):** custom connectors connect only via OAuth. For that there is a small OAuth 2.1 sign-in (`/.well-known/oauth-*`, `/oauth/register`, `/oauth/authorize`, `/oauth/token`, PKCE S256). Connecting opens "Agent verbinden" (connect agent), where Philipp confirms with the app password. Access tokens are valid for 1 hour, refresh tokens for 90 days. Tokens are signed with a key derived from `MCP_API_KEY` and stored nowhere: changing `MCP_API_KEY` disconnects all connections.
- Redirects go only to Claude's callback (`https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback`) and to loopback addresses (Claude Code). That way a forged connect link cannot send the code to third parties. More exact addresses via `MCP_OAUTH_REDIRECTS` (comma-separated).

## Setup after the merge

1. Set `MCP_API_KEY` in Coolify (`openssl rand -hex 32`), restart.
2. Claude app: Settings → Connectors → Add custom connector → URL `https://your-domain/mcp` → Connect → enter the app password.
3. Claude Code: `claude mcp add --transport http diabetes https://your-domain/mcp --header "Authorization: Bearer $MCP_API_KEY"`.

## Later

- Carb certainty and fat/protein-rich are stored and shown in the status; they enter the recommendation only once Philipp approves a use.
- Editing an entry instead of deleting and re-entering it, setting the day type, excluding a meal: as further tools when needed, each through the existing services.
