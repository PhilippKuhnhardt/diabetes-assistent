# MCP-Anbindung für einen Agenten

Ziel: Philipp spricht mit einem Agenten (Claude-App, auch per Sprache, oder Claude Code) über seinen Blutzucker, lässt sich für eine Mahlzeit eine Empfehlung geben und lässt den Agenten am Ende eintragen, was er gegessen und gespritzt hat. Der Agent bekommt dafür dieselben Angaben wie das Eintragsformular und speichert genauso.

## Grundsätze

- **Eine Rechnung.** Der MCP-Server rechnet nichts selbst. Die Tools übersetzen nur Agent-Argumente in die Aufrufe des Eintragsformulars (`entry.preview`, `entry.save`), der Basal-Eingabe und des Tagebuchs. Snack-Zuordnung, Sport, Basal-Chip, Split, Empfehlung, „Wirkt noch“ und alle Prüfungen sind dieselben wie im UI.
- **Schreiben nur mit eigenem Schlüssel.** `MCP_API_KEY` (mindestens 32 Zeichen) ist getrennt von `API_READ_KEY`. Ohne `MCP_API_KEY` ist `/mcp` samt Anmeldung aus. Der Schlüssel öffnet nur `/mcp`, keine anderen Seiten.
- **Wenig Prompt.** Die Server-Anweisungen erklären nur die Begriffe (KH, KE, IE, Faktor, KF, Split, KH-Sicherheit, Optionen). Wann gespeichert wird, steht in den Tool-Beschreibungen: `save_entry` nur mit Philipps bestätigten Werten, Zeit = jetzt, außer er nennt bewusst eine andere.
- **Kein Duplikat bei Wiederholung.** Jeder Eintrag hat eine `event_id`; wiederholt der Agent nach einem Timeout mit derselben, entsteht kein zweiter Eintrag (auch kein zweiter Basal).

## Tools

| Tool | Zweck | Schreibt? |
| --- | --- | --- |
| `get_status` | Aktueller CGM-Wert mit Trend, „Wirkt noch“ (Insulin, KH, Aufschlag, erwarteter Wert), offener 2. Split-Teil, Korrekturvorschlag ohne Essen, Basal heute, alle Einträge und Mahlzeitfaktoren von heute | nein |
| `preview_entry` | Was das Formular für diese KH zeigt: Snack oder Hauptmahlzeit, Sport vor der Mahlzeit, Basal offen, Tagesart, Split, Optionen normal / vorsichtig / nur KH mit Faktor, bei Split Teil jetzt + 2. Teil mit Uhrzeit | nein |
| `save_entry` | Mahlzeit, Snack, Korrektur oder Sport speichern, mit KH-Sicherheit (`weighed`/`known`/`unsure`), fett-/eiweißreich, gewählter Option, Split, Basal und bestätigtem Sporteinfluss | ja |
| `save_basal` | Basal-Injektion beliebiger Menge | ja |
| `delete_entry` | Einen falsch verstandenen Eintrag löschen (nur auf ausdrückliche Bitte) | ja |
| `get_day` | Ein Tag: Einträge mit id, Mahlzeiten mit Faktor oder Blockgrund, BZ-Kennzahlen | nein |
| `read_data` | Lese-API (`/api/v1`) für längere Zeiträume | nein |

Ein Gespräch läuft so:

1. „Wie läuft's?“ → `get_status`: „132, leicht steigend, es wirken noch 1,5 IE, erwartet um 21:40 etwa 110.“
2. „Ich esse jetzt 45 g.“ → `preview_entry(carbs_g=45)`: Optionen und Faktor; Hinweise, wenn die App es als Snack einordnet, Sport gefunden hat oder Basal fehlt.
3. „Ich spritze 5 Einheiten, halbwegs sicher geschätzt, fettreich.“ → `save_entry(carbs_g=45, insulin_units=5, carbs_certainty="known", fat_protein=true, dose_variant="normal")`.
4. Ab 80 g schlägt die App Split vor: Teil jetzt, Rest nach 120 min. Gespeichert wird der jetzt gespritzte Teil; der Rest der gewählten Option wird wie im UI als geplanter 2. Teil vermerkt und erscheint später in `get_status` und als Option `split` in `preview_entry` ohne KH.

Unterschied zum UI: Die Prüfung „angetippter Vorschlag noch aktuell“ (`dose_check`) entfällt. Im Gespräch nennt Philipp die Menge, die er tatsächlich gespritzt hat; die wird eingetragen, auch wenn sich der Vorschlag inzwischen leicht geändert hat.

## Transport und Anmeldung

- `POST /mcp`: MCP „Streamable HTTP“, je Anfrage eine JSON-RPC-Nachricht, Antwort als JSON (kein Event-Stream, keine Server-Sitzung). Unterstützt `initialize`, `ping`, `tools/list`, `tools/call`. `GET /mcp` = 405. Fremder `Origin` = 403.
- **Claude Code, Agent SDK, API:** `Authorization: Bearer <MCP_API_KEY>`.
- **Claude-App (Web, Desktop, iOS, Android):** benutzerdefinierte Connectoren verbinden sich nur per OAuth. Dafür gibt es eine kleine OAuth-2.1-Anmeldung (`/.well-known/oauth-*`, `/oauth/register`, `/oauth/authorize`, `/oauth/token`, PKCE S256). Beim Verbinden öffnet sich „Agent verbinden“, Philipp bestätigt mit dem App-Passwort. Zugriffstoken gelten 1 Stunde, Erneuerungstoken 90 Tage. Tokens sind mit einem aus `MCP_API_KEY` abgeleiteten Schlüssel signiert und nirgends gespeichert: `MCP_API_KEY` ändern trennt alle Verbindungen.
- Rücksprung nur zu Claudes Callback (`https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback`) und zu Loopback-Adressen (Claude Code). So kann ein gefälschter Verbinden-Link den Code nicht an Dritte schicken. Weitere exakte Adressen über `MCP_OAUTH_REDIRECTS` (kommagetrennt).

## Einrichten nach dem Merge

1. In Coolify `MCP_API_KEY` setzen (`openssl rand -hex 32`), neu starten.
2. Claude-App: Einstellungen → Connectoren → Benutzerdefinierten Connector hinzufügen → URL `https://aps.philippkuhnhardt.de/mcp` → Verbinden → App-Passwort eingeben.
3. Claude Code: `claude mcp add --transport http diabetes https://aps.philippkuhnhardt.de/mcp --header "Authorization: Bearer $MCP_API_KEY"`.

## Später

- KH-Sicherheit und fett-/eiweißreich werden gespeichert und im Status gezeigt; in die Empfehlung gehen sie erst ein, wenn Philipp eine Verwendung freigibt.
- Eintrag ändern statt löschen und neu eintragen, Tagesart setzen, Mahlzeit ausklammern: bei Bedarf als weitere Tools, jeweils über die bestehenden Services.
