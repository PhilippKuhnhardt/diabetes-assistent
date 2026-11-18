// The classic page logic, moving screen by screen into Preact components in web/ui/ (AGENTS.md).
// Shared helpers and the API call live in web/ui/lib.js.
import { html, render } from "./ui/html.js";
import "./ui/main.js";
import { AnalysisView } from "./ui/analysis.js";
import { DayReview, entryLabel, dayTypeSources, dayBounds } from "./ui/day.js";
import { Overview, Inbox, ActiveCard } from "./ui/today.js";
import { DoseHelp, sliderUnits, firstPart } from "./ui/dose.js";
import { glucosePoints } from "./ui/charts.js";
import { SettingsView, FORM_PAGES, zoneText, deviceZone, deviceZoneDiffers } from "./ui/settings.js";
import { names, sportNames, api, appUpdateWaiting, toast, num, sportChange, timeOnly, localInputTime, toInstant, trendSymbol, currentPosition } from "./ui/lib.js";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const state = { day: new Date().toLocaleDateString("sv-SE"), view: "diary", analysisTab: "werte", dayChosenAt: 0, settings: null, dayData: null, pendingMealId: null, pendingSportId: null, entryTimeTouched: false };
const views = new Set(["diary", "analysis", "settings"]);

// Where a carb entry was eaten; the place chip's name.
// What a carb entry was; the dish's name from the catalogue.

const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
// Nothing typed and no dialog open: a reload loses nothing (the URL keeps view and tab).
function entryFormIdle() {
  const form = $("#entry-form");
  return !["carbs", "insulin", "finger_mgdl", "notes", "duration_minutes"].some(name => form.elements[name]?.value) &&
    !$$("dialog").some(dialog => dialog.open);
}

// Called when the page comes back to the foreground: a waiting update reloads at once when
// nothing would be lost, otherwise the toast offers it.
function reloadForUpdate() {
  if (!appUpdateWaiting() || document.hidden) return false;
  if (entryFormIdle()) {
    window.location.reload();
    return true;
  }
  if (!state.updateOffered) {
    state.updateOffered = true;
    toast("Neue App-Version verfügbar", false, { label: "Neu laden", run: () => window.location.reload() });
  }
  return false;
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
      if (data.glucose_at) catchUpDay(new Date(data.glucose_at).getTime());
    } catch (error) {
      widget.dataset.status = "unavailable";
      $("#glucose-value").textContent = "—";
      $("#glucose-trend").textContent = "Trend nicht verfügbar";
      $("#glucose-meta").textContent = "CGM derzeit nicht verfügbar";
      $("#glucose-meta").hidden = false;
    }
    publishGlucose();
  })().finally(() => { glucoseRefresh = null; });
  return glucoseRefresh;
}

// A dose decision always needs the glucose in sight (user feedback 2026-11-18): on phones the top bar is
// one line (logo, value, inbox) and full-screen views open below it; the meal chat on wider screens is a
// modal over the dimmed page and shows a copy (web/ui/glucose.js).
function publishGlucose() {
  const stale = $("#glucose-meta").hidden ? "" : $("#glucose-meta").textContent;
  const glucose = { value: $("#glucose-value").textContent, status: $("#current-glucose").dataset.status,
    trend: stale || $("#glucose-trend").textContent };
  window.currentGlucose = glucose;
  document.dispatchEvent(new CustomEvent("glucose:update", { detail: glucose }));
}

// Full-screen views start below the top bar: its height as --topbar-h.
function trackTopbarHeight() {
  const bar = $(".topbar");
  const set = () => document.documentElement.style.setProperty("--topbar-h", bar.getBoundingClientRect().height + "px");
  set();
  if (window.ResizeObserver) new ResizeObserver(set).observe(bar);
}
document.addEventListener("DOMContentLoaded", trackTopbarHeight);

function validDay(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || "")) return false;
  const [year, month, day] = value.split("-").map(Number);
  const parsed = new Date(Date.UTC(year, month - 1, day));
  return parsed.getUTCFullYear() === year && parsed.getUTCMonth() === month - 1 && parsed.getUTCDate() === day;
}

// The URL carries only the view, never a day: an old link or a reloaded tab must not open an old day
// by accident (user request 2026-10-16). A chosen day lives in this page only; a reload, a new visit or
// DAY_HOLD_MS without touching the page brings back today.
const DAY_HOLD_MS = 30 * 60000;

function locationState() {
  const params = new URLSearchParams(window.location.search);
  const requestedView = params.get("view");
  return {
    view: views.has(requestedView) ? requestedView : "diary",
    analysisTab: ["automatik", "gerichte"].includes(params.get("tab")) ? params.get("tab") : "werte",
  };
}

function syncTabLinks(url = new URL(window.location.href)) {
  $$(".tab").forEach(link => {
    const linkURL = new URL(url);
    linkURL.searchParams.set("view", link.dataset.view);
    linkURL.searchParams.delete("tab");
    linkURL.searchParams.delete("date");
    link.href = linkURL.href;
  });
}

function updateLocation(replace = false) {
  const url = new URL(window.location.href);
  url.searchParams.set("view", state.view);
  url.searchParams.delete("date");
  if (state.view === "analysis" && state.analysisTab !== "werte") url.searchParams.set("tab", state.analysisTab);
  else url.searchParams.delete("tab");
  syncTabLinks(url);
  window.history[replace ? "replaceState" : "pushState"]({ view: state.view }, "", url);
}

function markDayActivity() {
  state.dayChosenAt = Date.now();
}

// Back to today once the page was left untouched for DAY_HOLD_MS while showing another day, which also
// covers a page left open past midnight. Never while someone is working on the page.
function returnToToday() {
  const today = todayDay();
  if (state.day === today || Date.now() - state.dayChosenAt < DAY_HOLD_MS) return;
  changeDay(today);
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
  if (name === "analysis") refreshAnalysis();
  if (changed) releaseEntryTime();
  if (name === "diary") setDefaultMealTime();
  if (name === "settings" && changed) showSettingsPage(null);
  if (changed) closeEntryDrawer();
  updateNav();
}

// Analysis (web/ui/analysis.js): mounted the first time it is opened; every opening, and every change
// elsewhere that it shows, fetches the summary again.
let analysisReload = 0;

function renderAnalysis() {
  if (!analysisReload) return;
  render(html`<${AnalysisView} tab=${state.analysisTab} onTab=${analysisTab} reloadKey=${analysisReload}
    getSettings=${() => state.settings} onSettings=${settings => { state.settings = settings; }}
    onSettingsChanged=${loadSettings} onOpenMeal=${openMealDay} />`, $("#analysis-view"));
}

function refreshAnalysis() {
  if (state.view !== "analysis") return;
  analysisReload += 1;
  renderAnalysis();
}

// Analysis sub-pages: values of the chosen period, how the Automatik would have done, the dishes.
function analysisTab(name, options = {}) {
  const changed = state.analysisTab !== name;
  state.analysisTab = name;
  renderAnalysis();
  if (changed && options.updateHistory !== false) updateLocation(true);
}

// A meal is corrected on its day: the analysis row opens "Tag durchgehen" at that meal.
async function openMealDay(day, mealId) {
  state.day = day;
  state.dayChosenAt = Date.now();
  state.modeDay = state.day;
  state.diaryMode = "review";
  $("#selected-day").value = state.day;
  setDefaultMealTime();
  view("diary");
  await loadDay();
  $("#meal-" + CSS.escape(mealId))?.scrollIntoView({ block: "start" });
}

function changeDay(day, options = {}) {
  if (!validDay(day) || day === state.day) return;
  resetEntryForm();
  state.day = day;
  state.dayChosenAt = Date.now();
  $("#selected-day").value = day;
  setDefaultMealTime();
  setDefaultSportTime();
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

function updateEntryDayType() {
  const form = $("#entry-form");
  const dayType = form.elements.kind.value === "meal" ? currentPreview()?.day_type : null;
  $("#entry-day-type").hidden = !dayType;
  updateEntryInfluences();
  if (!dayType) return;
  $$(".day-type-option", $("#entry-day-type")).forEach(button =>
    button.setAttribute("aria-pressed", String(button.dataset.dayType === dayType.value)));
  $("#entry-day-type-hint").textContent = (dayTypeSources[dayType.source] || "");
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

// The zone the app calculates with right now (home or the latest travel switch, user request 2026-11-07).
function appZone() {
  return state.travel?.current || state.settings?.timezone || "Europe/Berlin";
}

// The diary's timezone banner: shown while the device's clock differs from the app's.
function renderTimezone() {
  const travel = state.travel;
  const banner = $("#timezone-banner");
  const differs = deviceZoneDiffers(travel);
  banner.hidden = !differs;
  if (!differs) return;
  const device = deviceZone();
  banner.innerHTML = "Dein Gerät zeigt " + escapeHTML(zoneText(device)) + ", die App rechnet noch mit " +
    escapeHTML(zoneText(travel.current)) + " (Mahlzeiten, Korrekturfaktor, Warnzeiten). " +
    '<button type="button" class="banner-action" data-timezone-switch="' + escapeHTML(device) + '">Auf ' +
    escapeHTML(device) + " umstellen</button>";
}

// Switches the zone the app calculates with (settings or the banner); true when it worked.
async function switchTimezone(payload) {
  try {
    state.travel = await api("/api/timezone", { method: "POST", body: JSON.stringify(payload) });
    if (state.settingsData) state.settingsData = { ...state.settingsData, travel: state.travel };
    renderTimezone();
    renderSettings();
    toast(payload.undo ? "Zeitzonen-Wechsel zurückgenommen" : "Die App rechnet jetzt mit " + state.travel.current);
    if (todayDay() !== state.day) changeDay(todayDay()); else await loadDay();
    return true;
  } catch (error) {
    toast(error.message, true);
    return false;
  }
}

function setDefaultSportTime() {}
function setSportDuration() {}

// The server decides whether a new carb entry counts as the snack of the preceding main meal.
function updateEntryAssignment() {
  const chip = $("#entry-assign-chip");
  const form = $("#entry-form");
  const assignment = form.elements.kind.value === "meal" ? currentPreview()?.assignment : null;
  chip.hidden = !assignment;
  if (!assignment) {
    updateEntryClose(null);
    return;
  }
  const snack = assignment.meal ? "Snack zu " + names[assignment.meal] + " " + timeOnly(assignment.at) : "Snack ohne Mahlzeit";
  chip.textContent = "Zählt als: " + (assignment.snack ? snack : "eigenes " + names[assignment.own_meal])
    + (assignment.own_meal_taken ? " (" + names[assignment.own_meal] + " heute schon eingetragen)" : " ⇄");
  chip.disabled = Boolean(assignment.own_meal_taken);
  updateEntryClose(assignment);
}

// A snack after a main meal can end that meal here instead; the server offers it and sets the window on save.
function updateEntryClose(assignment) {
  const chip = $("#entry-close-chip");
  const close = assignment?.close;
  chip.hidden = !close;
  if (!close) return;
  chip.setAttribute("aria-pressed", String(Boolean(close.active)));
  chip.textContent = close.active
    ? names[close.meal] + " " + timeOnly(close.at) + " endet " + timeOnly(close.end) + " ✓"
    : names[close.meal] + " " + timeOnly(close.at) + " hier abschließen";
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
const CERTAINTY_SLIDER_LABELS = ["KH unsicher", "KH halbwegs", "KH sicher"];

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

function entryPlaceId() {
  if (!(Number($("#entry-form").elements.carbs.value) > 0)) return null;
  // "none" tells the server that no place was chosen on purpose (no place factor).
  if (state.entryPlace === "none") return "none";
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
  // A place with its own place factor changes the dose.
  schedulePreview(0);
}

function updateEntryQuality() {
  const form = $("#entry-form");
  // The manual entry ("KE & IE") shows its fields right away, not only once carbs are typed (user request 2026-11-18).
  const manual = $("#entry-drawer").dataset.quick === "manual";
  const hidden = !(form.elements.kind.value === "meal" && (manual || Number(form.elements.carbs.value) > 0));
  $("#entry-quality").hidden = hidden;
  $("#entry-factors").hidden = hidden;
}

function updateEntryOptions() {
  updateEntryAssignment();
  updateEntrySplit();
  updateEntryQuality();
  updateEntryPlace();
  updateEntryInfluences();
}

function updateEntryInfluences() {
  $("#entry-influences").hidden = $("#entry-day-type").hidden;
}

// A sport entry starts with its kind's setting; a typed value stays until the entry is saved.
function setSportEffectDefault() {
  const form = $("#entry-form");
  if (form.elements.sport_effect_mgdl.dataset.touched) return;
  const effects = state.settings?.sport_effects_mgdl || {};
  form.elements.sport_effect_mgdl.value = sportChange(effects[form.elements.sport_kind.value]);
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
  $("#entry-sport-effect-field").hidden = mode !== "exercise";
  form.elements.sport_effect_mgdl.disabled = mode !== "exercise";
  if (mode === "exercise") setSportEffectDefault();
  form.elements.carbs.disabled = mode !== "meal";
  form.elements.insulin.disabled = mode !== "meal";
  form.elements.sport_kind.disabled = mode !== "exercise";
  form.elements.duration_minutes.disabled = mode !== "exercise";
  form.elements.duration_minutes.required = mode === "exercise";
  updateEntryChips();
  renderPreview();
  schedulePreview(0);
}

// Settings (web/ui/settings.js) render from /api/settings; the rest of the page reads state.settings
// (dose defaults, sport effects) and state.travel (the zone the app calculates with).
let settingsLoads = 0;

async function loadSettings() {
  const data = await api("/api/settings");
  state.settings = data.settings;
  state.settingsData = data;
  state.travel = data.travel || null;
  settingsLoads += 1;
  $("#connection").textContent = data.connected ? "" : "Nightscout einrichten";
  $("#connection").hidden = data.connected;
  renderTimezone();
  renderSettings();
  await loadAgent();
}

function renderSettings() {
  if (!state.settingsData) return;
  render(html`<${SettingsView} data=${state.settingsData} loadCount=${settingsLoads} page=${state.settingsPage || null}
    onPage=${showSettingsPage} today=${todayDay()} agentInfo=${agent.info}
    onAgentInfo=${info => { agent.info = info; renderSettings(); updateEntryAgent(); }}
    onReload=${async ({ day }) => { await loadSettings(); if (day) await loadDay(); }}
    onSwitchZone=${switchTimezone}
    onVacation=${async vacation => { state.settingsData = { ...state.settingsData, vacation }; renderSettings(); await loadDay(); }}
    onPlacesChanged=${() => { if (state.dayData) loadDay(); schedulePreview(0); }}
    onProfileSaved=${refreshAnalysis} />`, $("#settings-view"));
}

// Overview above the entry form: today the last hours up to now, other days the whole day.
function overviewHours() {
  try { return Number(localStorage.getItem("overviewHours")) || 3; } catch { return 3; }
}

// The manual entry on phones covers "Heute", so it shows "Wirkt noch" itself (web/ui/today.js ActiveCard).
function renderEntryActive() {
  const host = $("#entry-active-host");
  if (!host) return;
  const show = $("#entry-drawer").dataset.quick === "manual" && state.day === todayDay() && state.preview?.on_board;
  render(show ? html`<${ActiveCard} id=${null} active=${state.preview.on_board} />` : null, host);
}

// "Heute" (web/ui/today.js): chart, "Wirkt noch" from the forecast, the last three hours.
function renderDayOverview() {
  renderEntryActive();
  render(html`<${Overview} data=${state.dayData} day=${state.day} today=${todayDay()} active=${state.preview?.on_board}
    hours=${overviewHours()} onEdit=${editEntry} onHours=${hours => { try { localStorage.setItem("overviewHours", hours); } catch {} renderDayOverview(); }} />`,
    $("#overview-host"));
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
  updateNav();
}

function renderVisibleCharts() {
  if (!state.dayData) return;
  renderDayOverview();
}

function renderActiveCard() {
  renderDayOverview();
}


function renderDay(data) {
  state.dayData = data;
  updateEntryChips();
  const banner = $("#banner");
  const errors = [data.sync_error, data.profile_sync_error].filter(Boolean);
  banner.hidden = data.connected && !errors.length;
  banner.textContent = !data.connected
    ? "Nightscout ist noch nicht verbunden. Setze NIGHTSCOUT_URL und NIGHTSCOUT_CREDENTIAL im Deployment."
    : errors.length ? "Nightscout konnte nicht vollständig aktualisiert werden: " + errors.join(" · ") + ". Lokale Daten werden angezeigt." : "";
  state.dayStatus = "ready";
  applyDiaryMode();
  renderReview();
  renderVisibleCharts();
}

// "Tag durchgehen" (web/ui/day.js): the day's chart, effects, night and list with the window editor.
function renderReview() {
  render(html`<${DayReview} data=${state.dayData} day=${state.day} status=${state.dayStatus} reload=${loadDay}
    onEdit=${editEntry} onAnalysisChanged=${refreshAnalysis} />`, $(".review-panel"));
}

// "Zu erledigen": what needs an action across the last days (user request 2026-10-21). The server decides what
// is open; an opened hint folds away, and a done one is no longer listed.
function renderInbox(data) {
  state.inbox = data;
  const fresh = data.items.filter(item => !item.seen);
  // The header names new hints on every page, since the list itself sits in the diary.
  const badge = $("#inbox-badge");
  badge.hidden = !fresh.length;
  // On phones only the number shows, so logo, glucose and inbox fit one line.
  badge.innerHTML = fresh.length + '<span class="inbox-badge-label"> zu erledigen</span>';
  badge.setAttribute("aria-label", fresh.length + " zu erledigen");
  render(html`<${Inbox} data=${data} today=${todayDay()} onOpen=${openInboxItem} />`, $("#inbox-host"));
}

async function loadInbox() {
  try {
    renderInbox(await api("/api/inbox"));
  } catch {
    // The hint list is extra; the diary works without it.
  }
}

// A hint opens "Tag durchgehen" at its meal and counts as seen from then on.
async function openInboxItem(item) {
  api("/api/inbox/seen", { method: "POST", body: JSON.stringify({ key: item.key }) })
    .then(renderInbox).catch(() => {});
  state.modeDay = item.day;
  state.diaryMode = "review";
  if (state.day !== item.day) changeDay(item.day, { load: false });
  else markDayActivity();
  applyDiaryMode();
  await loadDay();
  const meal = $("#meal-" + CSS.escape(item.meal_id));
  if (!meal) return;
  meal.scrollIntoView({ block: "start" });
  meal.classList.add("is-target");
  setTimeout(() => meal.classList.remove("is-target"), 2500);
}

// The server reads its local copy, which the background sync keeps current; refresh asks Nightscout first.
async function loadDay({ refresh = false } = {}) {
  state.dayLoadedAt = Date.now();
  $("#selected-day").value = state.day;
  // Another day shows "loading"; reloading the same day keeps the list (and what is unfolded) in place.
  if (state.dayData?.day !== state.day) {
    state.dayStatus = "loading";
    renderReview();
  }
  try {
    renderDay(await api("/api/day?date=" + encodeURIComponent(state.day) + (refresh ? "&refresh=1" : "")));
    loadInbox();
    await updatePreview();
  } catch (error) {
    state.dayStatus = "error";
    renderReview();
    toast(error.message, true);
  }
}

// The header reads Nightscout directly, the day the local copy. On the iPhone home screen the page
// stays alive for hours without a reload, so today's day follows on its own once the header is ahead
// of the chart. The copy catches up with each background round, hence the pause between attempts.
const DAY_CATCH_UP_MS = 2 * 60000;
let dayCatchUp = null;

function catchUpDay(latestAt) {
  const data = state.dayData;
  if (document.hidden || dayCatchUp || !data || state.day !== todayDay() || data.day !== state.day) return;
  if (Date.now() - (state.dayLoadedAt || 0) < DAY_CATCH_UP_MS) return;
  const points = glucosePoints(data.entries || []);
  if (points.length && points[points.length - 1].t >= latestAt) return;
  // Someone typing in the list keeps it as it is; the next attempt follows.
  if ($("#timeline").contains(document.activeElement)) return;
  const day = state.day;
  state.dayLoadedAt = Date.now();
  dayCatchUp = (async () => {
    try {
      const fresh = await api("/api/day?date=" + encodeURIComponent(day));
      if (day !== state.day || $("#timeline").contains(document.activeElement)) return;
      // Folded and unfolded blocks stay as the user left them.
      renderDay(fresh);
      updatePreview();
    } catch {
      // Quietly retried with the next glucose value.
    }
  })().finally(() => { dayCatchUp = null; });
}

let previewTimer;
let previewRequestId = 0;
let doseHelp = null;

function todayDay() {
  return new Date().toLocaleDateString("sv-SE", { timeZone: appZone() });
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
    close_meal: Boolean(state.closeMeal),
    // Saving re-checks the shown options with these, so the preview must send the same marks.
    carbs_certainty: Number(form.elements.carbs.value) > 0 ? certaintyValue(form) : null,
    fat_protein: Number(form.elements.carbs.value) > 0 && form.elements.fat_protein.checked,
    fast: Number(form.elements.carbs.value) > 0 && form.elements.fast.checked,
    // A position only rides along while the entry is "now" and only picks the chip; the server checks that again.
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
  updateEntryOptions();
  updateEntryDayType();
  updateEntryBasalOption();
  const dose = currentPreview()?.dose;
  doseHelp = dose?.options ? dose : null;
  $("#dose-result").removeAttribute("aria-busy");
  state.doseNote = dose?.message || "";
  state.doseChanged = doseHelp ? changedDoses(doseHelp, currentPreview().inputKey) : {};
  if (doseHelp?.slider) {
    // New input (other carbs, marks, place): the slider starts again at the recommendation.
    const key = currentPreview().inputKey;
    if (state.sliderKey !== key) {
      state.sliderKey = key;
      if (state.slider?.follow !== false) state.slider = null;
    }
    updateSliderView();
  }
  applyDoseChoice();
}

// The dose help (web/ui/dose.js) under the form; app.js keeps the choice and the slider position.
function renderDose() {
  render(html`<${DoseHelp} help=${doseHelp} note=${state.doseNote} changed=${state.doseChanged} choice=${state.doseChoice}
    chosen=${state.slider} onChoose=${selectDoseSuggestion} onSlide=${moveSlider} />`, $("#dose-result"));
}

// What the user typed, without the clock: a preview for the same input that shows other doses
// was recalculated from newer data (CGM, entries, profile), and those options are marked.
function previewInputKey(body) {
  return JSON.stringify({ ...body, at: state.entryTimeTouched ? body.at : null });
}

const doseShown = option => ({ units: option.units, later_units: option.later_units ?? null });
const sameDose = (a, b) => Number(a.units) === Number(b.units) &&
  (a.later_units == null ? b.later_units == null : b.later_units != null && Number(a.later_units) === Number(b.later_units));

// The doses seen for this input stay the reference until the input changes or an option is tapped.
function changedDoses(data, inputKey) {
  const shown = Object.fromEntries(data.options.map(option => [option.variant, doseShown(option)]));
  if (!state.doseSeen || state.doseSeen.key !== inputKey) {
    state.doseSeen = { key: inputKey, doses: shown };
    return {};
  }
  const changed = {};
  for (const [variant, dose] of Object.entries(shown)) {
    const before = state.doseSeen.doses[variant];
    if (before && !sameDose(before, dose)) changed[variant] = before;
  }
  return changed;
}

function acknowledgeDoses() {
  if (state.doseSeen && doseHelp) {
    state.doseSeen.doses = Object.fromEntries(doseHelp.options.map(option => [option.variant, doseShown(option)]));
  }
}

// Today's preview follows new data on its own; the minute tick and the return to the foreground call this.
function refreshTodayPreview() {
  if (document.hidden || state.day !== todayDay() || $("#entry-submit")?.disabled) return;
  updatePreview();
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
  const body = previewRequest();
  if (body.mode === "meal" && !doseHelp && !state.doseNote && body.day === todayDay()) {
    state.doseNote = "Empfehlung wird berechnet …";
    renderDose();
  }
  try {
    const data = await api("/api/entry/preview", { method: "POST", body: JSON.stringify(body) });
    if (requestId !== previewRequestId) return null;
    state.preview = { ...data, mode: body.mode, inputKey: previewInputKey(body), loadedAt: Date.now() };
    renderPreview();
    return state.preview;
  } catch (error) {
    if (requestId === previewRequestId) {
      state.preview = null;
      renderPreview();
      if (body.mode === "meal") {
        state.doseNote = error.message;
        renderDose();
      }
    }
    return null;
  }
}

// A chosen suggestion fills the insulin field and follows later recalculations until the field is edited.
function applyDoseChoice() {
  const form = $("#entry-form");
  const option = doseHelp?.options.find(item => item.variant === state.doseChoice);
  if (state.doseChoice && doseHelp && !option?.savable) state.doseChoice = null;
  if (state.doseChoice && option) form.elements.insulin.value = String(Number(option.units));
  renderDose();
  updateEntrySubmitLabel();
}

// A tapped suggestion saves carbs and that dose directly in Nightscout; the toast offers undo.
function selectDoseSuggestion(variant) {
  if (!doseHelp) return;
  const form = $("#entry-form");
  if ($("#entry-submit").disabled) return;
  state.doseChoice = variant;
  acknowledgeDoses();
  form.elements.carbs.setCustomValidity("");
  applyDoseChoice();
  if (form.elements.insulin.value === "") return;
  form.requestSubmit($("#entry-submit"));
}

// The slider's dose fills the insulin field (with a split: the part given now) while it follows the slider.
function updateSliderView(fillField = true) {
  renderDose();
  const slider = doseHelp?.slider;
  if (!slider || !fillField || state.slider?.follow === false) return;
  const units = sliderUnits(slider, state.slider, false);
  $("#entry-form").elements.insulin.value = String(firstPart(slider, units) ?? units);
  updateEntrySubmitLabel();
}

function moveSlider(units) {
  state.slider = { units, follow: true };
  state.doseChoice = null;
  $("#entry-form").elements.carbs.setCustomValidity("");
  updateSliderView();
}

function closeEditDialog() {
  state.editing = null;
  window.entryCard?.close();
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
  // A saved entry changes what still acts: the next suggestions are new, not "changed" (user report 2026-11-15).
  state.doseSeen = null;
  state.slider = null;
  state.sliderKey = null;
  state.entryAs = null;
  state.split = null;
  state.closeMeal = false;
  state.entryPlace = undefined;
  state.entryAgent = null;
  updateEntryAgent();
  closeEntryDrawer();
  delete form.elements.sport_effect_mgdl.dataset.touched;
  setDefaultMealTime();
  updateEntryFields();
  if ($("#edit-dialog").open) closeEditDialog();
}

// A saved entry opens as a card (web/ui/entry-card.js); here what it needs from the day and how it saves.
function editEntry(id) {
  const item = state.dayData?.timeline.find(row => row.id === id);
  if (!item || item.kind === "other" || item.kind === "finger") return;
  if (!window.entryCard) { toast("Seite neu laden, die Karte ist noch nicht geladen", true); return; }
  state.editing = item;
  const meal = item.kind === "meal" ? state.dayData.meals.find(row => row.source_id === item.id) : null;
  const factor = meal?.calculation?.factor_units_per_ke;
  const sportDefaults = Object.fromEntries(Object.keys(sportNames).map(kind => [kind, {
    effect: state.settings?.sport_effects_mgdl?.[kind] ?? 0,
    minutes: state.settings?.sport_durations_minutes?.[kind] ?? 300 }]));
  // The session's own default (from the day) wins for its current kind.
  if (item.kind === "exercise" && item.sport_kind) sportDefaults[item.sport_kind] = {
    effect: item.sport_effect_default_mgdl, minutes: item.sport_minutes_default };
  window.entryCard.open({
    item: { ...item, time: timeOnly(item.at), title: entryLabel(item, state.dayData) },
    dateLabel: new Date(state.day + "T12:00:00").toLocaleDateString("de-DE", { weekday: "short", day: "numeric", month: "long" }) +
      " · " + timeOnly(item.at),
    factor: factor == null ? null : Number(factor),
    places: state.dayData.place_options || [], dishes: state.dayData.dish_options || [],
    sportDefaults, agentInfo: agent.info, api, toast,
    instant: time => { try { return toInstant(state.day, time); } catch (_) { return null; } },
    onSave: values => saveEditedEntry(item, values),
    onDelete: () => deleteEditedEntry(item),
    onClosed: () => { state.editing = null; },
  });
}

async function saveEditedEntry(item, values) {
  const at = toInstant(state.day, values.at);
  if (item.kind === "basal") {
    if (!(values.units > 0 && values.units <= 100 && Number.isInteger(values.units * 2)))
      throw new Error("Bitte Basal zwischen 0,5 und 100 IE in 0,5er-Schritten eintragen.");
    // A new dose replaces the old one, so Nightscout gets the changed note as well.
    const result = await saveBasal(at, values.units, values.notes);
    try { await deleteBasal(item.id); }
    catch (error) { await deleteBasal(result.id).catch(() => {}); throw error; }
    return finishEdit(result.day, "Basal gespeichert");
  }
  const sport = item.kind === "exercise";
  const { carbs, insulin } = sport ? { carbs: 0, insulin: 0 } : values;
  if (!sport && carbs <= 0 && insulin <= 0) throw new Error("Bitte Kohlenhydrate, Insulin oder beides eintragen.");
  if (sport && !(values.duration >= 1 && values.duration <= 1440)) throw new Error("Bitte eine Dauer zwischen 1 und 1440 Minuten eintragen.");
  if (sport && values.effect != null && !(Number.isInteger(values.effect) && Math.abs(values.effect) <= 500))
    throw new Error("Bitte eine BZ-Wirkung zwischen −500 und +500 mg/dL eintragen.");
  if (sport && values.minutes != null && !(values.minutes >= 30 && values.minutes <= 1440))
    throw new Error("Die Wirkdauer muss zwischen 0,5 und 24 h liegen.");
  const kind = sport ? "exercise" : carbs > 0
    ? (item.kind === "snack" ? "snack" : item.kind === "carbs" && insulin === 0 ? "carbs" : "meal")
    : "correction";
  const payload = {
    id: item.id, original_day: state.day, kind, at, carbs, insulin,
    sport_kind: values.sportKind, duration_minutes: sport ? values.duration : 0, notes: values.notes,
    carbs_certainty: carbs > 0 ? values.certainty ?? item.carbs_certainty ?? null : null,
    fat_protein: carbs > 0 && values.fat,
    // No chip any more (user request 2026-11-15); an old mark stays when the entry is edited.
    alcohol: carbs > 0 && Boolean(item.alcohol),
    fast: carbs > 0 && values.fast,
    ...(carbs > 0 ? { place_id: values.place || null, dish_id: null, dish_name: values.dish.trim(), description: values.description } : {}),
    ...(values.session ? { agent_session: values.session } : {}),
  };
  // The session's own effect and duration first: right after the Nightscout update the day's
  // cached copy may not hold the session yet.
  if (sport) {
    const changes = {};
    if (values.effect == null ? item.sport_effect_custom : !item.sport_effect_custom || sportChange(item.sport_effect_mgdl) !== values.effect)
      Object.assign(changes, values.effect == null ? { reset: true } : { effect_mgdl: sportChange(values.effect) });
    if ((values.minutes ?? null) !== (item.sport_minutes_custom ? item.sport_minutes : null)) changes.minutes = values.minutes;
    if (Object.keys(changes).length)
      await api("/api/analysis/sport-session", { method: "POST", body: JSON.stringify({ id: item.id, day: state.day, ...changes }) });
  }
  const result = await api("/api/treatment", { method: "POST", body: JSON.stringify(payload) });
  const refreshed = await api("/api/day?date=" + encodeURIComponent(result.day));
  const saved = refreshed.timeline.find(row => row.id === item.id);
  if (!saved || Number(saved.carbs) !== carbs || Number(saved.insulin) !== insulin)
    throw new Error("Die Änderung ist in Nightscout noch nicht sichtbar. Bitte Eintrag aktualisieren.");
  return finishEdit(result.day, "Änderung in Nightscout gespeichert", refreshed);
}

async function finishEdit(day, message, refreshed = null) {
  state.day = day;
  $("#selected-day").value = state.day;
  updateLocation(true);
  if (refreshed) renderDay(refreshed);
  else await loadDay();
  await updatePreview();
  refreshAnalysis();
  closeEditDialog();
  toast(message);
}

async function deleteEditedEntry(item) {
  if (item.kind === "basal") {
    await deleteBasal(item.id);
    toast("Basal gelöscht");
  } else {
    await api("/api/treatment/delete", { method: "POST", body: JSON.stringify({ id: item.id, day: state.day }) });
    toast("Eintrag aus Nightscout gelöscht");
  }
  closeEditDialog();
  await loadDay();
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
  const chosenOption = mode === "meal" && state.doseChoice && doseHelp
    ? doseHelp.options.find(option => option.variant === state.doseChoice) : null;
  const chosen = chosenOption?.units ?? null;
  const payload = {
    ...previewRequest(), at,
    carbs: form.elements.carbs.value, insulin: form.elements.insulin.value,
    with_basal: basalUnits > 0,
    sport_kind: form.elements.sport_kind.value,
    // Only a changed value is stored on the session; the default keeps following the setting.
    sport_effect_mgdl: mode === "exercise" && form.elements.sport_effect_mgdl.value !== ""
      && Number(form.elements.sport_effect_mgdl.value) !== sportChange(state.settings?.sport_effects_mgdl?.[form.elements.sport_kind.value])
      ? form.elements.sport_effect_mgdl.value : null,
    duration_minutes: form.elements.duration_minutes.value || 0,
    notes: form.elements.notes.value,
    carbs_certainty: carbs > 0 ? certaintyValue(form) : null,
    fat_protein: carbs > 0 && form.elements.fat_protein.checked,
    fast: carbs > 0 && form.elements.fast.checked,
    event_id: state.pendingEventId || crypto.randomUUID(),
    basal_event_id: basalUnits > 0 ? state.pendingBasalId || crypto.randomUUID() : null,
    dose_check: chosen != null && Number(chosen) === insulin ? doseHelp.check : null,
    dose_variant: state.doseChoice || null,
    dose_shown: chosenOption && Number(chosenOption.units) === insulin ? doseShown(chosenOption) : null,
    // A split meal dosed on the slider: the total, of which the insulin field holds the first part.
    dose_total_units: mode === "meal" && doseHelp?.slider?.first_percent != null && state.slider?.follow !== false
      ? sliderUnits(doseHelp.slider, state.slider) : null,
    // What the agent's estimate named; the carbs themselves are whatever the form holds now.
    ...(mode === "meal" && carbs > 0 && state.entryAgent ? {
      dish_name: state.entryAgent.dish_name, description: state.entryAgent.description,
      agent_session: state.entryAgent.session_id } : {}),
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
      if (error.code === "dose_changed") {
        // The tapped dose is no longer current: the new one is shown marked and has to be tapped again,
        // so an old dose cannot be confirmed by accident.
        state.doseChoice = null;
        if (Number(form.elements.insulin.value) === Number(chosen)) form.elements.insulin.value = "";
        await updatePreview();
        $("#dose-result").scrollIntoView({ block: "nearest", behavior: "smooth" });
      } else if (payload.dose_check) await updatePreview();
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
      (basal && !basal.nightscout_synced ? " · Basal vorerst nur lokal" : "") +
      (result.basal_error ? " · Basal nicht gespeichert: " + result.basal_error : "");
    const undo = async () => {
      if (basal) await deleteBasal(basal.id).catch(error => toast(error.message, true));
      if (result.id) await undoEntry(result.id, restore);
      else if (basal) await loadDay();
    };
    toast(message, Boolean(result.basal_error),
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

document.addEventListener("DOMContentLoaded", async () => {
  const initialLocation = locationState();
  state.day = todayDay();
  state.view = initialLocation.view;
  analysisTab(initialLocation.analysisTab, { updateHistory: false });
  $("#selected-day").value = state.day;
  setDefaultMealTime();
  setDefaultSportTime();
  setSportDuration();
  view(state.view, { updateHistory: false });
  updateLocation(true);
  $$(".tab").forEach(link => link.addEventListener("click", event => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    view(link.dataset.view);
  }));
  window.addEventListener("popstate", () => {
    const nextLocation = locationState();
    analysisTab(nextLocation.analysisTab, { updateHistory: false });
    syncTabLinks();
    view(nextLocation.view, { updateHistory: false });
    if (nextLocation.view === "diary") loadDay();
  });
  $("#prev-day").addEventListener("click", () => { const d = new Date(state.day + "T12:00"); d.setDate(d.getDate() - 1); changeDay(d.toLocaleDateString("sv-SE")); });
  $("#next-day").addEventListener("click", () => { const d = new Date(state.day + "T12:00"); d.setDate(d.getDate() + 1); changeDay(d.toLocaleDateString("sv-SE")); });
  $("#selected-day").addEventListener("change", event => changeDay(event.target.value));
  $("#refresh-day").addEventListener("click", () => loadDay({ refresh: true }));
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
  $("#entry-close-chip").addEventListener("click", () => {
    state.closeMeal = !currentPreview()?.assignment?.close?.active;
    schedulePreview(0);
  });
  $("#entry-split-chip").addEventListener("click", () => {
    state.split = !currentPreview()?.split?.active;
    schedulePreview(0);
  });
  $("#entry-form").elements.sport_effect_mgdl.addEventListener("input", event => { event.target.dataset.touched = "1"; });
  $("#entry-form").elements.sport_kind.addEventListener("change", setSportEffectDefault);
  // Carb certainty and the fat mark decide whether the lower target is offered.
  $("#entry-form").elements.certainty_level.addEventListener("change", () => schedulePreview(0));
  $("#entry-form").elements.fat_protein.addEventListener("change", () => schedulePreview(0));
  $$(".entry-mode").forEach(button => button.addEventListener("click", () => {
    $("#entry-kind").value = button.dataset.entryMode;
    updateEntryFields();
  }));
  $$("[data-diary-mode]").forEach(button => button.addEventListener("click", () => {
    state.modeDay = state.day;
    state.diaryMode = button.dataset.diaryMode;
    applyDiaryMode();
    renderVisibleCharts();
  }));
  $("#inbox-badge").addEventListener("click", () => {
    view("diary");
    $("#inbox")?.scrollIntoView({ block: "start" });
  });
  $("#entry-day-type").addEventListener("click", saveEntryDayType);
  $("#timezone-banner").addEventListener("click", event => {
    const button = event.target.closest("[data-timezone-switch]");
    if (button) switchTimezone({ zone: button.dataset.timezoneSwitch });
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
    schedulePreview(0);
  });
  $("#entry-form").elements.insulin.addEventListener("input", () => {
    $("#entry-form").elements.carbs.setCustomValidity("");
    state.doseChoice = null;
    // Own IE: the slider shows where they land (as a total without a split) and stops filling the field.
    const field = $("#entry-form").elements.insulin.value, own = Number(field);
    // An emptied field hands the dose back to the slider.
    state.slider = field === "" ? null
      : { units: doseHelp?.slider?.first_percent == null && own >= 0 ? own : state.slider?.units, follow: false };
    updateSliderView(false);
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
  updateEntryFields();
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
  // New CGM values arrive every minute and "Wirkt noch" ages, so today's suggestion follows on its own
  // and a tapped suggestion rarely meets a newer one on saving.
  setInterval(refreshTodayPreview, 60000);
  // A form left open keeps showing "jetzt" with the current clock, and an old manual time expires.
  setInterval(() => { if (!document.hidden) setDefaultMealTime({ preview: false }); }, 30000);
  setInterval(returnToToday, 60000);
  // A meal finishes on its own, so its hint can appear without a reload.
  setInterval(() => { if (!document.hidden) loadInbox(); }, 5 * 60000);
  document.addEventListener("pointerdown", markDayActivity, true);
  document.addEventListener("keydown", markDayActivity, true);
  // A home-screen app has no reload button: coming back to the foreground refreshes everything shown
  // and picks up a deployed update.
  const resume = async () => {
    if (document.hidden) return;
    if (reloadForUpdate()) return;
    renderTimezone();
    renderSettings();
    returnToToday();
    setDefaultMealTime({ preview: false });
    refreshTodayPreview();
    loadInbox();
    await loadCurrentGlucose();
    reloadForUpdate();
  };
  document.addEventListener("visibilitychange", resume);
  window.addEventListener("pageshow", event => { if (event.persisted) resume(); });
  window.addEventListener("focus", loadCurrentGlucose);
});

// ---- Meal agent (user request 2026-11-16): photos and a description in, carbs and a dish out. ----
// The agent only fills the form; the dose help calculates as always. One chat serves the entry form
// and the edit dialog ("Tag durchgehen", where it continues an earlier estimate: "Nachbessern").
const agent = { info: null };

async function loadAgent() {
  try { agent.info = await api("/api/agent"); } catch (_) { agent.info = null; }
  renderSettings();
  updateEntryAgent();
}

// The entry form's agent line: the button, and what an applied estimate will save with the entry.
function updateEntryAgent() {
  const form = $("#entry-form");
  const meal = form.elements.kind.value === "meal";
  $("#entry-agent-open").hidden = !meal;
  const applied = meal ? state.entryAgent : null;
  $("#entry-agent-summary").hidden = !applied;
  $("#entry-agent-clear").hidden = !applied;
  if (applied) $("#entry-agent-summary").textContent =
    (applied.dish_name ? "Gericht: " + applied.dish_name : "Ohne Gericht") + " · geschätzt " + applied.range;
}

// The chat itself lives in web/ui/agent-chat.js (Preact); here only what it needs from the entry
// form and where "übernehmen" writes. Nothing is saved yet. A saved meal opens the chat from its
// card ("Nachbessern", web/ui/entry-card.js).
function openAgent() {
  if (!agent.info?.available) {
    toast("Essens-Agent ist nicht eingerichtet: in den Einstellungen Anbieter und Modell wählen", true);
    return;
  }
  if (!window.mealChat) { toast("Seite neu laden, der Chat ist noch nicht geladen", true); return; }
  loadCurrentGlucose();
  const form = $("#entry-form");
  window.mealChat.open({
    target: "entry", info: agent.info, api, toast, previousCarbs: form.elements.carbs.value,
    context: () => {
      let at = null;
      try { at = toInstant(state.day, form.elements.at.value); } catch (_) { /* Without a valid time the server takes now. */ }
      const placeId = entryPlaceId();
      return { at, placeId: placeId === "none" ? null : placeId };
    },
    onApply: applyAgent,
  });
}

function applyAgent({ session, estimate, carbs, certainty, fat_protein, fast }) {
  const form = $("#entry-form");
  form.elements.carbs.value = carbs;
  setCertainty(form, certainty);
  form.elements.fat_protein.checked = fat_protein;
  form.elements.fast.checked = fast;
  state.entryAgent = { session_id: session, dish_name: estimate.dish?.name || "", description: estimate.description,
    range: estimate.carbs_low_g + "–" + estimate.carbs_high_g + " g" };
  updateEntryAgent();
  updateEntryOptions();
  requestEntryPosition();
  schedulePreview(0);
}

// ---- Navigation and quick entry (concept 2026-11-18) ----
// Phones and tablets: tab bar at the bottom (Heute, Verlauf, +, Analyse, Mehr), the entry as a
// full-screen sheet opened from "+", settings as one page at a time. Wide screens: a rail on the left,
// the entry stays in the page. The breakpoint matches the CSS.
const compactLayout = () => matchMedia("(max-width: 950px)").matches;
const QUICK_TITLES = { meal: "Essen", manual: "KE & IE", exercise: "Sport", basal: "Basal" };

function updateNav() {
  const current = state.view === "diary" ? (state.diaryMode === "review" ? "history" : "today") : state.view;
  $$(".appnav-item").forEach(link => {
    const active = link.dataset.nav === current;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
}

function navigate(target) {
  if (target === "today" || target === "history") {
    view("diary");
    if (target === "today" && state.day !== todayDay()) changeDay(todayDay());
    state.modeDay = state.day;
    state.diaryMode = target === "today" ? "now" : "review";
    applyDiaryMode();
    renderVisibleCharts();
    if (target === "history" && !compactLayout()) $(".review-panel")?.scrollIntoView({ behavior: "smooth", block: "start" });
    else window.scrollTo({ top: 0 });
  } else {
    view(target);
    window.scrollTo({ top: 0 });
  }
}

// Each choice says what it would do: the correction dose now, whether today's basal is still open.
function quickAddHints() {
  const today = state.day === todayDay();
  const hints = { meal: agent.info?.available ? "Foto, Sprache oder Text" : "KH eintragen", manual: "KH und Insulin, auch nur Korrektur",
    exercise: "Art, Dauer, Wirkung", basal: "" };
  const preview = state.preview;
  const suggested = today && preview && preview.day === state.day && preview.mode === "meal" && !(Number(preview.carbs_g) > 0)
    ? (preview.options || []).find(option => option.suggested) : null;
  if (suggested && Number(suggested.units) > 0) hints.manual = "Korrektur jetzt " + num(suggested.units, 1) + " IE empfohlen";
  if (today && state.dayData) {
    const doses = state.dayData.basal || [];
    hints.basal = doses.length ? "heute " + num(doses[doses.length - 1].units, 1) + " IE um " + timeOnly(doses[doses.length - 1].at) : "heute noch offen";
  }
  return hints;
}

function openQuickAdd() {
  if (!window.quickAdd) { toast("Seite neu laden, die Auswahl ist noch nicht geladen", true); return; }
  window.quickAdd.open({ hints: quickAddHints(), onChoose: chooseEntry });
}

function chooseEntry(kind) {
  if (state.view !== "diary") view("diary");
  const mode = kind === "exercise" || kind === "basal" ? kind : "meal";
  $('.entry-mode[data-entry-mode="' + mode + '"]')?.click();
  $("#entry-title").textContent = QUICK_TITLES[kind] || "Eintrag hinzufügen";
  // "KE & IE" is the manual entry: no "Essen einschätzen" there, and it shows what still acts (user feedback 2026-11-18).
  $("#entry-drawer").dataset.quick = kind;
  renderEntryActive();
  updateEntryQuality();
  openEntryDrawer();
  const form = $("#entry-form");
  if (kind === "meal" && agent.info?.available) openAgent();
  else if (kind === "meal" || kind === "manual") form.elements.carbs.focus();
  else if (kind === "exercise") form.elements.sport_kind.focus();
  else if (kind === "basal") form.elements.basal_units.focus();
}

function openEntryDrawer() {
  if (compactLayout()) {
    loadCurrentGlucose();
    document.body.classList.add("entry-open");
    $("#entry-drawer").scrollTop = 0;
  } else {
    $("#entry-drawer").scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function closeEntryDrawer() {
  document.body.classList.remove("entry-open");
  const title = $("#entry-title");
  if (title) title.textContent = "Eintrag hinzufügen";
  delete $("#entry-drawer")?.dataset.quick;
  renderEntryActive();
}

// Settings on phones: a list of pages, one page at a time (the CSS reads data-page and page-in-form).
function showSettingsPage(id) {
  const root = $("#settings-view");
  state.settingsPage = id;
  if (id) root.dataset.page = id;
  else delete root.dataset.page;
  root.classList.toggle("page-in-form", FORM_PAGES.has(id));
  renderSettings();
  window.scrollTo({ top: 0 });
}

document.addEventListener("DOMContentLoaded", () => {
  $$(".appnav-item").forEach(link => link.addEventListener("click", event => {
    event.preventDefault();
    navigate(link.dataset.nav);
  }));
  $("#nav-add").addEventListener("click", openQuickAdd);
  $("#entry-back").addEventListener("click", () => { closeEntryDrawer(); openQuickAdd(); });
  $("#entry-close").addEventListener("click", closeEntryDrawer);
  // Switching the kind by hand (wide screens) drops the title the "+" choice gave.
  $$(".entry-mode").forEach(button => button.addEventListener("click", event => {
    if (!event.isTrusted) return;
    $("#entry-title").textContent = "Eintrag hinzufügen";
    delete $("#entry-drawer").dataset.quick;
  }));
  updateNav();
});

// Tables become cards on phones (concept 2026-11-18, user report: tables are bad on mobile): every
// cell carries its column's name, which the phone layout shows above the value (styles.css).
function labelTables(root) {
  // Preact adds rows to a table that is already on the page, so a row counts for its table too.
  const own = root.closest?.(".analysis-table");
  const tables = own ? [own] : root.querySelectorAll?.(".analysis-table") || [];
  for (const table of tables) {
    const heads = [...table.querySelectorAll("thead th")].map(cell => cell.textContent.replace(/\s+/g, " ").trim());
    for (const row of table.querySelectorAll("tbody tr")) {
      let column = 0;
      for (const cell of row.children) {
        if (cell.tagName === "TD" && cell.colSpan === 1 && heads[column] && !cell.dataset.label) cell.dataset.label = heads[column];
        column += cell.colSpan || 1;
      }
    }
  }
}
new MutationObserver(records => {
  for (const record of records) for (const node of record.addedNodes) if (node.nodeType === 1) labelTables(node);
}).observe(document.documentElement, { childList: true, subtree: true });

document.addEventListener("DOMContentLoaded", () => {
  $("#entry-agent-open").addEventListener("click", openAgent);
  $("#entry-agent-clear").addEventListener("click", () => { state.entryAgent = null; updateEntryAgent(); });
  $$(".entry-mode").forEach(button => button.addEventListener("click", () => setTimeout(updateEntryAgent)));
});
