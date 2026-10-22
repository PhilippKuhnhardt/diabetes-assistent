# Rechenwege

Die Oberfläche zeigt Ergebnisse, Eingaben und Blockierungsgründe. Formeln, Zwischenschritte und Modellannahmen stehen hier. Maßgeblich für die Implementierung sind `app/dosing.py`, `app/calculation.py`, `app/forecast.py` und die Anwendungsfälle in `app/services/` (Tag, Empfehlung, Analyse).

## Daten und Einheiten

Nightscout liefert CGM-Werte, Kohlenhydrate, Injektionen und das aktuelle Profil. Der aktuelle I:C- und Korrekturfaktor-Zeitplan gilt auch für ältere Analysen. Der Zeitplan wird nach der lokalen Uhrzeit des Ereignisses ausgewählt. Gerechnet wird mit `Decimal`.

- 1 KE = 10 g KH.
- Insulinfaktor: IE/KE. Nightscout-I:C: g/IE. Daher `IE/KE = 10 / (g/IE)`.
- Korrekturfaktor (KF): mg/dL je IE.

## Spritzhilfe

Mit Kohlenhydraten verwendet die Empfehlung nur den berechneten Faktor `A` in IE/KE (siehe Automatik-Faktor unten). Für die eingegebenen Kohlenhydrate `KH`, den aktuellen CGM-Wert `G`, den Zielwert `Z` und den zeitabhängigen KF gilt:

```text
KH-Anteil        = KH × A / 10
Korrekturanteil  = (G - Z) / KF
Vorschlag        = max(0, KH-Anteil + Korrekturanteil)
Spritzmenge      = Vorschlag auf nächste 0,5 IE gerundet
Erwarteter BG    = G + (KH-Anteil - Spritzmenge) × KF
```

Angezeigt werden drei Werte: **Normal** mit dem lokalen Zielwert `Z` (Standard 100 mg/dL), **Vorsichtig** mit dem vorsichtigen Zielwert (Standard 140 mg/dL) und **Nur KH** ohne Korrekturanteil: `max(0, KH-Anteil + Sport / KF)`, erwarteter BG `G + Sport + (KH-Anteil − Spritzmenge) × KF`; dieser Wert braucht keinen aktuellen CGM-Wert. Hat `A` weniger als vier Mahlzeiten, steht ersatzweise der Frühstücksmedian der Tagesart (unten) oder der Nightscout-I:C-Wert `R` ein (`KH-Anteil = KH / R`). Ohne Kohlenhydrate gibt es nur Normal und Vorsichtig als reine Korrektur. Zusätzlich erscheint **Tiefer** mit dem tieferen Zielwert (Standard 100 mg/dL, aus derselben Vorhersage wie Normal), wenn keine KH-Unsicherheit eine vorsichtigere Schätzung verlangt: ohne KH oder bis 30 g KH, die nicht als unsicher oder fett-/eiweißreich markiert sind, ohne offenen 2. Split-Teil, BZ mindestens 70 mg/dL und nicht fallend, nur zwischen 6 und 20 Uhr und solange weniger als 150 mg/dL nachwirken (Insulin, KH und Zuschlag zusammen) oder die letzten KH und die letzte schnelle Gabe mindestens 2 h her sind. Grundlage: Auswertung vom 14.10. (Projektdateien `analysen/tiefer-zielwert.md`): Tagsüber mit wenig Rest kam bis Ziel 100 kein Unterzucker dazu, abends und mit viel Rest schon. Ist der tiefere Zielwert gleich dem Zielwert, ist die Option aus. Die Spritzmenge wird auf 0,5 IE gerundet. Der erwartete BG wird aus dieser gerundeten Spritzmenge berechnet und ist ein Modellwert bei voller KH- und Insulinwirkung, kein zeitlicher Glukoseverlauf. Ein CGM-Wert muss höchstens 15 Minuten alt sein. Vor dem Schreiben nach Nightscout wird mit aktuellen Daten erneut gerechnet; ein geänderter Vorschlag muss erneut bestätigt werden. Der angezeigte Glukosetrend wird nicht verrechnet.

### Was noch wirkt

Vor der Korrektur wird `G` um das verschoben, was noch wirkt (Herleitung: Analysen-Notiz `fiasp-wirkkurve.md` im Projektordner):

```text
Insulinrest   = Σ IE × noch aktiv(t) × KF zur Spritzzeit          (schnelles Insulin der letzten 6 h)
KH-Rest       = Σ KH in g × (1 − t / Aufnahmedauer) × A / 10 × KF zur Essenszeit
Zuschlag-Rest = Σ Z × Anteil × noch offen(t)                       (nur Methode Zuschlag, je Hauptmahlzeit)
G normal      = G + Sport − Insulinrest + KH-Rest + Zuschlag-Rest
G vorsichtig  = max(G + Sport − max(0, Insulinrest − KH-Rest − Zuschlag-Rest), G normal − 60)
```

- noch aktiv: exponentielle Fiasp-Kurve wie Loop (Peak 55 min nach 10 min Verzögerung, 6 h Dauer): 100 % bis 15 min, 75 % nach 1 h, 36 % nach 2 h, 14 % nach 3 h, 4 % nach 4 h.
- Aufnahmedauer: 2 h bis 40 g, 3 h ab 100 g, dazwischen linear; die Fett-Markierung verlängert sie nicht, nur den Zuschlag (neu geschätzt 22.10.). `A` ist der Automatik-Faktor der Mahlzeitart zur Essenszeit, sonst der Nightscout-I:C-Wert.
- Zuschlag: wirkt linear von 2 h bis 5 h nach Mahlzeitbeginn (mit Fett-Markierung bis 6 h); Snacks haben keinen.
- Normal und Nur KH rechnen mit `G normal`, Vorsichtig mit `G vorsichtig`: die Vorhersage minus ihre typische Abweichung (60 mg/dL), mindestens aber so, dass KH- und Zuschlag-Rest nur das aktive Insulin aufheben. Ohne offene KH ist das `G − Insulinrest`; nach großen Mahlzeiten deckt Vorsichtig so auch einen geplanten 2. Teil (Fehler vom 08.10.: vorher 0,5 statt 4,5 IE). Unter 70 mg/dL rechnen Normal und Vorsichtig nur mit `G + Sport − max(0, Insulinrest − KH-Rest − Zuschlag-Rest)`.
- Die Karte „Wirkt noch“ im Tagebuch (heute) zeigt Insulinrest („senkt noch“), KH- und Zuschlag-Rest („hebt noch“) mit dem Zeitpunkt, ab dem weniger als 5 mg/dL übrig sind, und `G − Insulinrest + KH-Rest + Zuschlag-Rest` als erwarteten Wert mit dem wahrscheinlichen Bereich (8 von 10 Fällen): je nachdem, wie viel insgesamt noch wirkt (Insulin + KH + Zuschlag), unter 40 mg/dL −30 bis +30, 40–150 mg/dL −50 bis +60, ab 150 mg/dL −75 bis +90 (offline geschätzt, `on_board.FORECAST_BAND`). Rechnungen nutzen weiter nur den erwarteten Wert. Bis 90 min nach einer Hauptmahlzeit steht ein Hinweis, dass die Prognose dort am unsichersten ist.
- Der Faktor je Hauptmahlzeit (unten) nutzt diese Kurve nicht; dort zählen alle IE im Fenster voll.
- „Wirkungen über den Tag“ (Tag durchgehen) rechnet dasselbe Modell alle 5 Minuten: wie viel mg/dL pro Stunde gerade gehoben (KH, Zuschlag) und gesenkt (Insulin) werden, und als Vergleichslinie `BZ(t − 3 h) − Insulin wirkte seitdem + KH und Zuschlag wirkten seitdem`. KH werden mit dem Automatik-Faktor des Tages bewertet. Angezeigt werden die mittlere Abweichung zum gemessenen BZ und die größte Abweichung nach oben und unten ab 40 mg/dL.

### Split-Bolus

Ab `split_min_grams` KH (Standard 80 g, 0 = aus) ist bei einer Hauptmahlzeit „Split“ vorausgewählt; ein Snack bekommt keinen. Jede Option zeigt dann den ersten Teil und den Rest:

```text
jetzt   = auf 0,5 IE gerundet (Empfehlung × split_first_percent / 100)     (Standard 60 %)
später  = Empfehlung − jetzt, um Mahlzeit + split_delay_minutes            (Standard 120 min)
```

- Der erwartete Wert der Option gilt für die volle Empfehlung, also beide Teile.
- Gespeichert wird beim Eintrag für heute `später = gewählte Empfehlung − tatsächlich eingetragene IE` als Markierung `[DASP:<IE>@<min>]` in der Nightscout-Notiz der Mahlzeit.
- Der Plan ist offen, bis eine schnelle Insulingabe mehr als 15 min nach der Mahlzeit eingetragen ist oder 3 h nach dem geplanten Zeitpunkt. Solange zeigt die Karte „Wirkt noch“ den 2. Teil (nicht in der Prognose enthalten), und ein Eintrag ohne KH bietet zuerst die Option „2. Teil“ mit der geplanten Menge an; daneben stehen Normal und Vorsichtig aus der aktuellen Prognose (aktives Insulin und offene KH).

Vor dem Ende des Frühstücksfensters hängt `R` von der Tagesart ab (Arbeitstag oder freier Tag). Liegen in den letzten 28 Tagen vor heute mindestens drei berechnete, nicht ausgeklammerte Frühstücke derselben Tagesart vor, gilt `KH-Anteil = KH × F / 10` mit `F` = Median dieser Frühstücksfaktoren in IE/KE; sonst bleibt der Nightscout-Wert. Die Tagesart ist standardmäßig Montag bis Freitag Arbeitstag, Samstag und Sonntag frei. Ist in den Einstellungen Urlaub aktiv, zählt jeder Tag ab dem Einschalten als frei; beim Ausschalten endet der Urlaub am Vortag. Eine manuelle Auswahl für einen Tag hat immer Vorrang. Die Tagesart wird nur lokal gespeichert, nicht in Nightscout.

## Automatik-Faktor (Vorschlagsfaktor)

Der berechnete Faktor `A` stammt aus den berechneten, nicht ausgeklammerten Faktoren derselben Mahlzeit (Frühstück nur derselben Tagesart) der letzten `Z` Tage vor heute, ab vier Mahlzeiten:

```text
A = gewichtetes Perzentil p der Faktoren, Gewicht = 0,5 ^ (Alter in Tagen / Halbwertszeit)
```

Einstellbar sind der Zeitraum `Z` (8, 15 oder 30 Tage, Standard 30; 8 Tage, damit am Sonntag der Samstag der Vorwoche noch zählt), die Halbwertszeit (Standard 7 Tage; kürzer = neue Werte zählen stärker) und das Perzentil `p` (Standard 37,5 %). Jeder Faktor steht in der Mitte seines kumulierten Gewichts, so dass sich bei gleichen Gewichten genau die lineare Interpolation an `(n − 1) × p` ergibt. Ein Perzentil hängt von der Reihenfolge ab, nicht vom Betrag: Ein einzelner extremer Faktor verschiebt es höchstens um seinen Gewichtsanteil.

Methode „Perzentil + Mahlzeitzuschlag“ (`auto_method = surcharge`, wählbar in den Einstellungen): Jede Hauptmahlzeit hebt den BZ unabhängig von den KH um einen festen Zuschlag `Z` (Fett und Eiweiß; `auto_surcharge_mgdl`, Standard 40 mg/dL, einstellbar 5–150), bei weniger als 40 g KH anteilig `Z × KH / 40 g`. Vor dem Perzentil wird jeder frühere Faktor um diesen Anteil bereinigt: `F' = F − 10 × Z × Anteil / Σ[KH in g × KF]`. Die Dosis ist dann `A' × KE + Z × Anteil / KF` (plus Korrektur und Sport). Der Zuschlag kommt einmal je Hauptmahlzeit; ein Snack, den das Eintragsformular einer Mahlzeit zuordnet, bekommt nur `A' × KE`. Herleitung und Backtest: Analysen-Notiz `formel-grundbedarf.md` im Projektordner.

Rückblick-Matrix: Für jede Mahlzeit der letzten 90 Tage (unabhängig vom Analysezeitraum) rechnet die App mit den Daten davor aus, welchen Faktor jede Kombination aus Zeitraum (8, 15, 30 Tage), Halbwertszeit (1, 2, 3, 5, 7, 14 Tage) und Perzentil (25 %, eingestellt, 50 %, eingestellt + Mahlzeitzuschlag) vorgeschlagen hätte, und wo der BG am Endpunkt damit gelandet wäre: `BG' = G(Endpunkt) − (Vorschlag − Faktor) × Σ[KH in g × KF] / 10`, alle übrigen Dosen unverändert. Alle Felder werten dieselben Mahlzeiten aus (nur solche, für die jede Kombination genug Vorgeschichte hat). Gezählt wird unter 80, 80–160 und über 160 mg/dL; das ist bewusst enger als der Nightscout-Zielbereich (80–180), weil nach drei Stunden ein Ende unter 160 erwartet wird. Dazu Durchschnitt und Median dieses BZ am Endpunkt und die typische Abweichung des Faktors. Zeitraum, Gewichtung und Perzentil sind Modellannahmen zum Ausprobieren, keine Dosierempfehlung.

Analyse → Automatik: Dieselbe Simulation, übersichtlicher. **Regler-Zeilen** ändern je einen Regler bei sonst eingestellten Werten: Perzentil (25, 30, 35, 40, 45, 50 %), Zuschlag (aus, 20, 40, 60, 80 mg/dL; aus = reines Perzentil), Halbwertszeit (1, 2, 3, 5, 7, 14 Tage), Zeitraum (8, 15, 30 Tage), jeweils plus eingestellter Wert. Jede Zeile wertet die Mahlzeiten aus, die alle ihre Optionen berechnen können, sodass nur die Zeitraum-Zeile die 8-Tage-Vorgeschichte braucht. Urteil je Zeile: „besser“, wenn die beste Option (meiste im Bereich, dann wenigste unter 80) die eingestellte um mehr als max(2 Mahlzeiten, 5 %) übertrifft; sonst „verschiebt tief ↔ hoch“, wenn die Anzahl unter 80 oder über 160 um mehr als diese Schwelle streut; sonst „kein Unterschied“. **Aktuell und Alternativen** rechnet alle Kombinationen dieser Optionen auf denselben Mahlzeiten und zeigt die Trade-off-Linie: Kombinationen, die keine andere bei „unter 80“ und „über 160“ zugleich schlägt (bei gleichen Zahlen die mit den wenigsten geänderten Reglern), mit der Differenz zur Einstellung. **So rechnet die Automatik** zeigt je Mahlzeitart den aktuellen Faktor und die Dosis ohne Korrektur für die typische Mahlzeit (Median der KH und von Σ[KH × KF] / KH der verwendeten Mahlzeiten): `KE × Faktor + Z × Anteil / KF`. Jede Option lässt sich direkt als Einstellung übernehmen (neue Einstellungsversion ab jetzt).

## Faktor je Hauptmahlzeit

Eine Mahlzeit beginnt bei `t₀`. Der Standardendpunkt ist `t₀ + window_minutes` (Standard 180 Minuten). Jede schnell wirksame Injektion, die innerhalb des aktuellen Fensters und vor der nächsten Hauptmahlzeit liegt, kann den Endpunkt bis `Injektion + window_minutes` verlängern. Liegt die nächste Hauptmahlzeit mehr als 120 Minuten nach `t₀`, aber vor dem Endpunkt, wird der letzte geeignete CGM-Wert innerhalb von 15 Minuten davor als Endpunkt verwendet. Kann die nächste Hauptmahlzeit nicht getrennt werden, wird der Faktor blockiert.

CGM-Werte zu `t₀` und Endpunkt werden bei Bedarf linear interpoliert. Die beiden umgebenden Messpunkte dürfen höchstens 15 Minuten auseinanderliegen. Ohne geeignete Punkte gibt es keinen Faktor.

Alle KH-Ereignisse und alle **vollen** schnell wirksamen IE im halboffenen Fenster `[t₀, Endpunkt)` gehen ein. Das umfasst Snacks, geteilte Boli und spätere Korrekturen; vorherige Injektionen und Basalinsulin gehen nicht ein. Für jedes Ereignis wird der zu seiner Uhrzeit gültige KF verwendet:

```text
ΔG = CGM(Endpunkt) - CGM(t₀)
C  = Σ(KHᵢ in g × KFᵢ)
I  = Σ(schnell wirksame IEⱼ × KFⱼ)
S  = bestätigte Sport-Senkung in mg/dL (negativ bei BZ-Anstieg, z. B. Adrenalin beim Rennsport), sonst 0

Insulinfaktor in IE/KE = 10 × (ΔG + S + I) / C
```

### Manueller Zeitraum

Im Tagebuch lässt sich der Zeitraum je Mahlzeit grafisch anpassen, um Tagebuchdaten zu bereinigen. Beginn und Endpunkt ersetzen dann die automatische Regel; ein späterer Abruf berechnet den Faktor mit diesem Zeitraum neu. Der Beginn darf bis zu zwei Stunden vor `t₀` liegen (z. B. für einen Spritz-Ess-Abstand) oder bis zu zwei Stunden danach (z. B. wenn tatsächlich später gegessen wurde als eingetragen). Der Mahlzeiteintrag selbst, also seine KH und ein darin erfasster Bolus, zählt immer voll, auch wenn der Beginn danach liegt; andere Ereignisse vor dem Beginn zählen nicht. Der Endpunkt liegt mindestens 30 Minuten nach `t₀` und nach dem Beginn, das Fenster ist höchstens zehn Stunden lang. Es gilt weiterhin das halboffene Fenster `[Beginn, Endpunkt)` mit vollen IE und allen KH; ΔG wird zwischen den CGM-Werten an Beginn und Endpunkt gebildet. Eine weitere Hauptmahlzeit im manuellen Fenster blockiert weiterhin. Der Zeitraum wird nur in SQLite gespeichert, nicht in Nightscout, und kann auf den Standard zurückgesetzt werden.

Sport wird je Mahlzeit bestätigt und bearbeitet; ein Vorschlag allein ändert den Faktor nicht. Ein unbekannter Insulintyp im Fenster oder ein nicht positiver Zähler blockiert den Faktor. Ein positiver KH-Nenner ist erforderlich.

Das Modell setzt voraus, dass alle KH und alle erfassten schnell wirksamen IE bis zum Endpunkt vollständig wirken, auch bei späten Ereignissen. Es setzt einen gemeinsamen Insulinfaktor für alle KH im Fenster und keinen zusätzlichen Hintergrundtrend voraus. Die bestätigte Sport-Senkung wird additiv auf `ΔG` angewendet; ein bestätigter Anstieg (Rennsport) wird abgezogen. Das sind Rechenannahmen, keine Aussagen über den tatsächlichen Wirkverlauf oder eine Dosierungsempfehlung.

## Analyse

Für die Spalten „Erwartet“ und „Delta“ wird der aktuelle Nightscout-I:C-Zeitplan verwendet:

```text
Erwartete Glukoseänderung =
    Σ(KHᵢ / I:Cᵢ × KFᵢ) - Σ(IEⱼ × KFⱼ) - S
Delta = gemessene Glukoseänderung - erwartete Glukoseänderung
```

Manuell ausgeklammerte Mahlzeiten bleiben in der Liste, gehen aber nicht in die Kennzahlen ein. Der Mittelwert und Median verwenden berechenbare, nicht ausgeklammerte Faktoren. Die mittleren 50 % werden ab fünf solchen Werten als Quartile mit linearer Interpolation an den Positionen `(n - 1) × 0,25` und `(n - 1) × 0,75` gezeigt. Ab sechs Werten wird der Median der letzten drei mit dem der drei davor verglichen.

Die Markierung „Stark abweichend“ erscheint ab drei berechenbaren Werten derselben Mahlzeitenart. Frühstück wird nach Arbeit und Frei getrennt. Für Wert `x`, Median `m` und Median der absoluten Abweichungen `MAD` gilt `|x - m| > max(3 × MAD, 0,5 × m)`. Auch bereits ausgeklammerte Werte gehen in diesen Vergleich ein. Die Markierung klammert selbst nichts aus.

Für die Nachtübersicht beginnt das Fenster drei Stunden nach der letzten erfassten schnell wirksamen Injektion zwischen Abendessen und folgendem Frühstück. Es endet beim Frühstück. `Veränderung = CGM(Ende) - CGM(Start)`; der Minimalwert ist das Minimum der Messwerte im Fenster einschließlich interpolierter Randwerte. Drei Stunden sind nur die Auswertungsgrenze des Modells.

## Stressaufschlag beim Frühstück (nur Info)

Arbeitsstress senkt die Insulinsensitivität, deshalb sind freie Tage die Basis: `Arbeit = Frei × (1 + Stressaufschlag)`. Die Analyse vergleicht für das Frühstück der letzten 30 Tage die Faktoren an freien Tagen mit denen an Arbeitstagen. Der Gesamtwert ist Median Arbeit ÷ Median Frei − 1. „Je freiem Tag“ teilt den Median des Frühstücks an den 2 Arbeitstagen davor und den 2 danach (höchstens 7 Tage entfernt, mindestens 2 Werte; für das letzte Wochenende fehlen die Tage danach noch) durch den Faktor des freien Tages, minus 1. Liegen diese Einzelwerte eng beieinander, ist der Aufschlag stabil und könnte später als allgemeiner Stressaufschlag dienen. Die Kachel ändert keinen Vorschlag.
