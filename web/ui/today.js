// "Heute": the chart of the last hours with what still acts ("Wirkt noch"), the entries of the last three
// hours and today's meals and sport. The server sends the day and the forecast (`on_board` of
// /api/entry/preview); these components only draw them. Open tasks of past days live in Verlauf's week
// strip and day cards (the former "Zu erledigen" list was removed 2026-11-23).
import { html, useState } from "./html.js";
import { MINUTE, num, glNum, gl, factorNum, timeOnly, names, sportNames, SPLIT_TIMING, UNIT, FactorIcons } from "./lib.js";
import { t } from "./i18n.js";
import { DayChart, glucosePoints } from "./charts.js";
import { dayBounds, mealBlocks, entryLabel } from "./day.js";
import { sportSummary } from "./sport.js";

const HOURS = [[3, "3 h"], [6, "6 h"], [24, t("today.wholeDay")]];

// Today's "Wirkt noch" card: what still lowers and raises glucose and the expected value after it.
export function ActiveCard({ active, id = "day-overview-active" }) {
  const until = value => value ? t("today.until", { time: timeOnly(value) }) : t("today.actsNoMore");
  const nothing = !active.lowering_until && !active.raising_until;
  const value = (sign, mgdl) => html`${sign + glNum(mgdl, 0)} <small>${UNIT.glucose}</small>`;
  const Item = ({ label, strong, lines, className = "" }) => html`<div class=${"active-item" + className}><span>${label}</span>
    <strong>${strong}</strong>${lines.map(line => html`<small>${line}</small>`)}</div>`;
  const split = active.split_plan;
  return html`<div id=${id} class="active-card" aria-label=${t("today.activeLabel")} aria-live="polite">
    <${Item} label=${t("today.lowering")} strong=${value("−", active.lowering_mgdl)} lines=${[t("today.insulinLine", { units: num(active.insulin_units, 1), unit: UNIT.insulin }),
      ...(active.sports || []).filter(row => Number(row.remaining_mgdl) > 0).map(row =>
        (sportNames[row.kind] || t("sport.sport")) + " " + timeOnly(row.at) + " −" + glNum(row.remaining_mgdl, 0)),
      until(active.lowering_until)]} />
    <${Item} label=${t("today.raising")} strong=${value("+", active.raising_mgdl)} lines=${[t("today.carbsLine", { grams: num(active.carbs_remaining_g, 0), mgdl: glNum(active.carbs_mgdl, 0) }),
      t("today.surchargeLine", { mgdl: glNum(active.surcharge_mgdl, 0) }), until(active.raising_until)]} />
    <${Item} label=${t("today.expected")} className=" expected" strong=${active.expected_mgdl == null ? "—" : value("≈ ", active.expected_mgdl)}
      lines=${active.expected_mgdl == null ? [t("today.noCgm")]
        : [(active.expected_at ? t("today.at", { time: timeOnly(active.expected_at) }) : t("today.now")),
           t("today.likely", { low: glNum(active.expected_low_mgdl, 0), high: glNum(active.expected_high_mgdl, 0) })]} />
    ${nothing && html`<p class="hint">${t("today.nothingActs")}</p>`}
    ${split && html`<p class="hint">${t("today.splitPlanned", { units: num(split.units, 1), unit: UNIT.insulin, time: timeOnly(split.due_at),
      state: split.state === "later" ? "" : " (" + (split.state === "wait" ? t("today.waitUntil", { wait: SPLIT_TIMING.wait, time: timeOnly(split.latest_at) }) : SPLIT_TIMING[split.state]) + ")" })}</p>`}
    ${active.fresh_meal_at && html`<p class="hint">${t("today.freshMeal", { time: timeOnly(active.fresh_meal_at) })}</p>`}</div>`;
}

// What happened in the last three hours, newest first, as context for the next decision.
function Recent({ treatments }) {
  const now = Date.now();
  const items = treatments
    .filter(item => { const t = new Date(item.at).getTime(); return t <= now && t >= now - 180 * MINUTE; })
    .sort((a, b) => new Date(b.at) - new Date(a.at))
    .map(item => {
      const parts = item.eventType === "Exercise"
        ? [(sportNames[item.sport_type] || t("sport.sport")) + (Number(item.duration) ? " " + num(item.duration, 0) + " min" : "")]
        : item.basal ? [t("entryKind.basal") + " " + num(item.basal, 1) + " " + UNIT.insulin]
        : item.finger_mgdl ? [t("entryKind.finger") + " " + gl(item.finger_mgdl, 0)]
        : [Number(item.carbs) > 0 ? num(item.carbs, 0) + " g" : "", Number(item.insulin) > 0 ? num(item.insulin, 2) + " " + UNIT.insulin : ""].filter(Boolean);
      return parts.length ? html`<span class="recent-chip"><time>${timeOnly(item.at)}</time> ${parts.join(" + ")}<${FactorIcons} marks=${item} /></span>` : null;
    }).filter(Boolean);
  return html`<div id="day-overview-recent" class="overview-recent" aria-label=${t("today.recentLabel")}>${items.length
    ? html`<span class="recent-label">${t("today.recent")}</span>${items}` : html`<span class="recent-label">${t("today.recentNone")}</span>`}</div>`;
}

// Today's meals in short: time, carbs, insulin and the factor once it is known.
// Each row sums all entries the server grouped under the meal (`meal_id`, as in "Verlauf") and unfolds to
// them, each tappable to correct it; the meal still running is unfolded.
const sumOf = (items, key) => items.reduce((sum, item) => sum + (Number(item[key]) || 0), 0);
const amounts = item => [Number(item.carbs) > 0 ? num(item.carbs, 0) + " g" : "", Number(item.insulin) > 0 ? num(item.insulin, 1) + " " + UNIT.insulin : "",
  item.kind === "exercise" ? num(item.duration_minutes, 0) + " min" : ""].filter(Boolean);

function MealToday({ meal, items, data, onEdit }) {
  const factor = meal.calculation?.factor_units_per_ke;
  const running = factor == null && meal.window?.end && new Date(meal.window.end).getTime() > Date.now();
  const [open, setOpen] = useState(Boolean(running));
  // Basal stands on its own and sport has its own row; an entry the card cannot edit is still listed for context.
  const entries = items.filter(item => item.kind !== "basal" && item.kind !== "exercise");
  const carbs = sumOf(entries, "carbs");
  const insulin = sumOf(entries, "insulin");
  const listId = "meal-today-" + meal.id;
  return html`<li class=${open ? "is-open" : ""}>
    <button type="button" class="meal-today" aria-expanded=${String(open)} aria-controls=${listId} onClick=${() => setOpen(!open)}>
      <time>${timeOnly(meal.at)}</time><b>${names[meal.meal] || t("meal.meal")}</b>
      <span>${[num(carbs, 0) + " g", insulin > 0 ? num(insulin, 1) + " " + UNIT.insulin : "",
        entries.length > 1 ? t("common.entries", { count: entries.length }) : ""].filter(Boolean).join(" · ")}</span>
      <small>${factor != null ? factorNum(factor) + " " + UNIT.factor : running ? t("today.until", { time: timeOnly(meal.window.end) }) : ""}</small>
      <i class="meal-today-toggle" aria-hidden="true">›</i></button>
    <ul id=${listId} class="meal-today-entries" hidden=${!open}>${entries.map(item => {
      const editable = item.kind !== "other" && item.kind !== "finger";
      const label = entryLabel(item, data);
      return html`<li key=${item.id}><button type="button" class="meal-today-entry" disabled=${!editable}
        aria-label=${t(editable ? "common.editAt" : "common.itemAt", { label, time: timeOnly(item.at) })} onClick=${() => onEdit(item.id)}>
        <time>${timeOnly(item.at)}</time><b>${label}<${FactorIcons} marks=${item} /></b>
        <span>${amounts(item).join(" + ")}</span>
        ${item.dish?.name && html`<small>${item.dish.name}</small>`}</button></li>`;
    })}</ul></li>`;
}

// A sport session of today: what it does and which meals it reaches; a tap opens the same card as in Verlauf.
function SportToday({ item, onEdit }) {
  const summary = sportSummary(item);
  return html`<li><button type="button" class="meal-today sport-today" onClick=${() => onEdit(item.id)}
    aria-label=${t("common.openAt", { label: summary.title, time: timeOnly(item.at) })}>
    <time>${timeOnly(item.at)}</time><b>${summary.title}</b>
    <span>${summary.parts.map(part => part.text).join(" · ") || t("today.sportNoMeal")}</span>
    <small>${summary.effect}</small><i class="meal-today-toggle" aria-hidden="true">›</i></button></li>`;
}

function MealsToday({ data, onEdit }) {
  const { blocks } = mealBlocks(data);
  const sessions = data.timeline.filter(item => item.kind === "exercise");
  const label = sessions.length ? t("today.mealsAndSport") : t("today.meals");
  if (!blocks.length && !sessions.length) return html`<div class="meals-today"><span class="recent-label">${t("today.mealsNone")}</span></div>`;
  const rows = [...blocks.map(block => ({ t: new Date(block.meal.at).getTime(), block })),
    ...sessions.map(item => ({ t: new Date(item.at).getTime(), item }))].sort((a, b) => a.t - b.t);
  return html`<div class="meals-today" aria-label=${label}><span class="recent-label">${label}</span>
    <ul>${rows.map(row => row.block
      ? html`<${MealToday} key=${row.block.meal.id} meal=${row.block.meal} items=${row.block.items} data=${data} onEdit=${onEdit} />`
      : html`<${SportToday} key=${row.item.id} item=${row.item} onEdit=${onEdit} />`)}</ul></div>`;
}

// Today the last hours up to now (3 h, 6 h or the whole day), other days the whole day.
export function Overview({ data, day, today, active, hours, onHours, onEdit = () => {} }) {
  const isToday = day === today;
  const bounds = data ? dayBounds(data, day) : null;
  let view = bounds && { from: bounds.dayStart, to: bounds.dayEnd };
  if (bounds && isToday) {
    const to = Math.max(Date.now() + 10 * MINUTE, bounds.dayStart + 60 * MINUTE);
    view = { from: Math.max(bounds.dayStart, to - hours * 60 * MINUTE), to };
  }
  const clipped = bounds && isToday && hours < 24 && view.from === bounds.dayStart && view.to - view.from < hours * 60 * MINUTE;
  const last = data && glucosePoints(data.entries).some(point => point.t >= view.from && point.t <= view.to);
  // The current value is in the header; the note only explains gaps.
  const note = data ? [last ? "" : t("today.noValues"), clipped ? t("today.beforeMidnight") : ""].filter(Boolean).join(" ") : "";
  return html`<div id="day-overview" class="day-overview">
    <div class="section-head"><h2>${t("today.chart")}</h2>
      <div class="overview-span" role="group" aria-label=${t("today.span")}>${HOURS.map(([value, label]) =>
        html`<button type="button" data-overview-hours=${value} aria-pressed=${String(value === hours)} hidden=${!isToday}
          onClick=${() => onHours(value)}>${label}</button>`)}</div></div>
    ${data ? html`<${DayChart} id="day-overview-chart" data=${data} view=${view} clipId="overview-clip" />` : html`<div class="day-overview-chart"></div>`}
    ${isToday && active && html`<${ActiveCard} active=${active} />`}
    ${isToday && data && html`<${Recent} treatments=${[...data.treatments, ...(data.basal || []).map(dose => ({ at: dose.at, basal: dose.units }))]} />`}
    ${isToday && data && html`<${MealsToday} data=${data} onEdit=${onEdit} />`}
    ${note && html`<p id="day-overview-note" class="hint">${note}</p>`}</div>`;
}
