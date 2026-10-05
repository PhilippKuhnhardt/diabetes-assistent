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

Der aktuelle Nightscout-CGM-Wert steht mit Trend und Messzeit dauerhaft in der Kopfzeile. Der Trend ist die Ausgleichsgerade über alle CGM-Werte der letzten 15 Minuten (mindestens 5 Minuten Verlauf), angegeben als Änderung pro 15 Minuten: unter 10 mg/dL gerade (→), ab 10 mg/dL schräg (↗/↘), ab 30 mg/dL senkrecht (↑/↓). Spritzhilfe und Tagesgrafik verwenden dieselbe Regel. Der zeitabhängige Zielbereich des aktuellen Nightscout-Profils färbt Werte im Bereich grün und Werte außerhalb zwischen 70 und 250 mg/dL gelb; unter 70 mg/dL erscheint Rot, über 250 mg/dL Orange. Fehlende oder mehr als 15 Minuten alte Werte bleiben neutral. Die Anzeige aktualisiert sich jede Minute und beim Zurückkehren zum Browserfenster.

Das Tagebuch hat am Handy zwei Ansichten: „Jetzt“ (Tagesübersicht der letzten Stunden, Einträge der letzten 3 h und Eintragsformular mit Spritzhilfe) und „Tag durchgehen“ (ganzer Tag als Kurve, Nacht und eine chronologische Liste, in der jede Hauptmahlzeit als Block mit den Einträgen ihres Auswertungsfensters, ihrem Faktor, Sport, Kurve und „Zeitraum anpassen“ steht). Der heutige Tag öffnet „Jetzt“, frühere Tage öffnen „Tag durchgehen“; Mahlzeiten ohne Faktor sind markiert, oben gezählt und aufgeklappt. Auf breiten Bildschirmen stehen beide Ansichten nebeneinander. Gelöscht wird im Bearbeiten-Dialog. Im Formular ist „Essen & Insulin“ der Standard; „Sport“ lässt sich direkt daneben auswählen. Bei Mahlzeit & Insulin können Kohlenhydrate, Insulin oder beide Werte zusammen erfasst werden. Ein vorhandener Snack oder reiner KH-Eintrag bleibt beim Bearbeiten als solcher erhalten. Alle Einträge können bearbeitet und gelöscht werden. Jeder Eintrag wird in Nightscout gespeichert. Über dem Eintragsformular zeigt eine Tagesübersicht die CGM-Kurve mit Insulin, Kohlenhydraten und Sport; am heutigen Tag wahlweise die letzten 3 oder 6 Stunden bis jetzt oder den ganzen Tag, an anderen Tagen den ganzen Tag. Die Tagesliste zeigt alle Einträge und, wo ein passender CGM-Wert vorliegt, den Glukosewert zum Ereignis. Bestehende Nightscout-Behandlungen werden ebenfalls angezeigt. Hauptmahlzeiten werden anhand einstellbarer Uhrzeitgrenzen als Frühstück, Mittag- oder Abendessen erkannt; die Kategorien müssen beim Eintragen nicht gewählt werden. Ein neuer KH-Eintrag weniger als 2 h nach der vorigen Hauptmahlzeit (unabhängig von der Uhrzeitgrenze) oder innerhalb des Auswertungsfensters einer Hauptmahlzeit derselben Kategorie zählt als Snack dieser Mahlzeit, auch wenn er bereits als separate Mahlzeit aus der App in Nightscout steht. Das Formular zeigt die Zuordnung als Chip („Zählt als: Snack zu Frühstück 09:33“); ein Tipp darauf speichert den Eintrag stattdessen als eigene Hauptmahlzeit. Legacy-Einträge mit alter Kategorie bleiben lesbar. Die Zeiteingabe verwendet auf Mobilgeräten die native Uhrzeitauswahl. Nach einem passenden Sporteintrag erscheint beim Hinzufügen einer Hauptmahlzeit eine Option, den vorgeschlagenen oder einen angepassten Sporteffekt direkt als „Sport wirkte mit“ zu bestätigen.

Die Spritzhilfe verwendet I:C und KF aus dem **aktuellen Nightscout-Profil**. Der Insulinfaktor wird in der App immer als **IE/KE** angezeigt: 1,4 IE/KE bedeutet 1,4 IE für 10 g KH. Nightscout speichert stattdessen I:C in g/IE; die Schnittstelle rechnet mit `g/IE = 10 / (IE/KE)` um. Der normale Vorschlag rechnet mit einem lokalen Zielwert, der in den App-Einstellungen von 60 bis 140 mg/dL geändert werden kann (Standard 100 mg/dL). Der vorsichtige Vorschlag rechnet mit einem eigenen, ebenfalls einstellbaren Zielwert (Standard 140 mg/dL, zwischen normalem Zielwert und 250 mg/dL). Der dritte Vorschlag **Automatik** ersetzt den Insulinfaktor durch einen vorsichtigen Wert aus den berechneten Mahlzeiten: Aus den gültigen, nicht ausgeschlossenen Faktoren derselben Mahlzeit (Frühstück, Mittag- oder Abendessen nach Uhrzeit; Frühstück nur desselben Tagestyps) der letzten 28 Tage wird ab vier Mahlzeiten ein Perzentil zwischen unterem Quartil und Median gebildet (Standard 37,5 %, einstellbar von 25 bis 50 %, lineare Interpolation an Position (n − 1) × p wie bei den Quartilen der Analyse). Standardmäßig zählen neuere Mahlzeiten mehr (Halbwertszeit 7 Tage), und eine „Tageslage“ passt den Wert um höchstens ±15 % an, wenn die letzten drei abgeschlossenen Mahlzeiten alle höher oder alle niedriger als üblich lagen; in den Einstellungen lässt sich zwischen „Gewichtet + Tageslage“, „Gewichtet“ und „28 Tage gleich“ wählen. Die Analyse zeigt als farbige Rückblick-Matrix für alle drei Methoden (gewichtete mit Halbwertszeit 3, 5, 7, 10 und 14 Tage) bei 25 %, dem eingestellten Perzentil und 50 %, wie oft der BG am Endpunkt unter 80, zwischen 80 und 160 oder über 160 mg/dL gelandet wäre (enger als der Nightscout-Zielbereich), dazu die typische Abweichung und die aktuelle Tageslage ([Rechenwege](docs/rechenwege.md)). Ein kleinerer Faktor bedeutet weniger Insulin; die Korrektur läuft auf den normalen Zielwert. Bei weniger als vier Mahlzeiten entfällt dieser Vorschlag mit Hinweis. Alle Vorschläge begrenzen die Summe aus KH- und Korrekturanteil bei null. Die Eintragswerte werden auf 0,5 IE gerundet; der rechnerisch erwartete BG wird für die gerundete Spritzmenge angezeigt. Für den heutigen Tag steht die Empfehlung unter den Eingabefeldern; ohne Kohlenhydrate zeigt sie eine reine Korrektur mit normalem und vorsichtigem Vorschlag. Die Vorschläge stehen nebeneinander mit Spritzmenge und erwartetem BG (ohne Zielwert; der aktuelle BG steht nur oben in der Kopfzeile); darüber zeigt eine Zeile den gesetzten Nightscout-Faktor, die berechneten Faktoren und den KF, ohne Erklärtexte. Ein Antippen eines Vorschlags speichert den Eintrag sofort mit allen Formularwerten (KH, diese Spritzmenge, Zeit, Notiz, Sport) in Nightscout; alternativ speichert der Knopf unter dem Formular einen frei eingegebenen Wert. Vor dem Speichern eines gewählten Vorschlags wird die Rechnung mit einem aktuellen CGM-Wert erneut geprüft; hat sich der Vorschlag geändert, wird nichts gespeichert und der neue Wert muss erneut angetippt werden. Nach jedem Speichern zeigt die Meldung einige Sekunden lang „Rückgängig“: Das löscht den Eintrag wieder aus Nightscout und legt die Werte zur Korrektur zurück ins Formular. Zeit und Notiz liegen hinter Chips, solange der Eintrag „jetzt“ gilt. CGM-Trend und schnell wirksame Dosen der letzten drei Stunden werden angezeigt, aber nicht automatisch in diese Rechnung eingerechnet. Fehlen aktuelle CGM-Werte oder Profilwerte, zeigt die App den Grund statt eines Vorschlags.

## Analyse

Für jede automatisch erkannte Hauptmahlzeit wird ein Faktor aus allen Kohlenhydraten und allen vollen schnell wirksamen IE im Auswertungsfenster berechnet:

`IE/KE = 10 × (gemessenes ΔG + bestätigte Sport-Senkung + Σ[IE × KF]) / Σ[KH in g × KF]`

Der Standardendpunkt liegt 180 Minuten nach Beginn. Jede weitere Injektion innerhalb des Fensters verlängert es bis 180 Minuten nach dieser Injektion. Eine nächste Hauptmahlzeit kann den Endpunkt ab zwei Stunden auf den letzten geeigneten CGM-Wert davor begrenzen; wenn eine Trennung nicht möglich ist, wird der Faktor blockiert. CGM-Werte an exakten Endpunkten werden nur bei einer Lücke von höchstens 15 Minuten interpoliert. Das Tagebuch zeigt Faktor, KH- und Insulinsumme, Endpunkt und Gründe für blockierte Werte.

Sport vor einer Mahlzeit wird anhand einer einstellbaren Rückblickzeit vorgeschlagen. In den Einstellungen stehen Vorschlagswerte je Sportart, etwa 40 mg/dL Senkung für Gym. Rennsport ist eine eigene Sportart mit BZ-Anstieg durch Adrenalin (Vorschlag +40 mg/dL); ein bestätigter Anstieg wird entsprechend von ΔG abgezogen und senkt so den berechneten Faktor nicht künstlich ab. In der Oberfläche steht die BZ-Wirkung vorzeichenbehaftet: negativ senkt, positiv hebt. Erst eine Bestätigung an der einzelnen Mahlzeit aktiviert den editierbaren Effekt. Eine bestätigte Senkung um 40 mg/dL **zwischen Mahlzeitenbeginn und Endpunkt** addiert **40 mg/dL zum gemessenen ΔG** und bereinigt so rechnerisch den Faktor. Sport schließt die Mahlzeit nicht aus. Die manuelle Ausklammerung bleibt unabhängig davon verfügbar.

Die Analyse zeigt Einzelwerte, Mittelwert, Median, beobachteten Bereich, bei mindestens fünf Werten die mittleren 50 %, und ab sechs berücksichtigten Mahlzeiten einen Vergleich der letzten drei mit den vorherigen drei Faktoren. Frühstück wird zusätzlich nach Arbeit/Frei getrennt. Standard ist Montag bis Freitag Arbeitstag; morgens lässt sich die Tagesart im Tagebuch umstellen, und ein in den Einstellungen eingeschalteter Urlaub macht jeden Tag frei, bis er ausgeschaltet wird. Morgens nutzt die Spritzhilfe den Median-Frühstücksfaktor der gewählten Tagesart, sobald mindestens drei berechnete Frühstücke dieser Art aus den letzten 28 Tagen vorliegen, sonst den Nightscout-Faktor. Korrekturfaktorvergleich und Nachtübersicht bleiben erhalten. Das aktuelle Nightscout-Profil gilt auch für ältere Auswertungen; frühere Profile werden nicht rekonstruiert.

Die Berechnung setzt vollständige KH-Wirkung bis zum Endpunkt, vollständige rechnerische Wirkung auch später IE, keinen weiteren Hintergrundtrend und den bestätigten Sporteffekt voraus. Diese Annahmen sind keine Beschreibung des tatsächlichen Wirkverlaufs.

## Entwicklung

Laufzeit: Python-Standardbibliothek, HTML/CSS/JavaScript, SQLite. Relevante Module: `app/nightscout.py` für die Nightscout-Schnittstelle, `app/storage.py` für Persistenz, `app/episodes.py` für Ereignisse und Mahlzeitenerkennung, `app/calculation.py` für Faktoren, `app/dosing.py` für die KH-/Korrekturhilfe, `app/auth.py` für die Browser-Anmeldung. `app/server.py` stellt API und statische Dateien bereit.

Prüfen mit:

```sh
python3 -m unittest discover -s tests -v
node --check web/app.js
```

Die Integrationstests verwenden einen lokalen Fake-Nightscout-Server und schreiben nichts in das persönliche Nightscout.
