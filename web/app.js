"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const state = { day: new Date().toLocaleDateString("sv-SE"), view: "diary", analysisTab: "werte", persistDay: false, settings: null, dayData: null, pendingMealId: null, pendingSportId: null, entryTimeTouched: false };
const views = new Set(["diary", "analysis", "settings"]);
const names = { breakfast: "Frühstück", lunch: "Mittagessen", dinner: "Abendessen" };
const CERTAINTY_LABELS = { weighed: "Gewogen", known: "Halbwegs sicher", unsure: "KH unsicher" };
// Meal factors picked on a carb entry; shown as icons wherever the meal or entry appears.
const MEAL_FACTORS = { alcohol: "Alkohol", fat_protein: "Fettreich" };

function factorIconsHTML(marks) {
  const on = Object.keys(MEAL_FACTORS).filter(key => marks?.[key]);
  if (!on.length) return "";
  const label = on.map(key => MEAL_FACTORS[key]).join(", ");
  return '<span class="factor-icons" title="' + escapeHTML(label) + '" role="img" aria-label="' + escapeHTML(label) + '">' +
    on.map(key => '<span class="factor-icon factor-' + key + '"></span>').join("") + '</span>';
}
// Where a carb entry was eaten; the place chip's name.
const placeBadgeHTML = place => place ? '<span class="place-badge" title="Ort">' + escapeHTML(place.name) + '</span>' : "";

// The browser position for the place suggestion and for adding a spot to a place.
function currentPosition() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) { reject({ code: 0 }); return; }
    navigator.geolocation.getCurrentPosition(position => resolve({
      lat: position.coords.latitude, lon: position.coords.longitude,
      accuracy_m: Math.round(position.coords.accuracy), captured_at: new Date(position.timestamp).toISOString(),
    }), reject, { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 });
  });
}

const osmLink = (lat, lon) => "https://www.openstreetmap.org/?mlat=" + lat + "&mlon=" + lon + "#map=18/" + lat + "/" + lon;

const num = (value, digits = 2) => Number(value).toLocaleString("de-DE", { maximumFractionDigits: digits, minimumFractionDigits: 0 });
const factorNum = value => Number(value).toLocaleString("de-DE", { minimumFractionDigits: 1, maximumFractionDigits: 2 });
const signedChange = (value, digits = 1) => value == null ? "—" : (Number(value) > 0 ? "+" : "") + num(value, digits) + " mg/dL";
const glucoseChangeHTML = value => value == null ? '—' :
  '<span class="glucose-change ' + (Number(value) > 0 ? 'rising' : Number(value) < 0 ? 'falling' : 'steady') + '">' +
  escapeHTML(signedChange(value)) + '</span>';
const sportKinds = ["jogging", "gym", "light_cardio", "racing"];
const sportNames = { jogging: "Joggen", gym: "Gym", light_cardio: "Leichtes Cardio", racing: "Rennsport" };
// The API stores a sport effect as glucose drop; the UI shows the signed glucose change.
const sportChange = effect => -Number(effect || 0);
const timeOnly = (value) => new Date(value).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
// Keep an existing fractional value valid while number-input arrows use the requested step.
function pickerMinimum(value, step, positive = false) {
  const remainder = Number((((value % step) + step) % step).toFixed(2));
  return positive && remainder === 0 ? step : remainder;
}

function setPickerBase(input, step, positive = false) {
  const value = Number(input.value);
  input.min = String(input.value && Number.isFinite(value)
    ? pickerMinimum(value, step, positive) : positive ? step : 0);
}

const localInputTime = date => {
  const d = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return d.toISOString().slice(0, 16);
};

const toInstant = (date, clock) => {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !/^([01]\d|2[0-3]):[0-5]\d$/.test(clock)) throw new Error("Zeit bitte im 24-Stunden-Format HH:MM eingeben");
  const parsed = new Date(date + "T" + clock + ":00");
  if (!Number.isFinite(parsed.getTime()) || localInputTime(parsed) !== date + "T" + clock) throw new Error("Dieser lokale Zeitpunkt existiert nicht");
  return parsed.toISOString();
};

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, cache: "no-store", ...options });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Anfrage fehlgeschlagen");
  return data;
}

let toastTimeout;
// An optional action (e.g. "Rückgängig") adds a button and keeps the toast visible longer.
function toast(message, error = false, action = null) {
  const node = $("#toast");
  node.textContent = message;
  if (action) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "toast-action";
    button.textContent = action.label;
    button.addEventListener("click", () => {
      node.classList.remove("visible", "has-action");
      action.run();
    }, { once: true });
    node.append(" ", button);
  }
  node.classList.toggle("error", error);
  node.classList.toggle("has-action", Boolean(action));
  node.classList.add("visible");
  clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => node.classList.remove("visible", "has-action"), action ? 12000 : 4500);
}

const glucoseStatuses = new Set(["in_range", "outside", "low", "high", "stale", "no_target", "unavailable"]);
let glucoseRefresh = null;

function loadCurrentGlucose() {
  if (glucoseRefresh) return glucoseRefresh;
  glucoseRefresh = (async () => {
    const widget = $("#current-glucose");
    try {
      const data = await api("/api/glucose/current");
      const status = glucoseStatuses.has(data.status) ? data.status : "unavailable";
      widget.dataset.status = status;
      $("#glucose-value").textContent = data.glucose_mgdl == null
        ? "—" : num(data.glucose_mgdl, 1) + " mg/dL";
      const delta = data.trend_delta_mgdl;
      $("#glucose-trend").textContent = data.fresh && delta != null
        ? trendSymbol(data.trend_direction) + " " + (Number(delta) > 0 ? "+" : "") + num(delta, 0) +
          " mg/dL / " + num(data.trend_minutes, 0) + " min"
        : data.status === "stale" ? "Trend veraltet" : "Trend nicht verfügbar";
      const meta = status === "stale"
        ? "Messwert veraltet" + (data.glucose_at ? " · " + timeOnly(data.glucose_at) + " Uhr" : "")
        : data.glucose_mgdl == null ? "Kein aktueller Messwert" : "";
      $("#glucose-meta").textContent = meta;
      $("#glucose-meta").hidden = !meta;
    } catch (error) {
      widget.dataset.status = "unavailable";
      $("#glucose-value").textContent = "—";
      $("#glucose-trend").textContent = "Trend nicht verfügbar";
      $("#glucose-meta").textContent = "CGM derzeit nicht verfügbar";
      $("#glucose-meta").hidden = false;
    }
  })().finally(() => { glucoseRefresh = null; });
  return glucoseRefresh;
}

function validDay(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || "")) return false;
  const [year, month, day] = value.split("-").map(Number);
  const parsed = new Date(Date.UTC(year, month - 1, day));
  return parsed.getUTCFullYear() === year && parsed.getUTCMonth() === month - 1 && parsed.getUTCDate() === day;
}

function locationState() {
  const params = new URLSearchParams(window.location.search);
  const requestedView = params.get("view");
  const requestedDay = params.get("date");
  const currentView = views.has(requestedView) ? requestedView : "diary";
  const urlDay = currentView === "diary" && validDay(requestedDay) ? requestedDay : null;
  const historyDay = currentView !== "diary" && validDay(window.history.state?.day) ? window.history.state.day : null;
  return {
    view: currentView,
    analysisTab: params.get("tab") === "automatik" ? "automatik" : "werte",
    day: urlDay || historyDay || new Date().toLocaleDateString("sv-SE"),
    persistDay: Boolean(urlDay || (historyDay && window.history.state?.persistDay)),
  };
}

function syncTabLinks(url = new URL(window.location.href)) {
  $$(".tab").forEach(link => {
    const linkURL = new URL(url);
    linkURL.searchParams.set("view", link.dataset.view);
    linkURL.searchParams.delete("tab");
    if (link.dataset.view === "diary" && state.persistDay) linkURL.searchParams.set("date", state.day);
    else linkURL.searchParams.delete("date");
    link.href = linkURL.href;
  });
}

function updateLocation(replace = false) {
  const url = new URL(window.location.href);
  url.searchParams.set("view", state.view);
  if (state.view === "diary" && state.persistDay) url.searchParams.set("date", state.day);
  else url.searchParams.delete("date");
  if (state.view === "analysis" && state.analysisTab === "automatik") url.searchParams.set("tab", "automatik");
  else url.searchParams.delete("tab");
  syncTabLinks(url);
  window.history[replace ? "replaceState" : "pushState"]({ view: state.view, day: state.day, persistDay: state.persistDay }, "", url);
}

function view(name, options = {}) {
  if (!views.has(name)) return;
  const changed = state.view !== name;
  state.view = name;
  $$(".tab").forEach(link => {
    const active = link.dataset.view === name;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  $$(".view").forEach(section => section.hidden = section.id !== name + "-view");
  if (changed && options.updateHistory !== false) updateLocation();
  if (name === "analysis") loadSummary();
  if (changed) releaseEntryTime();
  if (name === "diary") setDefaultMealTime();
}

// Analysis sub-pages: values of the chosen period, or how the Automatik would have done.
function analysisTab(name, options = {}) {
  const changed = state.analysisTab !== name;
  state.analysisTab = name;
  $$("[data-analysis-tab]").forEach(button => button.setAttribute("aria-selected", String(button.dataset.analysisTab === name)));
  $("#analysis-werte").hidden = name !== "werte";
  $("#analysis-automatik").hidden = name !== "automatik";
  if (changed && options.updateHistory !== false) updateLocation(true);
}

function changeDay(day, options = {}) {
  if (!validDay(day) || day === state.day) return;
  resetEntryForm();
  state.day = day;
  if (day < new Date().toLocaleDateString("sv-SE")) state.persistDay = true;
  $("#selected-day").value = day;
  setDefaultMealTime();
  setDefaultSportTime();
  if (options.updateHistory !== false) updateLocation(Boolean(options.replaceHistory));
  if (options.load !== false) loadDay();
}

// An entry is always "now" unless the time was changed on purpose. That manual time ends with the
// next saved entry, a view or day change, or after 10 minutes without touching the form.
const ENTRY_TIME_HOLD_MS = 10 * 60000;

function markEntryActivity() {
  state.entryActivityAt = Date.now();
}

function releaseEntryTime() {
  if (!state.entryTimeTouched) return;
  state.entryTimeTouched = false;
  state.entryTimeOpen = false;
}

function setDefaultMealTime(options = {}) {
  if (state.entryTimeTouched && Date.now() - (state.entryActivityAt || 0) > ENTRY_TIME_HOLD_MS) releaseEntryTime();
  if (state.entryTimeTouched) return;
  const time = localInputTime(new Date()).slice(11, 16);
  const changed = $("#entry-time").value !== time;
  $("#entry-time").value = time;
  updateEntryChips();
  if (changed && options.preview !== false) schedulePreview(0);
}

// Time and note stay folded behind chips while the entry is "now"; other days always show the time.
function updateEntryChips() {
  const form = $("#entry-form");
  const timeOpen = Boolean(state.entryTimeOpen) || state.day !== todayDay();
  const timeChip = $("#entry-time-chip");
  $("#entry-time-field").hidden = !timeOpen;
  timeChip.hidden = timeOpen;
  timeChip.setAttribute("aria-expanded", String(timeOpen));
  timeChip.textContent = "Zeit: " + (state.entryTimeTouched ? "" : "jetzt · ") + (form.elements.at.value || "—");
  const noteOpen = Boolean(state.entryNoteOpen) || Boolean(form.elements.notes.value);
  $("#entry-note-field").hidden = !noteOpen;
  $("#entry-note-chip").hidden = noteOpen;
  $("#entry-note-chip").setAttribute("aria-expanded", String(noteOpen));
  updateEntryFinger();
  updateEntryBasalOption();
  updateEntrySubmitLabel();
}

function basalDefault() {
  const units = Number(state.settings?.basal_default_units ?? 17);
  return Number.isFinite(units) && units > 0 ? units : 0;
}

// The server offers the usual basal dose with an entry before breakfast end while the day has none.
function previewBasalUnits() {
  const form = $("#entry-form");
  const preview = currentPreview();
  return form.elements.kind.value === "meal" && preview ? Number(preview.basal_units) : 0;
}

function entryBasalUnits() {
  const form = $("#entry-form");
  return !$("#entry-basal-option").hidden && form.elements.with_basal.checked ? previewBasalUnits() : 0;
}

function updateEntryBasalOption() {
  const units = previewBasalUnits();
  $("#entry-basal-option").hidden = !(units > 0);
  $("#entry-basal-hint").textContent = "+ Basal " + num(units, 1) + " IE";
}

function updateEntrySubmitLabel() {
  const form = $("#entry-form");
  const button = $("#entry-submit");
  if (form.elements.kind.value === "exercise") {
    button.textContent = "Sport in Nightscout speichern";
    return;
  }
  if (form.elements.kind.value === "basal") {
    const units = Number(form.elements.basal_units.value);
    button.textContent = Number.isFinite(units) && units > 0 ? "Basal " + num(units, 1) + " IE speichern" : "Basal speichern";
    return;
  }
  const carbs = Number(form.elements.carbs.value);
  const insulin = Number(form.elements.insulin.value);
  const parts = [
    Number.isFinite(carbs) && carbs > 0 ? num(carbs, 0) + " g" : "",
    Number.isFinite(insulin) && insulin > 0 ? num(insulin, 2) + " IE" : "",
  ].filter(Boolean);
  const basal = entryBasalUnits();
  if (parts.length && basal > 0) parts.push("Basal " + num(basal, 1) + " IE");
  button.textContent = parts.length ? parts.join(" + ") + " speichern" : "In Nightscout speichern";
}

const dayTypeSources = {
  weekday: "Standard nach Wochentag",
  vacation: "Urlaub ist aktiv",
  manual: "Für diesen Tag manuell gewählt",
};

function updateEntryDayType() {
  const form = $("#entry-form");
  const dayType = form.elements.kind.value === "meal" ? currentPreview()?.day_type : null;
  $("#entry-day-type").hidden = !dayType;
  updateEntryInfluences();
  if (!dayType) return;
  $$(".day-type-option", $("#entry-day-type")).forEach(button =>
    button.setAttribute("aria-pressed", String(button.dataset.dayType === dayType.value)));
  $("#entry-day-type-hint").textContent = (dayTypeSources[dayType.source] || "") + " · bestimmt den Frühstücksfaktor";
}

async function saveEntryDayType(event) {
  const button = event.target.closest(".day-type-option");
  if (!button) return;
  const day = state.day;
  const buttons = $$(".day-type-option", $("#entry-day-type"));
  buttons.forEach(item => { item.disabled = true; });
  try {
    await api("/api/analysis/day-type", {
      method: "POST", body: JSON.stringify({ day, day_type: button.dataset.dayType }),
    });
    await updatePreview();
  } catch (error) {
    toast(error.message, true);
  } finally {
    buttons.forEach(item => { item.disabled = false; });
  }
}

// "Tag durchgehen" is where a day's data is kept clean: its day type is set here too.
function renderReviewDayType(data) {
  const box = $("#review-day-type");
  box.hidden = !data.day_type;
  if (!data.day_type) return;
  $$("[data-review-day-type]").forEach(button =>
    button.setAttribute("aria-pressed", String(button.dataset.reviewDayType === data.day_type)));
  $("#review-day-type-hint").textContent = (dayTypeSources[data.day_type_source] || "") + " · bestimmt den Frühstücksfaktor";
}

async function saveReviewDayType(button) {
  const buttons = $$("[data-review-day-type]");
  buttons.forEach(item => { item.disabled = true; });
  try {
    await api("/api/analysis/day-type", {
      method: "POST", body: JSON.stringify({ day: state.day, day_type: button.dataset.reviewDayType }),
    });
    await loadDay();
  } catch (error) {
    toast(error.message, true);
  } finally {
    buttons.forEach(item => { item.disabled = false; });
  }
}

async function saveVacation(event) {
  const input = event.target;
  input.disabled = true;
  try {
    const data = await api("/api/vacation", { method: "POST", body: JSON.stringify({ active: input.checked }) });
    renderVacation(data);
    toast(data.active ? "Urlaub aktiviert: jeder Tag zählt als frei" : "Urlaub beendet");
    await loadDay();
  } catch (error) {
    input.checked = !input.checked;
    toast(error.message, true);
  } finally {
    input.disabled = false;
  }
}

async function loadNotifications() {
  try {
    renderNotifications(await api("/api/notifications"));
  } catch (error) {
    $("#notify-status").textContent = error.message;
  }
}

function renderNotifications(data) {
  $("#notify-status").textContent = data.configured
    ? "Pushover ist eingerichtet."
    : "Pushover ist nicht eingerichtet: PUSHOVER_APP_TOKEN und PUSHOVER_USER_KEY im Deployment setzen.";
  $$(".notify-toggle").forEach(input => { input.checked = Boolean(data.enabled[input.dataset.kind]); });
  $$(".notify-priority").forEach(select => {
    select.replaceChildren(...data.priority_options.map(option => new Option("Stufe: " + option.label, String(option.value))));
    select.value = String(data.priorities[select.dataset.kind]);
  });
  $("#notify-basal-hint").textContent = "Wenn bis " + data.basal_after + " (Frühstücksende) heute noch kein Basal erfasst ist.";
  $("#notify-test").disabled = !data.configured;
  const list = $("#notify-recent");
  list.replaceChildren(...data.recent.map(item => {
    const row = document.createElement("li");
    row.textContent = new Date(item.sent_at).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" })
      + " · " + item.label + ": " + item.message;
    return row;
  }));
}

async function saveNotification(event) {
  const input = event.target;
  input.disabled = true;
  try {
    renderNotifications(await api("/api/notifications", {
      method: "POST", body: JSON.stringify({ kind: input.dataset.kind, enabled: input.checked }),
    }));
  } catch (error) {
    input.checked = !input.checked;
    toast(error.message, true);
  } finally {
    input.disabled = false;
  }
}

async function saveNotificationPriority(event) {
  const select = event.target;
  select.disabled = true;
  try {
    renderNotifications(await api("/api/notifications", {
      method: "POST", body: JSON.stringify({ kind: select.dataset.kind, priority: Number(select.value) }),
    }));
  } catch (error) {
    toast(error.message, true);
    loadNotifications();
  } finally {
    select.disabled = false;
  }
}

async function sendTestNotification(event) {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    toast((await api("/api/notifications/test", { method: "POST", body: "{}" })).message);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function renderVacation(vacation) {
  $("#vacation-toggle").checked = Boolean(vacation?.active);
  $("#vacation-status").textContent = vacation?.active
    ? "Aktiv seit " + new Date(vacation.since + "T12:00").toLocaleDateString("de-DE") + ". Jeder Tag zählt als frei."
    : "Mo–Fr zählt als Arbeitstag.";
}

function setDefaultSportTime() {}
function setSportDuration() {}

// The server decides whether a new carb entry counts as the snack of the preceding main meal.
function updateEntryAssignment() {
  const chip = $("#entry-assign-chip");
  const form = $("#entry-form");
  const assignment = form.elements.kind.value === "meal" ? currentPreview()?.assignment : null;
  chip.hidden = !assignment;
  if (!assignment) return;
  const snack = assignment.meal ? "Snack zu " + names[assignment.meal] + " " + timeOnly(assignment.at) : "Snack ohne Mahlzeit";
  chip.textContent = "Zählt als: " + (assignment.snack ? snack : "eigenes " + names[assignment.own_meal])
    + (assignment.own_meal_taken ? " (" + names[assignment.own_meal] + " heute schon eingetragen)" : " ⇄");
  chip.disabled = Boolean(assignment.own_meal_taken);
}

// The server offers a split bolus for a large main meal; the chip switches it off and on.
function updateEntrySplit() {
  const chip = $("#entry-split-chip");
  const split = $("#entry-form").elements.kind.value === "meal" ? currentPreview()?.split : null;
  chip.hidden = !split;
  if (!split) return;
  chip.setAttribute("aria-pressed", String(Boolean(split.active)));
  chip.textContent = split.active
    ? "Split " + split.first_percent + "/" + (100 - split.first_percent) + " · 2. Teil " + timeOnly(split.due_at)
    : "Ohne Split";
}

// KH-Sicherheit is a three-stop slider (unsure, roughly known, weighed) that starts in the middle.
// An edited entry without a mark keeps none until the slider is moved.
const CERTAINTY_LEVELS = ["unsure", "known", "weighed"];
const CERTAINTY_SLIDER_LABELS = ["KH unsicher", "KH halbwegs", "KH gewogen"];

function showCertainty(input) {
  input.parentElement.querySelector(".certainty-label").textContent = CERTAINTY_SLIDER_LABELS[Number(input.value)];
  input.parentElement.dataset.level = CERTAINTY_LEVELS[Number(input.value)];  // colours: red, yellow, green
}

function setCertainty(form, value) {
  const input = form.elements.certainty_level;
  const index = CERTAINTY_LEVELS.indexOf(value);
  input.value = String(index < 0 ? 1 : index);
  input.dataset.unset = value === undefined || index >= 0 ? "" : "1";
  showCertainty(input);
}

function certaintyValue(form) {
  const input = form.elements.certainty_level;
  return input.dataset.unset === "1" ? null : CERTAINTY_LEVELS[Number(input.value)];
}

document.addEventListener("input", event => {
  if (!(event.target instanceof HTMLInputElement) || event.target.name !== "certainty_level") return;
  event.target.dataset.unset = "";
  showCertainty(event.target);
});

// The place row: the server names the chips and which one the position suggests; a tap overrides it.
const POSITION_MAX_AGE_MS = 2 * 60000;

function requestEntryPosition() {
  const form = $("#entry-form");
  if (form.elements.kind.value !== "meal" || !(Number(form.elements.carbs.value) > 0) || state.entryTimeTouched
      || state.day !== todayDay() || state.entryPositionPending) return;
  if (state.entryPosition && Date.now() - state.entryPositionAt < POSITION_MAX_AGE_MS) return;
  state.entryPositionPending = true;
  updateEntryPlace();
  currentPosition().then(position => {
    state.entryPosition = position;
    state.entryPositionAt = Date.now();
    state.entryPositionError = null;
  }).catch(error => {
    state.entryPositionError = error?.code === 1 ? "denied" : "failed";
  }).finally(() => {
    state.entryPositionPending = false;
    schedulePreview(0);
  });
}

// Saving refreshes a position older than the cache, so a dose decided over several minutes still
// stores where the entry was typed; a fix that does not come quickly keeps the cached one.
async function freshEntryPosition() {
  const form = $("#entry-form");
  if (form.elements.kind.value !== "meal" || !(Number(form.elements.carbs.value) > 0) || state.entryTimeTouched
      || state.day !== todayDay()) return;
  if (state.entryPosition && Date.now() - state.entryPositionAt < POSITION_MAX_AGE_MS) return;
  try {
    state.entryPosition = await Promise.race([currentPosition(),
      new Promise((_, reject) => setTimeout(() => reject({ code: 3 }), 5000))]);
    state.entryPositionAt = Date.now();
  } catch (_) { /* The cached position, if any, rides along. */ }
}

function entryPlaceId() {
  if (!(Number($("#entry-form").elements.carbs.value) > 0) || state.entryPlace === "none") return null;
  return state.entryPlace ?? currentPreview()?.place?.place_id ?? null;
}

const positionHints = { denied: "Standort nicht freigegeben, Ort bitte selbst wählen",
  failed: "Standort nicht ermittelt, Ort bitte selbst wählen" };

function updateEntryPlace() {
  const box = $("#entry-place");
  const place = $("#entry-form").elements.kind.value === "meal" ? currentPreview()?.place : null;
  box.hidden = !place;
  if (!place) return;
  const selected = entryPlaceId();
  // Quick places have their own chip; another chosen or suggested place joins them, the rest sit behind "Weitere".
  const chips = place.options.filter(option => option.quick || option.id === selected);
  const more = place.options.filter(option => !chips.includes(option));
  $("#entry-place-chips").innerHTML = chips.map(option =>
    '<button type="button" class="chip place-chip" data-place-id="' + option.id + '" aria-pressed="' +
    (option.id === selected) + '">' + escapeHTML(option.name) + '</button>').join("") +
    (more.length ? '<select class="chip place-more" aria-label="Weitere Orte"><option value="">Weitere …</option>' +
      more.map(option => '<option value="' + option.id + '">' + escapeHTML(option.name) + '</option>').join("") + '</select>' : "");
  // Only what needs a look gets a line: no places yet, no position, or a position that is no place yet.
  const hint = !place.options.length ? "Noch keine Orte, in den Einstellungen anlegen"
    : state.entryPlace !== undefined || state.entryPositionPending || place.status === "matched" ? ""
    : place.status === "none" && state.entryPositionError ? positionHints[state.entryPositionError]
    : place.status === "unknown" ? place.hint : "";
  $("#entry-place-hint").textContent = hint ? "Ort: " + hint : "";
  $("#entry-place-hint").hidden = !hint;
}

function toggleEntryPlace(event) {
  const chip = event.target.closest(".place-chip");
  if (!chip) return;
  const id = Number(chip.dataset.placeId);
  state.entryPlace = entryPlaceId() === id ? "none" : id;
  updateEntryPlace();
}

function updateEntryQuality() {
  const form = $("#entry-form");
  const hidden = !(form.elements.kind.value === "meal" && Number(form.elements.carbs.value) > 0);
  $("#entry-quality").hidden = hidden;
  $("#entry-factors").hidden = hidden;
}

function updateEntrySportOption() {
  updateEntryAssignment();
  updateEntrySplit();
  updateEntryQuality();
  updateEntryPlace();
  const form = $("#entry-form");
  const option = $("#entry-sport-option");
  const checkbox = form.elements.consider_sport;
  const sport = form.elements.kind.value === "meal" ? currentPreview()?.sport : null;
  option.hidden = !sport;
  if (!sport) {
    checkbox.checked = false;
    $("#entry-sport-effect-field").hidden = true;
    updateEntryInfluences();
    return;
  }
  // The chip only says whether the sport counts; ticking it opens the editable BZ change.
  $("#entry-sport-hint").textContent = sport.events.map(item =>
    sportNames[item.sport_type] + " " + timeOnly(item.at)).join(", ") + (checkbox.checked ? " berücksichtigt" : " berücksichtigen");
  if (!checkbox.checked) form.elements.sport_effect_mgdl.value = Number(sport.suggested_change_mgdl);
  $("#entry-sport-effect-field").hidden = !checkbox.checked;
  updateEntryInfluences();
}

function updateEntryInfluences() {
  $("#entry-influences").hidden = $("#entry-sport-option").hidden && $("#entry-day-type").hidden;
}

function minuteClock(minute) {
  return String(Math.floor(minute / 60)).padStart(2, "0") + ":" + String(minute % 60).padStart(2, "0");
}

function clockMinute(value) {
  const [hours, minutes] = value.split(":").map(Number);
  return hours * 60 + minutes;
}

function updateEntryFields() {
  const form = $("#entry-form");
  const mode = form.elements.kind.value;
  form.elements.carbs.setCustomValidity("");
  $$(".entry-mode").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.entryMode === mode)));
  $("#entry-meal-fields").hidden = mode !== "meal";
  $("#entry-dose").hidden = mode !== "meal";
  $("#entry-basal-field").hidden = mode !== "basal";
  form.elements.basal_units.disabled = mode !== "basal";
  form.elements.basal_units.required = mode === "basal";
  if (mode === "basal" && !form.elements.basal_units.value && basalDefault() > 0) form.elements.basal_units.value = basalDefault();
  $("#entry-sport-field").hidden = mode !== "exercise";
  $("#entry-duration-field").hidden = mode !== "exercise";
  form.elements.carbs.disabled = mode !== "meal";
  form.elements.insulin.disabled = mode !== "meal";
  form.elements.sport_kind.disabled = mode !== "exercise";
  form.elements.duration_minutes.disabled = mode !== "exercise";
  form.elements.duration_minutes.required = mode === "exercise";
  updateEntryChips();
  renderPreview();
  schedulePreview(0);
}

async function loadSettings() {
  const data = await api("/api/settings");
  state.settings = data.settings;
  $("#connection").textContent = data.connected ? "" : "Nightscout einrichten";
  $("#connection").hidden = data.connected;
  $("#nightscout-location").hidden = data.connected;
  $("#nightscout-location").textContent = data.connected ? "" : "NIGHTSCOUT_URL und NIGHTSCOUT_CREDENTIAL setzen";
  $("#settings-version").textContent = "Einstellungsversion " + data.version;
  $("#logout-form").hidden = !data.login_required;
  const form = $("#settings-form");
  form.elements.window_minutes.value = data.settings.window_minutes;
  form.elements.dose_target_mgdl.value = data.settings.dose_target_mgdl;
  form.elements.dose_cautious_target_mgdl.value = data.settings.dose_cautious_target_mgdl;
  form.elements.dose_low_target_mgdl.value = data.settings.dose_low_target_mgdl ?? 100;
  form.elements.auto_method.value = data.settings.auto_method || "percentile";
  form.elements.auto_percentile.value = data.settings.auto_percentile;
  form.elements.auto_lookback_days.value = String(data.settings.auto_lookback_days);
  form.elements.auto_half_life_days.value = data.settings.auto_half_life_days;
  form.elements.auto_surcharge_mgdl.value = data.settings.auto_surcharge_mgdl ?? 40;
  form.elements.basal_default_units.value = data.settings.basal_default_units ?? 17;
  form.elements.split_min_grams.value = data.settings.split_min_grams ?? 80;
  form.elements.split_first_percent.value = data.settings.split_first_percent ?? 60;
  form.elements.split_delay_minutes.value = data.settings.split_delay_minutes ?? 120;
  form.elements.breakfast_end.value = minuteClock(data.settings.breakfast_end_minute);
  form.elements.lunch_end.value = minuteClock(data.settings.lunch_end_minute);
  form.elements.sport_lookback_minutes.value = data.settings.sport_lookback_minutes;
  for (const kind of sportKinds) {
    form.elements["sport_" + kind].value = sportChange(data.settings.sport_effects_mgdl[kind]);
  }
  renderIsfRows(data.settings.isf_schedule, data.settings.nightscout_profile, data.profile_sync_at, data.profile_sync_error);
  renderIcRows(data.settings.ic_schedule || []);
  renderTargetRows(data.settings.target_schedule || []);
  renderVacation(data.vacation);
  await loadNotifications();
  renderPlaces(await api("/api/places"));
}

// Place management: chips with their spots, and unknown positions that can join a chip or become one.
function spotHTML(spot, index) {
  return '<li class="place-spot"><a href="' + osmLink(spot.lat, spot.lon) + '" target="_blank" rel="noopener">Standort ' +
    (index + 1) + '</a>' +
    '<button type="button" class="quiet" data-place-action="delete_spot" data-spot-id="' + spot.id + '">Entfernen</button></li>';
}

function renderPlaces(data) {
  state.places = data;
  $("#places-list").innerHTML = data.places.length ? data.places.map(place =>
    '<div class="place-item" data-place-id="' + place.id + '"><div class="place-head">' +
    '<input type="text" class="place-name" maxlength="40" value="' + escapeHTML(place.name) + '" aria-label="Name des Orts">' +
    '<span class="hint">' + place.count + (place.count === 1 ? ' Eintrag' : ' Einträge') + '</span>' +
    '<label class="check-label place-quick"><input type="checkbox" class="place-quick-toggle"' + (place.quick ? ' checked' : '') +
    '> Schnellauswahl</label>' +
    '<button type="button" class="quiet delete-treatment" data-place-action="delete" data-place-id="' + place.id + '">Löschen</button></div>' +
    (place.spots.length ? '<ul class="place-spots">' + place.spots.map((spot, index) => spotHTML(spot, index)).join("") + '</ul>'
      : '<p class="hint">Ohne Standort: nur von Hand wählbar.</p>') +
    '<button type="button" class="quiet" data-place-action="here" data-place-id="' + place.id + '">+ Aktuellen Standort hinzufügen</button></div>'
  ).join("") : '<p class="hint">Noch keine Orte.</p>';
  const options = data.places.map(place => '<option value="' + place.id + '">' + escapeHTML(place.name) + '</option>').join("");
  $("#places-unknown").innerHTML = data.unknown.length ? data.unknown.map((group, index) =>
    '<div class="place-unknown" data-index="' + index + '"><p>' + group.count + '× · zuletzt ' +
    escapeHTML(new Date(group.last_at).toLocaleString("de-DE", { day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit" })) +
    (group.nearest_place ? ' · ' + escapeHTML(num(group.nearest_place.distance_m >= 1000 ? group.nearest_place.distance_m / 1000 : group.nearest_place.distance_m, 1)) +
      (group.nearest_place.distance_m >= 1000 ? ' km' : ' m') + ' von ' + escapeHTML(group.nearest_place.name) : '') +
    ' · <a href="' + osmLink(group.lat, group.lon) + '" target="_blank" rel="noopener">Karte</a></p>' +
    '<div class="place-assign"><select class="unknown-target" aria-label="Zu welchem Ort"><option value="">Neuer Ort …</option>' +
    options + '</select><input type="text" class="unknown-name" maxlength="40" placeholder="Name, z. B. Schulung" aria-label="Name des neuen Orts"><button type="button" data-place-action="adopt" data-index="' + index + '">Zuordnen</button></div></div>'
  ).join("") : '<p class="hint">Keine. Einträge, deren Standort zu keinem Ort passt, erscheinen hier.</p>';
}

async function placeAction(body) {
  try {
    renderPlaces(await api("/api/places", { method: "POST", body: JSON.stringify(body) }));
    if (state.dayData) loadDay();
    schedulePreview(0);
    return true;
  } catch (error) {
    toast(error.message, true);
    return false;
  }
}

async function placeClick(event) {
  const button = event.target.closest("[data-place-action]");
  if (!button) return;
  const action = button.dataset.placeAction;
  if (action === "delete") {
    const name = button.closest(".place-item").querySelector(".place-name").value;
    if (!confirm("Ort „" + name + "“ löschen? Einträge mit Standort erscheinen danach wieder als unbekannt.")) return;
    await placeAction({ action, id: button.dataset.placeId });
  } else if (action === "delete_spot") {
    await placeAction({ action, spot_id: button.dataset.spotId });
  } else if (action === "here") {
    button.disabled = true;
    try {
      const position = await currentPosition();
      if (position.accuracy_m > state.places.max_accuracy_m)
        throw new Error("Standort zu ungenau (± " + position.accuracy_m + " m). Bitte draußen oder mit WLAN erneut versuchen.");
      await placeAction({ action: "add_spot", place_id: button.dataset.placeId, lat: position.lat, lon: position.lon });
    } catch (error) {
      toast(error?.code === 1 ? "Standort nicht freigegeben" : error?.message || "Standort nicht ermittelt", true);
    } finally {
      button.disabled = false;
    }
  } else if (action === "adopt") {
    const group = state.places.unknown[Number(button.dataset.index)];
    const row = button.closest(".place-unknown");
    const target = $(".unknown-target", row).value;
    await placeAction({ action: "add_spot", place_id: target || null, name: $(".unknown-name", row).value,
      lat: group.lat, lon: group.lon });
  }
}

async function placeChange(event) {
  if (event.target.matches(".place-name")) {
    await placeAction({ action: "rename", id: event.target.closest(".place-item").dataset.placeId, name: event.target.value });
  } else if (event.target.matches(".place-quick-toggle")) {
    await placeAction({ action: "quick", id: event.target.closest(".place-item").dataset.placeId, quick: event.target.checked });
  } else if (event.target.matches(".unknown-target")) {
    $(".unknown-name", event.target.closest(".place-unknown")).hidden = event.target.value !== "";
  }
}

function renderTargetRows(rows) {
  $("#target-rows").innerHTML = rows.length
    ? '<div class="isf-table"><div class="isf-table-head"><span>Ab</span><span>Zielbereich</span></div>' +
      rows.map(row => '<div class="isf-readonly-row"><span>' +
        escapeHTML(minuteClock(Number(row.start_minute))) + '</span><strong>' +
        escapeHTML(num(row.low_mgdl, 0)) + '–' + escapeHTML(num(row.high_mgdl, 0)) +
        ' mg/dL</strong></div>').join("") + '</div>'
    : '<p class="hint">Kein Zielbereich im aktuellen Nightscout-Profil verfügbar.</p>';
}

function renderIcRows(rows) {
  const list = $("#ic-rows");
  if (!rows.length) {
    list.innerHTML = '<p class="hint">Kein I:C-Zeitplan aus Nightscout verfügbar.</p>';
    $("#save-ic").disabled = true;
    return;
  }
  $("#save-ic").disabled = false;
  list.innerHTML = '<div class="isf-table"><div class="isf-table-head"><span>Ab</span><span>Insulinfaktor (IE/KE)</span></div>' +
    rows.map(row => {
      const start = Number(row.start_minute);
      const time = String(Math.floor(start / 60)).padStart(2, "0") + ':' + String(start % 60).padStart(2, "0");
      const display = Number((10 / Number(row.grams_per_unit)).toFixed(2));
      return '<label class="ic-row"><span>' + time + '</span><input type="number" min="' + escapeHTML(pickerMinimum(display, 0.1, true)) + '" max="1000" step="0.1" required data-start-minute="' +
        escapeHTML(start) + '" data-grams-per-unit="' + escapeHTML(row.grams_per_unit) +
        '" data-original-display="' + escapeHTML(display) + '" value="' + escapeHTML(display) +
        '" aria-label="Insulinfaktor ab ' + time + ' in IE je KE"></label>';
    }).join("") + '</div>';
}

function icScheduleRow(input) {
  const start_minute = Number(input.dataset.startMinute);
  return Number(input.value) === Number(input.dataset.originalDisplay)
    ? { start_minute, grams_per_unit: input.dataset.gramsPerUnit }
    : { start_minute, units_per_ke: input.value };
}

async function saveProfileSchedule(kind) {
  const isIc = kind === "ic";
  const button = $(isIc ? "#save-ic" : "#save-isf");
  const inputs = $$(isIc ? "#ic-rows input" : "#isf-rows input");
  const status = $(isIc ? "#ic-status" : "#isf-sync-status");
  const key = isIc ? "units_per_ke" : "mgdl_per_unit";
  if (button.disabled) return;
  if (!inputs.every(input => input.reportValidity())) return;
  button.disabled = true;
  status.textContent = "Profil-Zeitplan wird in Nightscout gespeichert …";
  try {
    const result = await api("/api/profile/" + kind, { method: "POST", body: JSON.stringify({
      profile_id: state.settings.nightscout_profile.profile_id,
      [isIc ? "ic_schedule" : "isf_schedule"]: inputs.map(input => isIc
        ? icScheduleRow(input)
        : { start_minute: Number(input.dataset.startMinute), [key]: input.value }),
    }) });
    await loadSettings();
    if (state.view === "analysis") await loadSummary();
    status.textContent = result.message;
    toast(result.message);
  } catch (error) {
    status.textContent = error.message;
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function renderIsfRows(rows, profile, syncedAt, syncError) {
  const list = $("#isf-rows");
  if (!rows.length) {
    list.innerHTML = '<p class="hint">Kein Korrekturfaktor-Profil aus Nightscout im lokalen Cache.</p>';
    $("#save-isf").disabled = true;
  } else {
    $("#save-isf").disabled = false;
    list.innerHTML = '<div class="isf-table"><div class="isf-table-head"><span>Ab</span><span>Korrekturfaktor</span></div>' +
      rows.map(row => {
        const start = Number(row.start_minute);
        const hh = String(Math.floor(start / 60)).padStart(2, "0");
        const mm = String(start % 60).padStart(2, "0");
        return '<label class="ic-row"><span>' + hh + ':' + mm + '</span><input type="number" min="0.001" max="1000" step="any" required data-start-minute="' +
          escapeHTML(start) + '" value="' + escapeHTML(row.mgdl_per_unit) + '" aria-label="Korrekturfaktor ab ' + hh + ':' + mm + ' in mg/dL je IE"></label>';
      }).join("") + '</div>';
  }
  const source = profile
    ? 'Profil „' + escapeHTML(profile.profile_name) + '“ · gültig ab ' +
      escapeHTML(new Date(profile.start_at).toLocaleString("de-DE")) +
      (profile.timezone ? ' · ' + escapeHTML(profile.timezone) : '')
    : 'Nightscout-Profil nicht verfügbar.';
  const synced = syncedAt ? ' · zuletzt synchronisiert ' + escapeHTML(new Date(syncedAt).toLocaleString("de-DE")) : '';
  $("#isf-sync-status").textContent = (syncError ? syncError + ' · ' : '') + source + synced;
}

const MINUTE = 60000;
// The server's limits for a manual meal window, in milliseconds.
function windowLimits() {
  const limits = state.dayData?.window_limits || { lead_minutes: 120, lag_minutes: 120, min_minutes: 30, max_minutes: 600 };
  return { lead: limits.lead_minutes * MINUTE, lag: limits.lag_minutes * MINUTE,
    min: limits.min_minutes * MINUTE, max: limits.max_minutes * MINUTE };
}
const SNAP_PIXELS = 16;
let CHART = chartLayout(600);

// Chart units equal CSS pixels at the measured width, so text and touch targets keep their size.
function chartLayout(width) {
  const clamped = Math.max(300, Math.min(900, Math.round(width || 600)));
  return { width: clamped, left: 36, right: clamped - 12, plotTop: 70, plotBottom: 258 };
}
let windowEditor = null;

function glucosePoints(entries) {
  return entries.map(point => ({ t: new Date(point.at).getTime(), g: Number(point.sgv), tone: point.tone }))
    .filter(point => Number.isFinite(point.g)).sort((a, b) => a.t - b.t);
}

const trendSymbols = { up_fast: "↑", up: "↗", flat: "→", down: "↘", down_fast: "↓" };

function trendSymbol(direction) {
  return trendSymbols[direction] || "";
}

// The server sends each meal's counted window and the CGM value at both ends.
function mealWindow(meal) {
  const window = meal.window;
  const time = value => new Date(value).getTime();
  return {
    mealAt: time(meal.at), start: time(window.start), end: time(window.end),
    autoStart: time(window.auto_start), autoEnd: time(window.auto_end), manual: window.manual,
    startGlucose: window.start_glucose, endGlucose: window.end_glucose,
    sourceId: meal.source_id || null,
  };
}

function chartEvents(treatments) {
  return treatments.flatMap(item => {
    const t = new Date(item.at).getTime();
    if (item.eventType === "Exercise") {
      return [{ kind: "sport", t, end: t + Number(item.duration || 0) * MINUTE, label: sportNames[item.sport_type] || "Sport" }];
    }
    const result = [];
    if (Number(item.insulin) > 0) result.push({ kind: "insulin", id: item.id, t, amount: Number(item.insulin), rapid: Boolean(item.rapid) });
    if (Number(item.carbs) > 0) result.push({ kind: "carbs", id: item.id, t, amount: Number(item.carbs) });
    return result;
  });
}

function chartView(range) {
  const from = Math.min(range.start, range.autoStart) - 45 * MINUTE;
  const to = Math.max(range.end, range.autoEnd, range.mealAt + 240 * MINUTE) + 75 * MINUTE;
  return {
    from: Math.max(from, range.mealAt - windowLimits().lead - 30 * MINUTE),
    to: Math.min(to, range.mealAt + windowLimits().max + 60 * MINUTE),
  };
}

function chartScales(view, points) {
  const visible = points.filter(point => point.t >= view.from && point.t <= view.to).map(point => point.g);
  const top = Math.max(300, Math.ceil((Math.max(0, ...visible) + 20) / 50) * 50);
  const bottom = Math.min(40, Math.floor((Math.min(400, ...visible) - 10) / 10) * 10);
  return {
    x: t => CHART.left + (t - view.from) / (view.to - view.from) * (CHART.right - CHART.left),
    y: g => CHART.plotBottom - (g - bottom) / (top - bottom) * (CHART.plotBottom - CHART.plotTop),
    top, bottom,
  };
}

const chartIcons = {
  insulin: '<path d="M-5 5l3-3m-1-1 6-6 3 3-6 6zM4-7l3 3M5-8l3 3"/>',
  carbs: '<path d="M0-3c-2-2-6-1-6 3s3 7 6 7 6-3 6-7-4-5-6-3zM0-3c0-2 1-4 3-5"/>',
};

function eventLanes(events, scales, view, range) {
  const labelEnds = {};
  const shown = events.filter(event => (event.end || event.t) >= view.from && event.t <= view.to).sort((a, b) => a.t - b.t);
  const iconsCovered = (kind, from, to, own) => shown.some(other => other !== own && other.kind === kind &&
    scales.x(other.t) + 8 > from && scales.x(other.t) - 8 < to);
  return shown.map(event => {
    const x = scales.x(Math.max(event.t, view.from));
    const included = windowIncludes(range, event) ? " is-included" : "";
    if (event.kind === "sport") {
      const width = Math.max(4, scales.x(Math.min(event.end, view.to)) - x);
      return '<g class="chart-event sport' + included + '" data-t="' + event.t + '"><rect x="' + x.toFixed(1) +
        '" y="46" width="' + width.toFixed(1) + '" height="14" rx="4"/><text x="' + (x + 4).toFixed(1) +
        '" y="57">' + escapeHTML(event.label) + '</text></g>';
    }
    const y = event.kind === "insulin" ? 14 : 34;
    const label = event.kind === "insulin" ? num(event.amount, 1) + " IE" : num(event.amount, 0) + " g";
    // Labels near the right edge flip left; a label that would overlap its lane neighbour is left out.
    const labelWidth = label.length * 6;
    const flip = x + 10 + labelWidth > CHART.right;
    const textStart = flip ? x - 10 - labelWidth : x + 10;
    const showLabel = textStart >= (labelEnds[event.kind] ?? -Infinity) &&
      !iconsCovered(event.kind, textStart, textStart + labelWidth, event);
    if (showLabel) labelEnds[event.kind] = textStart + labelWidth + 4;
    return '<g class="chart-event ' + event.kind + (event.rapid === false ? ' non-rapid' : '') + included +
      '" data-t="' + event.t + '" data-id="' + escapeHTML(event.id || "") + '"><line x1="' + x.toFixed(1) + '" y1="' + (y + 9) + '" x2="' + x.toFixed(1) + '" y2="' + CHART.plotBottom + '" class="event-guide"/>' +
      '<g transform="translate(' + x.toFixed(1) + ' ' + y + ')">' + chartIcons[event.kind] + '</g>' +
      (showLabel ? '<text x="' + (flip ? x - 10 : x + 10).toFixed(1) + '" y="' + (y + 4) + '"' + (flip ? ' text-anchor="end"' : '') + '>' + escapeHTML(label) + '</text>' : '') + '</g>';
  }).join("");
}

function targetBand(view, scales) {
  return (state.dayData?.target_bands || []).map(band => {
    const from = Math.max(new Date(band.from).getTime(), view.from);
    const to = Math.min(new Date(band.to).getTime(), view.to);
    const high = Math.min(Number(band.high_mgdl), scales.top);
    const low = Math.max(Number(band.low_mgdl), scales.bottom);
    if (to <= from || high <= low) return "";
    return '<rect class="target-band" x="' + scales.x(from).toFixed(1) + '" y="' + scales.y(high).toFixed(1) +
      '" width="' + (scales.x(to) - scales.x(from)).toFixed(1) + '" height="' + (scales.y(low) - scales.y(high)).toFixed(1) + '"/>';
  }).join("");
}

function handleSVG(name, t, points, scales, interactive, reading) {
  const x = scales.x(t);
  // A drawn meal window brings its own reading; the editor's moving handles look theirs up.
  if (interactive) reading = editorReading(t);
  const value = reading?.value != null ? Number(reading.value) : null;
  const tone = reading?.tone || "none";
  const dotY = value == null ? CHART.plotBottom : scales.y(value);
  const label = name === "start" ? "Beginn" : "Ende";
  return '<g class="window-handle" data-handle="' + name + '" transform="translate(' + x.toFixed(1) + ' 0)"' +
    (interactive ? ' tabindex="0" role="slider" aria-label="' + label + ' des Zeitraums" aria-valuetext="' + escapeHTML(timeOnly(t)) + ' Uhr"' : '') + '>' +
    '<line class="handle-line" x1="0" y1="64" x2="0" y2="' + (interactive ? 290 : CHART.plotBottom) + '"/>' +
    '<circle class="handle-dot" data-tone="' + tone + '" cx="0" cy="' + dotY.toFixed(1) + '" r="' + (interactive ? 7 : 4.5) + '"' + (value == null ? ' hidden' : '') + '/>' +
    (interactive
      ? '<rect class="handle-hit" x="-24" y="62" width="48" height="262"/>' +
        '<g class="handle-knob"><rect x="-21" y="288" width="42" height="30" rx="15"/><path d="M-5 297v12M0 297v12M5 297v12"/></g>'
      : '') +
    '</g>';
}

function windowChartSVG(range, entries, treatments, options = {}) {
  const interactive = Boolean(options.interactive);
  const overview = Boolean(options.overview);
  const view = options.view || chartView(range);
  const clipId = options.clipId || (overview ? "overview-clip" : interactive ? "editor-clip" : "chart-clip-" + range.mealAt);
  const points = glucosePoints(entries);
  const scales = chartScales(view, points);
  const visible = points.filter(point => point.t >= view.from - 10 * MINUTE && point.t <= view.to + 10 * MINUTE);
  const height = interactive ? 324 : 284;
  const grid = [];
  const yLabels = [];
  for (let g = Math.ceil(scales.bottom / 50) * 50; g <= scales.top; g += 50) {
    grid.push('<line class="grid" x1="' + CHART.left + '" x2="' + CHART.right + '" y1="' + scales.y(g).toFixed(1) + '" y2="' + scales.y(g).toFixed(1) + '"/>');
    yLabels.push('<text class="y-label" x="' + (CHART.left - 6) + '" y="' + (scales.y(g) + 4).toFixed(1) + '" text-anchor="end">' + g + '</text>');
  }
  const ticks = [];
  const hour = 60 * MINUTE;
  const step = [1, 2, 3, 6].map(hours => hours * hour)
    .find(candidate => (view.to - view.from) / candidate * 48 <= CHART.right - CHART.left) || 6 * hour;
  for (let t = Math.ceil(view.from / step) * step; t <= view.to; t += step) {
    ticks.push('<line class="tick" x1="' + scales.x(t).toFixed(1) + '" x2="' + scales.x(t).toFixed(1) + '" y1="' + CHART.plotBottom + '" y2="' + (CHART.plotBottom + 5) + '"/>' +
      '<text class="x-label" x="' + scales.x(t).toFixed(1) + '" y="' + (CHART.plotBottom + 19) + '" text-anchor="middle">' + escapeHTML(timeOnly(t)) + '</text>');
  }
  const limit = (g, className) => g > scales.bottom && g < scales.top
    ? '<line class="' + className + '" x1="' + CHART.left + '" x2="' + CHART.right + '" y1="' + scales.y(g).toFixed(1) + '" y2="' + scales.y(g).toFixed(1) + '"/>' : '';
  const segments = [];
  let segment = [];
  visible.forEach((point, index) => {
    if (index && point.t - visible[index - 1].t > 15 * MINUTE) { segments.push(segment); segment = []; }
    segment.push(scales.x(point.t).toFixed(1) + "," + scales.y(point.g).toFixed(1));
  });
  segments.push(segment);
  const curve = segments.filter(item => item.length > 1).map(item => '<polyline class="curve" points="' + item.join(" ") + '"/>').join("");
  const shadeX = scales.x(range.start);
  const last = visible.filter(point => point.t <= view.to).pop();
  const label = overview
    ? 'Glukoseverlauf mit Ereignissen von ' + timeOnly(view.from) + ' bis ' + timeOnly(view.to) + ' Uhr'
    : 'Glukoseverlauf mit Auswertungszeitraum ' + timeOnly(range.start) + ' bis ' + timeOnly(range.end) + ' Uhr';
  return '<svg class="window-chart-svg' + (interactive ? ' is-interactive' : '') + '" viewBox="0 0 ' + CHART.width + ' ' + height +
    '" role="img" aria-label="' + escapeHTML(label) + '">' +
    '<defs><clipPath id="' + clipId + '"><rect x="' + CHART.left + '" y="0" width="' + (CHART.right - CHART.left) + '" height="' + height + '"/></clipPath></defs>' +
    '<rect class="plot-bg" x="' + CHART.left + '" y="' + CHART.plotTop + '" width="' + (CHART.right - CHART.left) + '" height="' + (CHART.plotBottom - CHART.plotTop) + '"/>' +
    '<g clip-path="url(#' + clipId + ')">' +
    targetBand(view, scales) + grid.join("") + limit(250, "limit-high") + limit(70, "limit-low") +
    (overview ? '' : '<rect class="window-shade" x="' + shadeX.toFixed(1) + '" y="' + (CHART.plotTop - 6) + '" width="' + (scales.x(range.end) - shadeX).toFixed(1) + '" height="' + (CHART.plotBottom - CHART.plotTop + 6) + '"/>') +
    (!overview && (range.manual || interactive) && (range.autoStart !== range.start || range.autoEnd !== range.end)
      ? '<line class="auto-marker" x1="' + scales.x(range.autoStart).toFixed(1) + '" x2="' + scales.x(range.autoStart).toFixed(1) + '" y1="' + CHART.plotTop + '" y2="' + CHART.plotBottom + '"/>' +
        '<line class="auto-marker" x1="' + scales.x(range.autoEnd).toFixed(1) + '" x2="' + scales.x(range.autoEnd).toFixed(1) + '" y1="' + CHART.plotTop + '" y2="' + CHART.plotBottom + '"/>'
      : '') +
    eventLanes(chartEvents(treatments), scales, view, range) + curve +
    (overview
      ? (last ? '<circle class="handle-dot" data-tone="' + (last.tone || "none") + '" cx="' + scales.x(last.t).toFixed(1) + '" cy="' + scales.y(last.g).toFixed(1) + '" r="4.5"/>' : '')
      : handleSVG("start", range.start, points, scales, interactive, range.startGlucose) +
        handleSVG("end", range.end, points, scales, interactive, range.endGlucose)) +
    '</g>' + '<line class="axis" x1="' + CHART.left + '" x2="' + CHART.right + '" y1="' + CHART.plotBottom + '" y2="' + CHART.plotBottom + '"/>' + ticks.join("") + yLabels.join("") +
    '<text class="y-unit" x="2" y="' + (CHART.plotTop - 16) + '">mg/dL</text>' +
    '</svg>';
}

function chartFor(meal, entries) {
  const range = mealWindow(meal);
  if (!entries.some(point => {
    const t = new Date(point.at).getTime();
    return t >= range.start - 30 * MINUTE && t <= range.end + 30 * MINUTE;
  })) return '<p class="hint">Keine CGM-Kurve verfügbar.</p>';
  return '<div class="meal-chart"><div class="meal-chart-plot" data-meal-id="' + escapeHTML(meal.id) + '"></div>' +
    '<div class="meal-chart-foot"><span>' + escapeHTML(timeOnly(range.start) + '–' + timeOnly(range.end)) + ' Uhr' +
    (range.manual ? ' · <b>manuell</b>' : '') + '</span>' +
    '<button type="button" class="open-window-editor" data-meal-id="' + escapeHTML(meal.id) + '">Zeitraum anpassen</button></div></div>';
}

// Charts are drawn after insertion so their geometry matches the rendered card width.
function renderMealCharts() {
  for (const node of $$(".meal-chart-plot")) {
    const meal = state.dayData?.meals.find(item => item.id === node.dataset.mealId);
    if (!meal || !node.clientWidth) continue;
    CHART = chartLayout(node.clientWidth);
    node.innerHTML = windowChartSVG(mealWindow(meal), state.dayData.entries, state.dayData.treatments);
  }
}

// Overview above the entry form: today the last hours up to now, other days the whole day.
function overviewHours() {
  try { return Number(localStorage.getItem("overviewHours")) || 3; } catch { return 3; }
}

function renderDayOverview() {
  const node = $("#day-overview-chart");
  const data = state.dayData;
  if (!node || !data) return;
  const hours = overviewHours();
  const today = new Date().toLocaleDateString("sv-SE", { timeZone: state.settings?.timezone || "Europe/Berlin" });
  const isToday = state.day === today;
  $$("[data-overview-hours]").forEach(button => {
    button.setAttribute("aria-pressed", String(Number(button.dataset.overviewHours) === hours));
    button.hidden = !isToday;
  });
  const dayStart = new Date(state.day + "T00:00:00").getTime();
  const next = new Date(dayStart + 36 * 60 * MINUTE);
  const dayEnd = new Date(next.toLocaleDateString("sv-SE") + "T00:00:00").getTime();
  let view = { from: dayStart, to: dayEnd };
  if (isToday) {
    const to = Math.max(Date.now() + 10 * MINUTE, dayStart + 60 * MINUTE);
    view = { from: Math.max(dayStart, to - hours * 60 * MINUTE), to };
  }
  const clipped = isToday && hours < 24 && view.from === dayStart && view.to - view.from < hours * 60 * MINUTE;
  const points = glucosePoints(data.entries).filter(point => point.t >= view.from && point.t <= view.to);
  const last = points[points.length - 1];
  CHART = chartLayout(node.clientWidth);
  node.innerHTML = windowChartSVG({ start: -Infinity, end: Infinity, mealAt: dayStart }, data.entries, data.treatments,
    { overview: true, view });
  // The current value is in the header; the note only explains gaps.
  $("#day-overview-note").textContent = [
    last ? "" : "Keine CGM-Werte in diesem Zeitraum.",
    clipped ? "Werte vor Mitternacht stehen beim Vortag." : "",
  ].filter(Boolean).join(" ");
  $("#day-overview-recent").innerHTML = isToday ? recentEntriesHTML([...data.treatments,
    ...(data.basal || []).map(dose => ({ at: dose.at, basal: dose.units }))]) : "";
}

// The whole day for "Tag durchgehen"; drawn only while visible so its width is known.
function renderReviewChart() {
  const node = $("#review-chart");
  const data = state.dayData;
  if (!node || !data || !node.clientWidth) return;
  const dayStart = new Date(state.day + "T00:00:00").getTime();
  const next = new Date(dayStart + 36 * 60 * MINUTE);
  const view = { from: dayStart, to: new Date(next.toLocaleDateString("sv-SE") + "T00:00:00").getTime() };
  CHART = chartLayout(node.clientWidth);
  node.innerHTML = windowChartSVG({ start: -Infinity, end: Infinity, mealAt: dayStart }, data.entries, data.treatments,
    { overview: true, view, clipId: "review-clip" });
}

// Today opens "Jetzt", other days open "Tag durchgehen"; a tap on the switch overrides it for that day.
function applyDiaryMode() {
  if (state.modeDay !== state.day) {
    state.modeDay = state.day;
    state.diaryMode = state.day === todayDay() ? "now" : "review";
  }
  $("#diary-view").dataset.mode = state.diaryMode;
  $$("[data-diary-mode]").forEach(button =>
    button.setAttribute("aria-pressed", String(button.dataset.diaryMode === state.diaryMode)));
}

function renderVisibleCharts() {
  if (!state.dayData) return;
  renderDayOverview();
  renderReviewChart();
  renderMealCharts();
  renderEffects();
}

// "Wirkungen": measured glucose against the 3-h model, and how strongly carbs raise and insulin lowers.
async function loadEffects() {
  const day = state.day;
  try {
    const data = await api("/api/day/effects?date=" + encodeURIComponent(day));
    if (day !== state.day) return;
    state.effects = data;
  } catch (error) {
    state.effects = { day, rows: [], reason: error.message };
  }
  renderEffects();
}

function effectsSummaryText(data) {
  const summary = data.summary;
  if (!summary?.count) return data.reason || "Noch keine Werte für das Modell.";
  const parts = ["Modell im Mittel " + num(summary.mean_abs_mgdl, 0) + " mg/dL daneben"];
  if (summary.max_above) parts.push("BZ bis " + signedChange(summary.max_above.mgdl, 0) + " über Modell um " + timeOnly(summary.max_above.at));
  if (summary.max_below) parts.push("bis " + signedChange(summary.max_below.mgdl, 0) + " darunter um " + timeOnly(summary.max_below.at));
  return parts.join(" · ");
}

function effectsChartSVG(data, width) {
  const W = Math.max(300, Math.min(900, Math.round(width))), L = 36, R = W - 10;
  const top = 8, mid = 168, rateTop = 186, rateBottom = 290, H = 312;
  const from = new Date(data.from).getTime(), to = Math.max(new Date(data.to).getTime(), from + 60 * MINUTE);
  const rows = data.rows.map(row => ({ t: new Date(row.at).getTime(), g: row.glucose_mgdl == null ? null : Number(row.glucose_mgdl),
    m: row.model_mgdl == null ? null : Number(row.model_mgdl), up: Number(row.raising_per_hour), down: Number(row.lowering_per_hour) }));
  const values = rows.flatMap(row => [row.g, row.m]).filter(value => value != null);
  const gTop = Math.max(250, Math.ceil((Math.max(0, ...values) + 10) / 50) * 50);
  const gBottom = Math.min(50, Math.floor(Math.min(400, ...values) / 10) * 10);
  const rateMax = Math.max(60, ...rows.map(row => Math.max(row.up, row.down)));
  const x = t => L + (t - from) / (to - from) * (R - L);
  const y = g => mid - (Math.min(Math.max(g, gBottom), gTop) - gBottom) / (gTop - gBottom) * (mid - top);
  const zero = (rateTop + rateBottom) / 2;
  const r = v => v / rateMax * (rateBottom - rateTop) / 2;
  const line = key => {
    let d = "", open = false;
    for (const row of rows) {
      if (row[key] == null) { open = false; continue; }
      d += (open ? "L" : "M") + x(row.t).toFixed(1) + " " + y(row[key]).toFixed(1);
      open = true;
    }
    return d;
  };
  const area = (key, sign) => rows.length ? "M" + x(rows[0].t).toFixed(1) + " " + zero + rows.map(row =>
    "L" + x(row.t).toFixed(1) + " " + (zero - sign * r(row[key])).toFixed(1)).join("") +
    "L" + x(rows[rows.length - 1].t).toFixed(1) + " " + zero + "Z" : "";
  const grid = [];
  for (let g = Math.ceil(gBottom / 50) * 50; g <= gTop; g += 50) {
    grid.push('<line class="grid" x1="' + L + '" x2="' + R + '" y1="' + y(g).toFixed(1) + '" y2="' + y(g).toFixed(1) + '"/>' +
      '<text x="' + (L - 6) + '" y="' + (y(g) + 4).toFixed(1) + '" text-anchor="end">' + g + '</text>');
  }
  const ticks = [];
  for (let t = Math.ceil(from / (180 * MINUTE)) * 180 * MINUTE; t <= to; t += 180 * MINUTE) {
    ticks.push('<line class="grid" x1="' + x(t).toFixed(1) + '" x2="' + x(t).toFixed(1) + '" y1="' + top + '" y2="' + rateBottom + '"/>' +
      '<text x="' + x(t).toFixed(1) + '" y="' + (rateBottom + 16) + '" text-anchor="middle">' + escapeHTML(timeOnly(t)) + '</text>');
  }
  return '<svg class="window-chart-svg" viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="Gemessener BZ, Modell und Wirkungen">' +
    grid.join("") + ticks.join("") +
    '<path class="model" d="' + line("m") + '"/><path class="measured" d="' + line("g") + '"/>' +
    '<path class="raising" d="' + area("up", 1) + '"/><path class="lowering" d="' + area("down", -1) + '"/>' +
    '<line class="axis" x1="' + L + '" x2="' + R + '" y1="' + zero + '" y2="' + zero + '"/>' +
    '<text x="' + (L - 6) + '" y="' + (rateTop + 8) + '" text-anchor="end">+' + Math.round(rateMax) + '</text>' +
    '<text x="' + (L - 6) + '" y="' + rateBottom + '" text-anchor="end">−' + Math.round(rateMax) + '</text>' +
    '<text class="y-unit" x="' + L + '" y="' + (rateTop - 4) + '">mg/dL pro h</text></svg>';
}

function renderEffects() {
  const card = $("#effects-card");
  const data = state.effects;
  if (!card) return;
  card.hidden = !data || data.day !== state.day;
  if (card.hidden) return;
  $("#effects-summary").textContent = effectsSummaryText(data);
  const node = $("#effects-chart");
  if (!card.open || !node.clientWidth) return;
  node.innerHTML = data.rows.length ? effectsChartSVG(data, node.clientWidth) : '<p class="hint">Keine Daten für diesen Tag.</p>';
}

// Today's "Wirkt noch" card: what still lowers and raises glucose and the expected value after it.
function activeCardHTML(active) {
  const until = value => value ? "bis " + timeOnly(value) : "wirkt nicht mehr";
  const nothing = !active.lowering_until && !active.raising_until;
  const value = (sign, mgdl) => escapeHTML(sign + num(mgdl, 0)) + ' <small>mg/dL</small>';
  const item = (label, strong, lines, extra = "") => '<div class="active-item' + extra + '"><span>' + label + '</span><strong>' +
    strong + '</strong>' + lines.map(line => '<small>' + escapeHTML(line) + '</small>').join("") + '</div>';
  return item("Senkt noch", value("−", active.lowering_mgdl),
      ["Insulin " + num(active.insulin_units, 1) + " IE", until(active.lowering_until)]) +
    item("Hebt noch", value("+", active.raising_mgdl),
      ["KH " + num(active.carbs_remaining_g, 0) + " g +" + num(active.carbs_mgdl, 0),
       "Fett/Eiweiß +" + num(active.surcharge_mgdl, 0), until(active.raising_until)]) +
    item("Erwartet", active.expected_mgdl == null ? "—" : value("≈ ", active.expected_mgdl),
      active.expected_mgdl == null ? ["kein aktueller CGM-Wert"]
        : [(active.expected_at ? "um " + timeOnly(active.expected_at) : "jetzt"),
           "wahrscheinlich " + num(active.expected_low_mgdl, 0) + "–" + num(active.expected_high_mgdl, 0) + " mg/dL"],
      " expected") +
    (nothing ? '<p class="hint">Nichts wirkt mehr nach.</p>' : '') +
    (active.split_plan ? '<p class="hint">2. Teil geplant: ' + escapeHTML(num(active.split_plan.units, 1)) + ' IE um ' +
      escapeHTML(timeOnly(active.split_plan.due_at)) + ', in der Prognose noch nicht enthalten.</p>' : '') +
    (active.fresh_meal_at ? '<p class="hint">Mahlzeit um ' + escapeHTML(timeOnly(active.fresh_meal_at)) +
      ': Die KH steigen noch, die Prognose ist hier am unsichersten.</p>' : '');
}

function renderActiveCard() {
  const node = $("#day-overview-active");
  if (!node) return;
  const active = state.day === todayDay() ? state.preview?.on_board : null;
  node.hidden = !active;
  node.innerHTML = active ? activeCardHTML(active) : "";
}

// What happened in the last three hours, newest first, as context for the next decision.
function recentEntriesHTML(treatments) {
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
      return parts.length ? '<span class="recent-chip"><time>' + escapeHTML(timeOnly(item.at)) + '</time> ' + escapeHTML(parts.join(" + ")) + factorIconsHTML(item) + '</span>' : "";
    }).filter(Boolean);
  return items.length ? '<span class="recent-label">Letzte 3 h</span>' + items.join("")
    : '<span class="recent-label">Letzte 3 h: keine Einträge</span>';
}

// --- Interactive window editor -------------------------------------------

function windowBounds(handle) {
  const editor = windowEditor;
  const limits = windowLimits();
  if (handle === "start") return { min: Math.max(editor.mealAt - limits.lead, editor.view.from), max: editor.mealAt + limits.lag };
  return { min: minWindowEnd(editor.start), max: Math.min(editor.start + limits.max, editor.view.to) };
}

function minWindowEnd(start) {
  return Math.max(start, windowEditor.mealAt) + windowLimits().min;
}

// The meal's own entry (carbs and bolus) always counts, even when the start was moved after it.
function windowIncludes(range, event) {
  return (range.sourceId && event.id === range.sourceId) || (event.t >= range.start && event.t < range.end);
}

function snapTargets() {
  const editor = windowEditor;
  const targets = [{ t: editor.autoStart, label: "Standardbeginn" }, { t: editor.autoEnd, label: "Standardende" }];
  for (const event of editor.events) {
    if (event.kind === "insulin") targets.push({ t: event.t, label: num(event.amount, 1) + " IE" });
    if (event.kind === "carbs") targets.push({ t: event.t, label: num(event.amount, 0) + " g KH" });
    if (event.kind === "sport") {
      targets.push({ t: event.t, label: event.label + " Beginn" }, { t: event.end, label: event.label + " Ende" });
    }
  }
  return targets;
}

function placeHandle(handle, raw, options = {}) {
  const editor = windowEditor;
  const bounds = windowBounds(handle);
  const clamp = value => Math.min(bounds.max, Math.max(bounds.min, value));
  let t = clamp(raw);
  let snapped = null;
  if (options.snapTolerance) {
    for (const target of snapTargets()) {
      const distance = Math.abs(target.t - t);
      if (target.t >= bounds.min && target.t <= bounds.max && distance <= options.snapTolerance &&
          (!snapped || distance < Math.abs(snapped.t - t))) snapped = target;
    }
  }
  t = snapped ? snapped.t : clamp(Math.round(t / MINUTE) * MINUTE);
  if (snapped && editor.snapped[handle]?.t !== snapped.t) navigator.vibrate?.(8);
  editor.snapped[handle] = snapped;
  editor[handle] = t;
  if (handle === "start") editor.end = Math.min(Math.max(editor.end, minWindowEnd(editor.start)), editor.start + windowLimits().max);
}

// The CGM reading at a handle position, from the per-minute readings the server sent for the editor.
function editorReading(t) {
  const readings = windowEditor?.readings;
  // A handle clamped to a limit can sit between minutes; it reads the minute it is in.
  return readings?.get(t) || readings?.get(Math.floor(t / MINUTE) * MINUTE) || null;
}

const readingValue = reading => reading?.value != null ? Number(reading.value) : null;

// The server counts the candidate window (same rule as the factor); the editor asks after each move.
function scheduleWindowPreview() {
  const editor = windowEditor;
  clearTimeout(editor.previewTimer);
  editor.previewTimer = setTimeout(async () => {
    const key = editor.start + "-" + editor.end;
    try {
      const summary = await api("/api/analysis/window/preview", { method: "POST", body: JSON.stringify({
        meal_id: editor.mealId, day: editor.day, start: new Date(editor.start).toISOString(), end: new Date(editor.end).toISOString(),
      }) });
      if (windowEditor !== editor || key !== editor.start + "-" + editor.end) return;
      editor.summary = summary;
      $("#window-summary").innerHTML = editorSummary();
    } catch (error) {
      toast(error.message, true);
    }
  }, 120);
}

function editorSummary() {
  const editor = windowEditor;
  const summary = editor.summary;
  const g0 = readingValue(editorReading(editor.start));
  const g1 = readingValue(editorReading(editor.end));
  const minutes = Math.round((editor.end - editor.start) / MINUTE);
  const changed = editor.start !== editor.savedStart || editor.end !== editor.savedEnd;
  const isDefault = editor.start === editor.autoStart && editor.end === editor.autoEnd;
  const cell = (label, value, detail = "") => '<div><span>' + label + '</span><strong>' + value + '</strong>' + (detail ? '<small>' + detail + '</small>' : '') + '</div>';
  return '<div class="window-facts">' +
    cell("Beginn", escapeHTML(timeOnly(editor.start)), g0 == null ? "kein CGM-Wert" : escapeHTML(num(g0, 0)) + " mg/dL") +
    cell("Ende <small>(exkl.)</small>", escapeHTML(timeOnly(editor.end)), g1 == null ? "kein CGM-Wert" : escapeHTML(num(g1, 0)) + " mg/dL") +
    cell("ΔG", g0 == null || g1 == null ? "—" : escapeHTML(signedChange(g1 - g0, 0))) +
    cell("Dauer", escapeHTML(Math.floor(minutes / 60) + " h " + String(minutes % 60).padStart(2, "0") + " min")) +
    cell("KH im Fenster", summary ? escapeHTML(num(summary.carbs_g, 0)) + " g" : "…") +
    cell("Schnelles Insulin", summary ? escapeHTML(num(summary.rapid_units, 1)) + " IE" : "…") +
    '</div><p class="window-state">' +
    (isDefault ? "Automatischer Standardzeitraum" : "Manueller Zeitraum") +
    (changed ? " · <b>nicht gespeichert</b>" : "") +
    (g0 == null || g1 == null ? ' · <span class="window-warning">An Beginn und Ende wird ein CGM-Wert benötigt.</span>' : "") + '</p>';
}

function updateWindowReadout() {
  const editor = windowEditor;
  const t = editor[editor.active];
  const reading = editorReading(t);
  const value = readingValue(reading);
  const snapped = editor.snapped[editor.active];
  const readout = $("#window-readout");
  readout.dataset.tone = reading?.tone || "none";
  readout.innerHTML = '<span class="readout-label">' + (editor.active === "start" ? "Beginn" : "Ende") + ' · ' + escapeHTML(timeOnly(t)) + ' Uhr' +
    (snapped ? ' · eingerastet: ' + escapeHTML(snapped.label) : '') + '</span>' +
    '<span class="readout-value"><strong>' + (value == null ? "—" : escapeHTML(num(value, 0))) + '</strong>' +
    '<span class="readout-trend">' + escapeHTML(value == null ? "" : trendSymbol(reading.trend)) + '</span><small>mg/dL</small></span>';
}

function updateWindowEditor() {
  const editor = windowEditor;
  const svg = $("#window-chart svg");
  if (!svg) return;
  CHART = editor.layout;
  const scales = chartScales(editor.view, editor.points);
  for (const name of ["start", "end"]) {
    const group = $('.window-handle[data-handle="' + name + '"]', svg);
    const t = editor[name];
    const reading = editorReading(t);
    const value = readingValue(reading);
    group.setAttribute("transform", "translate(" + scales.x(t).toFixed(1) + " 0)");
    group.setAttribute("aria-valuetext", timeOnly(t) + " Uhr");
    group.classList.toggle("is-active", editor.active === name);
    group.classList.toggle("is-snapped", Boolean(editor.snapped[name]));
    const dot = $(".handle-dot", group);
    dot.toggleAttribute("hidden", value == null);
    dot.dataset.tone = reading?.tone || "none";
    if (value != null) dot.setAttribute("cy", scales.y(value).toFixed(1));
  }
  const shade = $(".window-shade", svg);
  shade.setAttribute("x", scales.x(editor.start).toFixed(1));
  shade.setAttribute("width", (scales.x(editor.end) - scales.x(editor.start)).toFixed(1));
  $$(".chart-event", svg).forEach(node => {
    const t = Number(node.dataset.t);
    node.classList.toggle("is-included", windowIncludes(editor, { t, id: node.dataset.id }));
  });
  $$("[data-window-handle]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.windowHandle === editor.active)));
  updateWindowReadout();
  $("#window-summary").innerHTML = editorSummary();
  const key = editor.start + "-" + editor.end;
  if (editor.previewKey !== key) {
    editor.previewKey = key;
    scheduleWindowPreview();
  }
}

function renderWindowEditor(focus) {
  const editor = windowEditor;
  editor.view = chartView(editor);
  CHART = editor.layout = chartLayout($("#window-chart").clientWidth - 12);
  $("#window-chart").innerHTML = windowChartSVG(editor, state.dayData.entries, state.dayData.treatments,
    { interactive: true, view: editor.view });
  updateWindowEditor();
  if (focus) $('#window-chart .window-handle[data-handle="' + focus + '"]')?.focus();
}

async function openWindowEditor(mealId) {
  const meal = state.dayData?.meals.find(item => item.id === mealId);
  if (!meal) return;
  const range = mealWindow(meal);
  let editorData;
  try {
    editorData = await api("/api/analysis/window-editor?" + new URLSearchParams({ day: state.day, meal_id: mealId }));
  } catch (error) {
    toast(error.message, true);
    return;
  }
  windowEditor = {
    ...range, mealId, day: state.day, active: "end", snapped: {}, drag: null, tap: null,
    savedStart: range.start, savedEnd: range.end, summary: null,
    readings: new Map(editorData.readings.map(row => [new Date(row.t).getTime(), row])),
    points: glucosePoints(state.dayData.entries), events: chartEvents(state.dayData.treatments),
  };
  $("#window-meal-type").textContent = names[meal.meal] + " · " + timeOnly(meal.at) + " Uhr · " + num(meal.carbs) + " g KH";
  $("#window-dialog").showModal();
  renderWindowEditor();
}

function pointerTime(event) {
  const svg = $("#window-chart svg");
  const rect = svg.getBoundingClientRect();
  CHART = windowEditor.layout;
  const unitsPerPixel = CHART.width / rect.width;
  const x = (event.clientX - rect.left) * unitsPerPixel;
  const { from, to } = windowEditor.view;
  const msPerUnit = (to - from) / (CHART.right - CHART.left);
  return { t: from + (x - CHART.left) * msPerUnit, msPerPixel: msPerUnit * unitsPerPixel };
}

function windowPointerDown(event) {
  if (!windowEditor || (event.pointerType === "mouse" && event.button !== 0)) return;
  const { t, msPerPixel } = pointerTime(event);
  let handle = event.target.closest(".window-handle")?.dataset.handle;
  if (!handle) {
    const distance = name => Math.abs(windowEditor[name] - t) / msPerPixel;
    const nearest = distance("start") <= distance("end") ? "start" : "end";
    if (distance(nearest) <= 28) handle = nearest;
  }
  if (!handle) {
    windowEditor.tap = { x: event.clientX, y: event.clientY };
    return;
  }
  event.preventDefault();
  windowEditor.active = handle;
  windowEditor.drag = { handle, pointerId: event.pointerId };
  event.currentTarget.setPointerCapture(event.pointerId);
  event.currentTarget.classList.add("is-dragging");
  updateWindowEditor();
}

function windowPointerMove(event) {
  const drag = windowEditor?.drag;
  if (!drag || drag.pointerId !== event.pointerId) return;
  event.preventDefault();
  const { t, msPerPixel } = pointerTime(event);
  placeHandle(drag.handle, t, { snapTolerance: SNAP_PIXELS * msPerPixel });
  updateWindowEditor();
}

function windowPointerUp(event) {
  if (!windowEditor) return;
  const target = event.currentTarget;
  if (windowEditor.drag) {
    windowEditor.drag = null;
    target.classList.remove("is-dragging");
    renderWindowEditor();
    return;
  }
  const tap = windowEditor.tap;
  windowEditor.tap = null;
  if (event.type !== "pointerup" || !tap || Math.hypot(event.clientX - tap.x, event.clientY - tap.y) > 8) return;
  // A tap moves the selected handle, which works well on small touch screens.
  const { t, msPerPixel } = pointerTime(event);
  placeHandle(windowEditor.active, t, { snapTolerance: SNAP_PIXELS * msPerPixel });
  renderWindowEditor();
}

function nudgeWindow(handle, minutes, focus) {
  placeHandle(handle, windowEditor[handle] + minutes * MINUTE);
  windowEditor.snapped[handle] = null;
  renderWindowEditor(focus ? handle : null);
}

function windowKeydown(event) {
  const handle = event.target.closest?.(".window-handle")?.dataset.handle;
  if (!handle || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
  event.preventDefault();
  windowEditor.active = handle;
  nudgeWindow(handle, (event.key === "ArrowLeft" ? -1 : 1) * (event.shiftKey ? 5 : 1), true);
}

async function saveWindow(reset = false) {
  const editor = windowEditor;
  if (!editor) return;
  const button = $("#save-window");
  const isDefault = editor.start === editor.autoStart && editor.end === editor.autoEnd;
  if (!reset && !isDefault && (readingValue(editorReading(editor.start)) == null || readingValue(editorReading(editor.end)) == null)) {
    toast("An Beginn und Ende wird ein CGM-Wert benötigt.", true);
    return;
  }
  button.disabled = true;
  try {
    const body = reset || isDefault
      ? { meal_id: editor.mealId, day: editor.day, reset: true }
      : { meal_id: editor.mealId, day: editor.day, start: new Date(editor.start).toISOString(), end: new Date(editor.end).toISOString() };
    await api("/api/analysis/window", { method: "POST", body: JSON.stringify(body) });
    $("#window-dialog").close();
    toast(body.reset ? "Standardzeitraum wiederhergestellt" : "Zeitraum gespeichert");
    await loadDay();
    if (state.view === "analysis") await loadSummary();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function calculationHTML(result) {
  if (!result || !result.start) return '<div class="factor unavailable"><span>Insulinfaktor</span><strong>—</strong><small>' + escapeHTML(result?.reason || "Noch keine Berechnung") + '</small></div>';
  const valid = result.factor_units_per_ke != null;
  const summary = [
    num(result.carbs_total) + " g KH",
    num(result.rapid_units_in_window, 2) + " IE",
    "Ende " + timeOnly(result.end) + (result.manual_window ? " (manuell)" : ""),
  ].join(" · ");
  return '<div class="calc-block"><div class="factor' + (valid ? '' : ' unavailable') + '"><span>Insulinfaktor</span><strong>' +
    (valid ? escapeHTML(factorNum(result.factor_units_per_ke)) + ' <small>IE/KE</small>' : '—') +
    '</strong><small>' + escapeHTML(summary) + '</small>' +
    (result.reason ? '<small>' + escapeHTML(result.reason) + '</small>' : '') + '</div></div>';
}

function icon(name) {
  const paths = {
    diary: '<path d="M4 4h14a2 2 0 0 1 2 2v14H6a2 2 0 0 1-2-2V4Z"/><path d="M8 4v16M11 9h6M11 13h6"/>',
    check: '<path d="m4 12 5 5L20 6"/>',
    exclude: '<circle cx="12" cy="12" r="9"/><path d="m8 8 8 8"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    remove: '<path d="M5 12h14"/>',
  };
  return '<svg class="ui-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + paths[name] + '</svg>';
}

function sportReviewHTML(sport, mealId, day) {
  const applied = sport.status === "affected";
  const effect = applied ? sportChange(sport.effect_mgdl) : 0;
  const suggestion = sport.suggested ? sportChange(sport.suggested_effect_mgdl) : 0;
  return '<div class="sport-review sport-review-meal" data-meal-id="' + escapeHTML(mealId) +
    '" data-day="' + escapeHTML(day) + '" data-suggestion="' + escapeHTML(suggestion) + '">' +
    '<div class="sport-state' + (applied ? ' is-applied' : '') + '">' +
    (applied ? icon("check") + '<span>Sport ' + escapeHTML(signedChange(effect, 0)) + '</span>'
      : sport.status === "unaffected" && suggestion ? icon("check") + '<span>Kein Sporteinfluss</span>'
      : sport.status === "auto" && suggestion ? '<span>Sport offen</span>' : '<span>Kein Sporteffekt</span>') +
    ((sport.events || []).length ? '<span class="sport-events">· ' + escapeHTML(sport.events.map(item =>
      (sportNames[item.kind] || "Sport") + " " + timeOnly(item.at)).join(", ")) + '</span>' : '') + '</div>' +
    (applied ? '<button type="button" class="sport-remove icon-button" aria-label="Sporteinfluss entfernen" title="Sporteinfluss entfernen">' + icon("remove") + '</button>' : '') +
    (!applied && suggestion ? '<button type="button" class="sport-suggestion">' + icon("plus") + ' Vorschlag ' + escapeHTML(signedChange(suggestion, 0)) + ' übernehmen</button>' : '') +
    (sport.status === "auto" && suggestion ? '<button type="button" class="sport-none quiet">Kein Einfluss</button>' : '') +
    '<details class="sport-custom"><summary>' + (applied ? 'Wert ändern' : 'Eigenen Wert setzen') + '</summary>' +
    '<div class="sport-custom-fields"><label><span>BZ-Wirkung in mg/dL (− senkt, + hebt)</span><input class="sport-effect" type="number" min="-500" max="500" step="1" value="' +
    escapeHTML(applied ? effect : (suggestion || "")) +
    '"></label><button type="button" class="sport-save">' + (applied ? 'Ändern' : 'Setzen') + '</button></div></details></div>';
}

function renderNight(night) {
  const card = $("#night-card");
  card.hidden = false;
  const startTime = night.start ? timeOnly(night.start) : null;
  const endTime = night.end ? timeOnly(night.end) : null;
  $("#night-window").textContent = startTime && endTime
    ? startTime + " bis " + endTime + " Uhr"
    : "Drei Stunden nach letzter Injektion bis Beginn Frühstück";
  const metric = (label, value, detail) =>
    '<div class="night-metric"><span>' + label + '</span><strong>' +
    (value == null ? '—' : escapeHTML(num(value, 1)) + ' <small>mg/dL</small>') +
    '<small>' + escapeHTML(detail) + '</small></div>';
  $("#night-values").innerHTML =
    metric("Startwert", night.start_glucose, startTime ? "3 h nach letzter Injektion · " + startTime : "3 h nach letzter Injektion") +
    metric("Endwert", night.end_glucose, endTime ? "Beginn Frühstück · " + endTime : "Beginn Frühstück") +
    '<div class="night-metric"><span>Veränderung</span><strong>' + escapeHTML(signedChange(night.delta_mgdl)) + '</strong></div>' +
    metric("Minimalwert", night.minimum_glucose, night.minimum_at ? "um " + timeOnly(night.minimum_at) : "im Nachtfenster");
  $("#night-values").hidden = [night.start_glucose, night.end_glucose, night.minimum_glucose].every(value => value == null);
  const reason = $("#night-reason");
  reason.hidden = !night.reason;
  reason.textContent = night.reason || "";
}

function renderDay(data) {
  state.dayData = data;
  updateEntryChips();
  renderNight(data.night || { reason: "Nachtanalyse nicht verfügbar." });
  const banner = $("#banner");
  const errors = [data.sync_error, data.profile_sync_error].filter(Boolean);
  banner.hidden = data.connected && !errors.length;
  banner.textContent = !data.connected
    ? "Nightscout ist noch nicht verbunden. Setze NIGHTSCOUT_URL und NIGHTSCOUT_CREDENTIAL im Deployment."
    : errors.length ? "Nightscout konnte nicht vollständig aktualisiert werden: " + errors.join(" · ") + ". Lokale Daten werden angezeigt." : "";
  applyDiaryMode();
  $("#timeline").innerHTML = dayListHTML(data);
  renderReviewDayType(data);
  renderReviewSummary(data);
  renderVisibleCharts();
}

const entryLabels = { finger: "BZ blutig", basal: "Basal", meal: "Mahlzeit", snack: "Snack", carbs: "Kohlenhydrate", correction: "Korrektur / Bolus", exercise: "Sport", other: "Weiterer Eintrag" };

function entryLabel(item, data) {
  if (item.kind === "exercise") return sportNames[item.sport_kind] || "Sport";
  const meal = item.kind === "meal" ? data.meals.find(row => row.source_id === item.id) : null;
  return meal ? names[meal.meal] : entryLabels[item.kind];
}

function entryRowHTML(item, data) {
  const details = [
    Number(item.carbs) ? num(item.carbs) + " g KH" : "",
    Number(item.insulin) ? num(item.insulin) + " IE" : "",
    item.kind === "basal" ? num(item.basal_units) + " IE" + (item.nightscout_synced ? "" : " · nur lokal, noch nicht in Nightscout") : "",
    item.kind === "exercise" ? num(item.duration_minutes, 0) + " min" : "",
    item.glucose_mgdl != null
      ? (item.glucose_at === item.at ? "BZ " : "BZ um " + timeOnly(item.glucose_at) + ": ") + num(item.glucose_mgdl, 0) + " mg/dL" : "",
  ].filter(Boolean).join(" · ");
  const label = entryLabel(item, data);
  const action = item.kind === "other" ? "" : item.kind === "finger"
    ? '<button type="button" class="delete-treatment quiet" data-id="' + escapeHTML(item.id) + '" data-label="BZ blutig" data-at="' +
      escapeHTML(item.at) + '" aria-label="' + escapeHTML("Blutige Messung um " + timeOnly(item.at) + " löschen") + '">Löschen</button>' : item.kind === "basal"
    ? '<button type="button" class="delete-basal quiet" data-id="' + escapeHTML(item.id) + '" data-at="' + escapeHTML(item.at) +
      '" aria-label="' + escapeHTML("Basal um " + timeOnly(item.at) + " löschen") + '">Löschen</button>' :
    '<button type="button" class="edit-entry quiet" data-id="' + escapeHTML(item.id) + '" aria-label="' +
    escapeHTML(label + " um " + timeOnly(item.at) + " bearbeiten") + '">Bearbeiten</button>';
  return '<article class="timeline-item"><time>' + escapeHTML(timeOnly(item.at)) +
    '</time><div><strong>' + escapeHTML(label) + factorIconsHTML(item) + placeBadgeHTML(item.place) + '</strong><p>' + escapeHTML(details || item.eventType) +
    '</p>' + (item.notes ? '<small>' + escapeHTML(item.notes) + '</small>' : '') +
    '</div><div class="timeline-actions">' + action + '</div></article>';
}

// One chronological list: each main meal as a block holding the entries the server grouped under it.
function dayBlocks(data) {
  const blocks = new Map(data.meals.map(meal => [meal.id, { meal, range: mealWindow(meal), items: [] }]));
  const rows = [];
  for (const item of data.timeline) {
    const block = blocks.get(item.meal_id);
    if (block) block.items.push(item);
    else rows.push({ t: new Date(item.at).getTime(), item });
  }
  return [...[...blocks.values()].map(block => ({ t: block.range.mealAt, block })), ...rows].sort((a, b) => a.t - b.t);
}

function mealNeedsCheck(meal) {
  return (meal.review?.open || []).length > 0;
}

function excludeButtonHTML(mealId, day, excluded) {
  const label = excluded ? 'Mahlzeit wieder berücksichtigen' : 'Mahlzeit ausklammern';
  return '<button type="button" class="exclude-meal' + (excluded ? ' is-excluded' : '') +
    '" data-meal-id="' + escapeHTML(mealId) + '" data-day="' + escapeHTML(day) +
    '" data-excluded="' + excluded + '" aria-pressed="' + excluded + '" aria-label="' + label + '" title="' + label + '">' +
    icon(excluded ? "exclude" : "check") + '<span>' + (excluded ? 'Ausgeklammert' : 'Berücksichtigt') + '</span></button>';
}

function mealBlockHTML(block, data) {
  const { meal, range, items } = block;
  const calc = meal.calculation || {};
  const check = mealNeedsCheck(meal);
  const calculated = Boolean(meal.review?.calculated);
  const status = !calculated
    ? '<span class="day-meal-status">' + escapeHTML(calc.reason || "Noch keine Berechnung") + '</span>'
    : '<span class="day-meal-factor">' + escapeHTML(factorNum(calc.factor_units_per_ke)) + ' <small>IE/KE</small></span>';
  return '<article class="day-meal' + (check ? ' needs-check' : '') + (meal.excluded ? ' is-excluded' : '') + '" id="meal-' + escapeHTML(meal.id) + '">' +
    '<div class="day-meal-head"><div><span class="meal-type">' + escapeHTML(names[meal.meal]) + '</span>' + factorIconsHTML(meal.quality) + placeBadgeHTML(meal.place) +
    '<span class="day-meal-window">' + escapeHTML(timeOnly(range.start) + "–" + timeOnly(range.end)) + (range.manual ? " · manuell" : "") +
    '</span>' + (meal.quality?.carbs_certainty ? '<span class="outlier-badge certainty-' + meal.quality.carbs_certainty + '">' + CERTAINTY_LABELS[meal.quality.carbs_certainty] + '</span>' : '') +
    '</div><div class="day-meal-result">' + status +
    (meal.review?.excludable ? excludeButtonHTML(meal.id, state.day, meal.excluded) : '') + '</div></div>' +
    items.map(item => entryRowHTML(item, data)).join("") +
    // Sport is adjusted right on the meal, without opening the curve details.
    sportReviewHTML(meal.sport || {}, meal.id, state.day) +
    '<details class="day-meal-details" data-meal-id="' + escapeHTML(meal.id) + '"' + (calculated ? '' : ' open') +
    '><summary>Kurve und Zeitraum</summary>' +
    calculationHTML(meal.calculation) + chartFor(meal, data.entries) +
    (meal.notes ? '<p class="meal-note">' + escapeHTML(meal.notes) + '</p>' : '') + '</details></article>';
}

function dayListHTML(data) {
  if (!data.timeline.length) return '<div class="empty">Für diesen Tag liegen noch keine Einträge vor.</div>';
  return dayBlocks(data).map(entry => entry.block ? mealBlockHTML(entry.block, data) : entryRowHTML(entry.item, data)).join("");
}

function renderReviewSummary(data) {
  $("#record-count").textContent = data.timeline.length + (data.timeline.length === 1 ? " Eintrag" : " Einträge");
  const review = data.review || {};
  const link = text => '<a class="review-chip needs-check" href="#meal-' + escapeHTML(review.first_open) + '">' + text + '</a>';
  const chips = [
    review.blocked ? link(review.blocked === 1 ? "1 Mahlzeit nicht berechenbar" : review.blocked + " Mahlzeiten nicht berechenbar") : "",
    review.sport_open ? link(review.sport_open === 1 ? "1 Sportvorschlag offen" : review.sport_open + " Sportvorschläge offen") : "",
    review.calculated ? '<span class="review-chip">' + (review.calculated === 1 ? "1 Faktor zählt" : review.calculated + " Faktoren zählen") + '</span>' : "",
    review.excluded ? '<span class="review-chip">' + review.excluded + ' ausgeklammert</span>' : "",
    data.meals.length && !review.blocked && !review.sport_open ? '<span class="review-chip is-clean">' + icon("check") + ' Tag geprüft</span>' : "",
  ].filter(Boolean);
  $("#review-summary").innerHTML = chips.join("");
}

async function loadDay() {
  $("#selected-day").value = state.day;
  $("#timeline").innerHTML = '<div class="empty">Nightscout-Daten werden geladen …</div>';
  $("#night-card").hidden = true;
  try {
    renderDay(await api("/api/day?date=" + encodeURIComponent(state.day)));
    loadEffects();
    await updatePreview();
  } catch (error) {
    $("#timeline").innerHTML = '<div class="empty">Daten konnten nicht geladen werden.</div>';
    toast(error.message, true);
  }
}

let previewTimer;
let previewRequestId = 0;
let doseHelp = null;

function todayDay() {
  return new Date().toLocaleDateString("sv-SE", { timeZone: state.settings?.timezone || "Europe/Berlin" });
}

// The entry form shows what the server derives from the typed values: snack assignment, sport,
// basal chip, day type and the dose suggestion. A preview for another day or mode is never shown.
function previewRequest() {
  const form = $("#entry-form");
  setDefaultMealTime({ preview: false });
  let at = null;
  try { at = toInstant(state.day, form.elements.at.value); } catch (_) { /* The time field is incomplete. */ }
  return {
    day: state.day, at, mode: form.elements.kind.value, carbs: form.elements.carbs.value, as: state.entryAs || null,
    finger_mgdl: form.elements.finger_mgdl.disabled ? null : form.elements.finger_mgdl.value || null,
    split: state.split ?? null,
    consider_sport: form.elements.consider_sport.checked, sport_effect_mgdl: form.elements.sport_effect_mgdl.value || "0",
    // Saving re-checks the shown options with these, so the preview must send the same marks.
    carbs_certainty: Number(form.elements.carbs.value) > 0 ? certaintyValue(form) : null,
    fat_protein: Number(form.elements.carbs.value) > 0 && form.elements.fat_protein.checked,
    alcohol: Number(form.elements.carbs.value) > 0 && form.elements.alcohol.checked,
    // A position only rides along while the entry is "now"; the server checks that again.
    position: !state.entryTimeTouched && state.entryPosition ? state.entryPosition : null,
    place_id: entryPlaceId(),
  };
}

function currentPreview() {
  const preview = state.preview;
  return preview && preview.day === state.day && preview.mode === $("#entry-form").elements.kind.value ? preview : null;
}

// Blood glucose measured by hand: the field opens when the server offers it (no fresh CGM value),
// when the chip was tapped or a value is typed; otherwise a chip offers it in meal mode.
function updateEntryFinger() {
  const form = $("#entry-form");
  const meal = form.elements.kind.value === "meal" && state.day === todayDay();
  const open = meal && (Boolean(state.entryFingerOpen) || Boolean(form.elements.finger_mgdl.value) ||
    Boolean(currentPreview()?.dose?.finger_offer));
  $("#entry-finger-field").hidden = !open;
  form.elements.finger_mgdl.disabled = !meal;
  $("#entry-finger-chip").hidden = !meal || open;
  $("#entry-finger-chip").setAttribute("aria-expanded", String(open));
}

function renderPreview() {
  updateEntryFinger();
  renderActiveCard();
  updateEntrySportOption();
  updateEntryDayType();
  updateEntryBasalOption();
  const output = $("#dose-result");
  const dose = currentPreview()?.dose;
  doseHelp = dose?.options ? dose : null;
  output.removeAttribute("aria-busy");
  if (doseHelp) output.innerHTML = doseHelpHTML(doseHelp);
  else output.textContent = dose?.message || "";
  applyDoseChoice();
}

function schedulePreview(delay = 350) {
  clearTimeout(previewTimer);
  previewRequestId++;
  if ($("#entry-form").elements.kind.value === "meal") $("#dose-result").setAttribute("aria-busy", "true");
  previewTimer = setTimeout(updatePreview, delay);
}

async function updatePreview() {
  clearTimeout(previewTimer);
  const requestId = ++previewRequestId;
  const output = $("#dose-result");
  const body = previewRequest();
  if (body.mode === "meal" && !output.innerHTML && body.day === todayDay()) output.textContent = "Empfehlung wird berechnet …";
  try {
    const data = await api("/api/entry/preview", { method: "POST", body: JSON.stringify(body) });
    if (requestId !== previewRequestId) return null;
    state.preview = { ...data, mode: body.mode };
    renderPreview();
    return state.preview;
  } catch (error) {
    if (requestId === previewRequestId) {
      state.preview = null;
      renderPreview();
      if (body.mode === "meal") output.textContent = error.message;
    }
    return null;
  }
}

function doseUnits(data, variant) {
  return data?.options?.find(option => option.variant === variant)?.units ?? null;
}

// A chosen suggestion fills the insulin field and follows later recalculations until the field is edited.
function applyDoseChoice() {
  const form = $("#entry-form");
  const option = doseHelp?.options.find(item => item.variant === state.doseChoice);
  if (state.doseChoice && doseHelp && !option?.savable) state.doseChoice = null;
  if (state.doseChoice && option) form.elements.insulin.value = String(Number(option.units));
  $$(".apply-dose").forEach(button =>
    button.setAttribute("aria-pressed", String(button.dataset.variant === state.doseChoice)));
  updateEntrySubmitLabel();
}

// A tapped suggestion saves carbs and that dose directly in Nightscout; the toast offers undo.
function selectDoseSuggestion(event) {
  const button = event.target.closest(".apply-dose");
  if (!button || button.disabled || !doseHelp) return;
  const form = $("#entry-form");
  if ($("#entry-submit").disabled) return;
  state.doseChoice = button.dataset.variant;
  form.elements.carbs.setCustomValidity("");
  applyDoseChoice();
  if (form.elements.insulin.value === "") return;
  form.requestSubmit($("#entry-submit"));
}

const doseLabels = { split: "2. Teil", normal: "Normal", low: "Tiefer", cautious: "Vorsichtig", carbs: "Nur KH" };

function doseHelpHTML(data) {
  const withCarbs = Number(data.carbs_g) > 0;
  const options = data.options.map(option => {
    const label = doseLabels[option.variant];
    return '<button type="button" class="dose-option apply-dose" data-variant="' + option.variant + '" aria-pressed="false"' +
    ' title="' + escapeHTML(label + ": " + num(option.units, 2) + " IE direkt in Nightscout speichern") + '"' +
    (option.savable ? '' : ' disabled') + '><span class="dose-label">' + escapeHTML(label) + '</span><strong>' +
    escapeHTML(num(option.units, 2)) + ' <small>IE</small></strong><span class="dose-expected">' +
    (option.expected_mgdl != null ? '→ ' + escapeHTML(num(option.expected_mgdl, 0)) + ' mg/dL' : 'ohne Korrektur') + '</span>' +
    (option.later_units != null ? '<span class="dose-expected">+ ' + escapeHTML(num(option.later_units, 1)) + ' IE um ' +
      escapeHTML(timeOnly(option.later_at)) + '</span>' : '') +
    (option.due_at ? '<span class="dose-expected">geplant ' + escapeHTML(timeOnly(option.due_at)) + '</span>' : '') + '</button>';
  }).join("");
  return '<div class="dose-head"><span class="dose-heading">' + (withCarbs ? "Empfehlung" : "Korrektur ohne Essen") +
    '</span>' + (options ? '<span class="dose-hint">Antippen speichert direkt</span>' : '') + '</div>' +
    factorLine(data) +
    (data.glucose_source === "finger" && data.glucose_fresh ? '<p class="dose-factor">Startwert blutig <strong>' +
      escapeHTML(num(data.glucose_mgdl, 0)) + ' mg/dL</strong> um ' + escapeHTML(timeOnly(data.glucose_at)) + '</p>' : '') +
    (options ? '<div class="dose-options">' + options + '</div>' : '') +
    (Number(data.activity_change_mgdl) !== 0 && data.activity_units != null ? '<p class="dose-factor">Sport <strong>' +
      escapeHTML(signedChange(data.activity_change_mgdl, 0)) + '</strong> · ' + escapeHTML((Number(data.activity_units) > 0 ? "+" : "") + num(data.activity_units, 2)) + ' IE</p>' : '') +
    (data.on_board && (data.on_board.lowering_until || data.on_board.raising_until)
      ? '<p class="dose-factor">Wirkt noch <strong>−' + escapeHTML(num(data.on_board.lowering_mgdl, 0)) + ' / +' +
        escapeHTML(num(data.on_board.raising_mgdl, 0)) + ' mg/dL</strong> · eingerechnet</p>' : '') +
    (data.low_note ? '<p class="dose-factor">Tiefer (' + escapeHTML(num(data.low_target_mgdl, 0)) + ' mg/dL) nicht angeboten: ' +
      escapeHTML(data.low_note) + '</p>' : '') +
    (data.reason ? '<p class="hint">' + escapeHTML(data.reason) + '</p>' : '');
}

const mealNames = { breakfast: "Frühstück", lunch: "Mittagessen", dinner: "Abendessen" };

// The factor all suggestions use; a stand-in is named while Automatik lacks meals.
function factorLine(data) {
  if (data.units_per_ke == null) return "";
  const kf = 'KF ' + escapeHTML(num(data.isf_mgdl_per_unit, 1)) + ' mg/dL/IE';
  if (!(Number(data.carbs_g) > 0)) return '<p class="dose-factor">' + kf + '</p>';
  const factor = value => '<strong>' + escapeHTML(factorNum(value)) + ' IE/KE</strong>';
  const count = ' (' + (data.auto_factor_count || 0) + '/' + data.auto_factor_min_meals + ')';
  const surcharge = data.auto_surcharge_units != null
    ? ' + Zuschlag <strong>' + escapeHTML(num(data.auto_surcharge_units, 2)) + ' IE</strong>'
    : data.auto_method === "surcharge" && data.auto_units_per_ke != null ? ' · Snack ohne Zuschlag' : '';
  const used = data.auto_units_per_ke != null
    ? 'Berechneter Faktor ' + factor(data.auto_units_per_ke) + count + surcharge
    : 'Berechneter Faktor –' + count + ' · ersatzweise ' + (data.factor_source === "breakfast_day_type"
      ? 'Frühstück ' + (data.day_type === "free" ? "frei" : "Arbeit") : 'Nightscout') + ' ' + factor(data.units_per_ke);
  return '<p class="dose-factor">' + used + ' · ' + kf + '</p>';
}

function closeEditDialog() {
  state.editing = null;
  $("#edit-dialog").close();
}

function resetEntryForm() {
  state.pendingEventId = null;
  state.pendingBasalId = null;
  const form = $("#entry-form");
  form.reset();
  setCertainty(form);
  form.elements.carbs.setCustomValidity("");
  state.entryTimeTouched = false;
  state.entryTimeOpen = false;
  state.entryNoteOpen = false;
  state.entryFingerOpen = false;
  state.doseChoice = null;
  state.entryAs = null;
  state.split = null;
  state.entryPlace = undefined;
  setDefaultMealTime();
  updateEntryFields();
  if ($("#edit-dialog").open) closeEditDialog();
}

function editEntry(id) {
  const item = state.dayData?.timeline.find(row => row.id === id);
  if (!item || item.kind === "other") return;
  state.editing = item;
  const form = $("#edit-form");
  form.reset();
  form.elements.at.value = timeOnly(item.at);
  form.elements.carbs.value = Number(item.carbs) || "";
  form.elements.insulin.value = Number(item.insulin) || "";
  setPickerBase(form.elements.carbs, 1);
  setPickerBase(form.elements.insulin, 0.5);
  form.elements.sport_kind.value = item.sport_kind || "jogging";
  form.elements.duration_minutes.value = Number(item.duration_minutes) || "";
  form.elements.notes.value = item.notes || "";
  setCertainty(form, item.carbs_certainty);
  form.elements.fat_protein.checked = Boolean(item.fat_protein);
  form.elements.alcohol.checked = Boolean(item.alcohol);
  const placeSelect = form.elements.place_id;
  placeSelect.innerHTML = '<option value="">Kein Ort</option>' + (state.dayData.place_options || []).map(option =>
    '<option value="' + option.id + '">' + escapeHTML(option.name) + '</option>').join("");
  placeSelect.value = item.place ? String(item.place.id) : "";
  const sport = item.kind === "exercise";
  $("#edit-quality").hidden = sport || !(Number(item.carbs) > 0);
  updateEditPlace();
  $("#edit-meal-fields").hidden = sport;
  $("#edit-sport-field").hidden = !sport;
  $("#edit-duration-field").hidden = !sport;
  form.elements.carbs.disabled = sport;
  form.elements.insulin.disabled = sport;
  form.elements.sport_kind.disabled = !sport;
  form.elements.duration_minutes.disabled = !sport;
  form.elements.duration_minutes.required = sport;
  const remove = $("#delete-edit");
  remove.dataset.id = item.id;
  remove.dataset.label = entryLabel(item, state.dayData);
  remove.dataset.at = item.at;
  $("#edit-date").textContent = new Date(state.day + "T12:00:00").toLocaleDateString("de-DE", {day: "numeric", month: "long", year: "numeric"});
  $("#edit-dialog").showModal();
  // On touch screens a focused field would pop up the keyboard or picker before anything was tapped.
  if (matchMedia("(pointer: fine)").matches) form.elements.at.focus();
}

// Places can be set afterwards, e.g. for meals eaten before places existed.
function updateEditPlace() {
  const form = $("#edit-form");
  const hidden = state.editing?.kind === "exercise" || !(Number(form.elements.carbs.value) > 0)
    || !(state.dayData?.place_options || []).length;
  $("#edit-place-field").hidden = hidden;
  form.elements.place_id.disabled = hidden;
}

async function saveEditedEntry(event) {
  event.preventDefault();
  const item = state.editing;
  if (!item) return;
  const form = event.currentTarget;
  const sport = item.kind === "exercise";
  const carbs = sport ? 0 : Number(form.elements.carbs.value || 0);
  const insulin = sport ? 0 : Number(form.elements.insulin.value || 0);
  if (!sport && carbs <= 0 && insulin <= 0) {
    form.elements.carbs.setCustomValidity("Bitte Kohlenhydrate, Insulin oder beides eintragen.");
    form.elements.carbs.reportValidity();
    return;
  }
  const kind = sport ? "exercise" : carbs > 0
    ? (item.kind === "snack" ? "snack" : item.kind === "carbs" && insulin === 0 ? "carbs" : "meal")
    : "correction";
  let at;
  try { at = toInstant(state.day, form.elements.at.value); }
  catch (error) { toast(error.message, true); return; }
  const payload = {
    id: item.id, original_day: state.day, kind, at, carbs, insulin,
    sport_kind: form.elements.sport_kind.value,
    duration_minutes: form.elements.duration_minutes.value || 0,
    notes: form.elements.notes.value,
    carbs_certainty: carbs > 0 ? certaintyValue(form) : null,
    fat_protein: carbs > 0 && form.elements.fat_protein.checked,
    alcohol: carbs > 0 && form.elements.alcohol.checked,
    place_id: form.elements.place_id.disabled ? undefined : form.elements.place_id.value || null,
  };
  const button = $('button[type="submit"]', form);
  button.disabled = true;
  try {
    const result = await api("/api/treatment", { method: "POST", body: JSON.stringify(payload) });
    const refreshed = await api("/api/day?date=" + encodeURIComponent(result.day));
    const saved = refreshed.timeline.find(row => row.id === item.id);
    if (!saved || Number(saved.carbs) !== carbs || Number(saved.insulin) !== insulin)
      throw new Error("Die Änderung ist in Nightscout noch nicht sichtbar. Bitte Eintrag aktualisieren.");
    state.day = result.day;
    $("#selected-day").value = state.day;
    updateLocation(true);
    renderDay(refreshed);
    await updatePreview();
    closeEditDialog();
    toast("Änderung in Nightscout gespeichert");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function saveBasal(at, units, notes = "") {
  const event_id = state.pendingBasalId || crypto.randomUUID();
  state.pendingBasalId = event_id;
  const result = await api("/api/basal", { method: "POST", body: JSON.stringify({ at, units, event_id, notes }) });
  state.pendingBasalId = null;
  return result;
}

async function deleteBasal(id) {
  await api("/api/basal/delete", { method: "POST", body: JSON.stringify({ id }) });
}

async function saveBasalEntry(form) {
  const units = Number(form.elements.basal_units.value);
  if (!(units > 0 && units <= 100 && Number.isInteger(units * 2))) {
    form.elements.basal_units.setCustomValidity("Bitte Basal zwischen 0,5 und 100 IE in 0,5er-Schritten eintragen.");
    form.elements.basal_units.reportValidity();
    return;
  }
  let at;
  try { at = toInstant(state.day, form.elements.at.value); }
  catch (error) { toast(error.message, true); return; }
  const button = $("#entry-submit");
  button.disabled = true;
  try {
    const result = await saveBasal(at, units, form.elements.notes.value);
    state.day = result.day;
    updateLocation(true);
    resetEntryForm();
    await loadDay();
    toast("Basal " + num(units, 1) + " IE " + (result.nightscout_synced ? "gespeichert" : "lokal gespeichert, Nightscout folgt"),
      !result.nightscout_synced && Boolean(state.dayData?.connected),
      { label: "Rückgängig", run: () => undoBasal(result.id, result.day) });
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function undoBasal(id, day) {
  try {
    await deleteBasal(id);
    if (state.day === day) await loadDay();
    toast("Basal entfernt");
  } catch (error) {
    toast(error.message, true);
  }
}

async function deleteBasalRow(button) {
  if (!window.confirm("Basal um " + timeOnly(button.dataset.at) + " Uhr wirklich löschen?")) return;
  button.disabled = true;
  try {
    await deleteBasal(button.dataset.id);
    toast("Basal gelöscht");
    await loadDay();
  } catch (error) {
    button.disabled = false;
    toast(error.message, true);
  }
}

// One call saves the entry, the basal riding along and the confirmed sport; the server decides
// meal or snack and re-checks a tapped suggestion against the current one.
async function saveEntry(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const mode = form.elements.kind.value;
  setDefaultMealTime({ preview: false });
  if (mode === "basal") return saveBasalEntry(form);
  const carbs = mode === "meal" ? Number(form.elements.carbs.value || 0) : 0;
  const insulin = mode === "meal" ? Number(form.elements.insulin.value || 0) : 0;
  const finger = mode === "meal" && !form.elements.finger_mgdl.disabled ? Number(form.elements.finger_mgdl.value || 0) : 0;
  if (mode === "meal" && carbs <= 0 && insulin <= 0 && !finger) {
    form.elements.carbs.setCustomValidity("Bitte Kohlenhydrate, Insulin oder beides eintragen.");
    form.elements.carbs.reportValidity();
    return;
  }
  let at;
  try { at = toInstant(state.day, form.elements.at.value); }
  catch (error) { toast(error.message, true); return; }
  const basalUnits = entryBasalUnits();
  const considerSport = mode === "meal" && form.elements.consider_sport.checked && !$("#entry-sport-option").hidden;
  const chosen = mode === "meal" && state.doseChoice && doseHelp ? doseUnits(doseHelp, state.doseChoice) : null;
  const submit = $('button[type="submit"]', form);
  submit.disabled = true;
  try { await freshEntryPosition(); } finally { submit.disabled = false; }
  const payload = {
    ...previewRequest(), at,
    carbs: form.elements.carbs.value, insulin: form.elements.insulin.value,
    consider_sport: considerSport, with_basal: basalUnits > 0,
    sport_kind: form.elements.sport_kind.value,
    duration_minutes: form.elements.duration_minutes.value || 0,
    notes: form.elements.notes.value,
    carbs_certainty: carbs > 0 ? certaintyValue(form) : null,
    fat_protein: carbs > 0 && form.elements.fat_protein.checked,
    alcohol: carbs > 0 && form.elements.alcohol.checked,
    event_id: state.pendingEventId || crypto.randomUUID(),
    basal_event_id: basalUnits > 0 ? state.pendingBasalId || crypto.randomUUID() : null,
    dose_check: chosen != null && Number(chosen) === insulin ? doseHelp.check : null,
    dose_variant: state.doseChoice || null,
  };
  state.pendingEventId = payload.event_id;
  state.pendingBasalId = payload.basal_event_id;
  const button = $('button[type="submit"]', form);
  button.disabled = true;
  try {
    let result;
    try {
      result = await api("/api/entry", { method: "POST", body: JSON.stringify(payload) });
    } catch (error) {
      // A changed suggestion is shown again for checking.
      if (payload.dose_check) await updatePreview();
      throw error;
    }
    state.day = result.day;
    updateLocation(true);
    $("#selected-day").value = state.day;
    renderDay(result.day_view);
    const restore = { day: result.day, time: form.elements.at.value, carbs: form.elements.carbs.value,
      insulin: form.elements.insulin.value, notes: form.elements.notes.value };
    resetEntryForm();
    const basal = result.basal;
    const message = (result.kind === "exercise" ? "Sport" : [result.finger_mgdl != null ? "BZ " + num(result.finger_mgdl, 0) + " mg/dL" : "",
      carbs > 0 ? num(carbs, 0) + " g" : "", insulin > 0 ? num(insulin, 2) + " IE" : ""]
      .filter(Boolean).join(" + ")) + (basal ? " + Basal " + num(basal.units, 1) + " IE" : "") + " gespeichert" +
      (result.split_units ? " · 2. Teil " + num(result.split_units, 1) + " IE vorgemerkt" : "") +
      (result.sport_marked ? " · Sport berücksichtigt" : "") +
      (basal && !basal.nightscout_synced ? " · Basal vorerst nur lokal" : "") +
      (result.basal_error ? " · Basal nicht gespeichert: " + result.basal_error : "") +
      (result.sport_error ? " · " + result.sport_error : "");
    const undo = async () => {
      if (basal) await deleteBasal(basal.id).catch(error => toast(error.message, true));
      if (result.id) await undoEntry(result.id, restore);
      else if (basal) await loadDay();
    };
    toast(message, Boolean(result.basal_error || result.sport_error),
      result.id || basal ? { label: "Rückgängig", run: undo } : null);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

// Undo deletes the just-saved Nightscout entry and puts its values back into the form for correction.
async function undoEntry(id, restore) {
  try {
    await api("/api/treatment/delete", { method: "POST", body: JSON.stringify({ id, day: restore.day }) });
    if (state.day === restore.day) await loadDay();
    const form = $("#entry-form");
    if (state.day === restore.day && form.elements.kind.value === "meal" &&
        !form.elements.carbs.value && !form.elements.insulin.value) {
      form.elements.at.value = restore.time;
      state.entryTimeTouched = true;
      markEntryActivity();
      form.elements.carbs.value = restore.carbs;
      form.elements.insulin.value = restore.insulin;
      form.elements.notes.value = restore.notes;
      updateEntryFields();
      await updatePreview();
    }
    toast("Eintrag aus Nightscout entfernt");
  } catch (error) {
    toast(error.message, true);
  }
}

async function deleteTreatment(event) {
  const button = event.target.closest(".delete-treatment");
  if (!button) return;
  const label = button.dataset.label + " um " + timeOnly(button.dataset.at);
  if (!window.confirm(label + " Uhr wirklich aus Nightscout löschen?")) return;
  button.disabled = true;
  try {
    await api("/api/treatment/delete", { method: "POST", body: JSON.stringify({ id: button.dataset.id, day: state.day }) });
    toast("Eintrag aus Nightscout gelöscht");
    button.disabled = false;
    if ($("#edit-dialog").open) closeEditDialog();
    await loadDay();
  } catch (error) {
    button.disabled = false;
    toast(error.message, true);
  }
}

async function saveSportMark(event) {
  const button = event.target.closest(".sport-save, .sport-suggestion, .sport-remove, .sport-none");
  const block = button?.closest(".sport-review");
  if (!block) return;
  const remove = button.matches(".sport-remove, .sport-none");
  const suggestion = button.matches(".sport-suggestion");
  const effect = $(".sport-effect", block);
  const amount = remove ? 0 : Number(suggestion ? block.dataset.suggestion : effect.value);
  if (!remove && (!Number.isInteger(amount) || amount === 0 || Math.abs(amount) > 500)) {
    effect.setCustomValidity("Bitte eine BZ-Wirkung zwischen −500 und +500 mg/dL eingeben, nicht 0.");
    effect.reportValidity();
    return;
  }
  effect.setCustomValidity("");
  button.disabled = true;
  try {
    await api("/api/analysis/sport", { method: "POST", body: JSON.stringify({
      meal_id: block.dataset.mealId, day: block.dataset.day,
      status: remove ? "unaffected" : "affected", effect_mgdl: sportChange(amount),
    }) });
    toast(button.matches(".sport-none") ? "Kein Sporteinfluss vermerkt" : remove ? "Sporteinfluss entfernt" : "Sporteinfluss gespeichert");
    await loadDay();
    if (state.view === "analysis") await loadSummary();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    window_minutes: Number(form.elements.window_minutes.value),
    dose_target_mgdl: form.elements.dose_target_mgdl.value,
    dose_cautious_target_mgdl: form.elements.dose_cautious_target_mgdl.value,
    dose_low_target_mgdl: form.elements.dose_low_target_mgdl.value,
    auto_method: form.elements.auto_method.value,
    auto_percentile: form.elements.auto_percentile.value,
    auto_lookback_days: Number(form.elements.auto_lookback_days.value),
    auto_half_life_days: form.elements.auto_half_life_days.value,
    auto_surcharge_mgdl: form.elements.auto_surcharge_mgdl.value,
    basal_default_units: form.elements.basal_default_units.value,
    split_min_grams: Number(form.elements.split_min_grams.value),
    split_first_percent: Number(form.elements.split_first_percent.value),
    split_delay_minutes: Number(form.elements.split_delay_minutes.value),
    breakfast_end_minute: clockMinute(form.elements.breakfast_end.value),
    lunch_end_minute: clockMinute(form.elements.lunch_end.value),
    sport_lookback_minutes: Number(form.elements.sport_lookback_minutes.value),
    sport_effects_mgdl: Object.fromEntries(sportKinds
      .map(kind => [kind, sportChange(form.elements["sport_" + kind].value)])),
    effective_at: form.elements.effective_date.value ? toInstant(form.elements.effective_date.value, form.elements.effective_time.value || "00:00") : null,
  };
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify(payload) });
    toast("Einstellungen gespeichert");
    await loadSettings();
    await loadDay();
  } catch (error) {
    toast(error.message, true);
  }
}

function renderNightSummary(night) {
  const metrics = [
    ["Startwert", "median_start_glucose"],
    ["Endwert", "median_end_glucose"],
    ["Veränderung", "median_delta_mgdl"],
    ["Minimalwert", "median_minimum_glucose"],
  ];
  $("#night-summary-count").textContent = night.count + " von " +
    (night.count + night.unavailable_count) + " Nächten mit vollständigen Werten";
  $("#night-summary-cards").innerHTML = metrics.map(([label, key]) =>
    '<div class="summary-card"><span>' + label + ' · Median</span><strong>' +
    (night[key] == null ? '—' : label === "Veränderung" ? escapeHTML(signedChange(night[key])) : escapeHTML(num(night[key], 1)) + ' <small>mg/dL</small>') +
    '</strong></div>'
  ).join("");
  const cell = (value, at) => value == null ? '—' : escapeHTML(num(value, 1)) + ' mg/dL' +
    (at ? '<small class="night-error">' + escapeHTML(timeOnly(at)) + ' Uhr</small>' : '');
  const rows = night.nights.map(item => {
    const label = new Date(item.day + "T12:00:00").toLocaleDateString("de-DE", {
      weekday: "short", day: "numeric", month: "short",
    });
    const reason = item.reason
      ? '<small class="night-error">' + escapeHTML(item.reason) + '</small>' : '';
    return '<tr><td>' + escapeHTML(label) + reason + '</td><td>' +
      cell(item.start_glucose, item.start) + '</td><td>' + cell(item.end_glucose, item.end) +
      '</td><td>' + escapeHTML(signedChange(item.delta_mgdl)) + '</td><td>' + cell(item.minimum_glucose, item.minimum_at) + '</td></tr>';
  }).join("");
  $("#night-records").innerHTML = rows
    ? '<div class="table-wrap"><table class="analysis-table"><thead><tr><th>Nacht</th><th>Startwert</th><th>Endwert</th><th>Veränderung</th><th>Minimalwert</th></tr></thead><tbody>' + rows + '</tbody></table></div>'
    : '<div class="empty">Für diesen Zeitraum liegen keine Nächte vor.</div>';
}

function renderSportAnalysis(analysis) {
  const sessions = analysis?.sessions || [];
  const kinds = analysis?.kinds || [];
  $("#sport-analysis-count").textContent = sessions.length + (sessions.length === 1 ? " Einheit" : " Einheiten");
  $("#sport-analysis-kinds").innerHTML = kinds.map(group =>
    '<div class="summary-card"><span class="meal-type">' + escapeHTML(group.label) + '</span>' +
    '<div class="summary-primary"><span>Median Sportwirkung</span><strong>' +
    (group.median_effect_mgdl == null ? '—' : glucoseChangeHTML(group.median_effect_mgdl)) + '</strong></div>' +
    (group.effect_count > 1 ? '<p>Spanne ' + escapeHTML(signedChange(group.min_effect_mgdl)) + ' bis ' +
      escapeHTML(signedChange(group.max_effect_mgdl)) + '</p>' : '') +
    '<p>' + group.session_count + (group.session_count === 1 ? ' Einheit' : ' Einheiten') + ' · ' +
    group.effect_count + ' von ' + group.meal_count + (group.meal_count === 1 ? ' Mahlzeit' : ' Mahlzeiten') + ' auswertbar</p></div>'
  ).join("");
  const dateTime = value => new Date(value).toLocaleString("de-DE", {
    weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const period = session => session.end
    ? escapeHTML(dateTime(session.start)) + '–' + escapeHTML(timeOnly(session.end)) +
      ' <small>(' + escapeHTML(session.duration_minutes) + ' min)</small>'
    : '—';
  const mealCells = meal => {
    if (!meal) return '<td colspan="6"><span class="hint">Keine Mahlzeit im Einflussbereich (' +
      escapeHTML(analysis.lookback_minutes) + ' min davor bis Mahlzeitende)</span></td>';
    const baseline = meal.baseline_units_per_ke == null ? '' :
      '<br><small>Vergleich ' + escapeHTML(factorNum(meal.baseline_units_per_ke)) + ' IE/KE</small>';
    const effect = meal.effect_mgdl == null
      ? '<span class="summary-unavailable">' + escapeHTML(meal.reason || "Nicht berechenbar") + '</span>'
      : '<b>' + glucoseChangeHTML(meal.effect_mgdl) + '</b>' +
        (meal.shared ? ' <span class="outlier-badge">Mehrere Einheiten</span>' : '') +
        (meal.excluded ? ' <span class="outlier-badge">Ausgeklammert</span>' : '');
    return '<td>' + escapeHTML(names[meal.meal]) + '<br><small>' + escapeHTML(dateTime(meal.start)) + '–' +
      escapeHTML(timeOnly(meal.end)) + '</small></td>' +
      '<td>' + (meal.factor_without_sport_units_per_ke == null ? '—' :
        escapeHTML(factorNum(meal.factor_without_sport_units_per_ke)) + ' IE/KE') + baseline + '</td>' +
      '<td>' + glucoseChangeHTML(meal.delta_mgdl) + '</td>' +
      '<td>' + glucoseChangeHTML(meal.expected_delta_mgdl) + '</td>' +
      '<td>' + effect + '</td>' +
      '<td>' + (meal.confirmed_effect_mgdl == null ? '—' : escapeHTML(signedChange(-Number(meal.confirmed_effect_mgdl), 0))) + '</td>';
  };
  $("#sport-analysis-sessions").innerHTML = sessions.length
    ? '<div class="table-wrap"><table class="analysis-table"><thead><tr><th>Sport</th><th>Zeitraum</th><th>Mahlzeit</th><th>Faktor ohne Sport</th><th>Gemessen</th><th>Erwartet ohne Sport</th><th>Sportwirkung</th><th>Bestätigt</th></tr></thead><tbody>' +
      sessions.flatMap(session => (session.meals.length ? session.meals : [null]).map(meal =>
        '<tr><td><b>' + escapeHTML(session.label) + '</b>' +
        (session.notes ? '<br><small>' + escapeHTML(session.notes) + '</small>' : '') + '</td>' +
        '<td>' + period(session) + '</td>' + mealCells(meal) + '</tr>')).join("") +
      '</tbody></table></div>'
    : '<div class="empty">Im Zeitraum sind keine Sport- oder Rennsport-Einträge im lokalen Datenbestand.</div>';
}

// Backtest matrix: where the endpoint glucose would have landed with each lookback,
// weighting strength (half-life) and percentile.
function renderSuggestionDerivation(d) {
  const output = $("#suggestion-derivation");
  if (!d) { output.innerHTML = ""; return; }
  $("#suggestion-method").textContent = "Aktiv: " + d.lookback_days + " T. · Halbwertszeit " + num(d.half_life_days, 1) +
    " T. · " + num(d.percentile, 1) + " %" + (d.method === "surcharge" ? " + Zuschlag" : "");
  const low = Number(d.glucose_low_mgdl), high = Number(d.glucose_high_mgdl);
  const variants = d.variants || [];
  const percentiles = variants.length ? variants[0].cells.map(cell => cell.percentile) : [];
  const total = variants.length ? variants[0].cells[0].stats.glucose_count : 0;
  // The server marks the active cell and the best cells (per lookback period and overall).
  const strength = days => { const diff = Number(days) - 7; return num(days, 1) + ' T.' + (diff < 0 ? ' <small>stärker</small>' : diff > 0 ? ' <small>schwächer</small>' : ''); };
  const cellHTML = (variant, cell) => {
    const [below, inRange, above] = ["below", "in_range", "above"].map(key => Number(cell.shares[key]));
    const active = cell.active;
    const isBest = cell.best === "overall";
    const isPeriodBest = Boolean(cell.best);
    const bar = '<svg class="range-bar" viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true">' +
      '<rect class="range-low" x="0" width="' + below.toFixed(2) + '" height="8"/>' +
      '<rect class="range-ok" x="' + below.toFixed(2) + '" width="' + inRange.toFixed(2) + '" height="8"/>' +
      '<rect class="range-high" x="' + (below + inRange).toFixed(2) + '" width="' + above.toFixed(2) + '" height="8"/></svg>';
    const notes = [isBest ? 'bestes gesamt' : isPeriodBest ? 'bestes ' + variant.lookback_days + ' T.' : '',
      cell.stats.median_abs_deviation == null ? '' : '±' + num(Number(cell.stats.median_abs_deviation) * 100, 0) + ' %'];
    return '<td class="matrix-cell' + (active ? ' is-active' : '') + (isPeriodBest ? ' is-best' : '') + (isBest ? ' is-overall-best' : '') + '">' +
      (active ? '<span class="active-badge">Aktiv</span>' : '') + bar +
      '<span class="range-nums"><span class="range-low">' + num(below, 0) + '</span> · <b class="range-ok">' + num(inRange, 0) +
      ' %</b> · <span class="range-high">' + num(above, 0) + '</span></span>' +
      (cell.stats.mean_glucose == null ? '' : '<span class="range-glucose">Ø ' + num(cell.stats.mean_glucose, 0) +
        ' · Median ' + num(cell.stats.median_glucose, 0) + ' mg/dL</span>') + '<small>' +
      escapeHTML(notes.filter(Boolean).join(' · ')) + '</small></td>';
  };
  const rows = variants.map((variant, index) => {
    const first = index === 0 || variants[index - 1].lookback_days !== variant.lookback_days;
    return '<tr' + (first ? ' class="matrix-group"' : '') + '><td>' + (first ? escapeHTML(variant.lookback_days + ' Tage') : '') +
      '</td><td>' + strength(variant.half_life_days) + '</td>' + variant.cells.map(cell => cellHTML(variant, cell)).join("") + '</tr>';
  });
  const legend = '<p class="hint derivation-note range-legend"><span class="range-low">■ &lt; ' + num(low, 0) + '</span> ' +
    '<span class="range-ok">■ ' + num(low, 0) + '–' + num(high, 0) + '</span> <span class="range-high">■ &gt; ' + num(high, 0) +
    '</span> · Anteil der Mahlzeiten mit BZ am Endpunkt in diesem Bereich, darunter Durchschnitt und Median dieses BZ, dazu typische Abweichung des Faktors. ' +
    'Grün umrandet: bestes Feld je Zeitraum (meiste im Bereich, dann wenigste unter ' + num(low, 0) + '), kräftig: bestes gesamt. Dunkel umrandet mit „Aktiv“: deine Einstellung.</p>';
  output.innerHTML = total
    ? legend + '<div class="table-wrap"><table class="analysis-table compact matrix"><thead><tr><th>Zeitraum</th><th>Halbwertszeit</th>' +
      percentiles.map(value => '<th>' + (value === "surcharge" ? escapeHTML(num(d.percentile, 1)) + ' % + Zuschlag <small>' + num(d.surcharge_mgdl, 0) + ' mg/dL je Mahlzeit</small>'
        : escapeHTML(num(value, 1)) + ' %' + (Number(value) === 50 ? ' <small>Median</small>' : '')) + '</th>').join("") +
      '</tr></thead><tbody>' + rows.join("") + '</tbody></table></div>' +
      '<p class="hint derivation-note">Getestet werden dieselben ' + total + (total === 1 ? ' Mahlzeit' : ' Mahlzeiten') +
      ' der letzten ' + d.backtest_days + ' Tage für alle Felder, unabhängig vom Zeitraum unter „Werte“. Jede Mahlzeit bekommt den Vorschlag, ' +
      'den das Feld aus den Tagen davor berechnet hätte; gezeigt wird der BZ am Endpunkt mit diesem Faktor bei Korrektur auf den Zielwert (Modellrechnung: Ziel + (tatsächlicher − vorgeschlagener Faktor) × Σ[KH × KF] / 10, nur der Faktorfehler zählt). ' +
      'Kürzere Halbwertszeit = neue Werte zählen stärker. Zuschlag: eingestelltes Perzentil der Faktoren ohne einen festen Anstieg von ' +
      num(d.surcharge_mgdl, 0) + ' mg/dL je Hauptmahlzeit (Fett/Eiweiß, bis ' + num(d.surcharge_full_grams, 0) +
      ' g KH anteilig), dazu dieser Zuschlag für die jeweilige Mahlzeit.</p>'
    : '<p class="hint derivation-note">In den letzten ' + d.backtest_days + ' Tagen fehlen Mahlzeiten mit genug Vorgeschichte (mind. ' +
      d.min_meals + ' Faktoren derselben Mahlzeit in jedem Zeitraum davor, also auch in den letzten 8 Tagen).</p>';
}

// Automatik view: worked example, trade-offs against the current strategy and one row per setting.
// The server delivers every number, verdict and the settings change behind each "Übernehmen".
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
const rangeBar = (below, inRange, above) => {
  const total = below + inRange + above || 1, pct = value => (value * 100 / total).toFixed(2);
  return '<svg class="range-bar" viewBox="0 0 100 8" preserveAspectRatio="none" role="img" aria-label="' +
    below + ' unter 80, ' + inRange + ' im Bereich, ' + above + ' über 160">' +
    '<rect class="range-low" x="0" width="' + pct(below) + '" height="8"/>' +
    '<rect class="range-ok" x="' + pct(below) + '" width="' + pct(inRange) + '" height="8"/>' +
    '<rect class="range-high" x="' + (Number(pct(below)) + Number(pct(inRange))).toFixed(2) + '" width="' + pct(above) + '" height="8"/></svg>';
};
const rangeCounts = (below, inRange, above, medianGlucose) => '<span class="range-nums"><span class="range-low">' + below +
  '</span> · <b class="range-ok">' + inRange + '</b> · <span class="range-high">' + above + '</span>' +
  (medianGlucose == null ? '' : '<small class="range-median">Median ' + num(medianGlucose, 0) + ' mg/dL</small>') + '</span>';
const applyButton = (apply, label = "Übernehmen") => '<button type="button" class="quiet apply-button" data-apply="' +
  escapeHTML(JSON.stringify(apply)) + '">' + label + '</button>';

const deltaText = (value, label) => '<span class="' + (Number(value) < 0 ? 'delta-good' : Number(value) > 0 ? 'delta-bad' : '') + '">' +
  (Number(value) > 0 ? '+' : Number(value) < 0 ? '−' : '±') + Math.abs(Number(value)) + ' ' + label + '</span>';

function renderAutomatik(d) {
  const explain = $("#automatik-explain"), tradeoffs = $("#automatik-tradeoffs"), tuning = $("#automatik-tuning");
  if (!d || !d.tuning) { explain.innerHTML = tradeoffs.innerHTML = tuning.innerHTML = ""; return; }
  const surcharge = Number(d.active_surcharge_mgdl);
  const weights = (d.weights || []).map(item => (item.age_days === 1 ? 'gestern ' : 'vor ' + item.age_days + ' Tagen ') +
    num(item.weight, 2)).join(' · ');
  const exampleName = item => item.meal === "breakfast" ? "Frühstück " + (item.day_type === "work" ? "Arbeit" : "Frei") : names[item.meal];
  const examples = d.examples.map(item => {
    if (item.units_per_ke == null) {
      return '<tr><td>' + escapeHTML(exampleName(item)) + '</td><td>' + item.count + '</td><td colspan="5" class="hint">Zu wenige Mahlzeiten (mind. ' + item.min_meals + ')</td></tr>';
    }
    return '<tr><td>' + escapeHTML(exampleName(item)) + '</td><td>' + item.count + '</td><td><b>' + factorNum(item.units_per_ke) + '</b> IE/KE</td>' +
      '<td>' + num(item.carbs_g, 0) + ' g · KF ' + num(item.isf, 0) + '</td>' +
      '<td>' + num(Number(item.carbs_g) / 10, 1) + ' KE × ' + factorNum(item.units_per_ke) + ' = ' + num(item.carb_units, 1) + ' IE</td>' +
      '<td>' + (surcharge ? '+ ' + num(item.surcharge_units, 1) + ' IE' : '—') + '</td>' +
      '<td><b>' + num(item.total_units, 1) + ' IE</b></td></tr>';
  }).join("");
  explain.innerHTML = '<div class="automatik-chips">' +
    ['Perzentil <b>' + num(d.percentile, 1) + ' %</b>', 'Zuschlag <b>' + (surcharge ? num(surcharge, 0) + ' mg/dL' : 'aus') + '</b>',
     'Halbwertszeit <b>' + num(d.half_life_days, 1) + ' Tage</b>', 'Zeitraum <b>' + d.lookback_days + ' Tage</b>']
      .map(text => '<span class="chip">' + text + '</span>').join("") + '</div>' +
    '<ol class="automatik-steps">' +
    '<li>Alle gültigen Mahlzeiten derselben Art aus den letzten ' + d.lookback_days + ' Tagen (Frühstück getrennt nach Arbeit und Frei). Ältere zählen weniger: ' + escapeHTML(weights) + '.</li>' +
    (surcharge ? '<li>Von jedem Faktor wird der Zuschlag abgezogen: ' + num(surcharge, 0) + ' mg/dL je Hauptmahlzeit, umgerechnet in IE/KE dieser Mahlzeit (bis 40 g KH anteilig weniger).</li>' : '') +
    '<li>Davon der gewichtete ' + num(d.percentile, 1) + '-%-Wert. Das ist der Faktor für die KH.</li>' +
    '<li>Dosis = KE × Faktor' + (surcharge ? ' + Zuschlag (' + num(surcharge, 0) + ' mg/dL ÷ KF, einmal je Hauptmahlzeit, nicht für Snacks)' : '') + ', dazu wie immer die Korrektur auf den Zielwert.</li></ol>' +
    '<p class="hint">Beispiel mit einer typischen Mahlzeit (Median der KH und des KF der verwendeten Mahlzeiten), ohne Korrektur:</p>' +
    '<div class="table-wrap"><table class="analysis-table compact"><thead><tr><th>Mahlzeit</th><th>Mahlzeiten</th><th>Faktor</th><th>Typisch</th><th>KH-Teil</th><th>Zuschlag</th><th>Summe</th></tr></thead><tbody>' +
    examples + '</tbody></table></div>';

  const t = d.tradeoffs;
  $("#tradeoff-count").textContent = t.meal_count + " Mahlzeiten";
  if (!t.meal_count) {
    tradeoffs.innerHTML = '<div class="empty">In den letzten ' + d.backtest_days + ' Tagen fehlen Mahlzeiten mit genug Vorgeschichte (mind. ' +
      d.min_meals + ' derselben Art in den 8 Tagen davor).</div>';
    tuning.innerHTML = "";
    return;
  }
  const current = t.current;
  const points = t.cloud;
  const maxX = Math.max(1, ...points.map(p => p.below)), maxY = Math.max(1, ...points.map(p => p.above));
  const W = 320, H = 200, L = 34, B = 30, x = v => L + v / maxX * (W - L - 12), y = v => H - B - v / maxY * (H - B - 12);
  const ticks = (max, f) => Array.from(new Set([0, Math.round(max / 2), max])).map(f).join("");
  const plot = '<svg class="tradeoff-plot" viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="Unter 80 gegen über 160 für alle Kombinationen">' +
    ticks(maxX, v => '<text x="' + x(v) + '" y="' + (H - B + 16) + '" text-anchor="middle">' + v + '</text>') +
    ticks(maxY, v => '<text x="' + (L - 8) + '" y="' + (y(v) + 4) + '" text-anchor="end">' + v + '</text>') +
    '<line class="axis" x1="' + L + '" y1="' + (H - B) + '" x2="' + (W - 6) + '" y2="' + (H - B) + '"/>' +
    '<line class="axis" x1="' + L + '" y1="' + (H - B) + '" x2="' + L + '" y2="6"/>' +
    '<text class="axis-label" x="' + (W - 6) + '" y="' + (H - 2) + '" text-anchor="end">unter 80 →</text>' +
    '<text class="axis-label" x="' + (L + 4) + '" y="12">über 160 ↑</text>' +
    points.map(p => '<circle class="cloud" cx="' + x(p.below) + '" cy="' + y(p.above) + '" r="3"/>').join("") +
    '<polyline class="line" points="' + t.line.map(o => x(o.below) + ',' + y(o.above)).join(" ") + '"/>' +
    t.line.map(o => '<circle class="on-line" cx="' + x(o.below) + '" cy="' + y(o.above) + '" r="4.5"/>').join("") +
    '<circle class="current" cx="' + x(current.below) + '" cy="' + y(current.above) + '" r="7"/></svg>';
  const optionRow = (o, isCurrent) => '<li class="tradeoff' + (isCurrent ? ' is-current' : '') + '">' +
    '<div class="tradeoff-head"><b>' + (isCurrent ? 'Aktuell' : o.changes.map(key => tuningNames[key] + ' ' +
      tuningLabel(key, key === "percentile" ? o.percentile : key === "surcharge" ? o.surcharge_mgdl : key === "half_life" ? o.half_life_days : o.lookback_days)).join(' · ')) +
    '</b><small>' + escapeHTML(strategyText(o)) + '</small>' + (isCurrent ? '' : '<span class="tradeoff-delta">' +
      deltaText(o.delta_below, "zu tief") + ' · ' + deltaText(o.delta_above, "zu hoch") +
      (o.delta_median_glucose == null ? '' : ' · Median ' + (Number(o.delta_median_glucose) >= 0 ? '+' : '−') +
        num(Math.abs(Number(o.delta_median_glucose)), 0) + ' mg/dL') + '</span>') + '</div>' +
    rangeBar(o.below, o.in_range, o.above) + rangeCounts(o.below, o.in_range, o.above, o.median_glucose) +
    (isCurrent ? '<span class="hint">eingestellt</span>' : applyButton(o.apply)) + '</li>';
  const alternatives = t.line.filter(o => o.changes.length);
  tradeoffs.innerHTML = '<div class="tradeoff-layout">' + plot + '<div><ul class="tradeoff-list">' +
    optionRow(current, true) + alternatives.map(o => optionRow(o, false)).join("") + '</ul>' +
    '<p class="hint">Jeder Punkt ist eine Kombination der vier Regler, alle auf denselben ' + t.meal_count +
    ' Mahlzeiten. Auf der Linie liegen die, die keine andere bei „unter 80“ und „über 160“ zugleich schlägt: Nach links weniger zu tief, dafür mehr zu hoch. ' +
    (t.line.some(o => !o.changes.length) ? 'Deine Einstellung liegt auf der Linie.' : 'Deine Einstellung (Ring) liegt nicht auf der Linie, mindestens eine Alternative ist in beiden Richtungen gleich gut oder besser.') +
    ' Unterschiede von 1–2 Mahlzeiten können Zufall sein.</p></div></div>';

  $("#tuning-count").textContent = "Je Regler nur dieser geändert";
  const verdictText = row => row.verdict === "better" ? "besser: " + tuningLabel(row.key, row.options.find(o => o.best).value)
    : row.verdict === "shift" ? "verschiebt tief ↔ hoch" : "kein Unterschied";
  tuning.innerHTML = d.tuning.map(row => '<details class="tuning-row"' + (row.verdict !== "none" ? ' open' : '') + '><summary>' +
    '<b>' + tuningNames[row.key] + '</b><span class="verdict verdict-' + row.verdict + '">' + escapeHTML(verdictText(row)) + '</span>' +
    '<small>' + row.meal_count + ' Mahlzeiten</small></summary><p class="hint">' + tuningHints[row.key] + '</p>' +
    '<ul class="tuning-options">' + row.options.map(o => '<li class="' + (o.active ? 'is-active' : '') + (o.best ? ' is-best' : '') + '">' +
      '<span class="tuning-label">' + escapeHTML(tuningLabel(row.key, o.value)) + (o.active ? ' <small>aktiv</small>' : '') + '</span>' +
      rangeBar(o.below, o.in_range, o.above) + rangeCounts(o.below, o.in_range, o.above, o.median_glucose) +
      (o.active ? '<span></span>' : applyButton(o.apply)) + '</li>').join("") + '</ul></details>').join("") +
    '<p class="hint range-legend"><span class="range-low">■ unter 80</span> <span class="range-ok">■ 80–160</span> <span class="range-high">■ über 160</span> · ' +
    'Median = mittlerer BZ am Endpunkt dieser Mahlzeiten (Ziel ' + num(d.dose_target_mgdl, 0) + ' mg/dL). Jede Zeile prüft die Mahlzeiten, die alle ihre Optionen berechnen können. „Kein Unterschied“: die Optionen liegen höchstens ' +
    num(d.tuning[0].noise_meals, 1) + ' Mahlzeiten auseinander.</p>';
}

async function applyAutomatik(button) {
  const change = JSON.parse(button.dataset.apply);
  const previous = Object.fromEntries(["auto_percentile", "auto_half_life_days", "auto_lookback_days", "auto_method", "auto_surcharge_mgdl"]
    .map(key => [key, state.settings?.[key]]).filter(([, value]) => value != null));
  const save = async (payload, message) => {
    $$("#analysis-automatik [data-apply]").forEach(item => item.disabled = true);
    try {
      const result = await api("/api/settings", { method: "POST", body: JSON.stringify(payload) });
      state.settings = result.settings;
      toast(message, false, payload === change ? { label: "Rückgängig", run: () => save(previous, "Vorherige Einstellung wiederhergestellt") } : null);
      await loadSettings();
      await loadSummary();
    } catch (error) {
      toast(error.message, true);
      $$("#analysis-automatik [data-apply]").forEach(item => item.disabled = false);
    }
  };
  await save(change, "Automatik-Einstellung übernommen");
}

// Information only: how much more insulin per KE breakfast needs on work days than on free days.
function renderDayTypeRatio(d) {
  const output = $("#day-type-ratio");
  if (!d) { output.innerHTML = ""; return; }
  $("#day-type-ratio-range").textContent = "Letzte " + d.days + " Tage";
  const pct = number => (Number(number) >= 0 ? '+' : '−') + Math.abs(Number(number) * 100).toLocaleString("de-DE", { maximumFractionDigits: 0 }) + '\u00a0%';
  const value = (median, count) => median == null ? '—' : factorNum(median) + ' <small>(' + count + ')</small>';
  const rows = Object.values(d.meals).map(m => {
    const singles = m.singles.length
      ? pct(m.single_median) + ' <small>(' + m.singles.length + ', ' + pct(m.single_min) + ' bis ' + pct(m.single_max) + ')</small>' +
        '<br><small class="surcharge-days">' + m.singles.map(item => escapeHTML(new Date(item.day + "T12:00").toLocaleDateString("de-DE", { weekday: "short", day: "numeric", month: "numeric" })) + ' ' + pct(item.surcharge)).join(' · ') + '</small>'
      : '—';
    return '<tr><td>' + value(m.free_median, m.free_count) + '</td><td>' +
      value(m.work_median, m.work_count) + '</td><td><b>' + (m.surcharge == null ? '—' : pct(m.surcharge)) + '</b></td><td class="surcharge-cell">' + singles + '</td></tr>';
  });
  output.innerHTML = '<div class="table-wrap"><table class="analysis-table compact"><thead><tr>' +
    '<th>Frei <small>Median IE/KE</small></th><th>Arbeit <small>Median IE/KE</small></th><th>Stressaufschlag</th>' +
    '<th>Je freiem Tag</th></tr></thead><tbody>' + rows.join("") + '</tbody></table></div>' +
    '<p class="hint derivation-note">Frei ist die Basis: Arbeit = Frei × (1 + Stressaufschlag). „Je freiem Tag“ vergleicht jeden ' +
    'freien Tag mit dem Frühstück an den ' + d.neighbours + ' Arbeitstagen davor und den ' + d.neighbours +
    ' danach (Median, höchstens ' + d.reference_days + ' Tage entfernt, mind. 2 Werte). Liegen diese Einzelwerte eng beieinander, ' +
    'ist der Aufschlag stabil. Nur zur Info, der Vorschlag bleibt unverändert.</p>';
}

async function loadSummary() {
  $("#summary").innerHTML = '<div class="empty">Analyse wird geladen …</div>';
  $("#night-summary-cards").innerHTML = '<div class="empty">Nachtübersicht wird geladen …</div>';
  $("#night-records").innerHTML = "";
  $("#calculations").innerHTML = "";
  $("#automatik-status").textContent = "Wird berechnet …";
  try {
    const days = $("#summary-days").value;
    const data = await api("/api/summary?days=" + days);
    // The summary endpoint may refresh the Nightscout profile before calculating.
    state.settings = (await api("/api/settings")).settings;
    renderNightSummary(data.night || { count: 0, unavailable_count: Number(days), nights: [] });
    if (data.profile_sync_error) {
      $("#sync-progress").textContent = "Korrekturfaktor-Profil nicht aktualisiert: " + data.profile_sync_error;
    }
    const span = (low, high) => low == null ? '—'
      : Number(low) === Number(high) ? factorNum(low) : factorNum(low) + '–' + factorNum(high);
    const fact = (label, value, key = false) =>
      '<div' + (key ? ' class="key"' : '') + '><dt>' + label + '</dt><dd>' + escapeHTML(value) + '</dd></div>';
    const suggestion = s => s.auto_units_per_ke != null
      ? factorNum(s.auto_units_per_ke) + (s.surcharge ? ' + Zuschlag' : '') : '—';
    const suggestionNote = s => 'Vorschlag aus ' + s.count + '\u00a0(' + s.days + '\u00a0T.)';
    const mealsText = count => count + (count === 1 ? ' Mahlzeit' : ' Mahlzeiten');
    const breakfastSubcards = () => '<div class="breakfast-subcards">' + ["work", "free"].map(dayType => {
      const subset = data.groups.breakfast.by_day_type[dayType];
      const s = subset.suggestion;
      return '<div class="breakfast-subcard"><span>' + (dayType === "work" ? "Arbeit" : "Frei") + '</span>' +
        '<strong>' + (subset.count ? escapeHTML(factorNum(subset.median_units_per_ke)) + ' <small>IE/KE</small>' : '—') +
        '</strong><dl class="factor-facts compact">' +
        fact("Vorschlag", suggestion(s), true) +
        (subset.q1_units_per_ke != null ? fact("Mittlere 50 %", span(subset.q1_units_per_ke, subset.q3_units_per_ke)) : '') +
        '</dl><small>' + mealsText(subset.count) + '<br>' + escapeHTML(suggestionNote(s)) + '</small></div>';
    }).join("") + '</div>';
    $("#summary").innerHTML = ["breakfast", "lunch", "dinner"].map(kind => {
      const row = data.groups[kind];
      const hasValues = row.count > 0;
      const current = (row.nightscout_units_per_ke || []).map(value => factorNum(value)).join(' / ') || '—';
      const facts = (kind === "breakfast" ? '' : fact("Vorschlag", suggestion(row.suggestion), true)) +
        fact("Aktuell", current) +
        (row.q1_units_per_ke != null ? fact("Mittlere 50 %", span(row.q1_units_per_ke, row.q3_units_per_ke)) : '');
      return '<div class="summary-card"><span class="meal-type">' + escapeHTML(names[kind]) + '</span>' +
        '<div class="summary-primary"><span>Median</span><strong>' +
        (hasValues ? escapeHTML(factorNum(row.median_units_per_ke)) + ' <small>IE/KE</small>' : '—') +
        '</strong></div><dl class="factor-facts">' + facts + '</dl>' +
        '<p>' + mealsText(row.count) +
        (kind === "breakfast" ? '' : ' · ' + escapeHTML(suggestionNote(row.suggestion))) + '</p>' +
        (kind === "breakfast" ? breakfastSubcards() : '') + '</div>';
    }).join("");
    renderSportAnalysis(data.sport_analysis);
    renderSuggestionDerivation(data.suggestion_derivation);
    renderAutomatik(data.suggestion_derivation);
    $("#automatik-status").textContent = "";
    renderDayTypeRatio(data.day_type_ratio);
    const calculations = ["breakfast", "lunch", "dinner"]
      .flatMap(kind => data.groups[kind].calculations.map(item => ({ ...item, kind })))
      .sort((a, b) => b.at.localeCompare(a.at));
    $("#calculation-count").textContent = calculations.length + (calculations.length === 1 ? " Mahlzeit" : " Mahlzeiten");
    $("#calculations").innerHTML = calculations.length
      ? '<div class="table-wrap"><table class="analysis-table"><thead><tr><th>Mahlzeit</th><th>Insulinfaktor</th><th>KH</th><th>Insulin</th><th>Gemessen</th><th>Erwartet</th><th>Delta</th><th>Sport</th><th>Wertung</th><th></th></tr></thead><tbody>' +
        (() => {
          let previousDay = "";
          return calculations.map(item => {
            const newDay = item.day !== previousDay;
            previousDay = item.day;
            const dayHeading = newDay
              ? '<tr class="analysis-day-row"><th colspan="10" scope="colgroup">' +
                escapeHTML(new Date(item.day + "T12:00:00").toLocaleDateString("de-DE", {
                  weekday: "long", day: "numeric", month: "long", year: "numeric",
                })) + ' <label class="day-type-label">Tag: <select class="day-type" data-day="' +
                escapeHTML(item.day) + '"><option value="work"' + (item.day_type === "work" ? " selected" : "") +
                '>Arbeit</option><option value="free"' + (item.day_type === "free" ? " selected" : "") +
                '>Frei</option></select></label></th></tr>'
              : '';
            const factor = item.units_per_ke != null
              ? escapeHTML(factorNum(item.units_per_ke)) + ' IE/KE' +
                (item.strong_outlier ? ' <span class="outlier-badge">Stark abweichend</span>' : '') +
                (item.manual_window ? ' <span class="manual-badge">Zeitraum manuell</span>' : '')
              : '<span class="summary-unavailable">Nicht berechenbar: ' + escapeHTML(item.reason || "Kein Faktor") + '</span>';
            const excluded = item.units_per_ke != null && item.excluded;
            const sport = item.sport || {};
            // Read only here: sport is set on the meal in "Tag durchgehen".
            const sportReview = sport.status === "affected"
              ? '<span class="sport-state is-applied">' + icon("check") + escapeHTML(signedChange(sportChange(sport.effect_mgdl), 0)) + '</span>'
              : sport.status === "auto" && sport.suggested
                ? '<button type="button" class="review-chip needs-check open-meal-day" data-day="' + escapeHTML(item.day) +
                  '" data-meal-id="' + escapeHTML(item.id) + '">Sport offen</button>'
                : '—';
            return dayHeading + '<tr class="' + (excluded ? "analysis-excluded " : "") +
              (item.strong_outlier ? "analysis-outlier" : "") + '">' +
            '<td>' + escapeHTML(names[item.kind]) + '</td><td><b>' + factor + '</b></td>' +
            '<td>' + (item.carbs_total == null ? "—" : escapeHTML(num(item.carbs_total)) + ' g') + '</td>' +
            '<td>' + (item.rapid_units_in_window == null ? "—" : escapeHTML(num(item.rapid_units_in_window)) + ' IE') + '</td>' +
            '<td>' + glucoseChangeHTML(item.delta_mgdl) + '</td>' +
            '<td>' + glucoseChangeHTML(item.expected_delta_mgdl) + '</td>' +
            '<td>' + glucoseChangeHTML(item.deviation_mgdl) + '</td>' +
            '<td>' + sportReview + '</td>' +
            '<td>' + (item.units_per_ke == null ? "—" : excludeButtonHTML(item.id, item.day, excluded)) + '</td>' +
            '<td><button type="button" class="icon-button open-meal-day" data-day="' + escapeHTML(item.day) +
              '" data-meal-id="' + escapeHTML(item.id) + '" aria-label="Im Tag durchgehen öffnen" title="Im Tag durchgehen öffnen">' + icon("diary") + '</button></td></tr>';
          }).join("");
        })() + '</tbody></table></div>'
      : '<div class="empty">Für diesen Zeitraum sind keine Mahlzeiten im lokalen Datenbestand.</div>';
  } catch (error) {
    $("#summary").innerHTML = '<div class="empty">Analyse nicht verfügbar.</div>';
    $("#night-summary-cards").innerHTML = '<div class="empty">Nachtübersicht nicht verfügbar.</div>';
    $("#night-records").innerHTML = "";
    $("#calculations").innerHTML = "";
    $("#sport-analysis-kinds").innerHTML = "";
    $("#sport-analysis-sessions").innerHTML = "";
      toast(error.message, true);
  }
}

async function saveExclusion(event) {
  const button = event.target.closest(".exclude-meal");
  if (!button) return;
  button.disabled = true;
  try {
    await api("/api/analysis/exclusion", {
      method: "POST",
      body: JSON.stringify({ meal_id: button.dataset.mealId, day: button.dataset.day, excluded: button.dataset.excluded !== "true" }),
    });
    if (state.view === "analysis") await loadSummary();
    else await loadDay();
  } catch (error) {
    button.disabled = false;
    toast(error.message, true);
  }
}

async function saveDayType(event) {
  const input = event.target;
  if (!input.matches(".day-type")) return;
  input.disabled = true;
  try {
    await api("/api/analysis/day-type", {
      method: "POST",
      body: JSON.stringify({ day: input.dataset.day, day_type: input.value }),
    });
    await loadSummary();
  } catch (error) {
    input.disabled = false;
    toast(error.message, true);
    await loadSummary();
  }
}

async function syncRange() {
  const button = $("#sync-range");
  button.disabled = true;
  $("#sync-progress").textContent = "Synchronisiere …";
  try {
    const data = await api("/api/sync", { method: "POST", body: JSON.stringify({ days: Number($("#summary-days").value) }) });
    $("#sync-progress").textContent = data.synced + " Tage synchronisiert" +
      (data.errors.length ? " · " + data.errors.length + " Fehler" : "") +
      (data.profile_sync_error ? " · Korrekturfaktor: " + data.profile_sync_error : "");
    await loadSummary();
  } catch (error) {
    $("#sync-progress").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  const initialLocation = locationState();
  state.day = initialLocation.day;
  state.view = initialLocation.view;
  state.persistDay = initialLocation.persistDay;
  analysisTab(initialLocation.analysisTab, { updateHistory: false });
  $("#selected-day").value = state.day;
  setDefaultMealTime();
  setDefaultSportTime();
  setSportDuration();
  view(state.view, { updateHistory: false });
  updateLocation(true);
  $$("[data-analysis-tab]").forEach(button => button.addEventListener("click", () => analysisTab(button.dataset.analysisTab)));
  $("#analysis-automatik").addEventListener("click", event => {
    const button = event.target.closest("[data-apply]");
    if (button) applyAutomatik(button);
  });
  $$(".tab").forEach(link => link.addEventListener("click", event => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    view(link.dataset.view);
  }));
  window.addEventListener("popstate", () => {
    const nextLocation = locationState();
    const dayChanged = state.day !== nextLocation.day;
    if (dayChanged) resetEntryForm();
    state.day = nextLocation.day;
    state.persistDay = nextLocation.persistDay;
    analysisTab(nextLocation.analysisTab, { updateHistory: false });
    syncTabLinks();
    $("#selected-day").value = state.day;
    setDefaultMealTime();
    setDefaultSportTime();
    view(nextLocation.view, { updateHistory: false });
    if (dayChanged || nextLocation.view === "diary") loadDay();
  });
  $("#prev-day").addEventListener("click", () => { const d = new Date(state.day + "T12:00"); d.setDate(d.getDate() - 1); changeDay(d.toLocaleDateString("sv-SE")); });
  $("#next-day").addEventListener("click", () => { const d = new Date(state.day + "T12:00"); d.setDate(d.getDate() + 1); changeDay(d.toLocaleDateString("sv-SE")); });
  $("#selected-day").addEventListener("change", event => changeDay(event.target.value));
  $("#refresh-day").addEventListener("click", loadDay);
  $("#entry-form").addEventListener("submit", saveEntry);
  const touchEntryTime = () => {
    state.entryTimeTouched = true;
    markEntryActivity();
    updateEntryChips();
    schedulePreview(0);
  };
  $("#entry-time").addEventListener("change", touchEntryTime);
  $("#entry-time").addEventListener("input", touchEntryTime);
  $("#entry-form").addEventListener("focusin", setDefaultMealTime);
  for (const type of ["input", "change", "pointerdown", "keydown"]) $("#entry-form").addEventListener(type, markEntryActivity, true);
  $("#entry-assign-chip").addEventListener("click", () => {
    state.entryAs = currentPreview()?.assignment?.snack ? "meal" : "snack";
    schedulePreview(0);
  });
  $("#entry-split-chip").addEventListener("click", () => {
    state.split = !currentPreview()?.split?.active;
    schedulePreview(0);
  });
  $("#entry-form").elements.consider_sport.addEventListener("change", () => {
    updateEntrySportOption();
    schedulePreview(0);
  });
  $("#entry-form").elements.sport_effect_mgdl.addEventListener("input", () => schedulePreview());
  // Carb certainty and the fat mark decide whether the lower target is offered.
  $("#entry-form").elements.certainty_level.addEventListener("change", () => schedulePreview(0));
  $("#entry-form").elements.fat_protein.addEventListener("change", () => schedulePreview(0));
  $$(".entry-mode").forEach(button => button.addEventListener("click", () => {
    $("#entry-kind").value = button.dataset.entryMode;
    updateEntryFields();
  }));
  $("#edit-form").addEventListener("submit", saveEditedEntry);
  $("#edit-form").elements.carbs.addEventListener("input", event => {
    setPickerBase(event.target, 1);
    event.target.setCustomValidity("");
    $("#edit-quality").hidden = state.editing?.kind === "exercise" || !(Number(event.target.value) > 0);
    updateEditPlace();
  });
  $("#edit-form").elements.insulin.addEventListener("input", event => {
    setPickerBase(event.target, 0.5);
    $("#edit-form").elements.carbs.setCustomValidity("");
  });
  $("#ic-rows").addEventListener("input", event => {
    if (event.target.matches("input")) setPickerBase(event.target, 0.1, true);
  });
  $("#cancel-edit").addEventListener("click", closeEditDialog);
  $("#delete-edit").addEventListener("click", deleteTreatment);
  $$("[data-diary-mode]").forEach(button => button.addEventListener("click", () => {
    state.modeDay = state.day;
    state.diaryMode = button.dataset.diaryMode;
    applyDiaryMode();
    renderVisibleCharts();
  }));
  // Charts inside folded meal blocks are drawn when the block opens.
  $("#timeline").addEventListener("toggle", event => {
    if (event.target.matches(".day-meal-details") && event.target.open) renderMealCharts();
  }, true);
  $("#effects-card").addEventListener("toggle", renderEffects);
  $("#close-edit").addEventListener("click", closeEditDialog);
  $("#edit-dialog").addEventListener("close", () => { state.editing = null; });
  $("#dose-result").addEventListener("click", selectDoseSuggestion);
  $("#entry-day-type").addEventListener("click", saveEntryDayType);
  $("#vacation-toggle").addEventListener("change", saveVacation);
  $$(".notify-toggle").forEach(input => input.addEventListener("change", saveNotification));
  $$(".notify-priority").forEach(select => select.addEventListener("change", saveNotificationPriority));
  $("#notify-test").addEventListener("click", sendTestNotification);
  $(".places-panel").addEventListener("click", placeClick);
  $(".places-panel").addEventListener("change", placeChange);
  $("#place-new").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    if (await placeAction({ action: "create", name: form.elements.name.value })) form.reset();
  });
  $("#entry-form").elements.carbs.addEventListener("input", event => {
    event.target.setCustomValidity("");
    updateEntrySubmitLabel();
    updateEntryQuality();
    requestEntryPosition();
    schedulePreview();
  });
  $("#entry-place-chips").addEventListener("click", toggleEntryPlace);
  $("#entry-place-chips").addEventListener("change", event => {
    if (!event.target.matches(".place-more") || !event.target.value) return;
    state.entryPlace = Number(event.target.value);
    updateEntryPlace();
  });
  $("#entry-form").elements.insulin.addEventListener("input", () => {
    $("#entry-form").elements.carbs.setCustomValidity("");
    state.doseChoice = null;
    applyDoseChoice();
  });
  $("#entry-form").elements.notes.addEventListener("input", updateEntryChips);
  $("#entry-form").elements.with_basal.addEventListener("change", updateEntrySubmitLabel);
  $("#entry-form").elements.basal_units.addEventListener("input", event => {
    event.target.setCustomValidity("");
    updateEntrySubmitLabel();
  });
  $("#entry-time-chip").addEventListener("click", () => {
    state.entryTimeOpen = true;
    updateEntryChips();
    $("#entry-time").focus();
  });
  $("#entry-finger-chip").addEventListener("click", () => {
    state.entryFingerOpen = true;
    updateEntryFinger();
    $("#entry-form").elements.finger_mgdl.focus();
  });
  $("#entry-form").elements.finger_mgdl.addEventListener("input", () => {
    $("#entry-form").elements.carbs.setCustomValidity("");
    schedulePreview();
  });
  $("#entry-note-chip").addEventListener("click", () => {
    state.entryNoteOpen = true;
    updateEntryChips();
    $("#entry-form").elements.notes.focus();
  });
  $("#diary-view").addEventListener("click", event => {
    if (event.target.closest(".delete-treatment")) deleteTreatment(event);
    const basalButton = event.target.closest(".delete-basal");
    if (basalButton) deleteBasalRow(basalButton);
    const edit = event.target.closest(".edit-entry");
    if (edit) editEntry(edit.dataset.id);
    if (event.target.closest(".sport-save, .sport-suggestion, .sport-remove, .sport-none")) saveSportMark(event);
    if (event.target.closest(".exclude-meal")) saveExclusion(event);
    const dayTypeButton = event.target.closest("[data-review-day-type]");
    if (dayTypeButton) saveReviewDayType(dayTypeButton);
    const windowButton = event.target.closest(".open-window-editor");
    if (windowButton) openWindowEditor(windowButton.dataset.mealId);
  });
  const windowChart = $("#window-chart");
  windowChart.addEventListener("pointerdown", windowPointerDown);
  windowChart.addEventListener("pointermove", windowPointerMove);
  windowChart.addEventListener("pointerup", windowPointerUp);
  windowChart.addEventListener("pointercancel", windowPointerUp);
  windowChart.addEventListener("keydown", windowKeydown);
  // iOS Safari may still scroll during a handle drag without a non-passive touch listener.
  windowChart.addEventListener("touchmove", event => { if (windowEditor?.drag) event.preventDefault(); }, { passive: false });
  $$("[data-window-handle]").forEach(button => button.addEventListener("click", () => {
    windowEditor.active = button.dataset.windowHandle;
    updateWindowEditor();
  }));
  $$("[data-nudge]").forEach(button => button.addEventListener("click", () =>
    nudgeWindow(windowEditor.active, Number(button.dataset.nudge))));
  $("#save-window").addEventListener("click", () => saveWindow());
  $("#default-window").addEventListener("click", () => {
    windowEditor.start = windowEditor.autoStart;
    windowEditor.end = windowEditor.autoEnd;
    windowEditor.snapped = {};
    renderWindowEditor();
  });
  $$("[data-overview-hours]").forEach(button => button.addEventListener("click", () => {
    try { localStorage.setItem("overviewHours", button.dataset.overviewHours); } catch {}
    renderDayOverview();
  }));
  for (const id of ["#cancel-window", "#close-window"]) $(id).addEventListener("click", () => $("#window-dialog").close());
  $("#window-dialog").addEventListener("close", () => { windowEditor = null; });
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      renderVisibleCharts();
      if (windowEditor && !windowEditor.drag) renderWindowEditor();
    }, 150);
  });
  updateEntryFields();
  $("#settings-form").addEventListener("submit", saveSettings);
  $("#save-ic").addEventListener("click", () => saveProfileSchedule("ic"));
  $("#save-isf").addEventListener("click", () => saveProfileSchedule("isf"));
  for (const kind of ["ic", "isf"]) {
    $("#" + kind + "-rows").addEventListener("keydown", event => {
      if (event.key === "Enter" && event.target.matches("input")) {
        event.preventDefault();
        saveProfileSchedule(kind);
      }
    });
  }
  $("#sync-profile").addEventListener("click", async event => {
    const button = event.currentTarget;
    button.disabled = true;
    $("#isf-sync-status").textContent = "Nightscout-Profil wird synchronisiert …";
    try {
      const result = await api("/api/profile/sync", { method: "POST", body: JSON.stringify({}) });
      await loadSettings();
      await loadDay();
      toast(result.message);
    } catch (error) {
      $("#isf-sync-status").textContent = error.message;
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  $("#summary-days").addEventListener("change", loadSummary);
  $("#sync-range").addEventListener("click", syncRange);
  $("#calculations").addEventListener("click", saveExclusion);
  $("#calculations").addEventListener("change", saveDayType);
  // A meal is corrected on its day: the analysis row opens "Tag durchgehen" at that meal.
  $("#calculations").addEventListener("click", async event => {
    if (event.target.closest(".exclude-meal")) return;
    const button = event.target.closest(".open-meal-day");
    if (!button) return;
    state.day = button.dataset.day;
    state.persistDay = true;
    state.modeDay = state.day;
    state.diaryMode = "review";
    $("#selected-day").value = state.day;
    setDefaultMealTime();
    view("diary");
    await loadDay();
    $("#meal-" + CSS.escape(button.dataset.mealId))?.scrollIntoView({ block: "start" });
  });
  $("#test-connection").addEventListener("click", async () => {
    $("#test-result").textContent = "Prüfe …";
    try { const result = await api("/api/test-connection"); $("#test-result").textContent = result.message; }
    catch (error) { $("#test-result").textContent = error.message; }
  });
  document.addEventListener("keydown", event => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && !$("#diary-view").hidden) {
      event.preventDefault();
      $("#entry-form").requestSubmit();
    }
  });
  const initialGlucose = loadCurrentGlucose();
  try {
    await loadSettings();
    await Promise.all([loadDay(), initialGlucose]);
  } catch (error) {
    toast(error.message, true);
  }
  setInterval(() => { if (!document.hidden) loadCurrentGlucose(); }, 60000);
  // "Wirkt noch" and the suggestion age with the active insulin, so today's preview refreshes too.
  setInterval(() => { if (!document.hidden && state.day === todayDay()) updatePreview(); }, 5 * 60000);
  // A form left open keeps showing "jetzt" with the current clock, and an old manual time expires.
  setInterval(() => { if (!document.hidden) setDefaultMealTime({ preview: false }); }, 30000);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    setDefaultMealTime();
    loadCurrentGlucose();
  });
  window.addEventListener("focus", loadCurrentGlucose);
});
