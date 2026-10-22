# Diabetes-Tagebuch und Faktorenanalyse

[English summary](README.md)

Private, deutschsprachige Web-App für FreeStyle Libre 3 Plus über Nightscout und manuell erfasstes Fiasp. Nightscout ist die maßgebliche Quelle für CGM, Kohlenhydrate, Injektionen, Sport und das aktuelle Profil. SQLite spiegelt diese Daten und speichert lokale Analyseentscheidungen sowie versionierte Einstellungen. Berechnete Faktoren werden nie nach Nightscout geschrieben. Die Formeln und Annahmen stehen in [Rechenwege](docs/rechenwege.md).

## Deployment mit Umgebungsvariablen

Compose liest alle Zugangsdaten direkt aus der Umgebung des Deployment-Prozesses. Es gibt kein `env_file` und keine erforderliche `.env`-Datei.

| Variable | Bedeutung |
| --- | --- |
| `NIGHTSCOUT_URL` | HTTPS-Basisadresse von Nightscout |
| `NIGHTSCOUT_CREDENTIAL` | Roher Nightscout-Zugangstoken oder rohe `API_SECRET` |
| `APP_PASSWORD` | Eigenes Passwort für die App-Anmeldung |
| `API_READ_KEY` | Optional: Schlüssel für die Lese-API (mindestens 32 Zeichen) |
| `MCP_API_KEY` | Optional: schaltet die MCP-Anbindung für einen Agenten ein (mindestens 32 Zeichen, siehe `docs/mcp.md`) |
| `PUSHOVER_APP_TOKEN`, `PUSHOVER_USER_KEY` | Optional: Erinnerungen per Pushover (siehe „Benachrichtigungen“) |
| `APP_PUBLIC_URL` | Optional: HTTPS-Adresse der App; Pushover-Meldungen verlinken dorthin |
| `APP_HEARTBEAT_URL` | Optional: HTTPS-Ping-Adresse eines externen Wächters (z. B. Healthchecks.io) |
| `NIGHTSCOUT_AUTH_MODE` | `secret` (Standard) oder `token` |

Bei `secret` sendet die App den SHA-1-Hash der rohen `API_SECRET` im `api-secret`-Header. Bei `token` wird der rohe Zugangstoken verwendet. Die drei Zugangswerte müssen im Deployment als Umgebungsvariablen gesetzt werden; fehlende Werte stoppen Compose mit einer Fehlermeldung. Gib sie in der Secret-/Environment-Konfiguration deines Hosters an oder exportiere sie in der Shell.

```sh
docker compose --env-file /dev/null up -d --build
```

`--env-file /dev/null` sorgt bei einem lokalen Aufruf dafür, dass eine eventuell noch vorhandene alte `.env`-Datei nicht geladen wird. Nach einer Änderung der Variablen: `docker compose --env-file /dev/null up -d --force-recreate`.

Optional sind `APP_BIND_ADDRESS` (Standard `127.0.0.1`), `APP_PORT` (`8765`), `APP_TIMEZONE` (`Europe/Berlin`), `APP_UID`/`APP_GID` (`1000`), `APP_DATA_DIR` (`./data`) und `APP_SYNC_MINUTES` (`5`). Die App bindet standardmäßig nur an <http://127.0.0.1:8765/>. Für einen Reverse Proxy kann die Bind-Adresse passend gesetzt werden; der externe Zugriff sollte über HTTPS erfolgen.

Alle `APP_SYNC_MINUTES` Minuten (Standard 5, `0` schaltet ab) holt die App gestern und heute aus Nightscout und berechnet die Tage und die Analyse-Seite aller Zeiträume vor. Öffnen der App oder ein Abruf der Lese-API findet so aktuelle, gespeicherte Ergebnisse.

Die SQLite-Datei bleibt im gemounteten Verzeichnis `APP_DATA_DIR` als `app.sqlite3` über Container-Neustarts erhalten. Der Standard `./data` bewahrt den bisherigen Datenpfad bei. Das Verzeichnis muss für `APP_UID`/`APP_GID` schreibbar sein. Sichere das Datenverzeichnis und die Secrets deines Deployment-Systems. Im Browser meldest du dich einmal mit `APP_PASSWORD` an; die Sitzung bleibt als Cookie für ein Jahr bestehen und kann über **Abmelden** beendet werden.

### Deployment auf Coolify

Für Coolify die Git-Quelle als **Dockerfile-Anwendung** anlegen (Build-Kontext im Projektverzeichnis, Dockerfile `Dockerfile`). `compose.yaml` bleibt für den lokalen Start. In Coolify den internen Port **8765** als „Ports Exposes“ und die Domain beispielsweise als `https://tagebuch.example.de:8765` konfigurieren. Der Port im Domain-Feld ist das interne Proxy-Ziel; im Browser wird die Domain über HTTPS-Port 443 aufgerufen. Keinen Host-Port veröffentlichen. Das Docker-Image setzt `APP_HOST=0.0.0.0` und `APP_DB=/data/app.sqlite3` bereits selbst.

Unter **Environment Variables** `NIGHTSCOUT_URL`, `NIGHTSCOUT_CREDENTIAL` und ein starkes `APP_PASSWORD` als Laufzeitvariablen setzen. Optional: `NIGHTSCOUT_AUTH_MODE=token` statt `secret`, `APP_TIMEZONE=Europe/Berlin`, `API_READ_KEY` für die Lese-API, `MCP_API_KEY` für die Agenten-Anbindung, `APP_SYNC_MINUTES` für den Hintergrund-Sync sowie `PUSHOVER_APP_TOKEN`, `PUSHOVER_USER_KEY`, `APP_PUBLIC_URL` und `APP_HEARTBEAT_URL` für Benachrichtigungen. Zugangsdaten gehören nicht ins Repository oder in Build-Argumente.

Unter **Configuration → Persistent Storage** einen **Volume Mount** mit einem Namen wie `app-data`, leerem „Source Path“ und „Destination Path“ `/data` anlegen. So bleiben `app.sqlite3` und die zu SQLite-WAL gehörenden Dateien bei einem Redeploy zusammen erhalten. Nur eine laufende App-Instanz soll dieses Verzeichnis beschreiben. Nach dem ersten Deploy im Container prüfen, dass `/data/app.sqlite3` existiert und beschreibbar ist; nach einem weiteren Deploy muss die lokale Einstellungshistorie noch vorhanden sein.

Der Volume Mount schützt vor Container-Austausch, ist aber kein Backup. Die einfachste konsistente Sicherung in Coolify: Unter **Backups → Scheduled Backups** das Volume wählen, `daily` einstellen und **Stop containers while creating the archive** aktivieren. Während des kurzen Backups ist die App nicht erreichbar. Zusätzlich eine Kopie in S3-kompatiblem Speicher oder außerhalb des Servers aufbewahren und eine Wiederherstellung testen. Für Backups ohne Unterbrechung stattdessen die SQLite-Backup-API verwenden.

In **Einstellungen** kannst du prüfen, ob Nightscouts aktuelles Profil inklusive I:C, Korrekturfaktor und Zielbereich geladen wurde.

## Tagebuch

Der aktuelle Nightscout-CGM-Wert steht mit Trend und Messzeit dauerhaft in der Kopfzeile. Der Trend ist die Ausgleichsgerade über alle CGM-Werte der letzten 15 Minuten (mindestens 5 Minuten Verlauf), angegeben als Änderung pro 15 Minuten: unter 10 mg/dL gerade (→), ab 10 mg/dL schräg (↗/↘), ab 30 mg/dL senkrecht (↑/↓). Spritzhilfe und Tagesgrafik verwenden dieselbe Regel. Der zeitabhängige Zielbereich des aktuellen Nightscout-Profils färbt Werte im Bereich grün und Werte außerhalb zwischen 70 und 250 mg/dL gelb; unter 70 mg/dL erscheint Rot, über 250 mg/dL Orange. Fehlende oder mehr als 15 Minuten alte Werte bleiben neutral. Die Anzeige aktualisiert sich jede Minute und beim Zurückkehren zum Browserfenster.

Das Tagebuch hat am Handy zwei Ansichten: „Jetzt“ (Tagesübersicht der letzten Stunden, Einträge der letzten 3 h und Eintragsformular mit Spritzhilfe) und „Tag durchgehen“ (ganzer Tag als Kurve, Nacht und eine chronologische Liste, in der jede Hauptmahlzeit als Block mit den Einträgen ihres Auswertungsfensters, ihrem Faktor, Sport, Kurve und „Zeitraum anpassen“ steht). Der heutige Tag öffnet „Jetzt“, frühere Tage öffnen „Tag durchgehen“; Mahlzeiten ohne Faktor sind markiert, oben gezählt und aufgeklappt. Auf breiten Bildschirmen stehen beide Ansichten nebeneinander. Gelöscht wird im Bearbeiten-Dialog. Im Formular ist „Essen & Insulin“ der Standard; „Sport“ lässt sich direkt daneben auswählen. Bei Mahlzeit & Insulin können Kohlenhydrate, Insulin oder beide Werte zusammen erfasst werden. Ein vorhandener Snack oder reiner KH-Eintrag bleibt beim Bearbeiten als solcher erhalten. Alle Einträge können bearbeitet und gelöscht werden. Jeder Eintrag wird in Nightscout gespeichert. Über dem Eintragsformular zeigt eine Tagesübersicht die CGM-Kurve mit Insulin, Kohlenhydraten und Sport; am heutigen Tag wahlweise die letzten 3 oder 6 Stunden bis jetzt oder den ganzen Tag, an anderen Tagen den ganzen Tag. Die Tagesliste zeigt alle Einträge und, wo ein passender CGM-Wert vorliegt, den Glukosewert zum Ereignis. Bestehende Nightscout-Behandlungen werden ebenfalls angezeigt. Hauptmahlzeiten werden anhand einstellbarer Uhrzeitgrenzen als Frühstück, Mittag- oder Abendessen erkannt; die Kategorien müssen beim Eintragen nicht gewählt werden. Ein neuer KH-Eintrag weniger als 2 h nach der vorigen Hauptmahlzeit (unabhängig von der Uhrzeitgrenze) oder innerhalb des Auswertungsfensters einer Hauptmahlzeit derselben Kategorie zählt als Snack dieser Mahlzeit, auch wenn er bereits als separate Mahlzeit aus der App in Nightscout steht. Das Formular zeigt die Zuordnung als Chip („Zählt als: Snack zu Frühstück 09:33“); ein Tipp darauf speichert den Eintrag stattdessen als eigene Hauptmahlzeit. Legacy-Einträge mit alter Kategorie bleiben lesbar. Die Zeiteingabe verwendet auf Mobilgeräten die native Uhrzeitauswahl. Ein Sporteintrag hat ein Feld „Einfluss“ (vorausgefüllt mit dem Standard der Sportart); die Wirkung fließt danach automatisch in „Wirkt noch“, die Empfehlung und die Warnungen ein.

Die Spritzhilfe verwendet I:C und KF aus dem **aktuellen Nightscout-Profil**. Der Insulinfaktor wird in der App immer als **IE/KE** angezeigt: 1,4 IE/KE bedeutet 1,4 IE für 10 g KH. Nightscout speichert stattdessen I:C in g/IE; die Schnittstelle rechnet mit `g/IE = 10 / (IE/KE)` um. Mit Kohlenhydraten rechnet die Empfehlung nur noch mit dem **berechneten Faktor** (Automatik): Aus den gültigen, nicht ausgeschlossenen Faktoren derselben Mahlzeit (Frühstück, Mittag- oder Abendessen nach Uhrzeit; Frühstück nur desselben Tagestyps) der letzten 8, 15 oder 30 Tage (einstellbar, Standard 30) wird ab vier Mahlzeiten ein gewichtetes Perzentil gebildet: Neuere Mahlzeiten zählen mehr (Halbwertszeit einstellbar, Standard 7 Tage), das Perzentil liegt zwischen unterem Quartil und Median (Standard 37,5 %, einstellbar von 25 bis 50 %). Wahlweise rechnet die Methode **Perzentil + Mahlzeitzuschlag**: Jede Hauptmahlzeit bekommt zusätzlich einen einstellbaren Zuschlag (Standard 40 mg/dL) ÷ KF (etwa 1 IE, bis 40 g KH anteilig weniger), der Faktor wird ohne diesen Anteil bestimmt; zugeordnete Snacks bekommen keinen Zuschlag. Daraus zeigt sie drei Werte: **Normal** korrigiert auf den lokalen Zielwert (Einstellungen, 60 bis 140 mg/dL, Standard 100 mg/dL), **Vorsichtig** auf den vorsichtigen Zielwert (Standard 140 mg/dL, zwischen normalem Zielwert und 250 mg/dL) und **Nur KH** deckt nur die Kohlenhydrate (und den noch wirkenden Sport) ohne BZ-Korrektur ab; dieser Wert braucht keinen aktuellen CGM-Wert. Solange der berechnete Faktor weniger als vier Mahlzeiten hat, stehen ersatzweise der Frühstücksmedian der Tagesart oder der Nightscout-Faktor ein; die Faktorzeile nennt das. Die Analyse hat zwei Unterseiten: **Werte** (Faktoren, Kalkulationen, Sport, Nacht und Nightscout-Faktor im gewählten Zeitraum) und **Automatik** (Rechenweg mit Beispieldosis je Mahlzeit, aktuelle Strategie gegen die Trade-off-Linie aller Kombinationen, je Regler eine Zeile mit Urteil, jede Option direkt übernehmbar). Eingeklappt darunter zeigt sie als farbige Rückblick-Matrix über die Mahlzeiten der letzten 90 Tage für alle Kombinationen aus Zeitraum, Halbwertszeit (1, 2, 3, 5, 7, 14 Tage) und Perzentil (25 %, eingestellt, 50 %, eingestellt + Mahlzeitzuschlag), wie oft der BG am Endpunkt unter 80, zwischen 80 und 160 oder über 160 mg/dL gelandet wäre (enger als der Nightscout-Zielbereich), dazu die typische Abweichung ([Rechenwege](docs/rechenwege.md)). Je Zeitraum ist das beste Feld grün umrandet, das beste insgesamt kräftiger. Ein kleinerer Faktor bedeutet weniger Insulin. Alle Vorschläge begrenzen die Summe aus KH- und Korrekturanteil bei null. Die Eintragswerte werden auf 0,5 IE gerundet; der rechnerisch erwartete BG wird für die gerundete Spritzmenge angezeigt. Für den heutigen Tag steht die Empfehlung unter den Eingabefeldern; ohne Kohlenhydrate zeigt sie eine reine Korrektur mit normalem und vorsichtigem Vorschlag. Die Vorschläge stehen nebeneinander mit Spritzmenge und erwartetem BG (ohne Zielwert; der aktuelle BG steht nur oben in der Kopfzeile); darüber zeigt eine Zeile den verwendeten Faktor und den KF, ohne Erklärtexte. Ein Antippen eines Vorschlags speichert den Eintrag sofort mit allen Formularwerten (KH, diese Spritzmenge, Zeit, Notiz, Sport) in Nightscout; alternativ speichert der Knopf unter dem Formular einen frei eingegebenen Wert. Vor dem Speichern eines gewählten Vorschlags wird die Rechnung mit einem aktuellen CGM-Wert erneut geprüft; hat sich der Vorschlag geändert, wird nichts gespeichert und der neue Wert muss erneut angetippt werden. Nach jedem Speichern zeigt die Meldung einige Sekunden lang „Rückgängig“: Das löscht den Eintrag wieder aus Nightscout und legt die Werte zur Korrektur zurück ins Formular. Zeit und Notiz liegen hinter Chips, solange der Eintrag „jetzt“ gilt. CGM-Trend und schnell wirksame Dosen der letzten drei Stunden werden angezeigt, aber nicht automatisch in diese Rechnung eingerechnet. Fehlen aktuelle CGM-Werte oder Profilwerte, zeigt die App den Grund statt eines Vorschlags.

**Basal** wird in der App selbst gespeichert (SQLite, Tabelle `basal_doses`) und zusätzlich als Nightscout-Notiz (`eventType: Note`, `basal_units`, Marker `[DA2:basal]`) ohne `insulin`-Feld gespiegelt, damit weder Nightscouts IOB noch der Mahlzeitenfaktor es als schnelles Insulin zählt. Bis zum Ende des Frühstücks steht im Formular ein Chip „+ Basal 17 IE“ (Wert in den Einstellungen unter „Basal zum Frühstück“, 0 schaltet ihn aus), solange am Tag noch kein Basal erfasst ist; er ist vorausgewählt, und der Frühstückseintrag oder ein angetippter Vorschlag speichert das Basal mit. „Rückgängig“ entfernt beides. Ohne Frühstück trägt der Modus „Basal“ einen frei wählbaren Wert ein. Ist Nightscout nicht erreichbar, bleibt das Basal lokal und wird beim nächsten Laden des Tages nachgetragen; ein in Nightscout gelöschtes, bereits übertragenes Basal verschwindet auch lokal, und Basal-Notizen aus Nightscout ohne lokalen Eintrag werden übernommen. Gelöscht wird in der Tagesliste.

Das Eintragsformular folgt dem Ablauf Zeit (mit Basal-Chip), dann drei Karten: **Essen**, **Einflüsse** (Tagesart; nur sichtbar, wenn sie zutrifft) und Dosis (Empfehlung und eigene IE); die Notiz liegt unauffällig darunter. Unter den Kohlenhydraten liegen in einer Zeile ein Schieberegler für die KH-Sicherheit in drei Stufen (**KH unsicher** = keine ernsthafte Schätzung, **KH halbwegs** = Gericht bekannt oder FDDB-KI-Auswertung, Standard, **KH gewogen**) und der Chip **Fettreich** (fett-/eiweißreich). Sie stehen als `[DAQ:unsure]`, `[DAQ:known]`, `[DAQ:weighed]` bzw. `[DAQ:fat]` in der Nightscout-Notiz; ältere `[DAQ:unsure]`-Einträge bleiben „KH unsicher“, und ein bearbeiteter Eintrag ohne Stufe bleibt ohne, solange der Regler nicht bewegt wird. Eine Mahlzeit zeigt die unsicherste Stufe ihrer markierten KH-Einträge. Die Markierungen sind im Bearbeiten-Dialog änderbar und erscheinen an der Mahlzeit im Tagebuch und in der Lese-API. An der Faktorberechnung ändern sie nichts; sie sammeln Daten für die Weiterentwicklung der Formel.

## Analyse

Für jede automatisch erkannte Hauptmahlzeit wird ein Faktor aus allen Kohlenhydraten und allen vollen schnell wirksamen IE im Auswertungsfenster berechnet:

`IE/KE = 10 × (gemessenes ΔG + Sport-Senkung im Fenster + Σ[IE × KF]) / Σ[KH in g × KF]`

Der Standardendpunkt liegt 180 Minuten nach Beginn. Jede weitere Injektion innerhalb des Fensters verlängert es bis 180 Minuten nach dieser Injektion. Eine nächste Hauptmahlzeit kann den Endpunkt ab zwei Stunden auf den letzten geeigneten CGM-Wert davor begrenzen; wenn eine Trennung nicht möglich ist, wird der Faktor blockiert. CGM-Werte an exakten Endpunkten werden nur bei einer Lücke von höchstens 15 Minuten interpoliert. Das Tagebuch zeigt Faktor, KH- und Insulinsumme, Endpunkt und Gründe für blockierte Werte.

Sport wirkt automatisch (seit 22.10.2026): Jede Einheit senkt den BZ um ihre Sportwirkung, linear über 5 Stunden ab ihrem Ende. In den Einstellungen steht der Standard je Sportart (etwa −40 mg/dL für Gym); beim Eintragen und in „Tag durchgehen“ lässt sich der Wert je Einheit ändern (0 = keine Wirkung). Rennsport hebt den BZ (Adrenalin, Standard +40 mg/dL). Die Wirkung steckt in „Wirkt noch“, der Empfehlung, den Warnungen und im Mahlzeitfaktor: Dort zählt der Anteil, der zwischen Beginn und Endpunkt der Mahlzeit wirkt, und wird zum gemessenen ΔG addiert (ein Anstieg abgezogen). An der Mahlzeit lässt sich ein eigener Wert setzen oder „Kein Einfluss“ wählen. Sport schließt die Mahlzeit nicht aus. Die manuelle Ausklammerung bleibt unabhängig davon verfügbar.

Die Analyse zeigt Einzelwerte, Mittelwert, Median, beobachteten Bereich, bei mindestens fünf Werten die mittleren 50 %, und ab sechs berücksichtigten Mahlzeiten einen Vergleich der letzten drei mit den vorherigen drei Faktoren. Frühstück wird zusätzlich nach Arbeit/Frei getrennt. Standard ist Montag bis Freitag Arbeitstag; morgens lässt sich die Tagesart im Tagebuch umstellen, und ein in den Einstellungen eingeschalteter Urlaub macht jeden Tag frei, bis er ausgeschaltet wird. Morgens nutzt die Spritzhilfe den Median-Frühstücksfaktor der gewählten Tagesart, sobald mindestens drei berechnete Frühstücke dieser Art aus den letzten 28 Tagen vorliegen, sonst den Nightscout-Faktor. Die Nachtübersicht bleibt erhalten. Das aktuelle Nightscout-Profil gilt auch für ältere Auswertungen; frühere Profile werden nicht rekonstruiert.

Die Berechnung setzt vollständige KH-Wirkung bis zum Endpunkt, vollständige rechnerische Wirkung auch später IE, keinen weiteren Hintergrundtrend und die Sportwirkung nach der 5-h-Kurve voraus. Diese Annahmen sind keine Beschreibung des tatsächlichen Wirkverlaufs.

## Lese-API für Skripte und Agenten

Ist die Umgebungsvariable `API_READ_KEY` gesetzt (mindestens 32 Zeichen, z. B. `openssl rand -hex 32`), liefert `GET /api/v1/…` mit `Authorization: Bearer <API_READ_KEY>` die Daten als JSON. Der Schlüssel öffnet nur diese Lese-Endpunkte, keine anderen Seiten und keine Änderungen. Ohne die Variable funktioniert `/api/v1` nur mit der normalen Browser-Anmeldung. Wechseln = Variable im Deployment ändern und neu starten.

```sh
curl -H "Authorization: Bearer $DIABETES_API_KEY" "https://deine-domain/api/v1/meals?days=30"
```

- `GET /api/v1`: Übersicht mit Endpunkten, Spalten, Zeitzone und erstem gespeicherten Tag.
- `GET /api/v1/meals`, `/window-events`, `/entries`, `/treatments`, `/basal`: je eine Tabelle als JSON (`{"from", "to", "timezone", "rows"}`): Mahlzeiten mit Fenster, BZ Start/Ende, KH, IE, KH × KF, Sport, Ausklammerung und Faktor; alle KH und IE im Fenster einer Mahlzeit; CGM; alle Behandlungen; Basal aus der App, Zahlen als Dezimal-Strings.
- `GET /api/v1/data`: alle Tabellen auf einmal unter `tables`.
- `GET /api/v1/profile`: Einstellungen und aktuelles Nightscout-Profil.
- Zeitraum: `?days=N` (Standard 30) oder `?from=JJJJ-MM-TT&to=JJJJ-MM-TT` (lokale Tage, inklusive, höchstens 3650 Tage).

Die API liest nur SQLite und fragt Nightscout nicht ab; sie dient auch zur Weiterentwicklung der Vorhersageformel. Abgerufene Daten sind persönlich und gehören nicht ins Repository. Schreibzugriff für einen Agenten hat eine eigene Variable, siehe unten.

## MCP-Anbindung für einen Agenten

Ist `MCP_API_KEY` gesetzt (mindestens 32 Zeichen), bietet `POST /mcp` einen MCP-Server: Ein Agent kann den aktuellen BZ prüfen, sich wie im Eintragsformular eine Empfehlung geben lassen und Mahlzeiten, Korrekturen, Split, Sport und Basal mit KH-Sicherheit und Fett-Markierung eintragen. Claude Code und Skripte melden sich mit `Authorization: Bearer <MCP_API_KEY>` an, die Claude-App über OAuth mit dem App-Passwort. Tools, Ablauf und Einrichtung: `docs/mcp.md`.

## Entwicklung

Laufzeit: Python-Standardbibliothek, HTML/CSS/JavaScript, SQLite. Relevante Module: `app/ingestion/` für den Weg von Nightscout nach SQLite (`nightscout.py` als Client, `sync.py` spiegelt Tage und Profil, `basal_sync.py` gleicht Basal ab), `app/store/` für SQLite (`db.py` mit nummerierten Migrationen über `PRAGMA user_version`, dazu je ein Modul für Einstellungen und Profil, Nightscout-Spiegel, eigene Markierungen und Basal), `app/episodes.py` für Ereignisse und Mahlzeitenerkennung, `app/calculation.py` für Faktoren, `app/dosing.py` für die KH-/Korrekturhilfe, `app/forecast.py` für den berechneten Faktor und die Rückblick-Matrix, `app/auth.py` für die Browser-Anmeldung. `app/analysis/` enthält weitere reine Auswertungen (Statistik, Nacht), `app/services/` die Anwendungsfälle hinter den Endpunkten (Tag, Eintrag, Basal, Empfehlung, Analyse, Lese-API, Einstellungen, eigene Markierungen, Nightscout-Sync). `app/server.py` ist nur die HTTP-Schicht: Anmeldung, Routen-Tabelle, JSON und statische Dateien. `web/app.js` enthält keine Fachregeln mehr: Der Server liefert fertige Ansichtsdaten (Eintrags-Vorschau mit Snack-Zuordnung, Sport, Basal-Chip, Tagesart und Empfehlung über `/api/entry/preview`, Speichern in einem Aufruf über `/api/entry`; Mahlzeit-Zeiträume, Blockzuordnung, BZ-Farben und Zielband im Tag; BZ je Minute und Summen für den Zeitraum-Editor; Anteile und beste Felder der Matrix), das Frontend zeichnet nur. `app/services/background.py` hält Spiegel und gespeicherte Analyse im Hintergrund aktuell.

Prüfen mit:

```sh
python3 -m unittest discover -s tests -v
node --check web/app.js
```

Berechnete Tage liegen in SQLite (`day_results`) zusammen mit einem Fingerabdruck aller gelesenen Daten (Spiegel, Basal, Einstellungen, Profil, eigene Markierungen) und des Programmcodes; ändert sich davon etwas, wird der Tag beim nächsten Lesen neu berechnet. Tagebuch, Analyse und Lese-API lesen dieselben gespeicherten Ergebnisse. Die Analyse-Seite (Faktorgruppen, berechneter Faktor, Rückblick-Matrix, Stressaufschlag) liegt je Zeitraum in `summary_results` und gilt, solange heute derselbe Tag ist, die Ausklammerungen gleich sind und keiner der gelesenen Tage sich geändert hat.

Die Integrationstests verwenden einen lokalen Fake-Nightscout-Server und schreiben nichts in das persönliche Nightscout.

`tests/test_golden.py` lädt 40 Tage synthetische Testdaten bei eingefrorener Uhr und vergleicht Tagebuch, Analyse, Empfehlung und Lese-API mit den Referenzständen in `tests/golden/`. Umbauten müssen sie unverändert bestehen; eine gewollte Änderung erzeugt sie mit `GOLDEN_UPDATE=1 python3 -m unittest tests.test_golden` neu, und der Diff gehört in den Review. Für echte Daten vergleicht `scripts/compare_api.py` die Lese-API vor und nach einem Deployment (`snapshot` vorher und nachher, dann `compare`); die Snapshots enthalten persönliche Daten und gehören nicht ins Repository.

## Benachrichtigungen

Mit `PUSHOVER_APP_TOKEN` und `PUSHOVER_USER_KEY` schickt der Hintergrund-Sync Erinnerungen per [Pushover](https://pushover.net) aufs Handy, jede nur einmal und nur nach erfolgreichem Abgleich mit Nightscout:

- **Basal vergessen:** ab Frühstücksende ist heute noch kein Basal erfasst.
- **2. Split-Teil fällig:** der geplante 2. Teil ist fällig und nach der Mahlzeit wurde noch kein Insulin erfasst.

Beide lassen sich unter **Einstellungen → Benachrichtigungen** einzeln abschalten; dort gibt es auch eine Testmeldung und die letzten gesendeten Meldungen. Meldungen gehen mit hoher Priorität (Pushover-Ruhezeiten werden übergangen); ob sie auf dem iPhone als kritischer Alarm auch im Stumm-Modus klingeln, stellt man in der Pushover-App ein. Ist `APP_HEARTBEAT_URL` gesetzt, ruft die App diese Adresse nach jeder fehlerfreien Runde auf; ein externer Wächter wie Healthchecks.io meldet dann, wenn die App oder der Abgleich ausfällt.
