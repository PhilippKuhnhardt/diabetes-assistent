// "Analyse": evaluates several days and is read-only except exclusion and day type (page roles,
// AGENTS.md). Three sub-pages: Werte (the chosen period), Automatik (how the Automatik would have done,
// with "Übernehmen" for a settings change the server attaches) and Gerichte (the dish catalogue).
// The summary comes from /api/summary; every number, verdict and best cell is the server's.
import { html, useState, useEffect, useRef } from "./html.js";
import { api, post, toast, num, factorNum, signedChange, sportChange, timeOnly, signedPercent, dayDate, names,
  Icon, GlucoseChange, ExcludeButton } from "./lib.js";

const TABS = [["werte", "Werte"], ["automatik", "Automatik"], ["gerichte", "Gerichte"]];
const PERIODS = [[1, "1 Tag"], [3, "3 Tage"], [7, "7 Tage"], [14, "14 Tage"], [30, "30 Tage"]];
const mealsText = count => count + (count === 1 ? " Mahlzeit" : " Mahlzeiten");

const Empty = ({ children }) => html`<div class="empty">${children}</div>`;
const Section = ({ title, hint, children, className = "analysis-list panel" }) => html`<section class=${className}>
  <div class="section-head"><h2>${title}</h2><span class="hint" role=${hint?.role}>${hint?.text ?? hint}</span></div>${children}</section>`;
const Table = ({ head, children, compact }) => html`<div class="table-wrap"><table class=${"analysis-table" + (compact ? " compact" : "")}>
  <thead><tr>${head}</tr></thead><tbody>${children}</tbody></table></div>`;

// ---- Werte ----
const span = (low, high) => low == null ? "—" : Number(low) === Number(high) ? factorNum(low) : factorNum(low) + "–" + factorNum(high);
const Fact = ({ label, value, strong }) => html`<div class=${strong ? "key" : null}><dt>${label}</dt><dd>${value}</dd></div>`;
const suggestion = s => s.auto_units_per_ke != null ? factorNum(s.auto_units_per_ke) + (s.surcharge ? " + Zuschlag" : "") : "—";
const suggestionNote = s => "Vorschlag aus " + s.count + " (" + s.days + " T.)";
const Factor = ({ value }) => value == null ? "—" : html`${factorNum(value)} <small>IE/KE</small>`;

function SummaryCards({ groups }) {
  return ["breakfast", "lunch", "dinner"].map(kind => {
    const row = groups[kind];
    const current = (row.nightscout_units_per_ke || []).map(value => factorNum(value)).join(" / ") || "—";
    const breakfast = kind === "breakfast";
    return html`<div class="summary-card"><span class="meal-type">${names[kind]}</span>
      <div class="summary-primary"><span>Median</span><strong><${Factor} value=${row.count > 0 ? row.median_units_per_ke : null} /></strong></div>
      <dl class="factor-facts">
        ${!breakfast && html`<${Fact} label="Vorschlag" value=${suggestion(row.suggestion)} strong />`}
        <${Fact} label="Aktuell" value=${current} />
        ${row.q1_units_per_ke != null && html`<${Fact} label="Mittlere 50 %" value=${span(row.q1_units_per_ke, row.q3_units_per_ke)} />`}
      </dl>
      <p>${mealsText(row.count)}${breakfast ? "" : " · " + suggestionNote(row.suggestion)}</p>
      ${breakfast && html`<div class="breakfast-subcards">${["work", "free"].map(dayType => {
        const subset = row.by_day_type[dayType];
        return html`<div class="breakfast-subcard"><span>${dayType === "work" ? "Arbeit" : "Frei"}</span>
          <strong><${Factor} value=${subset.count ? subset.median_units_per_ke : null} /></strong>
          <dl class="factor-facts compact">
            <${Fact} label="Vorschlag" value=${suggestion(subset.suggestion)} strong />
            ${subset.q1_units_per_ke != null && html`<${Fact} label="Mittlere 50 %" value=${span(subset.q1_units_per_ke, subset.q3_units_per_ke)} />`}
          </dl><small>${mealsText(subset.count)}<br />${suggestionNote(subset.suggestion)}</small></div>`;
      })}</div>`}
    </div>`;
  });
}

function DayTypeSelect({ day, value, onDone }) {
  async function change(event) {
    const select = event.currentTarget;
    select.disabled = true;
    try {
      await post("/api/analysis/day-type", { day, day_type: select.value });
    } catch (error) {
      toast(error.message, true);
    } finally {
      select.disabled = false;
      await onDone();
    }
  }
  return html`<label class="day-type-label">Tag: <select class="day-type" value=${value} onChange=${change}>
    <option value="work">Arbeit</option><option value="free">Frei</option></select></label>`;
}

function Calculations({ groups, reload, onOpenMeal }) {
  const rows = ["breakfast", "lunch", "dinner"]
    .flatMap(kind => groups[kind].calculations.map(item => ({ ...item, kind })))
    .sort((a, b) => b.at.localeCompare(a.at));
  if (!rows.length) return { rows, node: html`<${Empty}>Für diesen Zeitraum sind keine Mahlzeiten im lokalen Datenbestand.<//>` };
  let previousDay = "";
  const body = rows.map(item => {
    const newDay = item.day !== previousDay;
    previousDay = item.day;
    const excluded = item.units_per_ke != null && item.excluded;
    const sport = item.sport || {};
    // Read only here: sport is set on the meal in "Tag durchgehen".
    const sportApplied = sport.status === "affected" || (sport.status === "auto" && sport.suggested);
    return html`
      ${newDay && html`<tr class="analysis-day-row"><th colspan="10" scope="colgroup">
        ${dayDate(item.day, { weekday: "long", day: "numeric", month: "long", year: "numeric" })}${" "}
        <${DayTypeSelect} day=${item.day} value=${item.day_type} onDone=${reload} /></th></tr>`}
      <tr class=${(excluded ? "analysis-excluded " : "") + (item.strong_outlier ? "analysis-outlier" : "")}>
        <td>${names[item.kind]}</td>
        <td><b>${item.units_per_ke != null
          ? html`${factorNum(item.units_per_ke)} IE/KE${item.strong_outlier && html` <span class="outlier-badge">Stark abweichend</span>`}${item.manual_window && html` <span class="manual-badge">Zeitraum manuell</span>`}`
          : html`<span class="summary-unavailable">Nicht berechenbar: ${item.reason || "Kein Faktor"}</span>`}</b></td>
        <td>${item.carbs_total == null ? "—" : num(item.carbs_total) + " g"}</td>
        <td>${item.rapid_units_in_window == null ? "—" : num(item.rapid_units_in_window) + " IE"}</td>
        <td><${GlucoseChange} value=${item.delta_mgdl} /></td>
        <td><${GlucoseChange} value=${item.expected_delta_mgdl} /></td>
        <td><${GlucoseChange} value=${item.deviation_mgdl} /></td>
        <td>${sportApplied ? html`<span class="sport-state is-applied"><${Icon} name="check" />${signedChange(sportChange(sport.effect_mgdl), 0)}${sport.status === "auto" && html` <small>auto</small>`}</span>` : "—"}</td>
        <td>${item.units_per_ke == null ? "—" : html`<${ExcludeButton} mealId=${item.id} day=${item.day} excluded=${excluded} onDone=${reload} />`}</td>
        <td><button type="button" class="icon-button open-meal-day" aria-label="Im Tag durchgehen öffnen" title="Im Tag durchgehen öffnen"
          onClick=${() => onOpenMeal(item.day, item.id)}><${Icon} name="diary" /></button></td>
      </tr>`;
  });
  return { rows, node: html`<${Table} head=${html`<th>Mahlzeit</th><th>Insulinfaktor</th><th>KH</th><th>Insulin</th><th>Gemessen</th><th>Erwartet</th><th>Delta</th><th>Sport</th><th>Wertung</th><th></th>`}>${body}<//>` };
}

function SportAnalysis({ analysis }) {
  const sessions = analysis?.sessions || [];
  const kinds = analysis?.kinds || [];
  const dateTime = value => new Date(value).toLocaleString("de-DE", {
    weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const mealCells = meal => {
    if (!meal) return html`<td colspan="6"><span class="hint">Keine Mahlzeit im Einflussbereich (${analysis.lookback_minutes} min davor bis Mahlzeitende)</span></td>`;
    return html`<td>${names[meal.meal]}<br /><small>${dateTime(meal.start)}–${timeOnly(meal.end)}</small></td>
      <td>${meal.factor_without_sport_units_per_ke == null ? "—" : factorNum(meal.factor_without_sport_units_per_ke) + " IE/KE"}${meal.baseline_units_per_ke != null &&
        html`<br /><small>Vergleich ${factorNum(meal.baseline_units_per_ke)} IE/KE</small>`}</td>
      <td><${GlucoseChange} value=${meal.delta_mgdl} /></td>
      <td><${GlucoseChange} value=${meal.expected_delta_mgdl} /></td>
      <td>${meal.effect_mgdl == null
        ? html`<span class="summary-unavailable">${meal.reason || "Nicht berechenbar"}</span>`
        : html`<b><${GlucoseChange} value=${meal.effect_mgdl} /></b>${meal.shared && html` <span class="outlier-badge">Mehrere Einheiten</span>`}${meal.excluded && html` <span class="outlier-badge">Ausgeklammert</span>`}`}</td>
      <td>${meal.confirmed_effect_mgdl == null ? "—" : signedChange(-Number(meal.confirmed_effect_mgdl), 0)}</td>`;
  };
  return html`<${Section} title="Sport & Rennsport · Wirkung auf den BG" hint=${sessions.length + (sessions.length === 1 ? " Einheit" : " Einheiten")}>
    <div class="summary-grid">${kinds.map(group => html`<div class="summary-card"><span class="meal-type">${group.label}</span>
      <div class="summary-primary"><span>Median Sportwirkung</span><strong><${GlucoseChange} value=${group.median_effect_mgdl} /></strong></div>
      ${group.effect_count > 1 && html`<p>Spanne ${signedChange(group.min_effect_mgdl)} bis ${signedChange(group.max_effect_mgdl)}</p>`}
      <p>${group.session_count}${group.session_count === 1 ? " Einheit" : " Einheiten"} · ${group.effect_count} von ${group.meal_count}${group.meal_count === 1 ? " Mahlzeit" : " Mahlzeiten"} auswertbar</p></div>`)}</div>
    ${sessions.length
      ? html`<${Table} head=${html`<th>Sport</th><th>Zeitraum</th><th>Mahlzeit</th><th>Faktor ohne Sport</th><th>Gemessen</th><th>Erwartet ohne Sport</th><th>Sportwirkung</th><th>Bestätigt</th>`}>
        ${sessions.flatMap(session => (session.meals.length ? session.meals : [null]).map(meal => html`<tr>
          <td><b>${session.label}</b>${session.notes && html`<br /><small>${session.notes}</small>`}</td>
          <td>${session.end ? html`${dateTime(session.start)}–${timeOnly(session.end)} <small>(${session.duration_minutes} min)</small>` : "—"}</td>
          ${mealCells(meal)}</tr>`))}<//>`
      : html`<${Empty}>Im Zeitraum sind keine Sport- oder Rennsport-Einträge im lokalen Datenbestand.<//>`}
  <//>`;
}

function NightSummary({ night, loading, failed }) {
  const metrics = [["Startwert", "median_start_glucose"], ["Endwert", "median_end_glucose"],
    ["Veränderung", "median_delta_mgdl"], ["Minimalwert", "median_minimum_glucose"]];
  const cell = (value, at) => value == null ? "—" : html`${num(value, 1)} mg/dL${at && html`<small class="night-error">${timeOnly(at)} Uhr</small>`}`;
  const cards = loading ? html`<${Empty}>Nachtübersicht wird geladen …<//>` : failed ? html`<${Empty}>Nachtübersicht nicht verfügbar.<//>`
    : metrics.map(([label, key]) => html`<div class="summary-card"><span>${label} · Median</span><strong>${night[key] == null ? "—"
      : label === "Veränderung" ? signedChange(night[key]) : html`${num(night[key], 1)} <small>mg/dL</small>`}</strong></div>`);
  return html`<${Section} title="Nachtübersicht" hint=${night ? night.count + " von " + (night.count + night.unavailable_count) + " Nächten mit vollständigen Werten" : ""}>
    <div class="summary-grid">${cards}</div>
    ${night && !loading && (night.nights.length
      ? html`<${Table} head=${html`<th>Nacht</th><th>Startwert</th><th>Endwert</th><th>Veränderung</th><th>Minimalwert</th>`}>${night.nights.map(item => html`<tr>
        <td>${dayDate(item.day, { weekday: "short", day: "numeric", month: "short" })}${item.reason && html`<small class="night-error">${item.reason}</small>`}</td>
        <td>${cell(item.start_glucose, item.start)}</td><td>${cell(item.end_glucose, item.end)}</td>
        <td>${signedChange(item.delta_mgdl)}</td><td>${cell(item.minimum_glucose, item.minimum_at)}</td></tr>`)}<//>`
      : html`<${Empty}>Für diesen Zeitraum liegen keine Nächte vor.<//>`)}
  <//>`;
}

function Werte({ days, setDays, summary, reload, onOpenMeal }) {
  const [progress, setProgress] = useState("");
  const { data, loading, failed } = summary;
  useEffect(() => {
    if (data?.profile_sync_error) setProgress("Korrekturfaktor-Profil nicht aktualisiert: " + data.profile_sync_error);
  }, [data]);
  async function syncRange(event) {
    const button = event.currentTarget;
    button.disabled = true;
    setProgress("Synchronisiere …");
    try {
      const result = await post("/api/sync", { days: Number(days) });
      setProgress(result.synced + " Tage synchronisiert" + (result.errors.length ? " · " + result.errors.length + " Fehler" : "") +
        (result.profile_sync_error ? " · Korrekturfaktor: " + result.profile_sync_error : ""));
      await reload();
    } catch (error) {
      setProgress(error.message);
    } finally {
      button.disabled = false;
    }
  }
  const calculations = data && !loading ? Calculations({ groups: data.groups, reload, onOpenMeal }) : null;
  return html`<div id="analysis-werte" class="analysis-pane">
    <div class="analysis-controls">
      <label>Zeitraum <select id="summary-days" value=${String(days)} onChange=${event => setDays(Number(event.currentTarget.value))}>
        ${PERIODS.map(([value, label]) => html`<option value=${String(value)}>${label}</option>`)}</select></label>
      <button id="sync-range" class="primary" type="button" onClick=${syncRange}>Zeitraum aus Nightscout laden</button>
      <span id="sync-progress" class="hint">${progress}</span>
    </div>
    <div id="summary" class="summary-grid">${loading ? html`<${Empty}>Analyse wird geladen …<//>`
      : failed ? html`<${Empty}>Analyse nicht verfügbar.<//>` : data && html`<${SummaryCards} groups=${data.groups} />`}</div>
    <${Section} title="Alle Kalkulationen" hint=${calculations ? mealsText(calculations.rows.length) : ""}>
      <div id="calculations">${calculations?.node}</div><//>
    ${!failed && html`<${SportAnalysis} analysis=${data?.sport_analysis} />`}
    <${NightSummary} night=${data ? data.night || { count: 0, unavailable_count: Number(days), nights: [] } : null} loading=${loading} failed=${failed} />
  </div>`;
}

// ---- Automatik ----
const tuningLabel = (key, value) => key === "percentile" ? num(value, 1) + " %"
  : key === "surcharge" ? (Number(value) ? num(value, 0) + " mg/dL" : "aus")
  : key === "half_life" ? num(value, 1) + (Number(value) === 1 ? " Tag" : " Tage")
  : num(value, 0) + " Tage";
const tuningNames = { percentile: "Perzentil", surcharge: "Zuschlag", half_life: "Halbwertszeit", lookback: "Zeitraum" };
const tuningHints = {
  percentile: "Welcher Wert aus den bisherigen Faktoren genommen wird. Tiefer = weniger Insulin.",
  surcharge: "Fester BZ-Anstieg je Hauptmahlzeit für Fett und Eiweiß, als Insulin zusätzlich zu den KH. Aus = nur Perzentil.",
  half_life: "Nach dieser Zeit zählt eine Mahlzeit nur noch halb. Kürzer = neue Werte zählen stärker.",
  lookback: "Wie viele Tage Vorgeschichte in den Vorschlag eingehen.",
};
const strategyText = o => num(o.percentile, 1) + " % · " + (Number(o.surcharge_mgdl) ? "Zuschlag " + num(o.surcharge_mgdl, 0) + " mg/dL" : "ohne Zuschlag") +
  " · Halbwertszeit " + num(o.half_life_days, 1) + " T. · " + o.lookback_days + " Tage";

function RangeBar({ below, inRange, above }) {
  const total = below + inRange + above || 1, pct = value => (value * 100 / total).toFixed(2);
  return html`<svg class="range-bar" viewBox="0 0 100 8" preserveAspectRatio="none" role="img"
    aria-label=${below + " unter 80, " + inRange + " im Bereich, " + above + " über 160"}>
    <rect class="range-low" x="0" width=${pct(below)} height="8" />
    <rect class="range-ok" x=${pct(below)} width=${pct(inRange)} height="8" />
    <rect class="range-high" x=${(Number(pct(below)) + Number(pct(inRange))).toFixed(2)} width=${pct(above)} height="8" /></svg>`;
}
const RangeCounts = ({ o }) => html`<span class="range-nums"><span class="range-low">${o.below}</span> · <b class="range-ok">${o.in_range}</b> · <span class="range-high">${o.above}</span>${o.median_glucose != null &&
  html`<small class="range-median">Median ${num(o.median_glucose, 0)} mg/dL</small>`}</span>`;
const Delta = ({ value, label }) => html`<span class=${Number(value) < 0 ? "delta-good" : Number(value) > 0 ? "delta-bad" : ""}>
  ${(Number(value) > 0 ? "+" : Number(value) < 0 ? "−" : "±") + Math.abs(Number(value)) + " " + label}</span>`;

function Explain({ d }) {
  const surcharge = Number(d.active_surcharge_mgdl);
  const weights = (d.weights || []).map(item => (item.age_days === 1 ? "gestern " : "vor " + item.age_days + " Tagen ") + num(item.weight, 2)).join(" · ");
  const exampleName = item => item.meal === "breakfast" ? "Frühstück " + (item.day_type === "work" ? "Arbeit" : "Frei") : names[item.meal];
  return html`<div class="automatik-chips">${[
      ["Perzentil", num(d.percentile, 1) + " %"], ["Zuschlag", surcharge ? num(surcharge, 0) + " mg/dL" : "aus"],
      ["Halbwertszeit", num(d.half_life_days, 1) + " Tage"], ["Zeitraum", d.lookback_days + " Tage"]]
      .map(([label, value]) => html`<span class="chip">${label} <b>${value}</b></span>`)}</div>
    <ol class="automatik-steps">
      <li>Alle gültigen Mahlzeiten derselben Art aus den letzten ${d.lookback_days} Tagen (Frühstück getrennt nach Arbeit und Frei). Ältere zählen weniger: ${weights}.</li>
      ${surcharge > 0 && html`<li>Von jedem Faktor wird der Zuschlag abgezogen: ${num(surcharge, 0)} mg/dL je Hauptmahlzeit, umgerechnet in IE/KE dieser Mahlzeit (bis 40 g KH anteilig weniger).</li>`}
      <li>Davon der gewichtete ${num(d.percentile, 1)}-%-Wert. Das ist der Faktor für die KH.</li>
      <li>Dosis = KE × Faktor${surcharge > 0 && " + Zuschlag (" + num(surcharge, 0) + " mg/dL ÷ KF, einmal je Hauptmahlzeit, nicht für Snacks)"}, dazu wie immer die Korrektur auf den Zielwert.</li></ol>
    <p class="hint">Beispiel mit einer typischen Mahlzeit (Median der KH und des KF der verwendeten Mahlzeiten), ohne Korrektur:</p>
    <${Table} compact head=${html`<th>Mahlzeit</th><th>Mahlzeiten</th><th>Faktor</th><th>Typisch</th><th>KH-Teil</th><th>Zuschlag</th><th>Summe</th>`}>
      ${d.examples.map(item => item.units_per_ke == null
        ? html`<tr><td>${exampleName(item)}</td><td>${item.count}</td><td colspan="5" class="hint">Zu wenige Mahlzeiten (mind. ${item.min_meals})</td></tr>`
        : html`<tr><td>${exampleName(item)}</td><td>${item.count}</td><td><b>${factorNum(item.units_per_ke)}</b> IE/KE</td>
          <td>${num(item.carbs_g, 0)} g · KF ${num(item.isf, 0)}</td>
          <td>${num(Number(item.carbs_g) / 10, 1)} KE × ${factorNum(item.units_per_ke)} = ${num(item.carb_units, 1)} IE</td>
          <td>${surcharge ? "+ " + num(item.surcharge_units, 1) + " IE" : "—"}</td><td><b>${num(item.total_units, 1)} IE</b></td></tr>`)}<//>`;
}

function Tradeoffs({ t, apply }) {
  const current = t.current;
  const points = t.cloud;
  const maxX = Math.max(1, ...points.map(p => p.below)), maxY = Math.max(1, ...points.map(p => p.above));
  const W = 320, H = 200, L = 34, B = 30, x = v => L + v / maxX * (W - L - 12), y = v => H - B - v / maxY * (H - B - 12);
  const ticks = (max, draw) => Array.from(new Set([0, Math.round(max / 2), max])).map(draw);
  const value = (o, key) => key === "percentile" ? o.percentile : key === "surcharge" ? o.surcharge_mgdl : key === "half_life" ? o.half_life_days : o.lookback_days;
  const Option = ({ o, isCurrent }) => html`<li class=${"tradeoff" + (isCurrent ? " is-current" : "")}>
    <div class="tradeoff-head"><b>${isCurrent ? "Aktuell" : o.changes.map(key => tuningNames[key] + " " + tuningLabel(key, value(o, key))).join(" · ")}</b>
      <small>${strategyText(o)}</small>
      ${!isCurrent && html`<span class="tradeoff-delta"><${Delta} value=${o.delta_below} label="zu tief" /> · <${Delta} value=${o.delta_above} label="zu hoch" />${o.delta_median_glucose != null &&
        " · Median " + (Number(o.delta_median_glucose) >= 0 ? "+" : "−") + num(Math.abs(Number(o.delta_median_glucose)), 0) + " mg/dL"}</span>`}</div>
    <${RangeBar} below=${o.below} inRange=${o.in_range} above=${o.above} /><${RangeCounts} o=${o} />
    ${isCurrent ? html`<span class="hint">eingestellt</span>` : html`<button type="button" class="quiet apply-button" onClick=${event => apply(event, o.apply)}>Übernehmen</button>`}</li>`;
  return html`<div class="tradeoff-layout">
    <svg class="tradeoff-plot" viewBox=${"0 0 " + W + " " + H} role="img" aria-label="Unter 80 gegen über 160 für alle Kombinationen">
      ${ticks(maxX, v => html`<text x=${x(v)} y=${H - B + 16} text-anchor="middle">${v}</text>`)}
      ${ticks(maxY, v => html`<text x=${L - 8} y=${y(v) + 4} text-anchor="end">${v}</text>`)}
      <line class="axis" x1=${L} y1=${H - B} x2=${W - 6} y2=${H - B} /><line class="axis" x1=${L} y1=${H - B} x2=${L} y2="6" />
      <text class="axis-label" x=${W - 6} y=${H - 2} text-anchor="end">unter 80 →</text>
      <text class="axis-label" x=${L + 4} y="12">über 160 ↑</text>
      ${points.map(p => html`<circle class="cloud" cx=${x(p.below)} cy=${y(p.above)} r="3" />`)}
      <polyline class="line" points=${t.line.map(o => x(o.below) + "," + y(o.above)).join(" ")} />
      ${t.line.map(o => html`<circle class="on-line" cx=${x(o.below)} cy=${y(o.above)} r="4.5" />`)}
      <circle class="current" cx=${x(current.below)} cy=${y(current.above)} r="7" /></svg>
    <div><ul class="tradeoff-list"><${Option} o=${current} isCurrent />${t.line.filter(o => o.changes.length).map(o => html`<${Option} o=${o} />`)}</ul>
      <p class="hint">Jeder Punkt ist eine Kombination der vier Regler, alle auf denselben ${t.meal_count} Mahlzeiten. Auf der Linie liegen die, die keine andere bei „unter 80“ und „über 160“ zugleich schlägt: Nach links weniger zu tief, dafür mehr zu hoch. ${t.line.some(o => !o.changes.length)
        ? "Deine Einstellung liegt auf der Linie." : "Deine Einstellung (Ring) liegt nicht auf der Linie, mindestens eine Alternative ist in beiden Richtungen gleich gut oder besser."} Unterschiede von 1–2 Mahlzeiten können Zufall sein.</p></div></div>`;
}

function Tuning({ d, apply }) {
  const verdictText = row => row.verdict === "better" ? "besser: " + tuningLabel(row.key, row.options.find(o => o.best).value)
    : row.verdict === "shift" ? "verschiebt tief ↔ hoch" : "kein Unterschied";
  return html`${d.tuning.map(row => html`<details class="tuning-row" open=${row.verdict !== "none"}><summary>
      <b>${tuningNames[row.key]}</b><span class=${"verdict verdict-" + row.verdict}>${verdictText(row)}</span><small>${row.meal_count} Mahlzeiten</small></summary>
      <p class="hint">${tuningHints[row.key]}</p>
      <ul class="tuning-options">${row.options.map(o => html`<li class=${(o.active ? "is-active" : "") + (o.best ? " is-best" : "")}>
        <span class="tuning-label">${tuningLabel(row.key, o.value)}${o.active && html` <small>aktiv</small>`}</span>
        <${RangeBar} below=${o.below} inRange=${o.in_range} above=${o.above} /><${RangeCounts} o=${o} />
        ${o.active ? html`<span></span>` : html`<button type="button" class="quiet apply-button" onClick=${event => apply(event, o.apply)}>Übernehmen</button>`}</li>`)}</ul></details>`)}
    <p class="hint range-legend"><span class="range-low">■ unter 80</span> <span class="range-ok">■ 80–160</span> <span class="range-high">■ über 160</span> · Median = mittlerer BZ am Endpunkt dieser Mahlzeiten (Ziel ${num(d.dose_target_mgdl, 0)} mg/dL). Jede Zeile prüft die Mahlzeiten, die alle ihre Optionen berechnen können. „Kein Unterschied“: die Optionen liegen höchstens ${num(d.tuning[0].noise_meals, 1)} Mahlzeiten auseinander.</p>`;
}

// Backtest matrix: where the endpoint glucose would have landed with each lookback, weighting
// strength (half-life) and percentile. The server marks the active cell and the best cells.
function Derivation({ d }) {
  const low = Number(d.glucose_low_mgdl), high = Number(d.glucose_high_mgdl);
  const variants = d.variants || [];
  const percentiles = variants.length ? variants[0].cells.map(cell => cell.percentile) : [];
  const total = variants.length ? variants[0].cells[0].stats.glucose_count : 0;
  if (!total) return html`<p class="hint derivation-note">In den letzten ${d.backtest_days} Tagen fehlen Mahlzeiten mit genug Vorgeschichte (mind. ${d.min_meals} Faktoren derselben Mahlzeit in jedem Zeitraum davor, also auch in den letzten 8 Tagen).</p>`;
  const strength = days => { const diff = Number(days) - 7; return html`${num(days, 1)} T.${diff < 0 ? html` <small>stärker</small>` : diff > 0 ? html` <small>schwächer</small>` : ""}`; };
  const Cell = ({ variant, cell }) => {
    const [below, inRange, above] = ["below", "in_range", "above"].map(key => Number(cell.shares[key]));
    const isBest = cell.best === "overall", isPeriodBest = Boolean(cell.best);
    const notes = [isBest ? "bestes gesamt" : isPeriodBest ? "bestes " + variant.lookback_days + " T." : "",
      cell.stats.median_abs_deviation == null ? "" : "±" + num(Number(cell.stats.median_abs_deviation) * 100, 0) + " %"];
    return html`<td class=${"matrix-cell" + (cell.active ? " is-active" : "") + (isPeriodBest ? " is-best" : "") + (isBest ? " is-overall-best" : "")}>
      ${cell.active && html`<span class="active-badge">Aktiv</span>`}
      <svg class="range-bar" viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true">
        <rect class="range-low" x="0" width=${below.toFixed(2)} height="8" /><rect class="range-ok" x=${below.toFixed(2)} width=${inRange.toFixed(2)} height="8" />
        <rect class="range-high" x=${(below + inRange).toFixed(2)} width=${above.toFixed(2)} height="8" /></svg>
      <span class="range-nums"><span class="range-low">${num(below, 0)}</span> · <b class="range-ok">${num(inRange, 0)} %</b> · <span class="range-high">${num(above, 0)}</span></span>
      ${cell.stats.mean_glucose != null && html`<span class="range-glucose">Ø ${num(cell.stats.mean_glucose, 0)} · Median ${num(cell.stats.median_glucose, 0)} mg/dL</span>`}
      <small>${notes.filter(Boolean).join(" · ")}</small></td>`;
  };
  return html`<p class="hint derivation-note range-legend"><span class="range-low">■ ${"<"} ${num(low, 0)}</span> <span class="range-ok">■ ${num(low, 0)}–${num(high, 0)}</span> <span class="range-high">■ ${">"} ${num(high, 0)}</span> · Anteil der Mahlzeiten mit BZ am Endpunkt in diesem Bereich, darunter Durchschnitt und Median dieses BZ, dazu typische Abweichung des Faktors. Grün umrandet: bestes Feld je Zeitraum (meiste im Bereich, dann wenigste unter ${num(low, 0)}), kräftig: bestes gesamt. Dunkel umrandet mit „Aktiv“: deine Einstellung.</p>
    <div class="table-wrap"><table class="analysis-table compact matrix"><thead><tr><th>Zeitraum</th><th>Halbwertszeit</th>
      ${percentiles.map(value => html`<th>${value === "surcharge" ? html`${num(d.percentile, 1)} % + Zuschlag <small>${num(d.surcharge_mgdl, 0)} mg/dL je Mahlzeit</small>`
        : html`${num(value, 1)} %${Number(value) === 50 && html` <small>Median</small>`}`}</th>`)}</tr></thead>
      <tbody>${variants.map((variant, index) => {
        const first = index === 0 || variants[index - 1].lookback_days !== variant.lookback_days;
        return html`<tr class=${first ? "matrix-group" : null}><td>${first ? variant.lookback_days + " Tage" : ""}</td><td>${strength(variant.half_life_days)}</td>
          ${variant.cells.map(cell => html`<${Cell} variant=${variant} cell=${cell} />`)}</tr>`;
      })}</tbody></table></div>
    <p class="hint derivation-note">Getestet werden dieselben ${total}${total === 1 ? " Mahlzeit" : " Mahlzeiten"} der letzten ${d.backtest_days} Tage für alle Felder, unabhängig vom Zeitraum unter „Werte“. Jede Mahlzeit bekommt den Vorschlag, den das Feld aus den Tagen davor berechnet hätte; gezeigt wird der BZ am Endpunkt mit diesem Faktor bei Korrektur auf den Zielwert (Modellrechnung: Ziel + (tatsächlicher − vorgeschlagener Faktor) × Σ[KH × KF] / 10, nur der Faktorfehler zählt). Kürzere Halbwertszeit = neue Werte zählen stärker. Zuschlag: eingestelltes Perzentil der Faktoren ohne einen festen Anstieg von ${num(d.surcharge_mgdl, 0)} mg/dL je Hauptmahlzeit (Fett/Eiweiß, bis ${num(d.surcharge_full_grams, 0)} g KH anteilig), dazu dieser Zuschlag für die jeweilige Mahlzeit.</p>`;
}

// Information only: how much more insulin per KE breakfast needs on work days than on free days.
function DayTypeRatio({ d }) {
  const pct = number => (Number(number) >= 0 ? "+" : "−") + Math.abs(Number(number) * 100).toLocaleString("de-DE", { maximumFractionDigits: 0 }) + " %";
  const value = (median, count) => median == null ? "—" : html`${factorNum(median)} <small>(${count})</small>`;
  return html`<${Table} compact head=${html`<th>Frei <small>Median IE/KE</small></th><th>Arbeit <small>Median IE/KE</small></th><th>Stressaufschlag</th><th>Je freiem Tag</th>`}>
    ${Object.values(d.meals).map(m => html`<tr><td>${value(m.free_median, m.free_count)}</td><td>${value(m.work_median, m.work_count)}</td>
      <td><b>${m.surcharge == null ? "—" : pct(m.surcharge)}</b></td>
      <td class="surcharge-cell">${m.singles.length ? html`${pct(m.single_median)} <small>(${m.singles.length}, ${pct(m.single_min)} bis ${pct(m.single_max)})</small><br />
        <small class="surcharge-days">${m.singles.map(item => dayDate(item.day, { weekday: "short", day: "numeric", month: "numeric" }) + " " + pct(item.surcharge)).join(" · ")}</small>` : "—"}</td></tr>`)}<//>
    <p class="hint derivation-note">Frei ist die Basis: Arbeit = Frei × (1 + Stressaufschlag). „Je freiem Tag“ vergleicht jeden freien Tag mit dem Frühstück an den ${d.neighbours} Arbeitstagen davor und den ${d.neighbours} danach (Median, höchstens ${d.reference_days} Tage entfernt, mind. 2 Werte). Liegen diese Einzelwerte eng beieinander, ist der Aufschlag stabil. Nur zur Info, der Vorschlag bleibt unverändert.</p>`;
}

function Automatik({ summary, getSettings, onSettingsChanged, reload }) {
  const { data, loading } = summary;
  const d = data?.suggestion_derivation;
  const [applying, setApplying] = useState(false);
  // Each "Übernehmen" saves the change the server attached; the toast offers the previous values back.
  async function apply(event, change) {
    const previous = Object.fromEntries(["auto_percentile", "auto_half_life_days", "auto_lookback_days", "auto_method", "auto_surcharge_mgdl"]
      .map(key => [key, getSettings()?.[key]]).filter(([, value]) => value != null));
    const save = async (payload, message) => {
      setApplying(true);
      try {
        await post("/api/settings", payload);
        toast(message, false, payload === change ? { label: "Rückgängig", run: () => save(previous, "Vorherige Einstellung wiederhergestellt") } : null);
        await onSettingsChanged();
        await reload();
      } catch (error) {
        toast(error.message, true);
      } finally {
        setApplying(false);
      }
    };
    await save(change, "Automatik-Einstellung übernommen");
  }
  const t = d?.tradeoffs;
  return html`<div id="analysis-automatik" class="analysis-pane" inert=${applying}>
    <${Section} title="So rechnet die Automatik" hint=${{ text: loading ? "Wird berechnet …" : "", role: "status" }}>
      <div id="automatik-explain">${d?.tuning && html`<${Explain} d=${d} />`}</div><//>
    <${Section} title="Aktuell und Alternativen" hint=${t ? t.meal_count + " Mahlzeiten" : ""}>
      <div id="automatik-tradeoffs">${d?.tuning && (t.meal_count ? html`<${Tradeoffs} t=${t} apply=${apply} />`
        : html`<${Empty}>In den letzten ${d.backtest_days} Tagen fehlen Mahlzeiten mit genug Vorgeschichte (mind. ${d.min_meals} derselben Art in den 8 Tagen davor).<//>`)}</div><//>
    <${Section} title="Was bringt welcher Regler?" hint=${t?.meal_count ? "Je Regler nur dieser geändert" : ""}>
      <div id="automatik-tuning">${d?.tuning && t.meal_count > 0 && html`<${Tuning} d=${d} apply=${apply} />`}</div><//>
    <details class="analysis-list panel analysis-details">
      <summary><h2>Alle Kombinationen</h2><span class="hint">${d ? "Aktiv: " + d.lookback_days + " T. · Halbwertszeit " + num(d.half_life_days, 1) +
        " T. · " + num(d.percentile, 1) + " %" + (d.method === "surcharge" ? " + Zuschlag" : "") : ""}</span></summary>
      <div id="suggestion-derivation">${d && html`<${Derivation} d=${d} />`}</div></details>
    <details class="analysis-list panel analysis-details">
      <summary><h2>Stressaufschlag beim Frühstück</h2><span class="hint">${data?.day_type_ratio ? "Letzte " + data.day_type_ratio.days + " Tage" : ""}</span></summary>
      <div id="day-type-ratio">${data?.day_type_ratio && html`<${DayTypeRatio} d=${data.day_type_ratio} />`}</div></details>
  </div>`;
}

// ---- Gerichte: each dish with how its main meals turned out ----
const MEAL_NAMES = { breakfast: "Frühstück", lunch: "Mittag", dinner: "Abend" };

function Dish({ dish, all, open, onToggle, act }) {
  const [armed, setArmed] = useState(false);
  const outcome = dish.outcome;
  const others = all.filter(other => other.id !== dish.id);
  return html`<details class="dish-item" open=${open} onToggle=${event => onToggle(event.currentTarget.open)}>
    <summary><strong>${dish.name}</strong>${dish.category && html` <span class="place-badge">${dish.category}</span>`}${dish.archived && html` <span class="outlier-badge">archiviert</span>`}
      <span class="hint dish-hint">${dish.hint}</span></summary>
    ${outcome.meals > 0 && html`<p class="hint">${outcome.meals} Hauptmahlzeiten im Zeitraum, davon ${outcome.valid} mit Vergleich${outcome.delta_mgdl != null &&
      " · BZ-Änderung im Median " + signedChange(outcome.delta_mgdl, 0)}${outcome.end_mgdl != null && " · Endwert im Median " + outcome.end_mgdl + " mg/dL"}</p>`}
    ${dish.history.length
      ? html`<${Table} compact head=${html`<th>Mahlzeit</th><th>KH</th><th>Insulin</th><th>Faktor</th><th>zu üblich</th><th>BZ</th><th>Ort</th>`}>
        ${dish.history.map(row => html`<tr><td>${dayDate(row.day, { day: "numeric", month: "numeric" })} ${MEAL_NAMES[row.meal] || ""}</td>
          <td>${row.carbs_g != null ? num(row.carbs_g, 0) + " g" : "—"}</td><td>${row.rapid_units != null ? num(row.rapid_units) + " IE" : "—"}</td>
          <td>${row.factor != null ? num(row.factor, 2) : row.excluded ? "ausgeklammert" : "kein Faktor"}</td>
          <td>${row.ratio != null ? signedPercent(row.ratio) : "—"}</td>
          <td>${row.g0_mgdl != null && row.g_end_mgdl != null ? num(row.g0_mgdl, 0) + " → " + num(row.g_end_mgdl, 0) : "—"}</td><td>${row.place || ""}</td></tr>`)}<//>`
      : html`<p class="hint">Noch keine Hauptmahlzeit mit diesem Gericht im Zeitraum.</p>`}
    <div class="dish-edit">
      <input type="text" class="dish-name" maxlength="60" value=${dish.name} aria-label="Name des Gerichts"
        onChange=${event => act({ action: "rename", id: dish.id, name: event.currentTarget.value })} />
      <input type="text" class="dish-category" maxlength="40" value=${dish.category} placeholder="Kategorie" aria-label="Kategorie"
        onChange=${event => act({ action: "category", id: dish.id, category: event.currentTarget.value })} />
      ${others.length > 0 && html`<select class="dish-merge" aria-label="Zusammenlegen mit" value=""
        onChange=${event => event.currentTarget.value && act({ action: "merge", id: dish.id, into_id: event.currentTarget.value })}>
        <option value="">Zusammenlegen mit …</option>${others.map(other => html`<option value=${String(other.id)}>${other.name}</option>`)}</select>`}
      <button type="button" class="quiet" onClick=${() => act({ action: "archive", id: dish.id, archived: !dish.archived })}>${dish.archived ? "Wieder nutzen" : "Archivieren"}</button>
      <button type="button" class="quiet delete-treatment" onClick=${() => armed ? act({ action: "delete", id: dish.id }) : setArmed(true)}>${armed ? "Wirklich löschen?" : "Löschen"}</button>
    </div></details>`;
}

function Dishes() {
  const [data, setData] = useState(null);
  const [open, setOpen] = useState(() => new Set());
  // Each change sends the action; the answer is the whole catalogue, drawn afresh.
  const act = async body => {
    try { setData(await post("/api/dishes", body)); }
    catch (error) { toast(error.message, true); setData(current => current && { ...current }); }
  };
  useEffect(() => { api("/api/dishes").then(setData).catch(error => toast(error.message, true)); }, []);
  const dishes = data?.dishes || [];
  return html`<div id="analysis-gerichte" class="analysis-pane">
    <${Section} title="Gerichte" hint=${data ? dishes.length + (dishes.length === 1 ? " Gericht" : " Gerichte") + " · letzte " + data.days + " Tage" : ""}>
      <div id="dishes-list" aria-live="polite">${data && (dishes.length
        ? dishes.map(dish => html`<${Dish} key=${dish.id} dish=${dish} all=${dishes} act=${act} open=${open.has(dish.id)}
            onToggle=${isOpen => setOpen(current => { const next = new Set(current); if (isOpen) next.add(dish.id); else next.delete(dish.id); return next; })} />`)
        : html`<p class="hint">Noch keine Gerichte. Sie entstehen, wenn der Essens-Agent eine Mahlzeit einschätzt oder du in „Tag durchgehen“ an einem Eintrag ein Gericht einträgst.</p>`)}</div><//>
  </div>`;
}

// The whole page. `reloadKey` changes whenever the summary must be fetched again (the page is opened,
// or something it shows changed elsewhere); `tab` and `onTab` keep the sub-page in the URL.
export function AnalysisView({ tab, onTab, reloadKey, getSettings, onSettings, onSettingsChanged, onOpenMeal }) {
  const [days, setDays] = useState(14);
  const [summary, setSummary] = useState({ data: null, loading: true, failed: false });
  const request = useRef(0);
  async function load() {
    const id = ++request.current;
    setSummary(current => ({ ...current, loading: true, failed: false }));
    try {
      const data = await api("/api/summary?days=" + days);
      // The summary endpoint may refresh the Nightscout profile before calculating.
      onSettings?.((await api("/api/settings")).settings);
      if (id === request.current) setSummary({ data, loading: false, failed: false });
    } catch (error) {
      if (id === request.current) setSummary({ data: null, loading: false, failed: true });
      toast(error.message, true);
    }
  }
  useEffect(() => { load(); }, [reloadKey, days]);
  return html`<div class="page-head"><div><h1>Insulinfaktor je KE</h1></div>
      <div class="segmented analysis-tabs" role="tablist" aria-label="Analyse">${TABS.map(([key, label]) =>
        html`<button type="button" role="tab" data-analysis-tab=${key} aria-selected=${String(tab === key)} onClick=${() => onTab(key)}>${label}</button>`)}</div></div>
    ${tab === "werte" && html`<${Werte} days=${days} setDays=${setDays} summary=${summary} reload=${load} onOpenMeal=${onOpenMeal} />`}
    ${tab === "automatik" && html`<${Automatik} summary=${summary} getSettings=${getSettings} onSettingsChanged=${onSettingsChanged} reload=${load} />`}
    ${tab === "gerichte" && html`<${Dishes} />`}`;
}
