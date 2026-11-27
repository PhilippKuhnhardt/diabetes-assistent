// "Analyse": evaluates several days and is read-only (page roles, AGENTS.md; exclusion and day type
// moved to "Verlauf" 2026-11-26). Three sub-pages: Werte (the chosen period), Automatik (how the Automatik would have done,
// with "Übernehmen" for a settings change the server attaches) and Gerichte (the dish catalogue).
// The summary comes from /api/summary; every number, verdict and best cell is the server's.
import { html, useState, useEffect, useRef } from "./html.js";
import { api, post, toast, num, gl, glNum, factorNum, signedChange, timeOnly, signedPercent, dayDate, names,
  UNIT, Icon, GlucoseChange, CERTAINTY_LABELS, MEAL_FACTORS } from "./lib.js";
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
// What happened in the chosen period, read only (user requests 2026-11-26/27): exclusion and day type are
// set in "Verlauf"; here the questions are how far meals ended from what the Automatik expected, what
// sport really did and how glucose runs on the basal alone. One sub-section at a time (Mahlzeiten,
// Sport, Basal), long lists start with the newest entries, and every row is a grid with the values
// right-aligned in fixed columns, so the page lines up and needs little scrolling on a phone.
const SECTIONS = ["meals", "sport", "basal"];
const LIST_START = 6;
const MISS_SCALE = 100; // mg/dL of typical miss that fill a bar
const whole = value => value == null ? null : Math.round(Number(value));
const miss = value => value == null ? "—" : "±" + glNum(value, 0);
// A signed change without unit (the unit stands in the column head or the line below).
const signedNum = value => value == null ? "—" : (whole(value) > 0 ? "+" : "") + glNum(whole(value), 0);
const shortDay = day => dayDate(day, { weekday: "short", day: "numeric", month: "numeric" });
const tone = value => value == null || whole(value) === 0 ? "" : Number(value) > 0 ? " is-up" : " is-down";

// A signed glucose change as plain coloured text (no pill), whole mg/dL: amber higher, blue lower.
const Change = ({ value }) => value == null ? "—" : html`<span class=${"change" + tone(value)}>${signedNum(value)}</span>`;

// The newest entries first; the rest behind one button.
function ShowMore({ items, render, limit = LIST_START }) {
  const [all, setAll] = useState(false);
  const shown = all ? items : items.slice(0, limit);
  return html`${shown.map(render)}${items.length > limit && !all && html`<li class="show-more-row"><button type="button" class="quiet show-more"
    onClick=${() => setAll(true)}>${t("analysis.showAll", { count: items.length })}</button></li>`}`;
}

const Card = ({ title, hint, id, children }) => html`<section class="werte-card" id=${id}>
  <header><h2>${title}</h2>${hint && html`<p class="hint">${hint}</p>`}</header>${children}</section>`;

// Two key numbers side by side.
const Pair = ({ items }) => html`<div class="werte-pair">${items.map(([label, value, note]) => html`<div>
  <span>${label}</span><strong>${value}</strong>${note && html`<small>${note}</small>`}</div>`)}</div>`;

// Which way a set of meals typically ended against the Automatik's expectation.
function Tendency({ value }) {
  if (value == null) return null;
  const rounded = whole(value);
  if (Math.abs(rounded) < 5) return html`<small class="tendency">${t("analysis.miss.even")}</small>`;
  return html`<small class=${"tendency" + tone(rounded)}>${t(rounded > 0 ? "analysis.miss.higher" : "analysis.miss.lower", { value: gl(Math.abs(rounded), 0) })}</small>`;
}

// One line of the typical miss: label and count, a bar for the size with its direction, the value.
const MissRow = ({ label, row }) => html`<li class="miss-row">
  <span class="miss-label"><b>${label}</b><small>${mealsText(row.count)}</small></span>
  <span class="miss-bar"><span class="meter"><span style=${{ width: Math.min(100, Number(row.median_abs_deviation_mgdl ?? 0) / MISS_SCALE * 100) + "%" }}></span></span>
    <${Tendency} value=${row.median_deviation_mgdl} /></span>
  <span class="miss-value"><strong>${miss(row.median_abs_deviation_mgdl)}</strong><small>${row.median_abs_deviation_mgdl == null ? "" : UNIT.glucose}</small></span></li>`;

// How far each meal kind ended from the Automatik's expectation.
function MealMisses({ groups }) {
  const rows = ["breakfast", "lunch", "dinner"].map(kind => [names[kind], { count: groups[kind].expected_count || 0,
    median_abs_deviation_mgdl: groups[kind].median_abs_deviation_mgdl, median_deviation_mgdl: groups[kind].median_deviation_mgdl }]);
  const total = rows.reduce((sum, [, row]) => sum + row.count, 0);
  return html`<${Card} id="summary" title=${t("analysis.miss.title")} hint=${t("analysis.miss.hint", { meals: mealsText(total) })}>
    <ul class="miss-list">${rows.map(([label, row]) => html`<${MissRow} key=${label} label=${label} row=${row} />`)}</ul><//>`;
}

// The miss week by week per meal kind (user question 2026-11-27): the Automatik adapts by itself, so a
// miss that keeps the same sign for weeks means it follows too slowly. Bars above the line ended higher.
function Trend({ trend }) {
  if (!trend?.weeks?.length) return null;
  const weeks = trend.weeks;
  const label = week => dayDate(week.start, { day: "numeric", month: "numeric" });
  const column = cell => {
    const value = cell.median_deviation_mgdl == null ? null : Number(cell.median_deviation_mgdl);
    const height = value == null ? 0 : Math.max(4, Math.min(100, Math.abs(value) / MISS_SCALE * 100));
    return html`<span class="trend-week" title=${value == null ? "—" : signedChange(whole(value), 0) + " · " + mealsText(cell.count)}>
      <span class="trend-up">${value > 0 && html`<i class="is-up" style=${{ height: height + "%" }}></i>`}</span>
      <span class="trend-down">${value != null && value <= 0 && html`<i class="is-down" style=${{ height: height + "%" }}></i>`}</span></span>`;
  };
  return html`<${Card} id="trend" title=${t("analysis.trend.title")} hint=${t("analysis.trend.hint", { weeks: weeks.length })}>
    <ul class="trend-list">${["breakfast", "lunch", "dinner"].map(kind => {
      const row = trend.kinds[kind];
      return html`<li class="trend-row" key=${kind}>
        <span class="miss-label"><b>${names[kind]}</b><small class=${row.drift ? "tendency" + (row.drift === "up" ? " is-up" : " is-down") : "tendency"}>
          ${row.drift ? t("analysis.trend." + row.drift) : t("analysis.trend.none")}</small></span>
        <span class="trend-weeks" role="img" aria-label=${row.weeks.map((cell, index) => label(weeks[index]) + " " + (cell.median_deviation_mgdl == null ? "—" : signedChange(whole(cell.median_deviation_mgdl), 0))).join(", ")}>
          ${row.weeks.map(column)}</span></li>`;
    })}</ul>
    <div class="trend-axis"><span>${label(weeks[0])}</span><span>${t("analysis.trend.lastWeek")}</span></div><//>`;
}

// The same miss split by carb certainty and the "Schnell" mark (user decision 2026-11-27): do unsure
// estimates or fast meals end further from what the Automatik expected?
function Misses({ misses }) {
  const certainty = ["weighed", "known", "unsure", "none"].map(level => [level === "none" ? t("analysis.misses.noCertainty") : CERTAINTY_LABELS[level], misses.certainty?.[level]])
    .filter(([, row]) => row);
  const fast = [[MEAL_FACTORS.fast, misses.fast?.fast], [t("analysis.misses.notFast"), misses.fast?.other]].filter(([, row]) => row);
  if (!certainty.length && !fast.length) return null;
  return html`<${Card} id="misses" title=${t("analysis.misses.title")} hint=${t("analysis.misses.hint")}>
    ${certainty.length > 0 && html`<h3 class="werte-sub">${t("analysis.misses.certainty")}</h3>
      <ul class="miss-list">${certainty.map(([label, row]) => html`<${MissRow} key=${label} label=${label} row=${row} />`)}</ul>`}
    ${fast.length > 0 && html`<h3 class="werte-sub">${MEAL_FACTORS.fast}</h3>
      <${Pair} items=${fast.map(([label, row]) => [label + " · " + row.count, miss(row.median_abs_deviation_mgdl), html`<${Tendency} value=${row.median_deviation_mgdl} />`])} />`}
  <//>`;
}

// Every meal, newest first: what it was, and how far it ended from the expectation. A tap opens the day.
function Calculations({ groups, onOpenMeal }) {
  const rows = ["breakfast", "lunch", "dinner"]
    .flatMap(kind => groups[kind].calculations.map(item => ({ ...item, kind })))
    .sort((a, b) => b.at.localeCompare(a.at));
  const row = item => {
    const blocked = item.units_per_ke == null;
    const facts = blocked ? [t("analysis.notCalculable", { reason: item.reason || t("analysis.noFactor") })]
      : [item.carbs_total == null ? null : num(item.carbs_total, 0) + " g", item.rapid_units_in_window == null ? null : num(item.rapid_units_in_window) + " " + UNIT.insulin,
        factorNum(item.units_per_ke) + " " + UNIT.factor + (item.expected_units_per_ke != null ? " " + t("analysis.automatikWas", { value: factorNum(item.expected_units_per_ke) }) : "")];
    const marks = [item.certainty && CERTAINTY_LABELS[item.certainty], item.fast && MEAL_FACTORS.fast, item.excluded && !blocked && t("exclude.excluded"),
      item.strong_outlier && t("analysis.outlier"), item.manual_window && t("analysis.manualWindow")];
    return html`<li key=${item.id}><button type="button" class=${"meal-miss open-meal-day" + (blocked || item.excluded ? " is-muted" : "")}
      title=${t("analysis.openDay")} onClick=${() => onOpenMeal(item.day, item.id)}>
      <span class="meal-miss-text"><small>${shortDay(item.day)} · ${names[item.kind]} · ${timeOnly(item.at)}</small>
        <b>${item.dish || names[item.kind]}</b><small>${[...facts, ...marks].filter(Boolean).join(" · ")}</small></span>
      <span class="miss-value"><strong><${Change} value=${item.deviation_mgdl} /></strong><small>${item.deviation_mgdl == null ? "" : UNIT.glucose}</small></span>
      <${Icon} name="chevron" /></button></li>`;
  };
  return html`<${Card} title=${t("analysis.meals.title")} hint=${t("analysis.meals.hint")}>
    ${rows.length ? html`<ul class="werte-list" id="calculations"><${ShowMore} items=${rows} render=${row} /></ul>
      <p class="hint">${t("analysis.expectedNote")}</p>` : html`<${Empty}>${t("analysis.noMeals")}<//>`}<//>`;
}

// Per sport kind: what the factors assumed for sport against what the meals measured, with "Übernehmen"
// for a standard that fits the measurements better (user decision 2026-11-27).
function SportAnalysis({ analysis, apply }) {
  const sessions = analysis?.sessions || [];
  const kinds = analysis?.kinds || [];
  if (!sessions.length) return html`<${Card} title=${t("analysis.sportTitle")}><${Empty}>${t("analysis.noSport")}<//><//>`;
  const scale = Math.max(40, ...kinds.flatMap(group => [group.median_effect_mgdl, group.median_applied_mgdl])
    .filter(value => value != null).map(value => Math.abs(Number(value))));
  const bar = (label, value) => html`<div class="sport-bar"><span>${label}</span>
    <span class="meter"><span class=${tone(value).trim()} style=${{ width: value == null ? 0 : Math.min(100, Math.abs(Number(value)) / scale * 100) + "%" }}></span></span>
    <strong><${Change} value=${value} /></strong></div>`;
  const dateTime = value => new Date(value).toLocaleString(locale, { weekday: "short", day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const rows = sessions.flatMap(session => session.meals.map(meal => ({ session, meal })));
  const alone = sessions.filter(session => !session.meals.length).length;
  return html`<${Card} id="sport-kinds" title=${t("analysis.sportTitle")} hint=${t("analysis.sport.note")}>
    <div class="sport-kinds">${kinds.map(group => html`<article class="sport-kind" key=${group.kind}>
      <header><b>${group.label}</b><small>${t("analysis.sessions", { count: group.session_count })} · ${t("analysis.evaluable", { count: group.effect_count, meals: mealsText(group.meal_count) })}</small></header>
      ${bar(t("analysis.sport.applied"), group.median_applied_mgdl)}
      ${bar(t("analysis.sport.measured"), group.median_effect_mgdl)}
      ${group.apply ? html`<button type="button" class="primary apply-button sport-apply" onClick=${event => apply(event, group.apply)}>
          ${t("analysis.sport.suggest", { from: signedChange(-Number(group.standard_drop_mgdl), 0), to: signedChange(-Number(group.suggested_drop_mgdl), 0) })}</button>`
        : html`<small class="hint">${group.effect_count >= 2 ? t("analysis.sport.fits") : t("analysis.sport.tooFew")}</small>`}
    </article>`)}</div>
    ${rows.length > 0 && html`<details class="analysis-fold"><summary>${t("analysis.sport.details", { count: rows.length })}</summary>
      <div class="werte-cols"><span>${UNIT.glucose}</span><span>${t("analysis.sport.applied")}</span><span>${t("analysis.sport.measured")}</span></div>
      <ul class="werte-list">${rows.map(({ session, meal }) => html`<li class="value-row" key=${session.id + "-" + meal.id}>
        <span class="value-text"><b>${session.label}</b><small>${session.end ? dateTime(session.start) + " · " + session.duration_minutes + " min" : ""} · ${names[meal.meal]} ${timeOnly(meal.start)}${meal.shared ? " · " + t("analysis.severalSessions") : ""}${meal.excluded ? " · " + t("exclude.excluded") : ""}${meal.effect_mgdl == null ? " · " + (meal.reason || t("analysis.notCalculableShort")) : ""}</small></span>
        <strong><${Change} value=${meal.applied_mgdl} /></strong><strong><${Change} value=${meal.effect_mgdl} /></strong></li>`)}</ul>
      ${alone > 0 && html`<p class="hint">${t("analysis.sport.noMeal", { count: alone, minutes: analysis.lookback_minutes })}</p>`}</details>`}
  <//>`;
}

// Stretches without bolus and food (user request 2026-11-27): how glucose runs on the basal alone.
function QuietPeriods({ quiet }) {
  const periods = quiet?.periods || [];
  return html`<${Card} id="quiet" title=${t("analysis.quiet.title")} hint=${t("analysis.quiet.note")}>
    <${Pair} items=${[[t("analysis.quiet.rate"), html`<${Change} value=${quiet.median_rate_mgdl_per_hour} />`, t("analysis.quiet.perHour")],
      [t("analysis.quiet.found"), String(quiet.count), t("analysis.quiet.hours", { hours: num(quiet.hours, 0) })]]} />
    ${periods.length ? html`<div class="werte-cols"><span>${UNIT.glucose}</span><span>${t("analysis.quiet.change")}</span><span>${t("analysis.quiet.rateShort")}</span></div>
      <ul class="werte-list" id="quiet-periods"><${ShowMore} items=${periods} render=${item => html`<li class="value-row" key=${item.start}>
        <span class="value-text"><b>${shortDay(item.day)} · ${timeOnly(item.start)}–${timeOnly(item.end)}</b>
          <small>${t("analysis.quiet.span", { start: glNum(item.start_glucose, 0), end: glNum(item.end_glucose, 0), hours: num(item.hours, 1), min: glNum(item.minimum_glucose, 0) })}</small></span>
        <strong><${Change} value=${item.delta_mgdl} /></strong><strong><${Change} value=${item.rate_mgdl_per_hour} /></strong></li>`} /></ul>`
      : html`<${Empty}>${t("analysis.quiet.none")}<//>`}
  <//>`;
}

// How the nights went (user request 2026-11-26): from three hours after the evening's last rapid dose to
// breakfast or a morning dose, how far glucose fell without bolus insulin and how much it rose again.
function NightSummary({ night }) {
  const nights = night?.nights || [];
  const complete = nights.filter(item => item.minimum_glucose != null && item.start_glucose != null);
  const missing = nights.filter(item => !(item.minimum_glucose != null && item.start_glucose != null));
  return html`<${Card} id="night" title=${t("analysis.night.title")} hint=${t("analysis.night.note")}>
    <${Pair} items=${[[t("analysis.night.drop"), html`<${Change} value=${night.median_drop_mgdl} />`, t("analysis.night.medianOf", { count: night.count })],
      [t("analysis.night.rise"), html`<${Change} value=${night.median_rise_mgdl} />`, t("analysis.night.medianOf", { count: night.count })]]} />
    ${complete.length ? html`<div class="werte-cols"><span>${UNIT.glucose}</span><span>${t("analysis.night.dropShort")}</span><span>${t("analysis.night.riseShort")}</span></div>
      <ul class="werte-list" id="nights"><${ShowMore} items=${complete} render=${item => html`<li class=${"value-row" + (Number(item.carbs_g) > 0 ? " is-muted" : "")} key=${item.day}>
        <span class="value-text"><b>${shortDay(item.day)}</b>
          <small>${[item.basal_units == null ? null : t("analysis.night.basalUnits", { units: num(item.basal_units, 1) + " " + UNIT.insulin }),
            t("analysis.night.low", { value: glNum(item.minimum_glucose, 0), at: timeOnly(item.minimum_at) }),
            item.morning_dose_at && t("analysis.night.morningDose"),
            Number(item.carbs_g) > 0 && t("analysis.night.carbs", { grams: num(item.carbs_g, 0) })].filter(Boolean).join(" · ")}</small></span>
        <strong><${Change} value=${item.drop_mgdl} /></strong><strong><${Change} value=${item.rise_mgdl} /></strong></li>`} /></ul>`
      : html`<${Empty}>${t("analysis.night.none")}<//>`}
    ${missing.length > 0 && html`<details class="analysis-fold"><summary>${t("analysis.night.missing", { count: missing.length })}</summary>
      <ul class="night-missing">${missing.map(item => html`<li key=${item.day}><b>${shortDay(item.day)}</b> ${item.reason || "—"}</li>`)}</ul></details>`}
  <//>`;
}

function readSection() {
  try { return SECTIONS.includes(localStorage.getItem("analysis-werte")) ? localStorage.getItem("analysis-werte") : "meals"; } catch { return "meals"; }
}

function Werte({ days, setDays, summary, onOpenMeal, onSettingsChanged, reload }) {
  const { data, loading, failed } = summary;
  const [section, setSection] = useState(readSection);
  const [applying, setApplying] = useState(false);
  const pick = value => { setSection(value); try { localStorage.setItem("analysis-werte", value); } catch { /* per-device convenience only */ } };
  // "Übernehmen" on a sport kind saves the standard effect the server attached.
  async function apply(event, change) {
    setApplying(true);
    try {
      await post("/api/settings", change);
      toast(t("analysis.sport.saved"));
      await onSettingsChanged();
      await reload();
    } catch (error) {
      toast(error.message, true);
    } finally {
      setApplying(false);
    }
  }
  const notes = [data?.profile_sync_error && t("analysis.profileNotUpdated", { error: data.profile_sync_error }),
    data?.sync_error_days?.length && t("analysis.notLoaded", { count: data.sync_error_days.length })].filter(Boolean).join(" · ");
  const body = loading && !data ? html`<${Empty}>${t("analysis.loading")}<//>` : failed ? html`<${Empty}>${t("analysis.failed")}<//>` : !data ? null
    : section === "sport" ? html`<${SportAnalysis} analysis=${data.sport_analysis} apply=${apply} />`
    : section === "basal" ? html`<${QuietPeriods} quiet=${data.quiet} /><${NightSummary} night=${data.night} />`
    : html`<${MealMisses} groups=${data.groups} /><${Trend} trend=${data.trend} /><${Misses} misses=${data.misses || {}} />
      <${Calculations} groups=${data.groups} onOpenMeal=${onOpenMeal} />`;
  return html`<div id="analysis-werte" class="analysis-pane werte" inert=${applying}>
    <div class="werte-head">
      <div class="section-tabs" role="tablist" id="werte-sections" aria-label=${t("analysis.tabs.werte")}>${SECTIONS.map(key =>
        html`<button type="button" role="tab" data-section=${key} aria-selected=${String(section === key)} onClick=${() => pick(key)}>${t("analysis.sections." + key)}</button>`)}</div>
      <label class="period-select">
        <select id="summary-days" aria-label=${t("analysis.period")} value=${String(days)} onChange=${event => setDays(Number(event.currentTarget.value))}>${PERIODS.map(([value, label]) =>
          html`<option value=${String(value)}>${label}</option>`)}</select></label>
    </div>
    ${(loading && data) || notes ? html`<p id="sync-progress" class="hint" role="status">${loading && data ? t("analysis.loading") : notes}</p>` : null}
    ${body}
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
    ${tab === "werte" && html`<${Werte} days=${days} setDays=${setDays} summary=${summary} onOpenMeal=${onOpenMeal} onSettingsChanged=${onSettingsChanged} reload=${load} />`}
    ${tab === "automatik" && html`<${Automatik} summary=${summary} getSettings=${getSettings} onSettingsChanged=${onSettingsChanged} reload=${load} />`}
    ${tab === "gerichte" && html`<${Dishes} />`}`;
}
