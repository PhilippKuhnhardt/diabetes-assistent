// "Heute": the chart of the last hours with what still acts ("Wirkt noch") and the entries of the last
// three hours, and the list of things to do across the last days ("Zu erledigen"). The server sends the
// day, the forecast (`on_board` of /api/entry/preview) and the inbox; these components only draw them.
import { html, useState } from "./html.js";
import { MINUTE, num, timeOnly, names, sportNames, SPLIT_TIMING, FactorIcons } from "./lib.js";
import { DayChart, glucosePoints } from "./charts.js";
import { dayBounds, mealBlocks, entryLabel } from "./day.js";

const HOURS = [[3, "3 h"], [6, "6 h"], [24, "Tag"]];

// Today's "Wirkt noch" card: what still lowers and raises glucose and the expected value after it.
export function ActiveCard({ active, id = "day-overview-active" }) {
  const until = value => value ? "bis " + timeOnly(value) : "wirkt nicht mehr";
  const nothing = !active.lowering_until && !active.raising_until;
  const value = (sign, mgdl) => html`${sign + num(mgdl, 0)} <small>mg/dL</small>`;
  const Item = ({ label, strong, lines, className = "" }) => html`<div class=${"active-item" + className}><span>${label}</span>
    <strong>${strong}</strong>${lines.map(line => html`<small>${line}</small>`)}</div>`;
  const split = active.split_plan;
  return html`<div id=${id} class="active-card" aria-label="Was noch wirkt" aria-live="polite">
    <${Item} label="Senkt noch" strong=${value("−", active.lowering_mgdl)} lines=${["Insulin " + num(active.insulin_units, 1) + " IE",
      ...(active.sports || []).filter(row => Number(row.remaining_mgdl) > 0).map(row =>
        (sportNames[row.kind] || "Sport") + " " + timeOnly(row.at) + " −" + num(row.remaining_mgdl, 0)),
      until(active.lowering_until)]} />
    <${Item} label="Hebt noch" strong=${value("+", active.raising_mgdl)} lines=${["KH " + num(active.carbs_remaining_g, 0) + " g +" + num(active.carbs_mgdl, 0),
      "Fett/Eiweiß +" + num(active.surcharge_mgdl, 0), until(active.raising_until)]} />
    <${Item} label="Erwartet" className=" expected" strong=${active.expected_mgdl == null ? "—" : value("≈ ", active.expected_mgdl)}
      lines=${active.expected_mgdl == null ? ["kein aktueller CGM-Wert"]
        : [(active.expected_at ? "um " + timeOnly(active.expected_at) : "jetzt"),
           "wahrscheinlich " + num(active.expected_low_mgdl, 0) + "–" + num(active.expected_high_mgdl, 0) + " mg/dL"]} />
    ${nothing && html`<p class="hint">Nichts wirkt mehr nach.</p>`}
    ${split && html`<p class="hint">2. Teil geplant: ${num(split.units, 1)} IE um ${timeOnly(split.due_at)}, in der Prognose noch nicht enthalten${split.state === "later" ? ""
      : " (" + (split.state === "wait" ? SPLIT_TIMING.wait + " bis " + timeOnly(split.latest_at) : SPLIT_TIMING[split.state]) + ")"}. Die Menge kommt aus der Prognose.</p>`}
    ${active.fresh_meal_at && html`<p class="hint">Mahlzeit um ${timeOnly(active.fresh_meal_at)}: Die KH steigen noch, die Prognose ist hier am unsichersten.</p>`}</div>`;
}

// What happened in the last three hours, newest first, as context for the next decision.
function Recent({ treatments }) {
  const now = Date.now();
  const items = treatments
    .filter(item => { const t = new Date(item.at).getTime(); return t <= now && t >= now - 180 * MINUTE; })
    .sort((a, b) => new Date(b.at) - new Date(a.at))
    .map(item => {
      const parts = item.eventType === "Exercise"
        ? [(sportNames[item.sport_type] || "Sport") + (Number(item.duration) ? " " + num(item.duration, 0) + " min" : "")]
        : item.basal ? ["Basal " + num(item.basal, 1) + " IE"]
        : item.finger_mgdl ? ["BZ blutig " + num(item.finger_mgdl, 0) + " mg/dL"]
        : [Number(item.carbs) > 0 ? num(item.carbs, 0) + " g" : "", Number(item.insulin) > 0 ? num(item.insulin, 2) + " IE" : ""].filter(Boolean);
      return parts.length ? html`<span class="recent-chip"><time>${timeOnly(item.at)}</time> ${parts.join(" + ")}<${FactorIcons} marks=${item} /></span>` : null;
    }).filter(Boolean);
  return html`<div id="day-overview-recent" class="overview-recent" aria-label="Einträge der letzten 3 Stunden">${items.length
    ? html`<span class="recent-label">Letzte 3 h</span>${items}` : html`<span class="recent-label">Letzte 3 h: keine Einträge</span>`}</div>`;
}

// Today's meals in short (user request 2026-11-18): time, carbs, insulin and the factor once it is known.
// Each row sums all entries the server grouped under the meal (`meal_id`, as in "Verlauf") and unfolds to
// them, each tappable to correct it; the meal still running is unfolded (user request 2026-11-20).
const sumOf = (items, key) => items.reduce((sum, item) => sum + (Number(item[key]) || 0), 0);
const amounts = item => [Number(item.carbs) > 0 ? num(item.carbs, 0) + " g" : "", Number(item.insulin) > 0 ? num(item.insulin, 1) + " IE" : "",
  item.kind === "exercise" ? num(item.duration_minutes, 0) + " min" : ""].filter(Boolean);

function MealToday({ meal, items, data, onEdit }) {
  const factor = meal.calculation?.factor_units_per_ke;
  const running = factor == null && meal.window?.end && new Date(meal.window.end).getTime() > Date.now();
  const [open, setOpen] = useState(Boolean(running));
  // Basal stands on its own; an entry the card cannot edit is still listed for context.
  const entries = items.filter(item => item.kind !== "basal");
  const carbs = sumOf(entries, "carbs");
  const insulin = sumOf(entries, "insulin");
  const listId = "meal-today-" + meal.id;
  return html`<li class=${open ? "is-open" : ""}>
    <button type="button" class="meal-today" aria-expanded=${String(open)} aria-controls=${listId} onClick=${() => setOpen(!open)}>
      <time>${timeOnly(meal.at)}</time><b>${names[meal.meal] || "Mahlzeit"}</b>
      <span>${[num(carbs, 0) + " g", insulin > 0 ? num(insulin, 1) + " IE" : "",
        entries.length > 1 ? entries.length + " Einträge" : ""].filter(Boolean).join(" · ")}</span>
      <small>${factor != null ? num(factor, 2) + " IE/KE" : running ? "bis " + timeOnly(meal.window.end) : ""}</small>
      <i class="meal-today-toggle" aria-hidden="true">›</i></button>
    <ul id=${listId} class="meal-today-entries" hidden=${!open}>${entries.map(item => {
      const editable = item.kind !== "other" && item.kind !== "finger";
      const label = entryLabel(item, data);
      return html`<li key=${item.id}><button type="button" class="meal-today-entry" disabled=${!editable}
        aria-label=${label + " um " + timeOnly(item.at) + (editable ? " bearbeiten" : "")} onClick=${() => onEdit(item.id)}>
        <time>${timeOnly(item.at)}</time><b>${label}<${FactorIcons} marks=${item} /></b>
        <span>${amounts(item).join(" + ")}</span>
        ${item.dish?.name && html`<small>${item.dish.name}</small>`}</button></li>`;
    })}</ul></li>`;
}

function MealsToday({ data, onEdit }) {
  const { blocks } = mealBlocks(data);
  if (!blocks.length) return html`<div class="meals-today"><span class="recent-label">Mahlzeiten heute: noch keine</span></div>`;
  return html`<div class="meals-today" aria-label="Mahlzeiten heute"><span class="recent-label">Mahlzeiten heute</span>
    <ul>${blocks.map(({ meal, items }) => html`<${MealToday} key=${meal.id} meal=${meal} items=${items} data=${data} onEdit=${onEdit} />`)}</ul></div>`;
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
  const note = data ? [last ? "" : "Keine CGM-Werte in diesem Zeitraum.", clipped ? "Werte vor Mitternacht stehen beim Vortag." : ""].filter(Boolean).join(" ") : "";
  return html`<div id="day-overview" class="day-overview">
    <div class="section-head"><h2>Verlauf</h2>
      <div class="overview-span" role="group" aria-label="Zeitraum des Verlaufs">${HOURS.map(([value, label]) =>
        html`<button type="button" data-overview-hours=${value} aria-pressed=${String(value === hours)} hidden=${!isToday}
          onClick=${() => onHours(value)}>${label}</button>`)}</div></div>
    ${data ? html`<${DayChart} id="day-overview-chart" data=${data} view=${view} clipId="overview-clip" />` : html`<div class="day-overview-chart"></div>`}
    ${isToday && active && html`<${ActiveCard} active=${active} />`}
    ${isToday && data && html`<${Recent} treatments=${[...data.treatments, ...(data.basal || []).map(dose => ({ at: dose.at, basal: dose.units }))]} />`}
    ${isToday && data && html`<${MealsToday} data=${data} onEdit=${onEdit} />`}
    ${note && html`<p id="day-overview-note" class="hint">${note}</p>`}</div>`;
}

// "Zu erledigen": what needs an action across the last days (user request 2026-10-21). The server decides
// what is open; an opened hint folds away, and a done one is no longer listed.
function dayLabel(day, today) {
  const yesterday = new Date(new Date(today + "T12:00:00").getTime() - 24 * 60 * MINUTE).toLocaleDateString("sv-SE");
  if (day === today) return "heute";
  if (day === yesterday) return "gestern";
  return new Date(day + "T12:00:00").toLocaleDateString("de-DE", { weekday: "short", day: "numeric", month: "numeric" });
}

function InboxItem({ item, today, onOpen }) {
  return html`<li><button type="button" class="inbox-item" onClick=${() => onOpen(item)}><span>
    <strong>${names[item.meal] + " " + dayLabel(item.day, today) + ", " + timeOnly(item.at)}</strong>
    ${item.reasons.map(reason => html`<small>${reason.text}</small>`)}</span><span class="inbox-go" aria-hidden="true">›</span></button></li>`;
}

export function Inbox({ data, today, onOpen }) {
  const items = data?.items || [];
  const fresh = items.filter(item => !item.seen);
  const seen = items.filter(item => item.seen);
  return html`<section id="inbox" class=${"inbox" + (fresh.length ? "" : " is-quiet")} aria-label="Zu erledigen" hidden=${!items.length}>
    <h2 class="inbox-title">Zu erledigen <span id="inbox-count" class="inbox-count">${fresh.length || ""}</span></h2>
    <ul id="inbox-new" class="inbox-list">${fresh.map(item => html`<${InboxItem} key=${item.key} item=${item} today=${today} onOpen=${onOpen} />`)}</ul>
    ${seen.length > 0 && html`<details id="inbox-seen" class="inbox-seen"><summary>${seen.length + (seen.length === 1 ? " Mahlzeit noch offen, schon angesehen" : " Mahlzeiten noch offen, schon angesehen")}</summary>
      <ul class="inbox-list">${seen.map(item => html`<${InboxItem} key=${item.key} item=${item} today=${today} onOpen=${onOpen} />`)}</ul></details>`}</section>`;
}
