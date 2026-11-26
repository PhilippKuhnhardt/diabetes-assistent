# Calculations

The UI shows results, inputs and blocking reasons. Formulas, intermediate steps and model assumptions are described here. The implementation is authoritative: `app/dosing.py`, `app/calculation.py`, `app/forecast.py` and the use cases in `app/services/` (day, recommendation, analysis).

German terms in quotes are the labels the German UI shows; KH = carbs, KE = carb unit, IE = insulin unit, KF = correction factor, BZ/BG = blood glucose.

## Data and units

Nightscout supplies CGM readings, carbs, injections and the current profile. The current I:C and correction-factor schedule also applies to older analyses. The schedule entry is chosen by the local time of the event. All arithmetic uses `Decimal`.

- 1 KE = 10 g carbs.
- Insulin factor: IE/KE. Nightscout I:C: g/IE. Hence `IE/KE = 10 / (g/IE)`.
- Correction factor (KF): mg/dL per IE.

## Dose help

With carbs the recommendation uses only the calculated factor `A` in IE/KE (see Automatik factor below). For the entered carbs `KH`, the current CGM value `G`, the target `Z` and the time-dependent KF:

```text
carb part        = KH × A / 10
correction part  = (G - Z) / KF
suggestion       = max(0, carb part + correction part)
dose             = suggestion rounded to the nearest 0.5 IE
expected BG      = G + (carb part - dose) × KF
```

Three values are shown: **Normal** with the local target `Z` (default 100 mg/dL), **Vorsichtig** (cautious) with the cautious target (default 140 mg/dL) and **Nur KH** (carbs only) without a correction part: `max(0, carb part − sport rest / KF)`, expected BG `G normal + (carb part − dose) × KF`; this value needs no current CGM reading. If `A` has fewer than four meals, the breakfast median of the day type (below) or the Nightscout I:C value `R` stands in (`carb part = KH / R`). Without carbs there are only Normal and Vorsichtig as a pure correction. If the start of the latest main meal was 1 to 7.5 h ago and the Normal correction is positive, the ends **Wenig** (little: correction × 0.6, at least 1 IE less) and **Viel** (a lot: × 1.4) stand next to Normal instead of Vorsichtig (request of 15 Nov; late corrections are small, but the right one scatters by about ±1 IE; no minimum gap upwards because late on that cost more lows): in hindsight the right correction after meals was a median 0.96 times the suggestion, the quartiles 0.55 and 1.45 times (122 corrections and "BZ prüfen" moments 21 Sep–7 Nov, project files `analysen/korrektur-skala-2026-11-15.md`). Only the tendency from the rise after eating is marked (blue): the steepest 15-min slope of the smoothed CGM in the first hour above 2.0 mg/dL/min is steep → Wenig, below 1.0 flat → Viel, otherwise Normal; without enough CGM the mark "Schnell" (fast) counts as steep. The "BZ prüfen" (check BG) reminder names the same three values. The former lower target (option "Tiefer") was removed on 2026-11-26 at Philipp's request. The dose is rounded to 0.5 IE. The expected BG is calculated from this rounded dose and is a model value at full carb and insulin effect, not a glucose curve over time. A CGM reading may be at most 15 minutes old. Before writing to Nightscout the dose is recalculated with current data; a changed suggestion must be confirmed again. The displayed glucose trend is not used in the calculation.

**Dose slider (since 15 Nov, Philipp's request):** in the entry form a slider replaces the buttons; Vorsichtig and Nur KH are no longer shown there (the API still calculates them, the "BZ prüfen" reminder names the ends as before). The slider ranges from little to a lot of need around the Normal dose `N` (0.5 IE): correction `N × 0.6`, at least 1 IE less, to `N × 1.4` (quartiles above); meal `N × 0.75`, at least 1 IE less, to `max(N × 1.2, N + 0.5)` (quartiles of needed to given dose over 117 main meals: 0.75 / 1.16). The recommendation is `N`, for a correction after a meal the end the rise leans to (steep → lower, flat → upper). For the chosen dose `D` the app shows where BG lands about 4 h later: centre `G expected + (N − D) × KF`, around it the corridor earlier doses showed (10/25/50/75/90 % of the deviation from the expected value in mg/dL; correction steep −65/−41/−18/+31/+90, normal −72/−31/−11/+31/+64, flat −70/−6/+20/+65/+97, no tendency −73/−36/−4/+50/+80; meal −89/−44/−8/+33/+79; project files `analysen/korrektur-skala-2026-11-15.md`, addendum 3). The large number "erwartet in 4 h" (expected in 4 h) is the median (50 %, Philipp's request 16 Nov), below it "meist" (mostly) the middle half (25–75 %), the bar for it, the line 8 out of 10 cases; the slider's track is coloured by the centre (below 70 red, below 90 light red, 90–160 green, above yellow). Dragging fills the field "Eigene IE" (own IE), the button below the form saves. An own value in the field stops that; the slider then shows the landing for that value (also outside the ends). With a split the slider picks the total dose: the field gets the first part (`total × share`, 0.5 IE), the second part becomes `total − first part` (`dose_total_units`).

### What still acts

Before the correction, `G` is shifted by what still acts (derivation: analysis note `fiasp-wirkkurve.md` in the project folder):

```text
insulin rest   = Σ IE × still active(t) × KF at injection time          (rapid insulin of the last 6 h)
carb rest      = Σ carbs in g × (1 − t / absorption time) × A / 10 × KF at meal time
surcharge rest = Σ Z × share × still open(t)                              (surcharge method only, per main meal)
sport rest     = Σ sport effect × (1 − minutes since end / effect duration) (per session, full before its end)
lowers         = insulin rest + sport rest     (a negative sport effect such as racing counts as "raises")
G normal       = G − lowers + carb rest + surcharge rest
G cautious     = max(G − max(0, lowers − carb rest − surcharge rest), G normal − 60)
```

- still active: exponential Fiasp curve as in Loop (peak 55 min after a 10 min delay, 6 h duration): 100 % up to 15 min, 75 % after 1 h, 36 % after 2 h, 14 % after 3 h, 4 % after 4 h.
- Digestion (stomach, since 21 Nov, Philipp's request: carbs are digested, insulin lowers, BG is the sum, without rules for meals, snacks or time windows): every carb entry, whether meal, snack or plain carbs, enters a shared stomach 15 min after the entry. It releases at most 1 g/min, less when little is in it (rate = 1 × S / (S + 40) with S g in the stomach). Alone, 40 g are half digested after about 1 h and 90 % after 2.4 h, 120 g after 1.7 h and 3.6 h. A second meal while the first is still in the stomach digests more slowly. `A` is the Automatik factor of the meal kind by the entry's time of day, else the Nightscout I:C value.
- Surcharge: every carb entry gets the share its carbs add to what is still in the stomach: `Z × (share(stomach + carbs) − share(stomach))`, share = carbs / 40 g, at most 1. Starter, main course and dessert in one or in three entries thus add up to one meal's surcharge, without grouping by time; once the stomach is empty again, the next entry starts afresh. The dose help gives an entry exactly this share. It acts linearly from the entry over 5 h, with the entry's fat mark (plus fat surcharge) over 6 h, with a calculated fat/protein surcharge over its own duration. Backtest over 60 days (3 h forecast, 8–23 h): mean error 50.5 instead of 51.7 mg/dL with the earlier rules (linear absorption 2–3 h, surcharge 2–5 h, breakfast 1–4 h, meal = entries at most 60 min apart; project files `analysen/vereinfachung-2026-11-21.md`).
- Sport (since 22 Oct, Philipp's request): every session lowers BG by its sport effect, linearly over its sport type's effect duration from its end (settings `sport_durations_minutes`, default 300 min = 5 h, 30–1440), without confirmation. The sport effect is the sport type's default (settings) or the value Philipp set for this session when entering it or in "Tag durchgehen" (review the day) (local in SQLite, `sport_session_effects`; 0 = no effect). A session entered for later (up to 6 h ahead) already counts in full. Basis: after the gym, BG 6 h after the start was about 130 mg/dL lower than on days without sport, mostly 1–5 h after the end (10 sessions, project files `analysen/pruefstand-2026-10/gym.md`). That is how sport enters Normal, Vorsichtig, Nur KH, "Wirkt noch", the early warnings and "Wirkungen über den Tag".
- Normal and Nur KH use `G normal`, Vorsichtig uses `G cautious`: the forecast minus its typical deviation (60 mg/dL), but at least such that carb and surcharge rest only cancel the active insulin. Without open carbs that is `G − insulin rest`; after large meals Vorsichtig thus also covers a planned second part (bug of 8 Oct: before, 0.5 instead of 4.5 IE). Below 70 mg/dL the same calculation applies (no special rule any more, request of 23 Oct).
- The "Wirkt noch" (still acting) card in the diary (today) shows insulin rest ("senkt noch", still lowers), carb and surcharge rest ("hebt noch", still raises) with the time from which less than 5 mg/dL are left, and `G − insulin rest + carb rest + surcharge rest` as the expected value with the likely range (8 out of 10 cases): depending on how much still acts in total (insulin + carbs + surcharge), below 40 mg/dL −30 to +30, 40–150 mg/dL −50 to +60, from 150 mg/dL −75 to +90 (estimated offline, `on_board.FORECAST_BAND`). Calculations still use only the expected value. Up to 90 min after a main meal a hint says the forecast is least certain there.
- The factor per main meal (below) counts all IE in the window in full; it uses the curve only for injections before the window.
- "Wirkungen über den Tag" (effects over the day, in "Tag durchgehen") runs the same model every 5 minutes: how many mg/dL per hour are currently raised (carbs, surcharge) and lowered (insulin), and as a comparison line `BG(t − 3 h) − insulin acted since + carbs and surcharge acted since`. Carbs are valued with the day's Automatik factor. Shown are the mean deviation from measured BG and the largest deviation up and down from 40 mg/dL.

### Calculated fat/protein surcharge (transition, since 19 Nov 2026)

If the meal agent estimated fat and protein, the entry form of a main meal (only with the method "Perzentil + Mahlzeitzuschlag") offers two surcharges: **Fest** (fixed: the meal surcharge, with "Fettreich" plus the fat surcharge, as before) and **Berechnet** (calculated). Philipp picks; the choice is stored in the browser; the insulin options follow below as usual.

```text
FPE        = (9 × fat in g + 4 × protein in g) / 100
surcharge  = fpe_surcharge_mgdl × FPE (default 20 mg/dL per FPE, setting "Zuschlag je FPE"), at most 200 mg/dL
acts       = evenly from meal start until 3 h + 30 min × FPE, at most 8 h
split      = carbs (and correction) now, the surcharge (rounded to 0.5 IE) as the second part
```

- With "Berechnet", split is offered below `split_min_grams` too, as soon as the surcharge part is at least 2 IE; `split_min_units` still applies.
- On the meal card (diary → tap a meal) the surcharge can be switched afterwards too ("Zuschlag Fett/Eiweiß": Fest or Berechnet), as soon as the meal has an agent estimate with fat and protein. Saving sets or removes the mark; forecast and factor recalculate afterwards.
- The meal gets `[DAZ:<mg/dL>@<min>]` in its Nightscout note. "Wirkt noch", forecast, warnings, the second split part and the adjustment of earlier factors in the Automatik use this surcharge instead of the fixed one for this meal. Combinations (one meal fixed, the next calculated) therefore just work.
- The analysis tuning tables (surcharge off/20/40/…) calculate calculated meals with their stored value.
- Basis: project files `analysen/fett-eiweiss-2026-11-19.md` (5 meals, a first tendency). Recalibrate after 15–20 meals.

### Split bolus

From `split_min_grams` carbs (default 80 g, 0 = off), "Split" is preselected for a main meal, and for a main meal marked fat/protein-rich regardless of the carb amount (since 14 Nov); a snack gets none. If the Normal recommendation (full dose) is smaller than `split_min_units` (default 6.5 IE in 0.5 steps, 0 = always; Philipp's choice 6 Nov, raised from 5 to 6.5 IE on 17 Nov), e.g. because a low BG shrinks it, there is no split and everything is suggested at once; this also applies with a fat mark. Each option then shows the first part and the rest:

```text
now     = rounded to 0.5 IE (recommendation × split_first_percent / 100)     (default 60 %)
later   = recommendation − now, at meal + split_delay_minutes                (default 120 min)
```

- The option's expected value applies to the full recommendation, i.e. both parts.
- When an entry for today is saved, `later = chosen recommendation − IE actually entered` is stored as the mark `[DASP:<IE>@<min>]` in the meal's Nightscout note.
- The plan is open until a rapid insulin dose without carbs is entered more than 15 min after the meal, or until 3 h after the planned time. Until then the "Wirkt noch" card shows the second part (not included in the forecast), and an entry without carbs first offers the option "2. Teil" (second part); next to it are Normal and Vorsichtig from the current forecast (active insulin and open carbs).
- Food with carbs while the second part is open (e.g. a dessert, request of 23 Nov) gets only the dose for its own carbs: the correction from the forecast then counts only downwards (`min(G normal, Z)` instead of `G normal`, likewise for the cautious value with its target). The rise still to come (carb rest and surcharge of the main meal) remains covered by the second part; injected with the dessert, that insulin would already act before the late rise arrives. The expected value therefore assumes the second part. Such a dose with carbs does not close the plan; the second part is later calculated as always from the forecast, which then includes the dessert and its dose.
- Amount and timing of the second part follow the course of BG (approved 22 Oct, analysis in project files `analysen/pruefstand-2026-10/split-bolus.md`). The amount is the correction from the current forecast (like Normal); the planned amount is shown next to it for orientation only; without a fresh CGM reading the planned amount applies.

```text
early   60 min after the meal until 15 min before the planned time:
        BG ≥ 180 and rising ≥ 15 mg/dL in 15 min       → now (carbs act faster or were more)
due     from the planned time
wait    from the planned time, BG < 110 or falling more than 5 mg/dL in 15 min
        → wait at most 60 min longer, then due (the first part still lowers enough)
```

Before the end of the breakfast window, `R` depends on the day type (work day or free day) while work/free days are switched on in settings (`day_types`, off for a new installation since 2026-11-26; off, all breakfasts count together and breakfast is one Automatik factor like lunch and dinner). If the last 28 days before today hold at least three calculated, non-excluded breakfasts of the same day type, `carb part = KH × F / 10` with `F` = median of these breakfast factors in IE/KE; otherwise the Nightscout value remains. By default Monday to Friday are work days and Saturday and Sunday free. If vacation is active in settings, every day from switching it on counts as free; switching it off ends the vacation on the previous day. A manual choice for a day always takes precedence. The day type is stored only locally, not in Nightscout.

## Automatik factor (suggestion factor)

The calculated factor `A` comes from the calculated, non-excluded factors of the same meal (breakfast only of the same day type) in the last `Z` days before today, from four meals:

```text
A = weighted percentile p of the factors, weight = 0.5 ^ (age in days / half-life)
```

Adjustable are the period `Z` (8, 15 or 30 days, default 30; 8 days so that on Sunday the previous week's Saturday still counts), the half-life (default 7 days; shorter = new values count more) and the percentile `p` (default 37.5 %). Each factor sits in the middle of its cumulative weight, so that with equal weights exactly the linear interpolation at `(n − 1) × p` results. A percentile depends on the order, not the magnitude: a single extreme factor shifts it by at most its share of the weight.

Method "Perzentil + Mahlzeitzuschlag" (percentile + meal surcharge, `auto_method = surcharge`, selectable in settings): every main meal raises BG by a fixed surcharge `Z` regardless of its carbs (fat and protein; `auto_surcharge_mgdl`, default 40 mg/dL, adjustable 5–150), proportionally `Z × carbs / 40 g` below 40 g carbs. Before the percentile, every earlier factor is adjusted by this share: `F' = F − 10 × Z × share / Σ[carbs in g × KF]`. The dose is then `A' × KE + Z × share / KF` (plus correction and sport). The surcharge comes once per meal, i.e. the carb entries at most 60 min apart: an entry gets the share its carbs add to this meal's surcharge (so nothing more after 40 g). Derivation and backtest: analysis note `formel-grundbedarf.md` in the project folder.

Backtest matrix: for every meal of the last 90 days (independent of the analysis period) the app calculates, with the data before it, which factor each combination of period (8, 15, 30 days), half-life (1, 2, 3, 5, 7, 14 days) and percentile (25 %, configured, 50 %, configured + meal surcharge) would have suggested, and where BG would have landed at the endpoint: `BG' = G(endpoint) − (suggestion − factor) × Σ[carbs in g × KF] / 10`, all other doses unchanged. All cells evaluate the same meals (only those for which every combination has enough history). Counted are below 80, 80–160 and above 160 mg/dL; that is deliberately narrower than the Nightscout target range (80–180), since an end below 160 is expected after three hours. Also the mean and median of this BG at the endpoint and the factor's typical deviation. Period, weighting and percentile are model assumptions to try out, not dosing advice.

Analysis → Automatik: the same simulation, clearer. **Tuning rows** change one control each with the other values as configured: percentile (25, 30, 35, 40, 45, 50 %), surcharge (off, 20, 40, 60, 80 mg/dL; off = plain percentile), half-life (1, 2, 3, 5, 7, 14 days), period (8, 15, 30 days), each plus the configured value. Each row evaluates the meals all its options can calculate, so only the period row needs the 8-day history. Verdict per row: "besser" (better) if the best option (most in range, then fewest below 80) beats the configured one by more than max(2 meals, 5 %); else "verschiebt tief ↔ hoch" (shifts low ↔ high) if the count below 80 or above 160 spreads by more than that threshold; else "kein Unterschied" (no difference). **Current and alternatives** calculates all combinations of these options on the same meals and shows the trade-off line: combinations no other beats on "below 80" and "above 160" at once (on equal numbers the one with the fewest changed controls), with the difference to the setting. **How the Automatik calculates** shows per meal kind the current factor and the dose without correction for the typical meal (median of the carbs and of Σ[carbs × KF] / carbs of the meals used): `KE × factor + Z × share / KF`. Every option can be applied directly as a setting (new settings version from now on).

## Factor per main meal

A meal starts at `t₀`. Whoever injects first and eats shortly after has an injection-to-meal gap (since 15 Nov, Philipp's report: 10 IE at 06:43 and breakfast at 07:05 without insulin gave no factor without a manual fix): if a rapid-acting injection without carbs lies at most 30 minutes before the meal, the meal entry itself carries no rapid insulin, no carbs lie in between and no window of an earlier main meal already counts this injection (then it is that meal's correction), the window starts at the earliest such injection; it then counts in full and ΔG is measured from it. The default endpoint, the 120-minute rule and the snack assignment stay relative to `t₀`. The default endpoint is `t₀ + window_minutes` (default 180 minutes). Every rapid-acting injection inside the current window and before the next main meal can extend the endpoint to `injection + window_minutes`. If the next main meal is more than 120 minutes after `t₀` but before the endpoint, the last suitable CGM reading within 15 minutes before it is used as the endpoint. If the next main meal cannot be separated, the factor is blocked.

CGM values at `t₀` and the endpoint are linearly interpolated when needed. The two surrounding readings may be at most 15 minutes apart. Without suitable readings there is no factor.

All carb events and all **full** rapid-acting IE in the half-open window `[t₀, endpoint)` count. That includes snacks, split boluses and later corrections; basal insulin does not count. Rapid-acting injections before `t₀` count with the share of their effect that, by the "Wirkt noch" action curve, falls between `t₀` and the endpoint (since 14 Nov): the dose help subtracts this insulin as "Wirkt noch" from the dose, so it must also be in the factor, otherwise such meals' factor is measured too small and the insulin subtracted twice (22 % of meals, 36 mg/dL on average; analysis note `dosisrechnung-pruefung-2026-11-14.md`). Since factors rise on average as a result, the percentile was moved from 40 to 35 % at the same time. Each event uses the KF valid at its time:

```text
ΔG = CGM(endpoint) - CGM(t₀)
C  = Σ(carbsᵢ in g × KFᵢ)
I  = Σ(rapid IEⱼ × KFⱼ) + Σ(earlier IEₖ × KFₖ × share of effect in the window)
S  = sport lowering in mg/dL in the window (negative for a BG rise, e.g. adrenaline when racing), else 0

insulin factor in IE/KE = 10 × (ΔG + S + I) / C
```

### Manual window

In the diary the window of each meal can be adjusted graphically to clean up diary data. Start and endpoint then replace the automatic rule; a later load recalculates the factor with this window. The start may be up to two hours before `t₀` (e.g. for an injection-to-meal gap) or up to two hours after it (e.g. when the meal was actually eaten later than entered). The meal entry itself, i.e. its carbs and a bolus recorded in it, always counts in full, even if the start lies after it; other carbs before the start do not count, earlier injections only with their share of effect in the window. The endpoint lies at least 30 minutes after `t₀` and after the start; the window is at most ten hours long. The half-open window `[start, endpoint)` with full IE and all carbs still applies; ΔG is taken between the CGM values at start and endpoint. Another main meal inside the manual window still blocks. The window is stored only in SQLite, not in Nightscout, and can be reset to the default.

Sport counts automatically (since 22 Oct): `S` is the share of each session that, by the same curve as above (linear over the effect duration from the end), acts between the window's start and endpoint, times its sport effect. Example: gym until 18:00 with −70, dinner 19:00–22:00 → 3 of 5 h → S = 42. On the meal an own value can be set or "Kein Einfluss" (no effect) chosen; "Automatisch" returns to the curve. An unknown insulin type in the window or a non-positive numerator blocks the factor. A positive carb denominator is required.

The model assumes that all carbs and all rapid-acting IE recorded in the window act fully by the endpoint, also for late events. It assumes one shared insulin factor for all carbs in the window and no additional background trend. The sport lowering is applied additively to `ΔG`; a rise (racing) is subtracted. These are calculation assumptions, not statements about the actual course of action or dosing advice.

## Analysis

The columns "Erwartet" (expected) and "Delta" use the current Nightscout I:C schedule:

```text
expected glucose change =
    Σ(carbsᵢ / I:Cᵢ × KFᵢ) - Σ(IEⱼ × KFⱼ) - S
delta = measured glucose change - expected glucose change
```

Manually excluded meals stay in the list but do not enter the metrics. Mean and median use calculable, non-excluded factors. From five such values the middle 50 % are shown as quartiles with linear interpolation at the positions `(n - 1) × 0.25` and `(n - 1) × 0.75`. From six values the median of the last three is compared with that of the three before.

The mark "Stark abweichend" (strongly deviating) appears from three calculable values of the same meal kind. Breakfast is split into work and free days. For value `x`, median `m` and median absolute deviation `MAD`: `|x - m| > max(3 × MAD, 0.5 × m)`. Already excluded values also enter this comparison. The mark excludes nothing by itself.

For the night overview the window starts three hours after the last recorded rapid-acting injection between dinner and the following breakfast. It ends at breakfast. `change = CGM(end) - CGM(start)`; the minimum is the minimum of the readings in the window including interpolated edge values. The three hours are only the model's evaluation boundary.

## Breakfast stress surcharge (info only)

Work stress lowers insulin sensitivity, so free days are the baseline: `work = free × (1 + stress surcharge)`. For breakfast over the last 30 days the analysis compares factors on free days with those on work days. The overall value is median work ÷ median free − 1. "Je freiem Tag" (per free day) divides the median breakfast of the 2 work days before and the 2 after (at most 7 days away, at least 2 values; for the latest weekend the days after are still missing) by the free day's factor, minus 1. If these single values lie close together, the surcharge is stable and could later serve as a general stress surcharge. The tile changes no suggestion.
