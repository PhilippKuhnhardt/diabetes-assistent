// "Analyse": evaluates several days and is read-only (page roles, AGENTS.md; exclusion and day type
// moved to "Verlauf" 2026-11-26). Three sub-pages: Werte (the chosen period), Automatik (how the Automatik would have done,
// with "Übernehmen" for a settings change the server attaches) and Gerichte (the dish catalogue).
// The summary comes from /api/summary; every number, verdict and best cell is the server's.
import { html, useState, useEffect, useRef } from "./html.js";
import { api, post, toast, num, gl, glNum, factorNum, signedChange, timeOnly, signedPercent, dayDate, names,
  UNIT, Icon, GlucoseChange } from "./lib.js";
import { t, locale } from "./i18n.js";
import { gramsPerIe, carbUnitGrams } from "./units.js";

const TABS = [["werte", t("analysis.tabs.werte")], ["automatik", t("analysis.tabs.automatik")], ["gerichte", t("analysis.tabs.gerichte")]];
const PERIODS = [1, 3, 7, 14, 30].map(count => [count, t("common.days", { count })]);
const mealsText = count => t("common.meals", { count });

const Empty = ({ children }) => html`<div class="empty">${children}</div>`;
const Section = ({ title, hint, children, className = "analysis-list panel" }) => html`<section class=${className}>
  <div class="section-head"><h2>${title}</h2><span class="hint" role=${hint?.role}>${hint?.text ?? hint}</span></div>${children}</section>`;
const Table = ({ head, children, compact }) => html`<div class="table-wrap"><table class=${"analysis-table" + (compact ? " compact" : "")}>
  <thead><tr>${head}</tr></thead><tbody>${children}</tbody></table></div>`;

// ---- Werte ----
// What the factors did in the chosen period, read only (user request 2026-11-26): exclusion and day type
// are set in "Verlauf"; here the question is how far each meal ended from what its factor expected.
// A factor range; with g/IE the larger IE/KE is the smaller number, so the ends swap.
const span = (low, high) => low == null ? "—" : Number(low) === Number(high) ? factorNum(low)
  : gramsPerIe ? factorNum(high) + "–" + factorNum(low) : factorNum(low) + "–" + factorNum(high);
const Fact = ({ label, value, strong }) => html`<div class=${strong ? "key" : null}><dt>${label}</dt><dd>${value}</dd></div>`;
const suggestion = s => s.auto_units_per_ke != null ? factorNum(s.auto_units_per_ke) + (s.surcharge ? " + " + t("analysis.surcharge") : "") : "—";
const suggestionNote = s => t("analysis.suggestionFrom", { count: s.count, days: s.days });
const Factor = ({ value }) => value == null ? "—" : html`${factorNum(value)} <small>${UNIT.factor}</small>`;
const whole = value => value == null ? null : Math.round(Number(value));
const miss = value => value == null ? "—" : "±" + gl(value, 0);

function SummaryCards({ groups }) {
  return ["breakfast", "lunch", "dinner"].map(kind => {
    const row = groups[kind];
    const current = (row.nightscout_units_per_ke || []).map(value => factorNum(value)).join(" / ") || "—";
    // Breakfast splits into work and free days only while day types are on (settings).
    const breakfast = kind === "breakfast" && Boolean(row.by_day_type?.work);
    return html`<div class="summary-card"><span class="meal-type">${names[kind]}</span>
      <div class="summary-primary"><span>${t("analysis.median")}</span><strong><${Factor} value=${row.count > 0 ? row.median_units_per_ke : null} /></strong></div>
      <dl class="factor-facts">
        ${!breakfast && html`<${Fact} label=${t("analysis.suggestion")} value=${suggestion(row.suggestion)} strong />`}
        <${Fact} label=${t("analysis.typicalMiss")} value=${miss(row.median_abs_deviation_mgdl)} />
        <${Fact} label=${t("analysis.current")} value=${current} />
        ${row.q1_units_per_ke != null && html`<${Fact} label=${t("analysis.middle50")} value=${span(row.q1_units_per_ke, row.q3_units_per_ke)} />`}
      </dl>
      <p>${mealsText(row.count)}${breakfast ? "" : " · " + suggestionNote(row.suggestion)}</p>
      ${breakfast && html`<div class="breakfast-subcards">${["work", "free"].map(dayType => {
        const subset = row.by_day_type[dayType];
        return html`<div class="breakfast-subcard"><span>${dayType === "work" ? t("dayType.work") : t("dayType.free")}</span>
          <strong><${Factor} value=${subset.count ? subset.median_units_per_ke : null} /></strong>
          <dl class="factor-facts compact">
            <${Fact} label=${t("analysis.suggestion")} value=${suggestion(subset.suggestion)} strong />
            ${subset.q1_units_per_ke != null && html`<${Fact} label=${t("analysis.middle50")} value=${span(subset.q1_units_per_ke, subset.q3_units_per_ke)} />`}
          </dl><small>${mealsText(subset.count)}<br />${suggestionNote(subset.suggestion)}</small></div>`;
      })}</div>`}
    </div>`;
  });
}

// One row per meal: factor, carbs and insulin, then expected against measured change and the miss.
function Calculations({ groups, onOpenMeal }) {
  const rows = ["breakfast", "lunch", "dinner"]
    .flatMap(kind => groups[kind].calculations.map(item => ({ ...item, kind })))
    .sort((a, b) => b.at.localeCompare(a.at));
  if (!rows.length) return { rows, node: html`<${Empty}>${t("analysis.noMeals")}<//>` };
  let previousDay = "";
  const body = rows.map(item => {
    const newDay = item.day !== previousDay;
    previousDay = item.day;
    const excluded = item.units_per_ke != null && item.excluded;
    return html`
      ${newDay && html`<tr class="analysis-day-row"><th colspan="8" scope="colgroup">
        ${dayDate(item.day, { weekday: "long", day: "numeric", month: "long" })}
        ${item.day_type && html`<span class="day-type-tag">${item.day_type === "free" ? t("dayType.free") : t("dayType.work")}</span>`}</th></tr>`}
      <tr class=${(excluded ? "analysis-excluded " : "") + (item.strong_outlier ? "analysis-outlier" : "")}>
        <td>${names[item.kind]} <small>${timeOnly(item.at)}</small>${excluded && html` <span class="outlier-badge">${t("exclude.excluded")}</span>`}</td>
        <td><b>${item.units_per_ke != null
          ? html`${factorNum(item.units_per_ke)} ${UNIT.factor}${item.strong_outlier && html` <span class="outlier-badge">${t("analysis.outlier")}</span>`}${item.manual_window && html` <span class="manual-badge">${t("analysis.manualWindow")}</span>`}`
          : html`<span class="summary-unavailable">${t("analysis.notCalculable", { reason: item.reason || t("analysis.noFactor") })}</span>`}</b></td>
        <td>${item.carbs_total == null ? "—" : num(item.carbs_total) + " g"}</td>
        <td>${item.rapid_units_in_window == null ? "—" : num(item.rapid_units_in_window) + " " + UNIT.insulin}</td>
        <td><${GlucoseChange} value=${item.expected_delta_mgdl} /></td>
        <td><${GlucoseChange} value=${item.delta_mgdl} /></td>
        <td><${GlucoseChange} value=${item.deviation_mgdl} /></td>
        <td><button type="button" class="icon-button open-meal-day" aria-label=${t("analysis.openDay")} title=${t("analysis.openDay")}
          onClick=${() => onOpenMeal(item.day, item.id)}><${Icon} name="diary" /></button></td>
      </tr>`;
  });
  return { rows, node: html`<${Table} head=${html`<th>${t("analysis.col.meal")}</th><th>${t("analysis.col.factor")}</th><th>${t("card.carbs")}</th><th>${t("card.insulin")}</th><th>${t("today.expected")}</th><th>${t("analysis.col.measured")}</th><th>${t("analysis.col.delta")}</th><th></th>`}>${body}<//>` };
}

// Per sport kind: what the factors counted for sport against what the meals measured (user request
// 2026-11-26: easier to read). The sessions with their meals sit folded below.
function SportAnalysis({ analysis }) {
  const sessions = analysis?.sessions || [];
  const kinds = analysis?.kinds || [];
  const dateTime = value => new Date(value).toLocaleString(locale, {
    weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const rows = sessions.flatMap(session => session.meals.map(meal => html`<tr>
    <td>${session.label} <small>${session.end ? dateTime(session.start) + " · " + session.duration_minutes + " min" : ""}</small></td>
    <td>${names[meal.meal]} <small>${timeOnly(meal.start)}</small></td>
    <td><${GlucoseChange} value=${whole(meal.applied_mgdl)} /></td>
    <td>${meal.effect_mgdl == null
      ? html`<span class="summary-unavailable">${meal.reason || t("analysis.notCalculableShort")}</span>`
      : html`<b><${GlucoseChange} value=${whole(meal.effect_mgdl)} /></b>${meal.shared && html` <span class="outlier-badge">${t("analysis.severalSessions")}</span>`}${meal.excluded && html` <span class="outlier-badge">${t("exclude.excluded")}</span>`}`}</td></tr>`));
  const alone = sessions.filter(session => !session.meals.length).length;
  return html`<${Section} title=${t("analysis.sportTitle")} hint=${t("analysis.sessions", { count: sessions.length })}>
    ${sessions.length ? html`<div class="summary-grid sport-kinds">${kinds.map(group => html`<div class="summary-card"><span class="meal-type">${group.label}</span>
      <dl class="factor-facts sport-facts">
        <${Fact} label=${t("analysis.sport.measured")} value=${html`<${GlucoseChange} value=${whole(group.median_effect_mgdl)} />`} strong />
        <${Fact} label=${t("analysis.sport.applied")} value=${html`<${GlucoseChange} value=${whole(group.median_applied_mgdl)} />`} />
      </dl>
      <p>${t("analysis.sessions", { count: group.session_count })} · ${t("analysis.evaluable", { count: group.effect_count, meals: mealsText(group.meal_count) })}${group.effect_count > 1
        ? " · " + t("analysis.spread", { low: signedChange(group.min_effect_mgdl, 0), high: signedChange(group.max_effect_mgdl, 0) }) : ""}</p></div>`)}</div>
      <p class="hint section-note">${t("analysis.sport.note")}</p>
      ${rows.length > 0 && html`<details class="analysis-fold"><summary>${t("analysis.sport.details", { count: rows.length })}</summary>
        <${Table} compact head=${html`<th>${t("sport.sport")}</th><th>${t("analysis.col.meal")}</th><th>${t("analysis.sport.applied")}</th><th>${t("analysis.sport.measured")}</th>`}>${rows}<//>
        ${alone > 0 && html`<p class="hint">${t("analysis.sport.noMeal", { count: alone, minutes: analysis.lookback_minutes })}</p>`}</details>`}`
      : html`<${Empty}>${t("analysis.noSport")}<//>`}
  <//>`;
}

// How the nights went (user request 2026-11-26): from three hours after the evening's last rapid dose to
// breakfast or a morning dose, how far glucose fell without bolus insulin and how much it rose again.
function NightSummary({ night, loading, failed }) {
  const at = value => value ? html`<small>${t("common.clock", { time: timeOnly(value) })}</small>` : "";
  const value = (mgdl, time) => mgdl == null ? "—" : html`${glNum(mgdl, 0)} ${at(time)}`;
  const cards = [["drop", "median_drop_mgdl", true], ["minimum", "median_minimum_glucose"], ["rise", "median_rise_mgdl", true], ["start", "median_start_glucose"]];
  const nights = night?.nights || [];
  const complete = nights.filter(item => item.minimum_glucose != null && item.start_glucose != null);
  const missing = nights.filter(item => !(item.minimum_glucose != null && item.start_glucose != null));
  const body = loading ? html`<${Empty}>${t("analysis.night.loading")}<//>` : failed ? html`<${Empty}>${t("analysis.night.failed")}<//>` : html`
    <div class="summary-grid night-cards">${cards.map(([key, field, signed]) => html`<div class="summary-card"><span>${t("analysis.night." + key)}</span>
      <strong>${night[field] == null ? "—" : signed ? html`<${GlucoseChange} value=${whole(night[field])} />` : html`${glNum(night[field], 0)} <small>${UNIT.glucose}</small>`}</strong>
      <p>${t("analysis.median")}</p></div>`)}</div>
    <p class="hint section-note">${t("analysis.night.note")}</p>
    ${complete.length ? html`<${Table} compact head=${html`<th>${t("analysis.night.night")}</th><th>${t("analysis.night.basal")}</th><th>${t("analysis.night.start")}</th><th>${t("analysis.night.minimum")}</th><th>${t("analysis.night.end")}</th><th>${t("analysis.night.dropShort")}</th><th>${t("analysis.night.riseShort")}</th>`}>
      ${complete.map(item => html`<tr class=${Number(item.carbs_g) > 0 ? "analysis-excluded" : null}>
        <td>${dayDate(item.day, { weekday: "short", day: "numeric", month: "short" })}${Number(item.carbs_g) > 0 && html` <span class="outlier-badge">${t("analysis.night.carbs", { grams: num(item.carbs_g, 0) })}</span>`}</td>
        <td>${item.basal_units == null ? "—" : num(item.basal_units, 1) + " " + UNIT.insulin}</td>
        <td>${value(item.start_glucose, item.start)}</td><td><b>${value(item.minimum_glucose, item.minimum_at)}</b></td>
        <td>${value(item.end_glucose, item.end)}${item.morning_dose_at && html` <small>${t("analysis.night.morningDose")}</small>`}</td>
        <td><${GlucoseChange} value=${whole(item.drop_mgdl)} /></td><td><${GlucoseChange} value=${whole(item.rise_mgdl)} /></td></tr>`)}<//>`
      : html`<${Empty}>${t("analysis.night.none")}<//>`}
    ${missing.length > 0 && html`<details class="analysis-fold"><summary>${t("analysis.night.missing", { count: missing.length })}</summary>
      <ul class="night-missing">${missing.map(item => html`<li><b>${dayDate(item.day, { weekday: "short", day: "numeric", month: "short" })}</b> ${item.reason || "—"}</li>`)}</ul></details>`}`;
  return html`<${Section} title=${t("analysis.night.title")} hint=${night ? t("analysis.night.count", { count: night.count, total: nights.length }) : ""}>${body}<//>`;
}

function Werte({ days, setDays, summary, onOpenMeal }) {
  const { data, loading, failed } = summary;
  const notes = [data?.profile_sync_error && t("analysis.profileNotUpdated", { error: data.profile_sync_error }),
    data?.sync_error_days?.length && t("analysis.notLoaded", { count: data.sync_error_days.length })].filter(Boolean).join(" · ");
  const calculations = data && !loading ? Calculations({ groups: data.groups, onOpenMeal }) : null;
  return html`<div id="analysis-werte" class="analysis-pane">
    <div class="analysis-controls">
      <div class="segmented period-switch" role="tablist" id="summary-days" aria-label=${t("analysis.period")}>${PERIODS.map(([value, label]) =>
        html`<button type="button" role="tab" data-days=${String(value)} aria-selected=${String(days === value)} onClick=${() => setDays(value)}>${label}</button>`)}</div>
      <span id="sync-progress" class="hint" role="status">${loading && data ? t("analysis.loading") : notes}</span>
    </div>
    <div id="summary" class="summary-grid">${loading && !data ? html`<${Empty}>${t("analysis.loading")}<//>`
      : failed ? html`<${Empty}>${t("analysis.failed")}<//>` : data && html`<${SummaryCards} groups=${data.groups} />`}</div>
    <${Section} title=${t("analysis.allCalculations")} hint=${calculations ? mealsText(calculations.rows.length) : ""}>
      <div id="calculations">${calculations?.node}</div><//>
    ${!failed && html`<${SportAnalysis} analysis=${data?.sport_analysis} />`}
    <${NightSummary} night=${data?.night} loading=${loading && !data} failed=${failed} />
  </div>`;
}

// ---- Automatik ----
const tuningLabel = (key, value) => key === "percentile" ? num(value, 1) + " %"
  : key === "surcharge" ? (Number(value) ? gl(value, 0) : t("analysis.off"))
  : key === "half_life" ? t("common.daysN", { count: Number(value), n: num(value, 1) })
  : t("common.daysN", { count: Number(value), n: num(value, 0) });
const tuningNames = Object.fromEntries(["percentile", "surcharge", "half_life", "lookback"].map(key => [key, t("analysis.tuning." + key)]));
const tuningHints = Object.fromEntries(["percentile", "surcharge", "half_life", "lookback"].map(key => [key, t("analysis.tuningHints." + key)]));
const strategyText = o => num(o.percentile, 1) + " % · " + (Number(o.surcharge_mgdl) ? t("analysis.surcharge") + " " + gl(o.surcharge_mgdl, 0) : t("analysis.noSurcharge")) +
  " · " + t("analysis.tuning.half_life") + " " + t("analysis.daysShort", { n: num(o.half_life_days, 1) }) + " · " + t("common.days", { count: o.lookback_days });

function RangeBar({ below, inRange, above }) {
  const total = below + inRange + above || 1, pct = value => (value * 100 / total).toFixed(2);
  return html`<svg class="range-bar" viewBox="0 0 100 8" preserveAspectRatio="none" role="img"
    aria-label=${t("analysis.rangeLabel", { below, inRange, above })}>
    <rect class="range-low" x="0" width=${pct(below)} height="8" />
    <rect class="range-ok" x=${pct(below)} width=${pct(inRange)} height="8" />
    <rect class="range-high" x=${(Number(pct(below)) + Number(pct(inRange))).toFixed(2)} width=${pct(above)} height="8" /></svg>`;
}
const RangeCounts = ({ o }) => html`<span class="range-nums"><span class="range-low">${o.below}</span> · <b class="range-ok">${o.in_range}</b> · <span class="range-high">${o.above}</span>${o.median_glucose != null &&
  html`<small class="range-median">${t("analysis.median")} ${gl(o.median_glucose, 0)}</small>`}</span>`;
const Delta = ({ value, label }) => html`<span class=${Number(value) < 0 ? "delta-good" : Number(value) > 0 ? "delta-bad" : ""}>
  ${(Number(value) > 0 ? "+" : Number(value) < 0 ? "−" : "±") + Math.abs(Number(value)) + " " + label}</span>`;

function Explain({ d }) {
  const surcharge = Number(d.active_surcharge_mgdl);
  const weights = (d.weights || []).map(item => (item.age_days === 1 ? t("analysis.yesterday") : t("analysis.daysAgo", { count: item.age_days })) + " " + num(item.weight, 2)).join(" · ");
  const exampleName = item => item.meal === "breakfast" && item.day_type ? names.breakfast + " " + (item.day_type === "work" ? t("dayType.work") : t("dayType.free")) : names[item.meal];
  const surchargeNote = surcharge > 0 ? " + " + t("analysis.explain.doseSurcharge", { mgdl: glNum(surcharge, 0) }) : "";
  return html`<div class="automatik-chips">${[
      [tuningNames.percentile, num(d.percentile, 1) + " %"], [tuningNames.surcharge, surcharge ? gl(surcharge, 0) : t("analysis.off")],
      [tuningNames.half_life, t("common.daysN", { count: Number(d.half_life_days), n: num(d.half_life_days, 1) })], [tuningNames.lookback, t("common.days", { count: d.lookback_days })]]
      .map(([label, value]) => html`<span class="chip">${label} <b>${value}</b></span>`)}</div>
    <ol class="automatik-steps">
      <li>${t("analysis.explain.meals", { days: d.lookback_days, weights })}</li>
      ${surcharge > 0 && html`<li>${t("analysis.explain.subtract", { mgdl: glNum(surcharge, 0), factor: UNIT.factor })}</li>`}
      <li>${t("analysis.explain.percentile", { percentile: num(d.percentile, 1) })}</li>
      <li>${t("analysis.explain.dose", { surcharge: surchargeNote })}</li></ol>
    <p class="hint">${t("analysis.explain.example")}</p>
    <${Table} compact head=${html`<th>${t("analysis.col.meal")}</th><th>${t("analysis.col.meals")}</th><th>${t("card.factor")}</th><th>${t("analysis.col.typical")}</th><th>${t("analysis.col.carbPart")}</th><th>${t("analysis.tuning.surcharge")}</th><th>${t("analysis.col.sum")}</th>`}>
      ${d.examples.map(item => item.units_per_ke == null
        ? html`<tr><td>${exampleName(item)}</td><td>${item.count}</td><td colspan="5" class="hint">${t("analysis.tooFew", { min: item.min_meals })}</td></tr>`
        : html`<tr><td>${exampleName(item)}</td><td>${item.count}</td><td><b>${factorNum(item.units_per_ke)}</b> ${UNIT.factor}</td>
          <td>${num(item.carbs_g, 0)} g · ${t("analysis.isf")} ${glNum(item.isf, 0)}</td>
          <td>${gramsPerIe ? num(item.carbs_g, 0) + " g ÷ " + factorNum(item.units_per_ke)
            : num(Number(item.carbs_g) / carbUnitGrams, 1) + " " + UNIT.carbUnit + " × " + factorNum(item.units_per_ke)} = ${num(item.carb_units, 1)} ${UNIT.insulin}</td>
          <td>${surcharge ? "+ " + num(item.surcharge_units, 1) + " " + UNIT.insulin : "—"}</td><td><b>${num(item.total_units, 1)} ${UNIT.insulin}</b></td></tr>`)}<//>`;
}

function Tradeoffs({ trade, apply }) {
  const current = trade.current;
  const points = trade.cloud;
  const maxX = Math.max(1, ...points.map(p => p.below)), maxY = Math.max(1, ...points.map(p => p.above));
  const W = 320, H = 200, L = 34, B = 30, x = v => L + v / maxX * (W - L - 12), y = v => H - B - v / maxY * (H - B - 12);
  const ticks = (max, draw) => Array.from(new Set([0, Math.round(max / 2), max])).map(draw);
  const value = (o, key) => key === "percentile" ? o.percentile : key === "surcharge" ? o.surcharge_mgdl : key === "half_life" ? o.half_life_days : o.lookback_days;
  const Option = ({ o, isCurrent }) => html`<li class=${"tradeoff" + (isCurrent ? " is-current" : "")}>
    <div class="tradeoff-head"><b>${isCurrent ? t("analysis.current") : o.changes.map(key => tuningNames[key] + " " + tuningLabel(key, value(o, key))).join(" · ")}</b>
      <small>${strategyText(o)}</small>
      ${!isCurrent && html`<span class="tradeoff-delta"><${Delta} value=${o.delta_below} label=${t("analysis.tooLow")} /> · <${Delta} value=${o.delta_above} label=${t("analysis.tooHigh")} />${o.delta_median_glucose != null &&
        " · " + t("analysis.median") + " " + (Number(o.delta_median_glucose) >= 0 ? "+" : "−") + gl(Math.abs(Number(o.delta_median_glucose)), 0)}</span>`}</div>
    <${RangeBar} below=${o.below} inRange=${o.in_range} above=${o.above} /><${RangeCounts} o=${o} />
    ${isCurrent ? html`<span class="hint">${t("analysis.set")}</span>` : html`<button type="button" class="quiet apply-button" onClick=${event => apply(event, o.apply)}>${t("analysis.apply")}</button>`}</li>`;
  return html`<div class="tradeoff-layout">
    <svg class="tradeoff-plot" viewBox=${"0 0 " + W + " " + H} role="img" aria-label=${t("analysis.plotLabel")}>
      ${ticks(maxX, v => html`<text x=${x(v)} y=${H - B + 16} text-anchor="middle">${v}</text>`)}
      ${ticks(maxY, v => html`<text x=${L - 8} y=${y(v) + 4} text-anchor="end">${v}</text>`)}
      <line class="axis" x1=${L} y1=${H - B} x2=${W - 6} y2=${H - B} /><line class="axis" x1=${L} y1=${H - B} x2=${L} y2="6" />
      <text class="axis-label" x=${W - 6} y=${H - 2} text-anchor="end">${t("analysis.below80", { value: glNum(80) })} →</text>
      <text class="axis-label" x=${L + 4} y="12">${t("analysis.above160", { value: glNum(160) })} ↑</text>
      ${points.map(p => html`<circle class="cloud" cx=${x(p.below)} cy=${y(p.above)} r="3" />`)}
      <polyline class="line" points=${trade.line.map(o => x(o.below) + "," + y(o.above)).join(" ")} />
      ${trade.line.map(o => html`<circle class="on-line" cx=${x(o.below)} cy=${y(o.above)} r="4.5" />`)}
      <circle class="current" cx=${x(current.below)} cy=${y(current.above)} r="7" /></svg>
    <div><ul class="tradeoff-list"><${Option} o=${current} isCurrent />${trade.line.filter(o => o.changes.length).map(o => html`<${Option} o=${o} />`)}</ul>
      <p class="hint">${t("analysis.tradeoffHint", { meals: trade.meal_count, position: trade.line.some(o => !o.changes.length)
        ? t("analysis.onLine") : t("analysis.offLine") })}</p></div></div>`;
}

function Tuning({ d, apply }) {
  const verdictText = row => row.verdict === "better" ? t("analysis.verdict.better", { value: tuningLabel(row.key, row.options.find(o => o.best).value) })
    : row.verdict === "shift" ? t("analysis.verdict.shift") : t("analysis.verdict.none");
  return html`${d.tuning.map(row => html`<details class="tuning-row" open=${row.verdict !== "none"}><summary>
      <b>${tuningNames[row.key]}</b><span class=${"verdict verdict-" + row.verdict}>${verdictText(row)}</span><small>${mealsText(row.meal_count)}</small></summary>
      <p class="hint">${tuningHints[row.key]}</p>
      <ul class="tuning-options">${row.options.map(o => html`<li class=${(o.active ? "is-active" : "") + (o.best ? " is-best" : "")}>
        <span class="tuning-label">${tuningLabel(row.key, o.value)}${o.active && html` <small>${t("analysis.active")}</small>`}</span>
        <${RangeBar} below=${o.below} inRange=${o.in_range} above=${o.above} /><${RangeCounts} o=${o} />
        ${o.active ? html`<span></span>` : html`<button type="button" class="quiet apply-button" onClick=${event => apply(event, o.apply)}>${t("analysis.apply")}</button>`}</li>`)}</ul></details>`)}
    <p class="hint range-legend"><span class="range-low">■ ${t("analysis.below80", { value: glNum(80) })}</span> <span class="range-ok">■ ${glNum(80)}–${glNum(160)}</span> <span class="range-high">■ ${t("analysis.above160", { value: glNum(160) })}</span> · ${t("analysis.tuningLegend", { target: glNum(d.dose_target_mgdl, 0), noise: num(d.tuning[0].noise_meals, 1) })}</p>`;
}

// Backtest matrix: where the endpoint glucose would have landed with each lookback, weighting
// strength (half-life) and percentile. The server marks the active cell and the best cells.
function Derivation({ d }) {
  const low = Number(d.glucose_low_mgdl), high = Number(d.glucose_high_mgdl);
  const variants = d.variants || [];
  const percentiles = variants.length ? variants[0].cells.map(cell => cell.percentile) : [];
  const total = variants.length ? variants[0].cells[0].stats.glucose_count : 0;
  if (!total) return html`<p class="hint derivation-note">${t("analysis.matrix.noHistory", { days: d.backtest_days, min: d.min_meals })}</p>`;
  const strength = days => { const diff = Number(days) - 7; return html`${t("analysis.daysShort", { n: num(days, 1) })}${diff < 0 ? html` <small>${t("analysis.matrix.stronger")}</small>` : diff > 0 ? html` <small>${t("analysis.matrix.weaker")}</small>` : ""}`; };
  const Cell = ({ variant, cell }) => {
    const [below, inRange, above] = ["below", "in_range", "above"].map(key => Number(cell.shares[key]));
    const isBest = cell.best === "overall", isPeriodBest = Boolean(cell.best);
    const notes = [isBest ? t("analysis.matrix.bestOverall") : isPeriodBest ? t("analysis.matrix.bestPeriod", { days: t("analysis.daysShort", { n: variant.lookback_days }) }) : "",
      cell.stats.median_abs_deviation == null ? "" : "±" + num(Number(cell.stats.median_abs_deviation) * 100, 0) + " %"];
    return html`<td class=${"matrix-cell" + (cell.active ? " is-active" : "") + (isPeriodBest ? " is-best" : "") + (isBest ? " is-overall-best" : "")}>
      ${cell.active && html`<span class="active-badge">${t("analysis.matrix.active")}</span>`}
      <svg class="range-bar" viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true">
        <rect class="range-low" x="0" width=${below.toFixed(2)} height="8" /><rect class="range-ok" x=${below.toFixed(2)} width=${inRange.toFixed(2)} height="8" />
        <rect class="range-high" x=${(below + inRange).toFixed(2)} width=${above.toFixed(2)} height="8" /></svg>
      <span class="range-nums"><span class="range-low">${num(below, 0)}</span> · <b class="range-ok">${num(inRange, 0)} %</b> · <span class="range-high">${num(above, 0)}</span></span>
      ${cell.stats.mean_glucose != null && html`<span class="range-glucose">Ø ${glNum(cell.stats.mean_glucose, 0)} · ${t("analysis.median")} ${gl(cell.stats.median_glucose, 0)}</span>`}
      <small>${notes.filter(Boolean).join(" · ")}</small></td>`;
  };
  return html`<p class="hint derivation-note range-legend"><span class="range-low">■ ${"<"} ${num(low, 0)}</span> <span class="range-ok">■ ${num(low, 0)}–${num(high, 0)}</span> <span class="range-high">■ ${">"} ${num(high, 0)}</span> · ${t("analysis.matrix.legend", { low: num(low, 0) })}</p>
    <div class="table-wrap"><table class="analysis-table compact matrix"><thead><tr><th>${t("analysis.tuning.lookback")}</th><th>${t("analysis.tuning.half_life")}</th>
      ${percentiles.map(value => html`<th>${value === "surcharge" ? html`${num(d.percentile, 1)} % + ${t("analysis.surcharge")} <small>${t("analysis.matrix.perMeal", { mgdl: glNum(d.surcharge_mgdl, 0) })}</small>`
        : html`${num(value, 1)} %${Number(value) === 50 && html` <small>${t("analysis.median")}</small>`}`}</th>`)}</tr></thead>
      <tbody>${variants.map((variant, index) => {
        const first = index === 0 || variants[index - 1].lookback_days !== variant.lookback_days;
        return html`<tr class=${first ? "matrix-group" : null}><td>${first ? t("common.days", { count: variant.lookback_days }) : ""}</td><td>${strength(variant.half_life_days)}</td>
          ${variant.cells.map(cell => html`<${Cell} variant=${variant} cell=${cell} />`)}</tr>`;
      })}</tbody></table></div>
    <p class="hint derivation-note">${t("analysis.matrix.note", { meals: mealsText(total), days: d.backtest_days, mgdl: glNum(d.surcharge_mgdl, 0), grams: num(d.surcharge_full_grams, 0) })}</p>`;
}

// Information only: how much more insulin per KE breakfast needs on work days than on free days.
function DayTypeRatio({ d }) {
  const pct = number => (Number(number) >= 0 ? "+" : "−") + Math.abs(Number(number) * 100).toLocaleString(locale, { maximumFractionDigits: 0 }) + " %";
  const value = (median, count) => median == null ? "—" : html`${factorNum(median)} <small>(${count})</small>`;
  return html`<${Table} compact head=${html`<th>${t("dayType.free")} <small>${t("analysis.median")} ${UNIT.factor}</small></th><th>${t("dayType.work")} <small>${t("analysis.median")} ${UNIT.factor}</small></th><th>${t("analysis.stress.surcharge")}</th><th>${t("analysis.stress.perDay")}</th>`}>
    ${Object.values(d.meals).map(m => html`<tr><td>${value(m.free_median, m.free_count)}</td><td>${value(m.work_median, m.work_count)}</td>
      <td><b>${m.surcharge == null ? "—" : pct(m.surcharge)}</b></td>
      <td class="surcharge-cell">${m.singles.length ? html`${pct(m.single_median)} <small>(${t("analysis.stress.singles", { count: m.singles.length, low: pct(m.single_min), high: pct(m.single_max) })})</small><br />
        <small class="surcharge-days">${m.singles.map(item => dayDate(item.day, { weekday: "short", day: "numeric", month: "numeric" }) + " " + pct(item.surcharge)).join(" · ")}</small>` : "—"}</td></tr>`)}<//>
    <p class="hint derivation-note">${t("analysis.stress.note", { neighbours: d.neighbours, days: d.reference_days })}</p>`;
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
        toast(message, false, payload === change ? { label: t("common.undo"), run: () => save(previous, t("analysis.restored")) } : null);
        await onSettingsChanged();
        await reload();
      } catch (error) {
        toast(error.message, true);
      } finally {
        setApplying(false);
      }
    };
    await save(change, t("analysis.applied"));
  }
  const trade = d?.tradeoffs;
  return html`<div id="analysis-automatik" class="analysis-pane" inert=${applying}>
    <${Section} title=${t("analysis.howItWorks")} hint=${{ text: loading ? t("analysis.calculating") : "", role: "status" }}>
      <div id="automatik-explain">${d?.tuning && html`<${Explain} d=${d} />`}</div><//>
    <${Section} title=${t("analysis.alternatives")} hint=${trade ? mealsText(trade.meal_count) : ""}>
      <div id="automatik-tradeoffs">${d?.tuning && (trade.meal_count ? html`<${Tradeoffs} trade=${trade} apply=${apply} />`
        : html`<${Empty}>${t("analysis.noHistory", { days: d.backtest_days, min: d.min_meals })}<//>`)}</div><//>
    <${Section} title=${t("analysis.whichControl")} hint=${trade?.meal_count ? t("analysis.oneAtATime") : ""}>
      <div id="automatik-tuning">${d?.tuning && trade.meal_count > 0 && html`<${Tuning} d=${d} apply=${apply} />`}</div><//>
    <details class="analysis-list panel analysis-details">
      <summary><h2>${t("analysis.allCombinations")}</h2><span class="hint">${d ? t("analysis.matrix.active") + ": " + t("analysis.daysShort", { n: d.lookback_days }) + " · " + t("analysis.tuning.half_life") + " " + t("analysis.daysShort", { n: num(d.half_life_days, 1) }) +
        " · " + num(d.percentile, 1) + " %" + (d.method === "surcharge" ? " + " + t("analysis.surcharge") : "") : ""}</span></summary>
      <div id="suggestion-derivation">${d && html`<${Derivation} d=${d} />`}</div></details>
    ${data?.day_types !== false && html`<details class="analysis-list panel analysis-details">
      <summary><h2>${t("analysis.stress.title")}</h2><span class="hint">${data?.day_type_ratio ? t("analysis.lastDays", { count: data.day_type_ratio.days }) : ""}</span></summary>
      <div id="day-type-ratio">${data?.day_type_ratio && html`<${DayTypeRatio} d=${data.day_type_ratio} />`}</div></details>`}
  </div>`;
}

// ---- Gerichte: each dish with how its main meals turned out ----
const MEAL_NAMES = { breakfast: t("meal.short.breakfast"), lunch: t("meal.short.lunch"), dinner: t("meal.short.dinner") };

function Dish({ dish, all, open, onToggle, act }) {
  const [armed, setArmed] = useState(false);
  const outcome = dish.outcome;
  const others = all.filter(other => other.id !== dish.id);
  return html`<details class="dish-item" open=${open} onToggle=${event => onToggle(event.currentTarget.open)}>
    <summary><strong>${dish.name}</strong>${dish.category && html` <span class="place-badge">${dish.category}</span>`}${dish.archived && html` <span class="outlier-badge">${t("dishes.archived")}</span>`}
      <span class="hint dish-hint">${dish.hint}</span></summary>
    ${outcome.meals > 0 && html`<p class="hint">${t("dishes.outcome", { meals: outcome.meals, valid: outcome.valid })}${outcome.delta_mgdl != null &&
      " · " + t("dishes.medianChange", { value: signedChange(outcome.delta_mgdl, 0) })}${outcome.end_mgdl != null && " · " + t("dishes.medianEnd", { value: glNum(outcome.end_mgdl, 0) })}</p>`}
    ${dish.history.length
      ? html`<${Table} compact head=${html`<th>${t("analysis.col.meal")}</th><th>${t("card.carbs")}</th><th>${t("card.insulin")}</th><th>${t("card.factor")}</th><th>${t("dishes.vsUsual")}</th><th>${t("dishes.bg")}</th><th>${t("common.place")}</th>`}>
        ${dish.history.map(row => html`<tr><td>${dayDate(row.day, { day: "numeric", month: "numeric" })} ${MEAL_NAMES[row.meal] || ""}</td>
          <td>${row.carbs_g != null ? num(row.carbs_g, 0) + " g" : "—"}</td><td>${row.rapid_units != null ? num(row.rapid_units) + " " + UNIT.insulin : "—"}</td>
          <td>${row.factor != null ? factorNum(row.factor) : row.excluded ? t("history.excluded") : t("dishes.noFactor")}</td>
          <td>${row.ratio != null ? signedPercent(row.ratio) : "—"}</td>
          <td>${row.g0_mgdl != null && row.g_end_mgdl != null ? glNum(row.g0_mgdl, 0) + " → " + glNum(row.g_end_mgdl, 0) : "—"}</td><td>${row.place || ""}</td></tr>`)}<//>`
      : html`<p class="hint">${t("dishes.noMeals")}</p>`}
    <div class="dish-edit">
      <input type="text" class="dish-name" maxlength="60" value=${dish.name} aria-label=${t("dishes.name")}
        onChange=${event => act({ action: "rename", id: dish.id, name: event.currentTarget.value })} />
      <input type="text" class="dish-category" maxlength="40" value=${dish.category} placeholder=${t("dishes.category")} aria-label=${t("dishes.category")}
        onChange=${event => act({ action: "category", id: dish.id, category: event.currentTarget.value })} />
      ${others.length > 0 && html`<select class="dish-merge" aria-label=${t("dishes.mergeWith")} value=""
        onChange=${event => event.currentTarget.value && act({ action: "merge", id: dish.id, into_id: event.currentTarget.value })}>
        <option value="">${t("dishes.mergeWithOption")}</option>${others.map(other => html`<option value=${String(other.id)}>${other.name}</option>`)}</select>`}
      <button type="button" class="quiet" onClick=${() => act({ action: "archive", id: dish.id, archived: !dish.archived })}>${dish.archived ? t("dishes.unarchive") : t("dishes.archive")}</button>
      <button type="button" class="quiet delete-treatment" onClick=${() => armed ? act({ action: "delete", id: dish.id }) : setArmed(true)}>${armed ? t("common.reallyDelete") : t("common.delete")}</button>
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
    <${Section} title=${t("analysis.tabs.gerichte")} hint=${data ? t("dishes.count", { count: dishes.length }) + " · " + t("analysis.lastDays", { count: data.days }) : ""}>
      <div id="dishes-list" aria-live="polite">${data && (dishes.length
        ? dishes.map(dish => html`<${Dish} key=${dish.id} dish=${dish} all=${dishes} act=${act} open=${open.has(dish.id)}
            onToggle=${isOpen => setOpen(current => { const next = new Set(current); if (isOpen) next.add(dish.id); else next.delete(dish.id); return next; })} />`)
        : html`<p class="hint">${t("dishes.none")}</p>`)}</div><//>
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
  return html`<div class="page-head"><div><h1>${t("analysis.title")}</h1></div>
      <div class="segmented analysis-tabs" role="tablist" aria-label=${t("nav.analysis")}>${TABS.map(([key, label]) =>
        html`<button type="button" role="tab" data-analysis-tab=${key} aria-selected=${String(tab === key)} onClick=${() => onTab(key)}>${label}</button>`)}</div></div>
    ${tab === "werte" && html`<${Werte} days=${days} setDays=${setDays} summary=${summary} onOpenMeal=${onOpenMeal} />`}
    ${tab === "automatik" && html`<${Automatik} summary=${summary} getSettings=${getSettings} onSettingsChanged=${onSettingsChanged} reload=${load} />`}
    ${tab === "gerichte" && html`<${Dishes} />`}`;
}
