# Diabetes-Tagebuch und Faktorenanalyse

Private, deutschsprachige Web-App für FreeStyle Libre 3 Plus über Nightscout und manuell erfasstes Fiasp. Nightscout ist die maßgebliche Quelle für CGM, Kohlenhydrate, Injektionen, Sport und das aktuelle Profil. SQLite spiegelt diese Daten und speichert lokale Analyseentscheidungen sowie versionierte Einstellungen. Berechnete Faktoren werden nie nach Nightscout geschrieben. Die Formeln und Annahmen stehen in [Rechenwege](docs/rechenwege.md).

## Deployment mit Umgebungsvariablen

Compose liest alle Zugangsdaten direkt aus der Umgebung des Deployment-Prozesses. Es gibt kein `env_file` und keine erforderliche `.env`-Datei.

| Variable | Bedeutung |
| --- | --- |
| `NIGHTSCOUT_URL` | HTTPS-Basisadresse von Nightscout |
| `NIGHTSCOUT_CREDENTIAL` | Roher Nightscout-Zugangstoken oder rohe `API_SECRET` |
| `APP_PASSWORD` | Eigenes Passwort für die App-Anmeldung |
| `NIGHTSCOUT_AUTH_MODE` | `secret` (Standard) oder `token` |

Bei `secret` sendet die App den SHA-1-Hash der rohen `API_SECRET` im `api-secret`-Header. Bei `token` wird der rohe Zugangstoken verwendet. Die drei Zugangswerte müssen im Deployment als Umgebungsvariablen gesetzt werden; fehlende Werte stoppen Compose mit einer Fehlermeldung. Gib sie in der Secret-/Environment-Konfiguration deines Hosters an oder exportiere sie in der Shell.

```sh
docker compose --env-file /dev/null up -d --build
```

`--env-file /dev/null` sorgt bei einem lokalen Aufruf dafür, dass eine eventuell noch vorhandene alte `.env`-Datei nicht geladen wird. Nach einer Änderung der Variablen: `docker compose --env-file /dev/null up -d --force-recreate`.

Optional sind `APP_BIND_ADDRESS` (Standard `127.0.0.1`), `APP_PORT` (`8765`), `APP_TIMEZONE` (`Europe/Berlin`), `APP_UID`/`APP_GID` (`1000`) und `APP_DATA_DIR` (`./data`). Die App bindet standardmäßig nur an <http://127.0.0.1:8765/>. Für einen Reverse Proxy kann die Bind-Adresse passend gesetzt werden; der externe Zugriff sollte über HTTPS erfolgen.

Die SQLite-Datei bleibt im gemounteten Verzeichnis `APP_DATA_DIR` als `app.sqlite3` über Container-Neustarts erhalten. Der Standard `./data` bewahrt den bisherigen Datenpfad bei. Das Verzeichnis muss für `APP_UID`/`APP_GID` schreibbar sein. Sichere das Datenverzeichnis und die Secrets deines Deployment-Systems. Im Browser meldest du dich einmal mit `APP_PASSWORD` an; die Sitzung bleibt als Cookie für ein Jahr bestehen und kann über **Abmelden** beendet werden.

### Deployment auf Coolify

Für Coolify die Git-Quelle als **Dockerfile-Anwendung** anlegen (Build-Kontext im Projektverzeichnis, Dockerfile `Dockerfile`). `compose.yaml` bleibt für den lokalen Start. In Coolify den internen Port **8765** als „Ports Exposes“ und die Domain beispielsweise als `https://tagebuch.example.de:8765` konfigurieren. Der Port im Domain-Feld ist das interne Proxy-Ziel; im Browser wird die Domain über HTTPS-Port 443 aufgerufen. Keinen Host-Port veröffentlichen. Das Docker-Image setzt `APP_HOST=0.0.0.0` und `APP_DB=/data/app.sqlite3` bereits selbst.

Unter **Environment Variables** `NIGHTSCOUT_URL`, `NIGHTSCOUT_CREDENTIAL` und ein starkes `APP_PASSWORD` als Laufzeitvariablen setzen. Optional: `NIGHTSCOUT_AUTH_MODE=token` statt `secret` und `APP_TIMEZONE=Europe/Berlin`. Zugangsdaten gehören nicht ins Repository oder in Build-Argumente.

Unter **Configuration → Persistent Storage** einen **Volume Mount** mit einem Namen wie `app-data`, leerem „Source Path“ und „Destination Path“ `/data` anlegen. So bleiben `app.sqlite3` und die zu SQLite-WAL gehörenden Dateien bei einem Redeploy zusammen erhalten. Nur eine laufende App-Instanz soll dieses Verzeichnis beschreiben. Nach dem ersten Deploy im Container prüfen, dass `/data/app.sqlite3` existiert und beschreibbar ist; nach einem weiteren Deploy muss die lokale Einstellungshistorie noch vorhanden sein.

Der Volume Mount schützt vor Container-Austausch, ist aber kein Backup. Die einfachste konsistente Sicherung in Coolify: Unter **Backups → Scheduled Backups** das Volume wählen, `daily` einstellen und **Stop containers while creating the archive** aktivieren. Während des kurzen Backups ist die App nicht erreichbar. Zusätzlich eine Kopie in S3-kompatiblem Speicher oder außerhalb des Servers aufbewahren und eine Wiederherstellung testen. Für Backups ohne Unterbrechung stattdessen die SQLite-Backup-API verwenden.

In **Einstellungen** kannst du prüfen, ob Nightscouts aktuelles Profil inklusive I:C, Korrekturfaktor und Zielbereich geladen wurde.

## Tagebuch

Der aktuelle Nightscout-CGM-Wert steht mit Trend und Messzeit dauerhaft in der Kopfzeile. Der zeitabhängige Zielbereich des aktuellen Nightscout-Profils färbt Werte im Bereich grün und Werte außerhalb zwischen 70 und 250 mg/dL gelb; unter 70 mg/dL erscheint Rot, über 250 mg/dL Orange. Fehlende oder mehr als 15 Minuten alte Werte bleiben neutral. Die Anzeige aktualisiert sich jede Minute und beim Zurückkehren zum Browserfenster.

Im Tagebuch ist „Mahlzeit & Insulin“ der Standard; „Sport“ lässt sich direkt daneben auswählen. Bei Mahlzeit & Insulin können Kohlenhydrate, Insulin oder beide Werte zusammen erfasst werden. Ein vorhandener Snack oder reiner KH-Eintrag bleibt beim Bearbeiten als solcher erhalten. Alle Einträge können bearbeitet und gelöscht werden. Jeder Eintrag wird in Nightscout gespeichert. Die Tagesliste zeigt alle Einträge und, wo ein passender CGM-Wert vorliegt, den Glukosewert zum Ereignis. Bestehende Nightscout-Behandlungen werden ebenfalls angezeigt. Hauptmahlzeiten werden anhand einstellbarer Uhrzeitgrenzen als Frühstück, Mittag- oder Abendessen erkannt; die Kategorien müssen beim Eintragen nicht gewählt werden. Weitere Einträge derselben Mahlzeit innerhalb ihres Auswertungsfensters werden als Ergänzungen aggregiert, auch wenn sie bereits als separate Mahlzeit aus der App in Nightscout stehen. Legacy-Einträge mit alter Kategorie bleiben lesbar. Die Zeiteingabe verwendet auf Mobilgeräten die native Uhrzeitauswahl. Nach einem passenden Sporteintrag erscheint beim Hinzufügen einer Hauptmahlzeit eine Option, den vorgeschlagenen oder einen angepassten Sporteffekt direkt als „Sport wirkte mit“ zu bestätigen.

Die Spritzhilfe verwendet I:C und KF aus dem **aktuellen Nightscout-Profil**. Der Insulinfaktor wird in der App immer als **IE/KE** angezeigt: 1,4 IE/KE bedeutet 1,4 IE für 10 g KH. Nightscout speichert stattdessen I:C in g/IE; die Schnittstelle rechnet mit `g/IE = 10 / (IE/KE)` um. Der normale Vorschlag rechnet mit einem lokalen Zielwert, der in den App-Einstellungen von 60 bis 140 mg/dL geändert werden kann (Standard 100 mg/dL). Der vorsichtige Vorschlag rechnet immer mit 140 mg/dL. Beide Vorschläge begrenzen die Summe aus KH- und Korrekturanteil bei null. Die Eintragswerte werden auf 0,5 IE gerundet; der rechnerisch erwartete BG wird für die gerundete Spritzmenge angezeigt. Sobald für den heutigen Tag Kohlenhydrate eingegeben werden, erscheint die Empfehlung unter den Eingabefeldern. Ohne Kohlenhydrate bleibt dieser Bereich leer. Ein Vorschlag kann ins Insulinfeld übernommen und danach zusammen mit den anderen Formularwerten in Nightscout gespeichert werden; vor dem Übernehmen wird die Rechnung mit einem aktuellen CGM-Wert erneut geprüft. Wenn sich der Vorschlag geändert hat, muss der neue Wert erst geprüft und erneut angeklickt werden. CGM-Trend und schnell wirksame Dosen der letzten drei Stunden werden angezeigt, aber nicht automatisch in diese Rechnung eingerechnet. Fehlen aktuelle CGM-Werte oder Profilwerte, zeigt die App den Grund statt eines Vorschlags.

## Analyse

Für jede automatisch erkannte Hauptmahlzeit wird ein Faktor aus allen Kohlenhydraten und allen vollen schnell wirksamen IE im Auswertungsfenster berechnet:

`IE/KE = 10 × (gemessenes ΔG + bestätigte Sport-Senkung + Σ[IE × KF]) / Σ[KH in g × KF]`

Der Standardendpunkt liegt 180 Minuten nach Beginn. Jede weitere Injektion innerhalb des Fensters verlängert es bis 180 Minuten nach dieser Injektion. Eine nächste Hauptmahlzeit kann den Endpunkt ab zwei Stunden auf den letzten geeigneten CGM-Wert davor begrenzen; wenn eine Trennung nicht möglich ist, wird der Faktor blockiert. CGM-Werte an exakten Endpunkten werden nur bei einer Lücke von höchstens 15 Minuten interpoliert. Das Tagebuch zeigt Faktor, KH- und Insulinsumme, Endpunkt und Gründe für blockierte Werte.

Sport vor einer Mahlzeit wird anhand einer einstellbaren Rückblickzeit vorgeschlagen. In den Einstellungen stehen Vorschlagswerte je Sportart, etwa 40 mg/dL Senkung für Gym. Rennsport ist eine eigene Sportart mit BZ-Anstieg durch Adrenalin (Vorschlag +40 mg/dL); ein bestätigter Anstieg wird entsprechend von ΔG abgezogen und senkt so den berechneten Faktor nicht künstlich ab. In der Oberfläche steht die BZ-Wirkung vorzeichenbehaftet: negativ senkt, positiv hebt. Erst eine Bestätigung an der einzelnen Mahlzeit aktiviert den editierbaren Effekt. Eine bestätigte Senkung um 40 mg/dL **zwischen Mahlzeitenbeginn und Endpunkt** addiert **40 mg/dL zum gemessenen ΔG** und bereinigt so rechnerisch den Faktor. Sport schließt die Mahlzeit nicht aus. Die manuelle Ausklammerung bleibt unabhängig davon verfügbar.

Die Analyse zeigt Einzelwerte, Mittelwert, Median, beobachteten Bereich, bei mindestens fünf Werten die mittleren 50 %, und ab sechs berücksichtigten Mahlzeiten einen Vergleich der letzten drei mit den vorherigen drei Faktoren. Frühstück wird zusätzlich nach Arbeit/Frei getrennt. Korrekturfaktorvergleich und Nachtübersicht bleiben erhalten. Das aktuelle Nightscout-Profil gilt auch für ältere Auswertungen; frühere Profile werden nicht rekonstruiert.

Die Berechnung setzt vollständige KH-Wirkung bis zum Endpunkt, vollständige rechnerische Wirkung auch später IE, keinen weiteren Hintergrundtrend und den bestätigten Sporteffekt voraus. Diese Annahmen sind keine Beschreibung des tatsächlichen Wirkverlaufs.

## Entwicklung

Laufzeit: Python-Standardbibliothek, HTML/CSS/JavaScript, SQLite. Relevante Module: `app/nightscout.py` für die Nightscout-Schnittstelle, `app/storage.py` für Persistenz, `app/episodes.py` für Ereignisse und Mahlzeitenerkennung, `app/calculation.py` für Faktoren, `app/dosing.py` für die KH-/Korrekturhilfe, `app/auth.py` für die Browser-Anmeldung. `app/server.py` stellt API und statische Dateien bereit.

Prüfen mit:

```sh
python3 -m unittest discover -s tests -v
node --check web/app.js
```

Die Integrationstests verwenden einen lokalen Fake-Nightscout-Server und schreiben nichts in das persönliche Nightscout.
