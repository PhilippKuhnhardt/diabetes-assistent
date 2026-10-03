"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const state = { day: new Date().toLocaleDateString("sv-SE"), view: "diary", persistDay: false, settings: null, dayData: null, pendingMealId: null, pendingSportId: null, entryTimeTouched: false };
const views = new Set(["diary", "analysis", "settings"]);
const names = { breakfast: "Frühstück", lunch: "Mittagessen", dinner: "Abendessen" };
const num = (value, digits = 2) => Number(value).toLocaleString("de-DE", { maximumFractionDigits: digits, minimumFractionDigits: 0 });
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
function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.toggle("error", error);
  node.classList.add("visible");
  clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => node.classList.remove("visible"), 4500);
}

const glucoseStatusLabels = {
  in_range: "Im Zielbereich", outside: "Außerhalb des Zielbereichs",
  low: "Unter 70", high: "Über 250", stale: "Messwert veraltet",
  no_target: "Zielbereich fehlt", unavailable: "Kein aktueller Messwert",
};
let glucoseRefresh = null;

function loadCurrentGlucose() {
  if (glucoseRefresh) return glucoseRefresh;
  glucoseRefresh = (async () => {
    const widget = $("#current-glucose");
    try {
      const data = await api("/api/glucose/current");
      const status = glucoseStatusLabels[data.status] ? data.status : "unavailable";
      widget.dataset.status = status;
      $("#glucose-value").textContent = data.glucose_mgdl == null
        ? "—" : num(data.glucose_mgdl, 1) + " mg/dL";
      const delta = data.trend_delta_mgdl;
      $("#glucose-trend").textContent = data.fresh && delta != null
        ? trendSymbol(data.trend_direction) + " " + (Number(delta) > 0 ? "+" : "") + num(delta, 0) +
          " mg/dL / " + num(data.trend_minutes, 0) + " min"
        : data.status === "stale" ? "Trend veraltet" : "Trend nicht verfügbar";
      const measured = data.glucose_at ? " · " + timeOnly(data.glucose_at) + " Uhr" : "";
      const target = data.target_low_mgdl != null && data.target_high_mgdl != null
        ? " · Ziel " + num(data.target_low_mgdl, 1) + "–" + num(data.target_high_mgdl, 1) + " mg/dL" : "";
      $("#glucose-meta").textContent = glucoseStatusLabels[status] + measured + target;
    } catch (error) {
      widget.dataset.status = "unavailable";
      $("#glucose-value").textContent = "—";
      $("#glucose-trend").textContent = "Trend nicht verfügbar";
      $("#glucose-meta").textContent = "CGM derzeit nicht verfügbar";
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
    day: urlDay || historyDay || new Date().toLocaleDateString("sv-SE"),
    persistDay: Boolean(urlDay || (historyDay && window.history.state?.persistDay)),
  };
}

function syncTabLinks(url = new URL(window.location.href)) {
  $$(".tab").forEach(link => {
    const linkURL = new URL(url);
    linkURL.searchParams.set("view", link.dataset.view);
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
  if (name === "diary") setDefaultMealTime();
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

function setDefaultMealTime() {
  if (state.entryTimeTouched) return;
  $("#entry-time").value = localInputTime(new Date()).slice(11, 16);
  updateEntrySportOption();
  updateEntryDayType();
  updateEntryChips();
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
  updateEntrySubmitLabel();
}

function updateEntrySubmitLabel() {
  const form = $("#entry-form");
  const button = $("#entry-submit");
  if (form.elements.kind.value === "exercise") {
    button.textContent = "Sport in Nightscout speichern";
    return;
  }
  const carbs = Number(form.elements.carbs.value);
  const insulin = Number(form.elements.insulin.value);
  const parts = [
    Number.isFinite(carbs) && carbs > 0 ? num(carbs, 0) + " g" : "",
    Number.isFinite(insulin) && insulin > 0 ? num(insulin, 2) + " IE" : "",
  ].filter(Boolean);
  button.textContent = parts.length ? parts.join(" + ") + " speichern" : "In Nightscout speichern";
}

const dayTypeSources = {
  weekday: "Standard nach Wochentag",
  vacation: "Urlaub ist aktiv",
  manual: "Für diesen Tag manuell gewählt",
};

function updateEntryDayType() {
  const form = $("#entry-form");
  const box = $("#entry-day-type");
  const time = clockMinute(form.elements.at.value);
  const breakfastEnd = Number(state.settings?.breakfast_end_minute ?? 660);
  const visible = form.elements.kind.value === "meal" && Boolean(state.dayData?.day_type) &&
    Number.isFinite(time) && time < breakfastEnd;
  box.hidden = !visible;
  if (!visible) return;
  $$(".day-type-option").forEach(button =>
    button.setAttribute("aria-pressed", String(button.dataset.dayType === state.dayData.day_type)));
  $("#entry-day-type-hint").textContent = (dayTypeSources[state.dayData.day_type_source] || "") +
    " · bestimmt den Frühstücksfaktor";
}

async function saveEntryDayType(event) {
  const button = event.target.closest(".day-type-option");
  if (!button || !state.dayData) return;
  const day = state.dayData.day;
  $$(".day-type-option").forEach(item => { item.disabled = true; });
  try {
    await api("/api/analysis/day-type", {
      method: "POST", body: JSON.stringify({ day, day_type: button.dataset.dayType }),
    });
    if (state.dayData?.day === day) {
      state.dayData.day_type = button.dataset.dayType;
      state.dayData.day_type_source = "manual";
    }
    updateEntryDayType();
    await updateDoseHelp();
  } catch (error) {
    toast(error.message, true);
  } finally {
    $$(".day-type-option").forEach(item => { item.disabled = false; });
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

function renderVacation(vacation) {
  $("#vacation-toggle").checked = Boolean(vacation?.active);
  $("#vacation-status").textContent = vacation?.active
    ? "Aktiv seit " + new Date(vacation.since + "T12:00").toLocaleDateString("de-DE") +
      ". Jeder Tag zählt als freier Tag, bis du Urlaub ausschaltest. Einzelne Tage lassen sich im Tagebuch weiterhin umstellen."
    : "Solange Urlaub aktiv ist, zählt jeder Tag als freier Tag. Ohne Urlaub gilt Montag bis Freitag als Arbeitstag.";
}

function setDefaultSportTime() {}
function setSportDuration() {}

function updateEntrySportOption() {
  const form = $("#entry-form");
  const option = $("#entry-sport-option");
  const effectField = $("#entry-sport-effect-field");
  const checkbox = form.elements.consider_sport;
  let eligible = [];
  let available = false;
  try {
    const at = new Date(toInstant(state.day, form.elements.at.value));
    const settings = state.settings;
    if (form.elements.kind.value === "meal" && Number(form.elements.carbs.value) > 0 && settings && state.dayData?.day === state.day) {
      const minute = clockMinute(form.elements.at.value);
      const meal = minute < settings.breakfast_end_minute ? "breakfast" : minute < settings.lunch_end_minute ? "lunch" : "dinner";
      const continuation = state.dayData.meals.some(item => item.meal === meal &&
        new Date(item.at) < at && at < new Date(item.calculation?.end ||
          new Date(item.at).getTime() + settings.window_minutes * 60000));
      if (!continuation) {
        available = true;
        eligible = state.dayData.treatments.filter(item => {
          if (item.eventType !== "Exercise" || !item.sport_type) return false;
          const end = new Date(item.at).getTime() + Number(item.duration || 0) * 60000;
          return end <= at.getTime() && end >= at.getTime() - settings.sport_lookback_minutes * 60000;
        });
      }
    }
  } catch (_) { /* The time field is incomplete. */ }
  const wasHidden = option.hidden;
  option.hidden = !available;
  if (!available) {
    checkbox.checked = false;
    effectField.hidden = true;
    if (!wasHidden) scheduleDoseHelp();
    return;
  }
  const suggested = eligible.reduce((total, item) => total + sportChange(state.settings.sport_effects_mgdl[item.sport_type]), 0);
  $("#entry-sport-hint").textContent = eligible.length ? eligible.map(item =>
    sportNames[item.sport_type] + " " + timeOnly(item.at)).join(", ") + " einrechnen · " + signedChange(suggested, 0)
    : "Sport-Wirkung schätzen";
  if (!checkbox.checked) form.elements.sport_effect_mgdl.value = eligible.length ? suggested : "";
  effectField.hidden = !checkbox.checked;
}

function entrySportChange() {
  const form = $("#entry-form");
  if (form.elements.kind.value !== "meal" || $("#entry-sport-option").hidden || !form.elements.consider_sport.checked) return 0;
  const input = form.elements.sport_effect_mgdl;
  const value = Number(input.value || 0);
  if (!Number.isFinite(value) || value < Number(input.min) || value > Number(input.max)) return null;
  // A field allowing negative values holds the signed BG change; otherwise it holds a drop.
  return Number(input.min) < 0 ? value : -value;
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
  $("#entry-sport-field").hidden = mode !== "exercise";
  $("#entry-duration-field").hidden = mode !== "exercise";
  form.elements.carbs.disabled = mode !== "meal";
  form.elements.insulin.disabled = mode !== "meal";
  form.elements.sport_kind.disabled = mode !== "exercise";
  form.elements.duration_minutes.disabled = mode !== "exercise";
  form.elements.duration_minutes.required = mode === "exercise";
  updateEntryChips();
  scheduleDoseHelp();
  updateEntrySportOption();
  updateEntryDayType();
}

async function loadSettings() {
  const data = await api("/api/settings");
  state.settings = data.settings;
  $("#connection").textContent = data.connected ? "Nightscout konfiguriert" : "Nightscout einrichten";
  $("#connection").classList.toggle("connected", data.connected);
  $("#nightscout-location").hidden = data.connected;
  $("#nightscout-location").textContent = data.connected ? "" : "NIGHTSCOUT_URL und NIGHTSCOUT_CREDENTIAL setzen";
  $("#settings-version").textContent = "Einstellungsversion " + data.version;
  $("#logout-form").hidden = !data.login_required;
  const form = $("#settings-form");
  form.elements.window_minutes.value = data.settings.window_minutes;
  form.elements.dose_target_mgdl.value = data.settings.dose_target_mgdl;
  form.elements.dose_cautious_target_mgdl.value = data.settings.dose_cautious_target_mgdl;
  form.elements.auto_percentile.value = data.settings.auto_percentile;
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
const RAPID_TYPES = new Set(["Meal Bolus", "Correction Bolus", "Snack Bolus", "Bolus"]);
// Mirrors the server-side limits for a manual meal window.
const WINDOW_LEAD = 120 * MINUTE;
const WINDOW_LAG = 120 * MINUTE;
const WINDOW_MIN = 30 * MINUTE;
const WINDOW_MAX = 600 * MINUTE;
const SNAP_PIXELS = 16;
let CHART = chartLayout(600);

// Chart units equal CSS pixels at the measured width, so text and touch targets keep their size.
function chartLayout(width) {
  const clamped = Math.max(300, Math.min(900, Math.round(width || 600)));
  return { width: clamped, left: 36, right: clamped - 12, plotTop: 70, plotBottom: 258 };
}
let windowEditor = null;

function localMinute(at) {
  const clock = new Date(at).toLocaleTimeString("de-DE", { timeZone: state.settings?.timezone || undefined, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  return clockMinute(clock);
}

function targetAt(at) {
  const minute = localMinute(at);
  const row = (state.settings?.target_schedule || [])
    .filter(item => Number(item.start_minute) <= minute)
    .sort((a, b) => Number(a.start_minute) - Number(b.start_minute)).pop();
  return row ? { low: Number(row.low_mgdl), high: Number(row.high_mgdl) } : null;
}

function glucoseTone(value, at) {
  if (value == null) return "none";
  if (value < 70) return "low";
  if (value > 250) return "high";
  const target = targetAt(at);
  if (!target) return "neutral";
  return value >= target.low && value <= target.high ? "in-range" : "outside";
}

function glucosePoints(entries) {
  return entries.map(point => ({ t: new Date(point.at).getTime(), g: Number(point.sgv) }))
    .filter(point => Number.isFinite(point.g)).sort((a, b) => a.t - b.t);
}

// Same rule as the server: linear interpolation, at most 15 minutes between readings.
function glucoseAt(points, t) {
  let before = null;
  for (const point of points) {
    if (point.t === t) return point.g;
    if (point.t > t) {
      if (!before || point.t - before.t > 15 * MINUTE) return null;
      return before.g + (point.g - before.g) * (t - before.t) / (point.t - before.t);
    }
    before = point;
  }
  return null;
}

const trendSymbols = { up_fast: "↑", up: "↗", flat: "→", down: "↘", down_fast: "↓" };

function trendSymbol(direction) {
  return trendSymbols[direction] || "";
}

// Same rule as app/glucose.py: least-squares slope over the last 15 minutes, as change per 15 minutes;
// below 5 mg/dL flat, from 30 mg/dL fast.
function trendDirection(delta) {
  if (Math.abs(delta) < 5) return "flat";
  if (delta > 0) return delta >= 30 ? "up_fast" : "up";
  return delta <= -30 ? "down_fast" : "down";
}

function trendArrow(points, t) {
  const current = glucoseAt(points, t);
  if (current == null) return "";
  const window = points.filter(point => point.t < t && point.t >= t - 16 * MINUTE);
  window.push({ t, g: current });
  if (window.length < 2 || window[window.length - 1].t - window[0].t < 5 * MINUTE) return "";
  const xs = window.map(point => (point.t - t) / MINUTE);
  const meanX = xs.reduce((sum, x) => sum + x, 0) / xs.length;
  const meanY = window.reduce((sum, point) => sum + point.g, 0) / window.length;
  let numerator = 0, denominator = 0;
  window.forEach((point, index) => {
    numerator += (xs[index] - meanX) * (point.g - meanY);
    denominator += (xs[index] - meanX) ** 2;
  });
  return trendSymbol(trendDirection(numerator / denominator * 15));
}

function mealWindow(meal) {
  const calc = meal.calculation || {};
  const fallback = new Date(meal.at).getTime() + Number(state.settings?.window_minutes || 180) * MINUTE;
  const time = value => value ? new Date(value).getTime() : null;
  return {
    mealAt: new Date(meal.at).getTime(),
    start: time(calc.start || calc.window_start) ?? new Date(meal.at).getTime(),
    end: time(calc.end || calc.window_end) ?? fallback,
    autoStart: time(calc.auto_start) ?? new Date(meal.at).getTime(),
    autoEnd: time(calc.auto_end) ?? fallback,
    manual: Boolean(calc.manual_window),
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
    if (Number(item.insulin) > 0) result.push({ kind: "insulin", id: item.id, t, amount: Number(item.insulin), rapid: RAPID_TYPES.has(item.eventType) || Boolean(item.role) });
    if (Number(item.carbs) > 0) result.push({ kind: "carbs", id: item.id, t, amount: Number(item.carbs) });
    return result;
  });
}

function chartView(range) {
  const from = Math.min(range.start, range.autoStart) - 45 * MINUTE;
  const to = Math.max(range.end, range.autoEnd, range.mealAt + 240 * MINUTE) + 75 * MINUTE;
  return {
    from: Math.max(from, range.mealAt - WINDOW_LEAD - 30 * MINUTE),
    to: Math.min(to, range.mealAt + WINDOW_MAX + 60 * MINUTE),
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
  const parts = [];
  for (let t = view.from; t < view.to; t += 15 * MINUTE) {
    const target = targetAt(t);
    const last = parts[parts.length - 1];
    if (last && target && last.low === target.low && last.high === target.high) last.to = t + 15 * MINUTE;
    else if (target) parts.push({ ...target, from: t, to: t + 15 * MINUTE });
  }
  return parts.map(part => {
    const high = Math.min(part.high, scales.top);
    const low = Math.max(part.low, scales.bottom);
    if (high <= low) return "";
    return '<rect class="target-band" x="' + scales.x(part.from).toFixed(1) + '" y="' + scales.y(high).toFixed(1) +
      '" width="' + (scales.x(Math.min(part.to, view.to)) - scales.x(part.from)).toFixed(1) + '" height="' + (scales.y(low) - scales.y(high)).toFixed(1) + '"/>';
  }).join("");
}

function handleSVG(name, t, points, scales, interactive) {
  const x = scales.x(t);
  const value = glucoseAt(points, t);
  const dotY = value == null ? CHART.plotBottom : scales.y(value);
  const label = name === "start" ? "Beginn" : "Ende";
  return '<g class="window-handle" data-handle="' + name + '" transform="translate(' + x.toFixed(1) + ' 0)"' +
    (interactive ? ' tabindex="0" role="slider" aria-label="' + label + ' des Zeitraums" aria-valuetext="' + escapeHTML(timeOnly(t)) + ' Uhr"' : '') + '>' +
    '<line class="handle-line" x1="0" y1="64" x2="0" y2="' + (interactive ? 290 : CHART.plotBottom) + '"/>' +
    '<circle class="handle-dot" data-tone="' + glucoseTone(value, t) + '" cx="0" cy="' + dotY.toFixed(1) + '" r="' + (interactive ? 7 : 4.5) + '"' + (value == null ? ' hidden' : '') + '/>' +
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
  const clipId = overview ? "overview-clip" : interactive ? "editor-clip" : "chart-clip-" + range.mealAt;
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
      ? (last ? '<circle class="handle-dot" data-tone="' + glucoseTone(last.g, last.t) + '" cx="' + scales.x(last.t).toFixed(1) + '" cy="' + scales.y(last.g).toFixed(1) + '" r="4.5"/>' : '')
      : handleSVG("start", range.start, points, scales, interactive) + handleSVG("end", range.end, points, scales, interactive)) +
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
    if (!meal) continue;
    CHART = chartLayout(node.clientWidth);
    node.innerHTML = windowChartSVG(mealWindow(meal), state.dayData.entries, state.dayData.treatments);
  }
}

// Overview above the entry form: today the last hours up to now, other days the whole day.
function overviewHours() {
  try { return Number(localStorage.getItem("overviewHours")) || 6; } catch { return 6; }
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
  $("#day-overview-note").textContent = (last
    ? "Letzter CGM-Wert: " + num(last.g, 0) + " mg/dL um " + timeOnly(last.t) + " Uhr."
    : "Keine CGM-Werte in diesem Zeitraum.") +
    (clipped ? " Werte vor Mitternacht stehen beim Vortag." : "");
}

// --- Interactive window editor -------------------------------------------

function windowBounds(handle) {
  const editor = windowEditor;
  if (handle === "start") return { min: Math.max(editor.mealAt - WINDOW_LEAD, editor.view.from), max: editor.mealAt + WINDOW_LAG };
  return { min: minWindowEnd(editor.start), max: Math.min(editor.start + WINDOW_MAX, editor.view.to) };
}

function minWindowEnd(start) {
  return Math.max(start, windowEditor.mealAt) + WINDOW_MIN;
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
  if (handle === "start") editor.end = Math.min(Math.max(editor.end, minWindowEnd(editor.start)), editor.start + WINDOW_MAX);
}

function editorSummary() {
  const editor = windowEditor;
  const included = editor.events.filter(event => windowIncludes(editor, event));
  const carbs = included.filter(event => event.kind === "carbs").reduce((sum, event) => sum + event.amount, 0);
  const insulin = included.filter(event => event.kind === "insulin" && event.rapid).reduce((sum, event) => sum + event.amount, 0);
  const g0 = glucoseAt(editor.points, editor.start);
  const g1 = glucoseAt(editor.points, editor.end);
  const minutes = Math.round((editor.end - editor.start) / MINUTE);
  const changed = editor.start !== editor.savedStart || editor.end !== editor.savedEnd;
  const isDefault = editor.start === editor.autoStart && editor.end === editor.autoEnd;
  const cell = (label, value, detail = "") => '<div><span>' + label + '</span><strong>' + value + '</strong>' + (detail ? '<small>' + detail + '</small>' : '') + '</div>';
  return '<div class="window-facts">' +
    cell("Beginn", escapeHTML(timeOnly(editor.start)), g0 == null ? "kein CGM-Wert" : escapeHTML(num(g0, 0)) + " mg/dL") +
    cell("Ende <small>(exkl.)</small>", escapeHTML(timeOnly(editor.end)), g1 == null ? "kein CGM-Wert" : escapeHTML(num(g1, 0)) + " mg/dL") +
    cell("ΔG", g0 == null || g1 == null ? "—" : escapeHTML(signedChange(g1 - g0, 0))) +
    cell("Dauer", escapeHTML(Math.floor(minutes / 60) + " h " + String(minutes % 60).padStart(2, "0") + " min")) +
    cell("KH im Fenster", escapeHTML(num(carbs, 0)) + " g") +
    cell("Schnelles Insulin", escapeHTML(num(insulin, 1)) + " IE") +
    '</div><p class="window-state">' +
    (isDefault ? "Automatischer Standardzeitraum" : "Manueller Zeitraum") +
    (changed ? " · <b>nicht gespeichert</b>" : "") +
    (g0 == null || g1 == null ? ' · <span class="window-warning">An Beginn und Ende wird ein CGM-Wert benötigt.</span>' : "") + '</p>';
}

function updateWindowReadout() {
  const editor = windowEditor;
  const t = editor[editor.active];
  const value = glucoseAt(editor.points, t);
  const snapped = editor.snapped[editor.active];
  const readout = $("#window-readout");
  readout.dataset.tone = glucoseTone(value, t);
  readout.innerHTML = '<span class="readout-label">' + (editor.active === "start" ? "Beginn" : "Ende") + ' · ' + escapeHTML(timeOnly(t)) + ' Uhr' +
    (snapped ? ' · eingerastet: ' + escapeHTML(snapped.label) : '') + '</span>' +
    '<span class="readout-value"><strong>' + (value == null ? "—" : escapeHTML(num(value, 0))) + '</strong>' +
    '<span class="readout-trend">' + escapeHTML(trendArrow(editor.points, t)) + '</span><small>mg/dL</small></span>';
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
    const value = glucoseAt(editor.points, t);
    group.setAttribute("transform", "translate(" + scales.x(t).toFixed(1) + " 0)");
    group.setAttribute("aria-valuetext", timeOnly(t) + " Uhr");
    group.classList.toggle("is-active", editor.active === name);
    group.classList.toggle("is-snapped", Boolean(editor.snapped[name]));
    const dot = $(".handle-dot", group);
    dot.toggleAttribute("hidden", value == null);
    dot.dataset.tone = glucoseTone(value, t);
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

function openWindowEditor(mealId) {
  const meal = state.dayData?.meals.find(item => item.id === mealId);
  if (!meal) return;
  const range = mealWindow(meal);
  windowEditor = {
    ...range, mealId, day: state.day, active: "end", snapped: {}, drag: null, tap: null,
    savedStart: range.start, savedEnd: range.end,
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
  if (!reset && !isDefault && (glucoseAt(editor.points, editor.start) == null || glucoseAt(editor.points, editor.end) == null)) {
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
    (valid ? escapeHTML(num(result.factor_units_per_ke, 2)) + ' <small>IE/KE</small>' : '—') +
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

function sportReviewHTML(sport, mealId, day, compact = false) {
  const applied = sport.status === "affected";
  const effect = applied ? sportChange(sport.effect_mgdl) : 0;
  const suggestion = sport.suggested ? sportChange(sport.suggested_effect_mgdl) : 0;
  return '<div class="sport-review' + (compact ? ' sport-review-table' : '') + '" data-meal-id="' + escapeHTML(mealId) +
    '" data-day="' + escapeHTML(day) + '" data-suggestion="' + escapeHTML(suggestion) + '">' +
    '<div class="sport-state' + (applied ? ' is-applied' : '') + '">' +
    (applied ? icon("check") + '<span>Sport ' + escapeHTML(signedChange(effect, 0)) + '</span>' : '<span>Kein Sporteffekt</span>') + '</div>' +
    (applied ? '<button type="button" class="sport-remove icon-button" aria-label="Sporteinfluss entfernen" title="Sporteinfluss entfernen">' + icon("remove") + '</button>' : '') +
    (!applied && suggestion ? '<button type="button" class="sport-suggestion">' + icon("plus") + ' Vorschlag ' + escapeHTML(signedChange(suggestion, 0)) + ' übernehmen</button>' : '') +
    '<details class="sport-custom"><summary>' + (applied ? 'Wert ändern' : 'Eigenen Wert setzen') + '</summary>' +
    '<div class="sport-custom-fields"><label><span>BZ-Wirkung in mg/dL (− senkt, + hebt)</span><input class="sport-effect" type="number" min="-500" max="500" step="1" value="' +
    escapeHTML(applied ? effect : (suggestion || "")) +
    '"></label><button type="button" class="sport-save">' + (applied ? 'Ändern' : 'Setzen') + '</button></div></details></div>';
}

function mealHTML(meal, entries) {
  return '<article class="meal-card">' +
    '<div class="meal-top"><div><span class="meal-type">' + escapeHTML(names[meal.meal]) +
    '</span><h3>' + escapeHTML(timeOnly(meal.at)) + ' <small>· ' + escapeHTML(num(meal.carbs)) + ' g KH</small></h3></div></div>' +
    sportReviewHTML(meal.sport || {}, meal.id, state.day) +
    calculationHTML(meal.calculation) + chartFor(meal, entries) +
    (meal.notes ? '<p class="meal-note">' + escapeHTML(meal.notes) + '</p>' : '') +
    '</article>';
}

function deleteButton(id, label, at) {
  if (!id) return "";
  return '<button type="button" class="delete-treatment" data-id="' + escapeHTML(id) +
    '" data-label="' + escapeHTML(label) + '" data-at="' + escapeHTML(at) +
    '" aria-label="' + escapeHTML(label + " um " + timeOnly(at) + " löschen") + '">Löschen</button>';
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
  const reason = $("#night-reason");
  reason.hidden = !night.reason;
  reason.textContent = night.reason || "";
}

function renderDay(data) {
  state.dayData = data;
  updateEntrySportOption();
  updateEntryDayType();
  updateEntryChips();
  $("#record-count").textContent = data.timeline.length + (data.timeline.length === 1 ? " Eintrag" : " Einträge");
  renderNight(data.night || { reason: "Nachtanalyse nicht verfügbar." });
  const banner = $("#banner");
  const errors = [data.sync_error, data.profile_sync_error].filter(Boolean);
  banner.hidden = data.connected && !errors.length;
  banner.textContent = !data.connected
    ? "Nightscout ist noch nicht verbunden. Setze NIGHTSCOUT_URL und NIGHTSCOUT_CREDENTIAL im Deployment."
    : errors.length ? "Nightscout konnte nicht vollständig aktualisiert werden: " + errors.join(" · ") + ". Lokale Daten werden angezeigt." : "";
  const labels = { meal: "Mahlzeit", snack: "Snack", carbs: "Kohlenhydrate", correction: "Korrektur / Bolus", exercise: "Sport", other: "Weiterer Eintrag" };
  $("#timeline").innerHTML = data.timeline.length
    ? data.timeline.map(item => {
      const details = [
        Number(item.carbs) ? num(item.carbs) + " g KH" : "",
        Number(item.insulin) ? num(item.insulin) + " IE" : "",
        item.kind === "exercise" ? num(item.duration_minutes, 0) + " min" : "",
        item.glucose_mgdl != null
          ? (item.glucose_at === item.at ? "BZ beim Eintrag " : "Letzter BZ um " + timeOnly(item.glucose_at) + ": ") +
            num(item.glucose_mgdl, 0) + " mg/dL" : "",
      ].filter(Boolean).join(" · ");
      const label = item.kind === "exercise"
        ? sportNames[item.sport_kind] || "Sport"
        : item.kind === "meal" ? (data.meals.find(meal => meal.source_id === item.id)?.meal
          ? names[data.meals.find(meal => meal.source_id === item.id).meal] : "Mahlzeit") : labels[item.kind];
      const actions = item.kind === "other" ? "" :
        '<button type="button" class="edit-entry quiet" data-id="' + escapeHTML(item.id) + '">Bearbeiten</button>' +
        deleteButton(item.id, label, item.at);
      return '<article class="timeline-item"><time>' + escapeHTML(timeOnly(item.at)) +
        '</time><div><strong>' + escapeHTML(label) + '</strong><p>' + escapeHTML(details || item.eventType) +
        '</p>' + (item.notes ? '<small>' + escapeHTML(item.notes) + '</small>' : '') +
        '</div><div class="timeline-actions">' + actions + '</div></article>';
    }).join("")
    : '<div class="empty">Für diesen Tag liegen noch keine Einträge vor.</div>';
  renderDayOverview();
  $("#meals").innerHTML = data.meals.length
    ? data.meals.map(meal => mealHTML(meal, data.entries)).join("")
    : '<div class="empty">Noch keine Hauptmahlzeit für diesen Tag.</div>';
  renderMealCharts();
}

async function loadDay() {
  $("#selected-day").value = state.day;
  $("#timeline").innerHTML = '<div class="empty">Nightscout-Daten werden geladen …</div>';
  $("#night-card").hidden = true;
  try {
    renderDay(await api("/api/day?date=" + encodeURIComponent(state.day)));
    await updateDoseHelp();
  } catch (error) {
    $("#timeline").innerHTML = '<div class="empty">Daten konnten nicht geladen werden.</div>';
    toast(error.message, true);
  }
}

let helpTimer;
let helpRequestId = 0;
let doseHelp = null;

function todayDay() {
  return new Date().toLocaleDateString("sv-SE", { timeZone: state.settings?.timezone || "Europe/Berlin" });
}

// Empty carbohydrates mean a correction without food; invalid input yields null.
function entryCarbs() {
  const raw = $("#entry-form").elements.carbs.value;
  if (raw === "") return 0;
  const carbs = Number(raw);
  return Number.isFinite(carbs) && carbs >= 0 && carbs <= 500 ? carbs : null;
}

function scheduleDoseHelp() {
  clearTimeout(helpTimer);
  helpRequestId++;
  doseHelp = null;
  const form = $("#entry-form");
  const output = $("#dose-result");
  if (form.elements.kind.value !== "meal" || entryCarbs() == null) {
    output.textContent = "";
    return;
  }
  output.setAttribute("aria-busy", "true");
  helpTimer = setTimeout(updateDoseHelp, 350);
}

const doseVariants = {
  normal: data => data.entry_units,
  cautious: data => data.cautious_entry_units,
  auto: data => data.auto_entry_units,
};

function doseUnits(data, variant) {
  return data && doseVariants[variant] ? doseVariants[variant](data) : null;
}

// A tapped suggestion fills the insulin field and follows later recalculations until the field is edited.
function applyDoseChoice() {
  const form = $("#entry-form");
  const units = doseUnits(doseHelp, state.doseChoice);
  if (state.doseChoice && doseHelp && (units == null || Number(units) > 100)) state.doseChoice = null;
  if (state.doseChoice && units != null) form.elements.insulin.value = String(Number(units));
  $$(".apply-dose").forEach(button =>
    button.setAttribute("aria-pressed", String(button.dataset.variant === state.doseChoice)));
  updateEntrySubmitLabel();
}

function selectDoseSuggestion(event) {
  const button = event.target.closest(".apply-dose");
  if (!button || !doseHelp) return;
  const form = $("#entry-form");
  if (state.doseChoice === button.dataset.variant) {
    if (Number(form.elements.insulin.value) === Number(doseUnits(doseHelp, state.doseChoice))) form.elements.insulin.value = "";
    state.doseChoice = null;
  } else {
    state.doseChoice = button.dataset.variant;
  }
  form.elements.carbs.setCustomValidity("");
  applyDoseChoice();
}

const doseKeys = ["carbs_g", "glucose_at", "glucose_mgdl", "isf_mgdl_per_unit", "target_mgdl",
  "cautious_target_mgdl", "units_per_ke", "factor_source", "day_type", "activity_change_mgdl", "total_units",
  "cautious_total_units", "entry_units", "cautious_entry_units", "expected_glucose_mgdl", "cautious_expected_glucose_mgdl",
  "auto_units_per_ke", "auto_total_units", "auto_entry_units", "auto_expected_glucose_mgdl"];

function doseChanged(previous, current) {
  return doseKeys.some(key => previous[key] !== current[key]) ||
    JSON.stringify(previous.recent_rapid_doses) !== JSON.stringify(current.recent_rapid_doses);
}

function trendText(data) {
  if (data.trend_delta_mgdl == null) return "";
  const delta = Number(data.trend_delta_mgdl);
  return (delta > 0 ? "↗ " : delta < 0 ? "↘ " : "→ ") + signedChange(data.trend_delta_mgdl, 0) + " in " + num(data.trend_minutes, 0) + " min";
}

function doseHelpHTML(data) {
  const withCarbs = Number(data.carbs_g) > 0;
  const glucose = data.glucose_mgdl == null ? "Kein CGM-Wert" :
    num(data.glucose_mgdl, 0) + " mg/dL um " + timeOnly(data.glucose_at) + (data.trend_delta_mgdl == null ? "" : " · " + trendText(data));
  const option = (label, variant, target, units, expected) => units == null ? "" :
    '<button type="button" class="dose-option apply-dose" data-variant="' + variant + '" aria-pressed="false"' +
    (Number(units) > 100 ? ' disabled' : '') + '><span class="dose-label">' + escapeHTML(label) + '</span><strong>' +
    escapeHTML(num(units, 2)) + ' <small>IE</small></strong><span class="dose-expected">Ziel ' + escapeHTML(num(target, 0)) +
    ' → ' + escapeHTML(num(expected, 0)) + '</span></button>';
  const autoMissing = withCarbs && data.entry_units != null && data.auto_entry_units == null && data.auto_factor_min_meals
    ? '<div class="dose-option is-off"><span class="dose-label">Automatik</span><strong>–</strong><span class="dose-expected">' +
      escapeHTML((data.auto_factor_count || 0) + " von " + data.auto_factor_min_meals + " Mahlzeiten") + '</span></div>' : "";
  const options = option("Normal", "normal", data.target_mgdl, data.entry_units, data.expected_glucose_mgdl) +
    option("Vorsichtig", "cautious", data.cautious_target_mgdl, data.cautious_entry_units, data.cautious_expected_glucose_mgdl) +
    (withCarbs ? option("Automatik", "auto", data.target_mgdl, data.auto_entry_units, data.auto_expected_glucose_mgdl) || autoMissing : "");
  const exact = [["Normal", data.total_units], ["Vorsichtig", data.cautious_total_units], ["Automatik", withCarbs ? data.auto_total_units : null]]
    .filter(([, value]) => value != null).map(([label, value]) => label + " " + num(value, 2) + " IE");
  const details = [
    exact.length ? "Berechnet: " + exact.join(" · ") + ", auf 0,5 IE gerundet. Pfeil: erwarteter BG laut Modell." : "",
    data.units_per_ke != null ? "Insulinfaktor " + num(data.units_per_ke, 2) + " IE/KE · Korrekturfaktor " + num(data.isf_mgdl_per_unit, 1) + " mg/dL je IE." : "",
    data.units_per_ke != null && data.breakfast ? breakfastFactorNote(data) : "",
    withCarbs && data.carb_units != null ? autoFactorNote(data) : "",
    data.expected_glucose_mgdl != null ? "Modellwert bei voller Wirkung der eingegebenen KH und der angezeigten IE. Trend, frühere Dosen und weitere Einflüsse" +
      (Number(data.activity_change_mgdl) !== 0 ? " außer dem angegebenen Sport" : "") + " sind nicht eingerechnet." : "",
  ].filter(Boolean);
  return '<div class="dose-head"><span class="dose-heading">' + (withCarbs ? "Empfehlung" : "Korrektur ohne Essen") +
    '</span><span class="dose-basis">' + escapeHTML(glucose) + '</span></div>' +
    (options ? '<div class="dose-options">' + options + '</div>' : '') +
    (Number(data.activity_change_mgdl) !== 0 && data.activity_units != null ? '<p class="dose-factor">Sport eingerechnet: <strong>' +
      escapeHTML(signedChange(data.activity_change_mgdl, 0)) + '</strong> · ' + escapeHTML((Number(data.activity_units) > 0 ? "+" : "") + num(data.activity_units, 2)) + ' IE</p>' : '') +
    (data.recent_rapid_doses?.length ? '<p class="dose-warning">Nicht eingerechnet, schnell wirksame Dosen der letzten 3 h: ' +
      data.recent_rapid_doses.map(row => escapeHTML(timeOnly(row.at) + " · " + num(row.units) + " IE")).join(", ") + '</p>' : '') +
    (data.reason ? '<p class="hint">' + escapeHTML(data.reason) + '</p>' : '') +
    (details.length ? '<details class="dose-details"><summary>Rechenweg und Hinweise</summary>' +
      details.map(text => '<p>' + escapeHTML(text) + '</p>').join("") + '</details>' : '');
}

async function updateDoseHelp() {
  clearTimeout(helpTimer);
  const form = $("#entry-form");
  const carbs = entryCarbs();
  const output = $("#dose-result");
  const requestId = ++helpRequestId;
  doseHelp = null;
  const finish = text => {
    output.removeAttribute("aria-busy");
    if (text != null) output.textContent = text;
    applyDoseChoice();
    return null;
  };
  if (form.elements.kind.value !== "meal" || carbs == null) return finish("");
  if (state.day !== todayDay()) return finish(carbs > 0 ? "Eine aktuelle Empfehlung gibt es nur für den heutigen Tag." : "");
  const activityChange = entrySportChange();
  if (activityChange == null) return finish("Der geschätzte Sporteinfluss liegt außerhalb des erlaubten Bereichs.");
  if (!output.innerHTML) output.textContent = "Empfehlung wird berechnet …";
  output.setAttribute("aria-busy", "true");
  try {
    const data = await api("/api/dose-help", { method: "POST",
      body: JSON.stringify({ carbs: String(carbs), activity_change_mgdl: activityChange }) });
    if (requestId !== helpRequestId) return null;
    doseHelp = data;
    const open = output.querySelector(".dose-details")?.open;
    output.innerHTML = doseHelpHTML(data);
    if (open) output.querySelector(".dose-details").open = true;
    finish(null);
    return data;
  } catch (error) {
    if (requestId === helpRequestId) finish(error.message);
    return null;
  }
}

const mealNames = { breakfast: "Frühstück", lunch: "Mittagessen", dinner: "Abendessen" };

function autoFactorNote(data) {
  const scope = mealNames[data.auto_meal] + (data.auto_meal === "breakfast" ?
    (data.day_type === "free" ? " an freien Tagen" : " an Arbeitstagen") : "");
  if (data.auto_units_per_ke == null) {
    return "Automatik: Für " + scope + " gibt es erst " + data.auto_factor_count + " von " + data.auto_factor_min_meals +
      " nötigen berechneten Mahlzeiten (letzte " + data.auto_factor_days + " Tage).";
  }
  return "Automatik: " + num(data.auto_units_per_ke, 2) + " IE/KE = " + num(data.auto_percentile, 1) +
    "-%-Perzentil aus " + data.auto_factor_count + " berechneten Mahlzeiten (" + scope + ", letzte " +
    data.auto_factor_days + " Tage; unteres Quartil " + num(data.auto_q1_units_per_ke, 2) + ", Median " +
    num(data.auto_median_units_per_ke, 2) + " IE/KE). Korrektur auf den normalen Zielwert.";
}

function breakfastFactorNote(data) {
  const kind = data.day_type === "free" ? "freien Tagen" : "Arbeitstagen";
  if (data.factor_source === "breakfast_day_type") {
    return "Frühstücksfaktor an " + kind + ": Median aus " + data.breakfast_factor_count +
      " berechneten Frühstücken der letzten " + data.breakfast_factor_days + " Tage. Nightscout-Profil: " +
      num(data.nightscout_units_per_ke, 2) + " IE/KE.";
  }
  return "Für Frühstück an " + kind + " gibt es erst " + (data.breakfast_factor_count || 0) + " von " +
    data.breakfast_factor_min_meals + " nötigen berechneten Mahlzeiten (letzte " + data.breakfast_factor_days +
    " Tage), daher gilt der Insulinfaktor aus Nightscout.";
}

function closeEditDialog() {
  state.editing = null;
  $("#edit-dialog").close();
}

function resetEntryForm() {
  state.pendingEventId = null;
  const form = $("#entry-form");
  form.reset();
  form.elements.carbs.setCustomValidity("");
  state.entryTimeTouched = false;
  state.entryTimeOpen = false;
  state.entryNoteOpen = false;
  state.doseChoice = null;
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
  const sport = item.kind === "exercise";
  $("#edit-meal-fields").hidden = sport;
  $("#edit-sport-field").hidden = !sport;
  $("#edit-duration-field").hidden = !sport;
  form.elements.carbs.disabled = sport;
  form.elements.insulin.disabled = sport;
  form.elements.sport_kind.disabled = !sport;
  form.elements.duration_minutes.disabled = !sport;
  form.elements.duration_minutes.required = sport;
  $("#edit-date").textContent = new Date(state.day + "T12:00:00").toLocaleDateString("de-DE", {day: "numeric", month: "long", year: "numeric"});
  $("#edit-dialog").showModal();
  form.elements.at.focus();
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
    await updateDoseHelp();
    closeEditDialog();
    toast("Änderung in Nightscout gespeichert");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function saveEntry(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const mode = form.elements.kind.value;
  const carbs = mode === "meal" ? Number(form.elements.carbs.value || 0) : 0;
  const insulin = mode === "meal" ? Number(form.elements.insulin.value || 0) : 0;
  if (mode === "meal" && carbs <= 0 && insulin <= 0) {
    form.elements.carbs.setCustomValidity("Bitte Kohlenhydrate, Insulin oder beides eintragen.");
    form.elements.carbs.reportValidity();
    return;
  }
  const kind = mode === "exercise" ? "exercise" : carbs > 0 ? "meal" : "correction";
  let at;
  try { at = toInstant(state.day, form.elements.at.value); }
  catch (error) { toast(error.message, true); return; }
  const considerSport = mode === "meal" && form.elements.consider_sport.checked && !$("#entry-sport-option").hidden;
  const sportEffect = form.elements.sport_effect_mgdl.value;
  const payload = {
    kind, at,
    carbs, insulin,
    sport_kind: form.elements.sport_kind.value,
    duration_minutes: form.elements.duration_minutes.value || 0,
    notes: form.elements.notes.value,
    event_id: state.pendingEventId || crypto.randomUUID(),
  };
  state.pendingEventId = payload.event_id;
  const button = $('button[type="submit"]', form);
  button.disabled = true;
  try {
    // A saved suggestion must still match the current recommendation (new CGM value, new dose, profile change).
    const chosen = mode === "meal" && state.doseChoice && doseHelp ? doseUnits(doseHelp, state.doseChoice) : null;
    if (chosen != null && Number(chosen) === insulin) {
      const previous = doseHelp;
      const current = await updateDoseHelp();
      if (!current) throw new Error("Empfehlung konnte nicht aktualisiert werden");
      if (doseChanged(previous, current)) {
        toast("Empfehlung geändert. Bitte den neuen Wert prüfen und erneut speichern.", true);
        return;
      }
    }
    const result = await api("/api/treatment", { method: "POST", body: JSON.stringify(payload) });
    state.day = result.day;
    updateLocation(true);
    await loadDay();
    if (considerSport) {
      const saved = state.dayData?.treatments.find(item => item.notes.includes("[DAE:" + payload.event_id + "]"));
      const meal = state.dayData?.meals.find(item => item.source_id === saved?.id);
      if (!meal) throw new Error("Eintrag gespeichert, aber die Sportzuordnung konnte nicht gefunden werden. Bitte im Mahlzeitenfaktor prüfen.");
      await api("/api/analysis/sport", { method: "POST", body: JSON.stringify({
        meal_id: meal.id, day: result.day, status: "affected", effect_mgdl: sportChange(sportEffect),
      }) });
      await loadDay();
    }
    resetEntryForm();
    toast("Eintrag in Nightscout gespeichert" + (considerSport ? " · Sport berücksichtigt" : ""));
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
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
    await loadDay();
  } catch (error) {
    button.disabled = false;
    toast(error.message, true);
  }
}

async function saveSportMark(event) {
  const button = event.target.closest(".sport-save, .sport-suggestion, .sport-remove");
  const block = button?.closest(".sport-review");
  if (!block) return;
  const remove = button.matches(".sport-remove");
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
    toast(remove ? "Sporteinfluss entfernt" : "Sporteinfluss gespeichert");
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
    auto_percentile: form.elements.auto_percentile.value,
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

function renderAnalysisIc() {
  const rows = state.settings?.ic_schedule || [];
  $("#analysis-ic-rows").innerHTML = rows.length
    ? rows.map(row => {
      const time = minuteClock(Number(row.start_minute));
      const display = Number((10 / Number(row.grams_per_unit)).toFixed(2));
      return '<label class="analysis-ic-row"><span>Ab ' + escapeHTML(time) + '</span>' +
        '<input type="number" min="' + escapeHTML(pickerMinimum(display, 0.1, true)) + '" max="1000" step="0.1" required data-start-minute="' +
        escapeHTML(row.start_minute) + '" data-grams-per-unit="' + escapeHTML(row.grams_per_unit) +
        '" data-original-display="' + escapeHTML(display) + '" value="' + escapeHTML(display) +
        '" aria-label="Aktueller Insulinfaktor ab ' + escapeHTML(time) + ' in IE je KE"><span>IE/KE</span></label>';
    }).join("")
    : '<p class="hint">Kein Insulinfaktor im aktuellen Nightscout-Profil verfügbar.</p>';
  $("#save-analysis-ic").disabled = !rows.length || !state.settings?.nightscout_profile?.profile_id;
}

async function saveAnalysisIc() {
  const button = $("#save-analysis-ic");
  const inputs = $$("#analysis-ic-rows input");
  if (!inputs.length || !inputs.every(input => input.reportValidity())) return;
  button.disabled = true;
  $("#analysis-ic-status").textContent = "Wird in Nightscout gespeichert …";
  try {
    const result = await api("/api/profile/ic", { method: "POST", body: JSON.stringify({
      profile_id: state.settings.nightscout_profile.profile_id,
      ic_schedule: inputs.map(icScheduleRow),
    }) });
    await loadSettings();
    await loadSummary();
    $("#analysis-ic-status").textContent = result.message;
    toast(result.message);
  } catch (error) {
    $("#analysis-ic-status").textContent = error.message;
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
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
      '<br><small>Vergleich ' + escapeHTML(num(meal.baseline_units_per_ke, 2)) + ' IE/KE</small>';
    const effect = meal.effect_mgdl == null
      ? '<span class="summary-unavailable">' + escapeHTML(meal.reason || "Nicht berechenbar") + '</span>'
      : '<b>' + glucoseChangeHTML(meal.effect_mgdl) + '</b>' +
        (meal.shared ? ' <span class="outlier-badge">Mehrere Einheiten</span>' : '') +
        (meal.excluded ? ' <span class="outlier-badge">Ausgeklammert</span>' : '');
    return '<td>' + escapeHTML(names[meal.meal]) + '<br><small>' + escapeHTML(dateTime(meal.start)) + '–' +
      escapeHTML(timeOnly(meal.end)) + '</small></td>' +
      '<td>' + (meal.factor_without_sport_units_per_ke == null ? '—' :
        escapeHTML(num(meal.factor_without_sport_units_per_ke, 2)) + ' IE/KE') + baseline + '</td>' +
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

async function loadSummary() {
  $("#summary").innerHTML = '<div class="empty">Analyse wird geladen …</div>';
  $("#night-summary-cards").innerHTML = '<div class="empty">Nachtübersicht wird geladen …</div>';
  $("#night-records").innerHTML = "";
  $("#calculations").innerHTML = "";
  try {
    const days = $("#summary-days").value;
    const data = await api("/api/summary?days=" + days);
    // The summary endpoint may refresh the Nightscout profile before calculating.
    state.settings = (await api("/api/settings")).settings;
    renderNightSummary(data.night || { count: 0, unavailable_count: Number(days), nights: [] });
    if (data.profile_sync_error) {
      $("#sync-progress").textContent = "Korrekturfaktor-Profil nicht aktualisiert: " + data.profile_sync_error;
    }
    const breakfastSubcards = () => '<div class="breakfast-subcards">' + ["work", "free"].map(dayType => {
      const subset = data.groups.breakfast.by_day_type[dayType];
      return '<div class="breakfast-subcard"><span>' + (dayType === "work" ? "Arbeit" : "Frei") + '</span>' +
        '<strong>' + (subset.count ? escapeHTML(num(subset.median_units_per_ke, 2)) + ' <small>IE/KE</small>' : '—') +
        '</strong><small>' + subset.count + ' Mahlzeiten</small></div>';
    }).join("") + '</div>';
    $("#summary").innerHTML = ["breakfast", "lunch", "dinner"].map(kind => {
      const row = data.groups[kind];
      const hasValues = row.count > 0;
      const typical = row.q1_units_per_ke != null
        ? '<div class="summary-metric"><span>Mittlere 50 %</span><strong>' +
          escapeHTML(num(row.q1_units_per_ke, 2)) + '–' + escapeHTML(num(row.q3_units_per_ke, 2)) +
          ' <small>IE/KE</small></strong></div>'
        : '';
      return '<div class="summary-card"><span class="meal-type">' + escapeHTML(names[kind]) + '</span>' +
        '<div class="summary-primary"><span>Median</span><strong>' +
        (hasValues ? escapeHTML(num(row.median_units_per_ke, 2)) + ' <small>IE/KE</small>' : '—') +
        '</strong></div>' + typical +
        '<p>' + row.count + (row.count === 1 ? ' Mahlzeit' : ' Mahlzeiten') + '</p>' +
        (kind === "breakfast" ? breakfastSubcards() : '') + '</div>';
    }).join("");
    renderAnalysisIc();
    renderSportAnalysis(data.sport_analysis);
    const comparisons = data.sensitivity_comparison || [];
    $("#sensitivity-comparison").innerHTML = comparisons.length
      ? '<div class="table-wrap"><table class="analysis-table"><thead><tr><th>Ab</th><th>Aktuell</th><th>Mathematisch bester Korrekturfaktor</th></tr></thead><tbody>' +
        comparisons.map(row => {
          const minute = Number(row.start_minute);
          const time = String(Math.floor(minute / 60)).padStart(2, "0") + ':' + String(minute % 60).padStart(2, "0");
          const candidate = row.candidate_mgdl_per_unit == null
            ? escapeHTML(row.reason || 'Kein Wert berechenbar')
            : '<b>' + escapeHTML(num(row.candidate_mgdl_per_unit, 2)) + ' mg/dL je IE</b>';
          return '<tr><td>' + escapeHTML(time) + '</td><td>' +
            escapeHTML(num(row.current_mgdl_per_unit, 2)) + ' mg/dL je IE</td><td>' +
            candidate + '</td></tr>';
        }).join('') + '</tbody></table></div>'
      : '<p class="hint">Für den Vergleich fehlen ein I:C- oder Korrekturfaktor-Zeitplan.</p>';
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
              ? escapeHTML(num(item.units_per_ke, 2)) + ' IE/KE' +
                (item.strong_outlier ? ' <span class="outlier-badge">Stark abweichend</span>' : '') +
                (item.manual_window ? ' <span class="manual-badge">Zeitraum manuell</span>' : '')
              : '<span class="summary-unavailable">Nicht berechenbar: ' + escapeHTML(item.reason || "Kein Faktor") + '</span>';
            const excluded = item.units_per_ke != null && item.excluded;
            const sportReview = sportReviewHTML(item.sport || {}, item.id, item.day, true);
            return dayHeading + '<tr class="' + (excluded ? "analysis-excluded " : "") +
              (item.strong_outlier ? "analysis-outlier" : "") + '">' +
            '<td>' + escapeHTML(names[item.kind]) + '</td><td><b>' + factor + '</b></td>' +
            '<td>' + (item.carbs_total == null ? "—" : escapeHTML(num(item.carbs_total)) + ' g') + '</td>' +
            '<td>' + (item.rapid_units_in_window == null ? "—" : escapeHTML(num(item.rapid_units_in_window)) + ' IE') + '</td>' +
            '<td>' + glucoseChangeHTML(item.delta_mgdl) + '</td>' +
            '<td>' + glucoseChangeHTML(item.expected_delta_mgdl) + '</td>' +
            '<td>' + glucoseChangeHTML(item.deviation_mgdl) + '</td>' +
            '<td>' + sportReview + '</td>' +
            '<td>' + (item.units_per_ke == null ? "—" :
              '<button type="button" class="exclude-meal' + (excluded ? ' is-excluded' : '') +
              '" data-meal-id="' + escapeHTML(item.id) + '" data-day="' + escapeHTML(item.day) +
              '" data-excluded="' + excluded + '" aria-pressed="' + excluded + '" aria-label="' +
              (excluded ? 'Mahlzeit wieder berücksichtigen' : 'Mahlzeit ausklammern') + '" title="' +
              (excluded ? 'Mahlzeit wieder berücksichtigen' : 'Mahlzeit ausklammern') + '">' +
              icon(excluded ? "exclude" : "check") + '<span>' +
              (excluded ? 'Ausgeklammert' : 'Berücksichtigt') + '</span></button>') + '</td>' +
            '<td><button type="button" class="icon-button open-meal-day" data-day="' + escapeHTML(item.day) +
              '" aria-label="Tagebuch öffnen" title="Tagebuch öffnen">' + icon("diary") + '</button></td></tr>';
          }).join("");
        })() + '</tbody></table></div>'
      : '<div class="empty">Für diesen Zeitraum sind keine Mahlzeiten im lokalen Datenbestand.</div>';
  } catch (error) {
    $("#summary").innerHTML = '<div class="empty">Analyse nicht verfügbar.</div>';
    $("#night-summary-cards").innerHTML = '<div class="empty">Nachtübersicht nicht verfügbar.</div>';
    $("#night-records").innerHTML = "";
    $("#calculations").innerHTML = "";
    $("#sensitivity-comparison").innerHTML = "";
    $("#sport-analysis-kinds").innerHTML = "";
    $("#sport-analysis-sessions").innerHTML = "";
      toast(error.message, true);
  }
}

async function saveAnalysisExclusion(event) {
  const button = event.target.closest(".exclude-meal");
  if (!button) return;
  button.disabled = true;
  try {
    await api("/api/analysis/exclusion", {
      method: "POST",
      body: JSON.stringify({ meal_id: button.dataset.mealId, day: button.dataset.day, excluded: button.dataset.excluded !== "true" }),
    });
    await loadSummary();
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
    const dayChanged = state.day !== nextLocation.day;
    if (dayChanged) resetEntryForm();
    state.day = nextLocation.day;
    state.persistDay = nextLocation.persistDay;
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
  $("#entry-time").addEventListener("change", () => {
    state.entryTimeTouched = true;
    updateEntrySportOption();
    updateEntryDayType();
    updateEntryChips();
  });
  $("#entry-form").addEventListener("focusin", setDefaultMealTime);
  $("#entry-form").elements.carbs.addEventListener("input", updateEntrySportOption);
  $("#entry-form").elements.consider_sport.addEventListener("change", () => {
    updateEntrySportOption();
    scheduleDoseHelp();
  });
  $("#entry-form").elements.sport_effect_mgdl.addEventListener("input", scheduleDoseHelp);
  $$(".entry-mode").forEach(button => button.addEventListener("click", () => {
    $("#entry-kind").value = button.dataset.entryMode;
    updateEntryFields();
  }));
  $("#edit-form").addEventListener("submit", saveEditedEntry);
  $("#edit-form").elements.carbs.addEventListener("input", event => {
    setPickerBase(event.target, 1);
    event.target.setCustomValidity("");
  });
  $("#edit-form").elements.insulin.addEventListener("input", event => {
    setPickerBase(event.target, 0.5);
    $("#edit-form").elements.carbs.setCustomValidity("");
  });
  for (const selector of ["#ic-rows", "#analysis-ic-rows"]) {
    $(selector).addEventListener("input", event => {
      if (event.target.matches("input")) setPickerBase(event.target, 0.1, true);
    });
  }
  $("#cancel-edit").addEventListener("click", closeEditDialog);
  $("#close-edit").addEventListener("click", closeEditDialog);
  $("#edit-dialog").addEventListener("close", () => { state.editing = null; });
  $("#dose-result").addEventListener("click", selectDoseSuggestion);
  $("#entry-day-type").addEventListener("click", saveEntryDayType);
  $("#vacation-toggle").addEventListener("change", saveVacation);
  $("#entry-form").elements.carbs.addEventListener("input", event => {
    event.target.setCustomValidity("");
    updateEntrySubmitLabel();
    scheduleDoseHelp();
  });
  $("#entry-form").elements.insulin.addEventListener("input", () => {
    $("#entry-form").elements.carbs.setCustomValidity("");
    state.doseChoice = null;
    applyDoseChoice();
  });
  $("#entry-form").elements.notes.addEventListener("input", updateEntryChips);
  $("#entry-time-chip").addEventListener("click", () => {
    state.entryTimeOpen = true;
    updateEntryChips();
    $("#entry-time").focus();
  });
  $("#entry-note-chip").addEventListener("click", () => {
    state.entryNoteOpen = true;
    updateEntryChips();
    $("#entry-form").elements.notes.focus();
  });
  $("#diary-view").addEventListener("click", event => {
    if (event.target.closest(".delete-treatment")) deleteTreatment(event);
    const edit = event.target.closest(".edit-entry");
    if (edit) editEntry(edit.dataset.id);
    if (event.target.closest(".sport-save, .sport-suggestion, .sport-remove")) saveSportMark(event);
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
      if (state.dayData) { renderMealCharts(); renderDayOverview(); }
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
  $("#save-analysis-ic").addEventListener("click", saveAnalysisIc);
  $("#sync-range").addEventListener("click", syncRange);
  $("#calculations").addEventListener("click", saveAnalysisExclusion);
  $("#calculations").addEventListener("change", saveDayType);
  $("#calculations").addEventListener("click", event => {
    if (event.target.closest(".exclude-meal")) return;
    if (event.target.closest(".sport-save, .sport-suggestion, .sport-remove")) {
      saveSportMark(event);
      return;
    }
    const button = event.target.closest(".open-meal-day");
    if (!button) return;
    state.day = button.dataset.day;
    setDefaultMealTime();
    view("diary");
    loadDay();
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
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) loadCurrentGlucose();
  });
  window.addEventListener("focus", loadCurrentGlucose);
});
