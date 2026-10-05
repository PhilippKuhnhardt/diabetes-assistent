# Rechenwege

Die Oberfläche zeigt Ergebnisse, Eingaben und Blockierungsgründe. Formeln, Zwischenschritte und Modellannahmen stehen hier. Maßgeblich für die Implementierung sind `app/dosing.py`, `app/calculation.py`, `app/server.py` und `app/sensitivity.py`.

## Daten und Einheiten

Nightscout liefert CGM-Werte, Kohlenhydrate, Injektionen und das aktuelle Profil. Der aktuelle I:C- und Korrekturfaktor-Zeitplan gilt auch für ältere Analysen. Der Zeitplan wird nach der lokalen Uhrzeit des Ereignisses ausgewählt. Gerechnet wird mit `Decimal`.

- 1 KE = 10 g KH.
- Insulinfaktor: IE/KE. Nightscout-I:C: g/IE. Daher `IE/KE = 10 / (g/IE)`.
- Korrekturfaktor (KF): mg/dL je IE.

## Spritzhilfe

Für die eingegebenen Kohlenhydrate `KH`, den aktuellen CGM-Wert `G`, den Zielwert `Z`, den aktuellen Nightscout-I:C-Wert `R` in g/IE und den zeitabhängigen KF gilt:

```text
KH-Anteil        = KH / R
Korrekturanteil  = (G - Z) / KF
Vorschlag        = max(0, KH-Anteil + Korrekturanteil)
Spritzmenge      = Vorschlag auf nächste 0,5 IE gerundet
Erwarteter BG    = G + (KH-Anteil - Spritzmenge) × KF
```

Der normale Vorschlag verwendet den lokalen Zielwert (Standard 100 mg/dL), der vorsichtige 140 mg/dL. Die Spritzmenge wird auf 0,5 IE gerundet. Der erwartete BG wird aus dieser gerundeten Spritzmenge berechnet und ist ein Modellwert bei voller KH- und Insulinwirkung, kein zeitlicher Glukoseverlauf. Ein CGM-Wert muss höchstens 15 Minuten alt sein. Vor dem Schreiben nach Nightscout wird mit aktuellen Daten erneut gerechnet; ein geänderter Vorschlag muss erneut bestätigt werden. Der angezeigte Glukosetrend und schnell wirksame Injektionen der letzten drei Stunden werden nicht verrechnet.

Vor dem Ende des Frühstücksfensters hängt `R` von der Tagesart ab (Arbeitstag oder freier Tag). Liegen in den letzten 28 Tagen vor heute mindestens drei berechnete, nicht ausgeklammerte Frühstücke derselben Tagesart vor, gilt `KH-Anteil = KH × F / 10` mit `F` = Median dieser Frühstücksfaktoren in IE/KE; sonst bleibt der Nightscout-Wert. Die Tagesart ist standardmäßig Montag bis Freitag Arbeitstag, Samstag und Sonntag frei. Ist in den Einstellungen Urlaub aktiv, zählt jeder Tag ab dem Einschalten als frei; beim Ausschalten endet der Urlaub am Vortag. Eine manuelle Auswahl für einen Tag hat immer Vorrang. Die Tagesart wird nur lokal gespeichert, nicht in Nightscout.

## Automatik-Faktor (Vorschlagsfaktor)

Der dritte Vorschlag verwendet einen Faktor `A` aus den berechneten, nicht ausgeklammerten Faktoren derselben Mahlzeit (Frühstück nur derselben Tagesart) der 28 Tage vor heute, ab vier Mahlzeiten. Drei Methoden sind wählbar:

```text
28 Tage gleich         A = Perzentil p der Faktoren, alle gleich gewichtet
Gewichtet              A = gewichtetes Perzentil p, Gewicht = 0,5 ^ (Alter in Tagen / Halbwertszeit)
Gewichtet + Tageslage  A = gewichtetes Perzentil p × Tageslage
```

`p` ist das Automatik-Perzentil (Standard 37,5 %), die Halbwertszeit standardmäßig 7 Tage. Beim gewichteten Perzentil steht jeder Faktor in der Mitte seines kumulierten Gewichts, so dass sich bei gleichen Gewichten genau die lineare Interpolation an `(n − 1) × p` ergibt. Ein Perzentil hängt von der Reihenfolge ab, nicht vom Betrag: Ein einzelner extremer Faktor verschiebt es höchstens um seinen Gewichtsanteil.

Tageslage: Die letzten drei abgeschlossenen Mahlzeiten beliebiger Art (Endpunkt vor jetzt, Beginn höchstens 48 h zurück) werden je mit dem gewichteten Median ihrer eigenen Mahlzeit verglichen (`r = Faktor / Median`). Liegen alle drei `r` über 1 oder alle unter 1, ist die Tageslage das `r` mit der kleinsten Abweichung von 1, begrenzt auf 0,85 bis 1,15; sonst 1.

Rückblick-Test: Für jede Mahlzeit im Analysezeitraum rechnet die App mit den Daten davor aus, was jede Methode vorgeschlagen hätte, und vergleicht mit dem berechneten Faktor. Treffer: Abweichung höchstens ±10 %; „zu viel Insulin“: Vorschlag mehr als 10 % über dem Faktor. Zum Vergleich läuft derselbe Test zusätzlich mit 25 % und 50 % (Median) neben dem eingestellten Perzentil. Methode, Gewichtung und Tageslage sind Modellannahmen zum Ausprobieren, keine Dosierempfehlung.

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

Der Korrekturfaktorvergleich betrachtet je Zeitabschnitt nur nicht ausgeklammerte Mahlzeiten mit einer berechenbaren Abweichung. Die Exposition eines Abschnitts ist `Σ(KH / I:C) - Σ(IE)`. Der angezeigte Kandidat minimiert die Summe der absoluten modellierten Abweichungen: gewichteter Median von `aktueller KF + Delta / Exposition` mit Gewicht `|Exposition|`. Der I:C-Zeitplan bleibt dabei fest. Der Kandidat wird nicht nach Nightscout geschrieben.

Für die Nachtübersicht beginnt das Fenster drei Stunden nach der letzten erfassten schnell wirksamen Injektion zwischen Abendessen und folgendem Frühstück. Es endet beim Frühstück. `Veränderung = CGM(Ende) - CGM(Start)`; der Minimalwert ist das Minimum der Messwerte im Fenster einschließlich interpolierter Randwerte. Drei Stunden sind nur die Auswertungsgrenze des Modells.
