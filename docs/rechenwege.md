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

## Faktor je Hauptmahlzeit

Eine Mahlzeit beginnt bei `t₀`. Der Standardendpunkt ist `t₀ + window_minutes` (Standard 180 Minuten). Jede schnell wirksame Injektion, die innerhalb des aktuellen Fensters und vor der nächsten Hauptmahlzeit liegt, kann den Endpunkt bis `Injektion + window_minutes` verlängern. Liegt die nächste Hauptmahlzeit mehr als 120 Minuten nach `t₀`, aber vor dem Endpunkt, wird der letzte geeignete CGM-Wert innerhalb von 15 Minuten davor als Endpunkt verwendet. Kann die nächste Hauptmahlzeit nicht getrennt werden, wird der Faktor blockiert.

CGM-Werte zu `t₀` und Endpunkt werden bei Bedarf linear interpoliert. Die beiden umgebenden Messpunkte dürfen höchstens 15 Minuten auseinanderliegen. Ohne geeignete Punkte gibt es keinen Faktor.

Alle KH-Ereignisse und alle **vollen** schnell wirksamen IE im halboffenen Fenster `[t₀, Endpunkt)` gehen ein. Das umfasst Snacks, geteilte Boli und spätere Korrekturen; vorherige Injektionen und Basalinsulin gehen nicht ein. Für jedes Ereignis wird der zu seiner Uhrzeit gültige KF verwendet:

```text
ΔG = CGM(Endpunkt) - CGM(t₀)
C  = Σ(KHᵢ in g × KFᵢ)
I  = Σ(schnell wirksame IEⱼ × KFⱼ)
S  = bestätigte Sport-Senkung in mg/dL, sonst 0

Insulinfaktor in IE/KE = 10 × (ΔG + S + I) / C
```

Sport wird je Mahlzeit bestätigt und bearbeitet; ein Vorschlag allein ändert den Faktor nicht. Ein unbekannter Insulintyp im Fenster oder ein nicht positiver Zähler blockiert den Faktor. Ein positiver KH-Nenner ist erforderlich.

Das Modell setzt voraus, dass alle KH und alle erfassten schnell wirksamen IE bis zum Endpunkt vollständig wirken, auch bei späten Ereignissen. Es setzt einen gemeinsamen Insulinfaktor für alle KH im Fenster und keinen zusätzlichen Hintergrundtrend voraus. Die bestätigte Sport-Senkung wird additiv auf `ΔG` angewendet. Das sind Rechenannahmen, keine Aussagen über den tatsächlichen Wirkverlauf oder eine Dosierungsempfehlung.

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
