# Diabetes-Tagebuch und Faktorenanalyse

[English version](README.md)

Eine private Web-App für Typ-1-Diabetes mit Pen. Sie liest CGM-Werte (FreeStyle Libre 3 Plus) und manuell gespritztes Fiasp aus [Nightscout](https://nightscout.github.io/), berechnet, wie viel Insulin jede Mahlzeit tatsächlich gebraucht hat, und schlägt daraus Dosen vor. Ziel ist weniger Kopfarbeit: ein Ort zum Eintragen, Prüfen und Spritzen.

> **Haftungsausschluss: kein Medizinprodukt, keine Therapieempfehlung.** Dies ist ein privates Hobbyprojekt, gebaut um die Daten, Einstellungen und Gewohnheiten einer Person. Die App ist kein Medizinprodukt, weder zertifiziert noch klinisch geprüft, und nichts, was sie anzeigt, ist eine Therapieempfehlung. Ihre Berechnungen beruhen auf vereinfachenden Modellannahmen und können falsch sein. Keine Insulindosis allein danach festlegen; Änderungen der Therapie mit dem Diabetes-Team besprechen. Die Nutzung erfolgt vollständig auf eigene Verantwortung; die Autoren haften nicht für Schäden aus der Nutzung (siehe auch Abschnitte 15 und 16 der Lizenz).

## Was die App macht

- **Heute.** Aktueller BZ mit Trend, Kurve der letzten Stunden, was noch wirkt (Insulin, KH, Sport) und die letzten Einträge.
- **Eintragen.** Über „+“: Essen (per Foto, Sprache oder Text an den Essens-Agenten), KE & IE, Sport oder Basal. Jeder Eintrag landet in Nightscout; Mahlzeiten werden nach Uhrzeit als Frühstück, Mittag- oder Abendessen erkannt, Snacks ihrer Mahlzeit zugeordnet.
- **Spritzhilfe.** Vorschläge für eine Mahlzeit oder Korrektur aus den Mahlzeitfaktoren der letzten Wochen, abzüglich dessen, was noch wirkt, mit dem Bereich, in dem der BZ meist landet. Große Mahlzeiten lassen sich splitten.
- **Verlauf.** Wochenweise jede Mahlzeit mit Faktor, Zeitraum und Kurve; hier wird geprüft und korrigiert.
- **Analyse.** Faktoren je Mahlzeit und Tagesart sowie ein Rückblick über 90 Tage, wie die Einstellungen gewirkt hätten.
- **Erinnerungen.** Optional per Pushover: Basal vergessen, 2. Split-Teil fällig, Frühwarnung vor Tief oder Hoch.
- **Agenten und Skripte.** Eine Lese-API (`/api/v1`) und ein MCP-Server (`/mcp`), über den ein Assistent wie Claude BZ abfragen, eine Empfehlung holen und eintragen kann.

Der Faktor je Hauptmahlzeit ist `IE/KE = 10 × (ΔG + Sport-Senkung + Σ[IE × KF]) / Σ[KH in g × KF]` über das Auswertungsfenster (Standard 3 h). Formeln, Annahmen und Grenzen stehen in [docs/rechenwege.md](docs/rechenwege.md).

## Sprache

Die Oberfläche spricht Deutsch und Englisch. Sie folgt dem Browser; unter Mehr → Sprache lässt sie sich je Gerät umstellen. Texte vom Server (Fehlermeldungen, Push-Nachrichten, Essens-Agent) folgen `APP_LANGUAGE`: `de` (Standard) oder `en`.

## Einrichtung

Gebraucht werden Docker mit Compose und eine Nightscout-Instanz mit CGM-Daten.

```sh
cp .env.example .env
# NIGHTSCOUT_URL, NIGHTSCOUT_CREDENTIAL und APP_PASSWORD eintragen
docker compose up -d --build
```

Die App läuft dann unter <http://127.0.0.1:8765/>; angemeldet wird mit `APP_PASSWORD` (die Sitzung hält ein Jahr, **Abmelden** beendet sie). Für Zugriff von außen gehört ein Reverse Proxy mit HTTPS davor.

Die Datenbank liegt als `app.sqlite3` in `APP_DATA_DIR` (Standard `./data`) und bleibt über Neustarts erhalten. Das Verzeichnis muss für `APP_UID`/`APP_GID` beschreibbar sein. Es braucht ein eigenes Backup.

### Umgebungsvariablen

| Variable | Bedeutung |
| --- | --- |
| `NIGHTSCOUT_URL` | HTTPS-Adresse von Nightscout. Pflicht. |
| `NIGHTSCOUT_CREDENTIAL` | Rohe `API_SECRET` oder Zugangstoken. Pflicht. |
| `APP_PASSWORD` | Passwort für die App-Anmeldung. Pflicht. |
| `NIGHTSCOUT_AUTH_MODE` | `secret` (Standard, sendet den SHA-1-Hash der `API_SECRET`) oder `token`. |
| `APP_LANGUAGE` | `de` (Standard) oder `en`: Sprache von Server-Texten, Push-Nachrichten und Essens-Agent. |
| `APP_TIMEZONE` | Heimat-Zeitzone, falls das Nightscout-Profil keine hat (Standard `Europe/Berlin`). |
| `APP_SYNC_MINUTES` | Abgleich mit Nightscout alle N Minuten (Standard `2`, `0` = aus). |
| `APP_BIND_ADDRESS`, `APP_PORT` | Wo Compose die App bereitstellt (Standard `127.0.0.1:8765`). |
| `APP_DATA_DIR`, `APP_UID`, `APP_GID` | Datenverzeichnis und sein Benutzer (Standard `./data`, `1000`). |
| `API_READ_KEY` | Optional: Schlüssel für die Lese-API (mindestens 32 Zeichen). |
| `MCP_API_KEY` | Optional: schaltet den MCP-Server ein (mindestens 32 Zeichen, siehe `docs/mcp.md`). |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY` | Optional: Anbieter für den Essens-Agenten; einer genügt. |
| `PUSHOVER_APP_TOKEN`, `PUSHOVER_USER_KEY` | Optional: Erinnerungen per Pushover. |
| `APP_PUBLIC_URL` | Optional: HTTPS-Adresse der App, auf die Push-Nachrichten verlinken. |
| `APP_HEARTBEAT_URL` | Optional: Ping-Adresse eines externen Wächters (z. B. Healthchecks.io), aufgerufen nach jedem fehlerfreien Abgleich. |

Zugangsdaten gehören in `.env` oder in die Secret-Einstellungen des Hosters, nie ins Repository oder in Build-Argumente.

Im Hintergrund holt die App alle `APP_SYNC_MINUTES` Minuten gestern und heute aus Nightscout und rechnet Tage und Analyse vor. Seiten und Lese-API lesen diese gespeicherten Ergebnisse und warten nicht auf Nightscout; eigene Einträge werden sofort übertragen.

### Deployment auf Coolify

Die Git-Quelle als **Dockerfile-Anwendung** anlegen (Dockerfile `Dockerfile`); `compose.yaml` ist nur für den lokalen Start. Als „Ports Exposes“ **8765** eintragen und die Domain etwa als `https://tagebuch.example.de:8765` (der Port ist das interne Ziel des Proxys, im Browser gilt 443). Keinen Host-Port veröffentlichen. Das Image setzt `APP_HOST=0.0.0.0` und `APP_DB=/data/app.sqlite3` selbst.

Die Variablen aus der Tabelle unter **Environment Variables** als Laufzeitvariablen setzen. Unter **Configuration → Persistent Storage** einen Volume Mount (Name z. B. `app-data`, leerer „Source Path“, „Destination Path“ `/data`) anlegen, damit `app.sqlite3` mit seinen WAL-Dateien einen Redeploy übersteht; nur eine Instanz darf dorthin schreiben.

Das Volume ist kein Backup. Einfach und konsistent: unter **Backups → Scheduled Backups** das Volume täglich sichern mit **Stop containers while creating the archive** (die App ist dabei kurz nicht erreichbar), dazu eine Kopie außerhalb des Servers, und die Wiederherstellung einmal ausprobieren.

## Lese-API

Mit `API_READ_KEY` liefert `GET /api/v1/…` mit `Authorization: Bearer <API_READ_KEY>` die gespeicherten Daten als JSON. Der Schlüssel öffnet nur diese Lese-Endpunkte; ohne ihn geht `/api/v1` nur mit der Browser-Anmeldung.

```sh
curl -H "Authorization: Bearer $API_READ_KEY" "https://deine-domain/api/v1/meals?days=30"
```

- `GET /api/v1`: Übersicht mit Endpunkten, Spalten, Zeitzone und erstem gespeicherten Tag.
- `GET /api/v1/meals`, `/window-events`, `/entries`, `/treatments`, `/basal`: je eine Tabelle (`{"from", "to", "timezone", "rows"}`), Zahlen als Dezimal-Strings.
- `GET /api/v1/data`: alle Tabellen auf einmal; `GET /api/v1/profile`: Einstellungen und Nightscout-Profil.
- Zeitraum: `?days=N` (Standard 30) oder `?from=JJJJ-MM-TT&to=JJJJ-MM-TT`.

Die API liest nur SQLite. Abgerufene Daten sind persönlich und gehören nicht ins Repository.

## MCP-Anbindung

Mit `MCP_API_KEY` bietet `POST /mcp` einen MCP-Server: Ein Agent kann den BZ prüfen, sich wie im Eintragsformular eine Empfehlung geben lassen und Mahlzeiten, Korrekturen, Sport und Basal eintragen. Claude Code und Skripte melden sich mit `Authorization: Bearer <MCP_API_KEY>` an, die Claude-App per OAuth mit dem App-Passwort. Details: [docs/mcp.md](docs/mcp.md).

## Benachrichtigungen

Mit `PUSHOVER_APP_TOKEN` und `PUSHOVER_USER_KEY` schickt der Hintergrund-Abgleich Erinnerungen per [Pushover](https://pushover.net), jede nur einmal:

- **Basal vergessen:** ab Frühstücksende ist heute noch kein Basal erfasst.
- **2. Split-Teil fällig:** mit der Menge aus der aktuellen Prognose; bei schnellem Anstieg früher, bei niedrigem oder fallendem BZ später.
- **Frühwarnung:** ein Tief oder Hoch ist in der nächsten Stunde wahrscheinlich.

Unter Mehr → Benachrichtigungen lassen sie sich einzeln abschalten und testen. Meldungen gehen mit hoher Priorität; ob sie auf dem iPhone auch im Stumm-Modus klingeln, stellt man in der Pushover-App ein.

## Entwicklung

```sh
pip install -r requirements.txt
python3 -m unittest discover -s tests -v
for f in web/app.js web/ui/*.js web/i18n/*.js; do node --check "$f"; done
```

Aufbau: `app/` enthält alle Fachregeln (Python-Standardbibliothek plus die Pakete aus `requirements.txt`), `web/` zeichnet nur, was der Server liefert (Preact + htm, ohne Build-Schritt). Nightscout ist die maßgebliche Quelle; SQLite spiegelt die Daten und speichert eigene Entscheidungen, Einstellungen und berechnete Tage.

Die Integrationstests laufen gegen einen lokalen Fake-Nightscout-Server. `tests/test_golden.py` vergleicht 40 Tage synthetische Daten mit den Referenzständen in `tests/golden/`; eine gewollte Änderung erzeugt sie mit `GOLDEN_UPDATE=1 python3 -m unittest tests.test_golden` neu. Die CI führt zusätzlich einen Klicktest im Browser aus (`tests/e2e/`). Die Projektregeln im Detail stehen in [AGENTS.md](AGENTS.md).

## Lizenz

[GNU Affero General Public License v3.0](LICENSE). Wer eine veränderte Fassung als Netzwerkdienst betreibt, muss deren Nutzern nach der AGPL den Quellcode dieser Fassung anbieten.
