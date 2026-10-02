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
      const arrow = Number(delta) > 0 ? "↗" : Number(delta) < 0 ? "↘" : "→";
      $("#glucose-trend").textContent = data.fresh && delta != null
        ? arrow + " " + (Number(delta) > 0 ? "+" : "") + num(delta, 0) +
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
}

function setDefaultSportTime() {}
function setSportDuration() {}

function updateEntrySportOption() {
  const form = $("#entry-form");
  const option = $("#entry-sport-option");
  const effectField = $("#entry-sport-effect-field");
  const checkbox = form.elements.consider_sport;
  let eligible = [];
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
        eligible = state.dayData.treatments.filter(item => {
          if (item.eventType !== "Exercise" || !item.sport_type) return false;
          const end = new Date(item.at).getTime() + Number(item.duration || 0) * 60000;
          return end <= at.getTime() && end >= at.getTime() - settings.sport_lookback_minutes * 60000;
        });
      }
    }
  } catch (_) { /* The time field is incomplete. */ }
  option.hidden = !eligible.length;
  if (!eligible.length) {
    checkbox.checked = false;
    effectField.hidden = true;
    return;
  }
  const suggested = eligible.reduce((total, item) => total + sportChange(state.settings.sport_effects_mgdl[item.sport_type]), 0);
  $("#entry-sport-hint").textContent = eligible.map(item =>
    sportNames[item.sport_type] + " um " + timeOnly(item.at)).join(", ") +
    " · Vorschlag: " + signedChange(suggested, 0);
  if (!checkbox.checked) form.elements.sport_effect_mgdl.value = suggested;
  effectField.hidden = !checkbox.checked;
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
  scheduleDoseHelp();
  updateEntrySportOption();
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
  form.elements.breakfast_end.value = minuteClock(data.settings.breakfast_end_minute);
  form.elements.lunch_end.value = minuteClock(data.settings.lunch_end_minute);
  form.elements.sport_lookback_minutes.value = data.settings.sport_lookback_minutes;
  for (const kind of sportKinds) {
    form.elements["sport_" + kind].value = sportChange(data.settings.sport_effects_mgdl[kind]);
  }
  renderIsfRows(data.settings.isf_schedule, data.settings.nightscout_profile, data.profile_sync_at, data.profile_sync_error);
  renderIcRows(data.settings.ic_schedule || []);
  renderTargetRows(data.settings.target_schedule || []);
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

function chartFor(meal, entries) {
  const start = new Date(meal.at).getTime();
  const end = meal.calculation?.end ? new Date(meal.calculation.end).getTime() : start + Number(state.settings.window_minutes) * 60000;
  const points = entries.filter(point => {
    const at = new Date(point.at).getTime();
    return at >= start - 30 * 60000 && at <= end + 30 * 60000;
  }).map(point => ({ t: new Date(point.at).getTime(), g: Number(point.sgv) }));
  if (points.length < 2) return '<p class="hint">Keine CGM-Kurve verfügbar.</p>';
  const min = Math.min(...points.map(point => point.g)) - 15;
  const max = Math.max(...points.map(point => point.g)) + 15;
  const x = t => 24 + (t - (start - 30 * 60000)) / (end - start + 60 * 60000) * 472;
  const y = g => 104 - (g - min) / (max - min) * 80;
  const line = points.map(point => x(point.t).toFixed(1) + "," + y(point.g).toFixed(1)).join(" ");
  const x0 = x(start).toFixed(1);
  const x3 = x(end).toFixed(1);
  return '<svg class="glucose-chart" viewBox="0 0 520 130" role="img" aria-label="Glukoseverlauf um die Mahlzeit">' +
    '<line x1="24" y1="104" x2="496" y2="104" class="axis"/>' +
    '<line x1="' + x0 + '" y1="14" x2="' + x0 + '" y2="104" class="marker"/>' +
    '<line x1="' + x3 + '" y1="14" x2="' + x3 + '" y2="104" class="marker"/>' +
    '<polyline points="' + line + '" class="curve"/>' +
    '<text x="' + x0 + '" y="122" text-anchor="middle">Mahlzeit</text>' +
    '<text x="' + x3 + '" y="122" text-anchor="middle">+' + escapeHTML(meal.calculation?.actual_window_minutes || state.settings.window_minutes) + ' min</text>' +
    '</svg>';
}

function calculationHTML(result) {
  if (!result || !result.start) return '<div class="factor unavailable"><span>Insulinfaktor</span><strong>—</strong><small>' + escapeHTML(result?.reason || "Noch keine Berechnung") + '</small></div>';
  const valid = result.factor_units_per_ke != null;
  const summary = [
    num(result.carbs_total) + " g KH",
    num(result.rapid_units_in_window, 2) + " IE",
    "Ende " + timeOnly(result.end),
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
  $("#meals").innerHTML = data.meals.length
    ? data.meals.map(meal => mealHTML(meal, data.entries)).join("")
    : '<div class="empty">Noch keine Hauptmahlzeit für diesen Tag.</div>';
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

function scheduleDoseHelp() {
  clearTimeout(helpTimer);
  helpRequestId++;
  doseHelp = null;
  const form = $("#entry-form");
  const carbs = Number(form.elements.carbs.value);
  if (form.elements.kind.value !== "meal" || !Number.isFinite(carbs) || carbs <= 0 || carbs > 500) {
    $("#dose-result").textContent = "";
    return;
  }
  $("#dose-result").textContent = "Empfehlung wird berechnet …";
  helpTimer = setTimeout(updateDoseHelp, 350);
}

async function updateDoseHelp() {
  clearTimeout(helpTimer);
  const form = $("#entry-form");
  const carbs = Number(form.elements.carbs.value);
  const output = $("#dose-result");
  const requestId = ++helpRequestId;
  doseHelp = null;
  if (form.elements.kind.value !== "meal" || !Number.isFinite(carbs) || carbs <= 0 || carbs > 500) {
    output.textContent = "";
    return null;
  }
  const today = new Date().toLocaleDateString("sv-SE", { timeZone: state.settings?.timezone || "Europe/Berlin" });
  if (state.day !== today) {
    output.textContent = "Eine aktuelle Empfehlung gibt es nur für den heutigen Tag.";
    return null;
  }
  output.textContent = "Empfehlung wird berechnet …";
  try {
    const data = await api("/api/dose-help", { method: "POST",
      body: JSON.stringify({ carbs: form.elements.carbs.value }) });
    if (requestId !== helpRequestId) return null;
    doseHelp = data;
    const glucose = data.glucose_mgdl == null ? "Kein CGM-Wert" :
      num(data.glucose_mgdl, 0) + " mg/dL um " + timeOnly(data.glucose_at) +
      (data.trend_delta_mgdl == null ? "" : " · " +
       (Number(data.trend_delta_mgdl) > 0 ? "↗ " : Number(data.trend_delta_mgdl) < 0 ? "↘ " : "→ ") +
       signedChange(data.trend_delta_mgdl, 0) + " in " + num(data.trend_minutes, 0) + " min");
    const option = (label, variant, target, units, exact, expectedGlucose) => {
      if (units == null) return "";
      return '<div class="dose-option"><span>' + escapeHTML(label) + ' · Ziel ' + escapeHTML(num(target, 0)) +
        ' mg/dL</span><strong>' + escapeHTML(num(units, 2)) +
        ' IE</strong><small>Berechnet: ' + escapeHTML(num(exact, 2)) + ' IE · auf 0,5 IE gerundet</small>' +
        '<span class="dose-expected">Erwarteter BG (Modell): <strong>' + escapeHTML(num(expectedGlucose, 0)) + ' mg/dL</strong></span><button type="button" class="apply-dose" data-variant="' + variant + '"' +
        (Number(units) > 100 ? ' disabled' : '') + '>IE übernehmen</button></div>';
    };
    output.innerHTML = '<div class="dose-heading">Aktuelle Empfehlung</div><div class="dose-glucose"><span>Aktueller Blutzucker</span><strong>' +
      escapeHTML(glucose) + '</strong></div>' +
      (data.units_per_ke != null ? '<p class="dose-factor">Verwendeter Insulinfaktor: <strong>' + escapeHTML(num(data.units_per_ke, 2)) + ' IE/KE</strong> · Korrekturfaktor: ' + escapeHTML(num(data.isf_mgdl_per_unit, 1)) + ' mg/dL je IE</p>' : '') +
      '<div class="dose-options">' +
      option("Normal", "normal", data.target_mgdl, data.entry_units, data.total_units, data.expected_glucose_mgdl) +
      option("Vorsichtig", "cautious", data.cautious_target_mgdl, data.cautious_entry_units, data.cautious_total_units, data.cautious_expected_glucose_mgdl) + '</div>' +
      (data.expected_glucose_mgdl != null ? '<p class="hint">Modellwert bei voller Wirkung der eingegebenen KH und der angezeigten IE. Trend, frühere Dosen und weitere Einflüsse sind nicht eingerechnet.</p>' : '') +
      (data.recent_rapid_doses?.length ? '<p class="hint">Schnell wirksame Dosen in den letzten 3 h: ' +
      data.recent_rapid_doses.map(row => escapeHTML(timeOnly(row.at) + " · " + num(row.units) + " IE")).join(", ") +
      '.</p>' : '') +
      (data.reason ? '<p class="hint">' + escapeHTML(data.reason) + '</p>' : '');
    return data;
  } catch (error) {
    if (requestId === helpRequestId) output.textContent = error.message;
    return null;
  }
}

async function applyDoseSuggestion(event) {
  const button = event.target.closest(".apply-dose");
  if (!button || !doseHelp) return;
  const previous = doseHelp;
  const variant = button.dataset.variant;
  button.disabled = true;
  try {
    const current = await updateDoseHelp();
    if (!current) throw new Error("Empfehlung konnte nicht aktualisiert werden");
    const keys = ["carbs_g", "glucose_at", "glucose_mgdl", "isf_mgdl_per_unit", "target_mgdl",
      "cautious_target_mgdl", "units_per_ke", "total_units", "cautious_total_units", "entry_units", "cautious_entry_units", "expected_glucose_mgdl", "cautious_expected_glucose_mgdl"];
    if (keys.some(key => previous[key] !== current[key]) ||
        JSON.stringify(previous.recent_rapid_doses) !== JSON.stringify(current.recent_rapid_doses)) {
      toast("Empfehlung geändert. Bitte den neuen Wert prüfen und erneut übernehmen.", true);
      return;
    }
    const units = variant === "cautious" ? current.cautious_entry_units : current.entry_units;
    if (units == null || Number(units) > 100 || Number($("#entry-form").elements.carbs.value) !== Number(current.carbs_g))
      throw new Error("Kohlenhydrate geändert. Bitte neue Empfehlung prüfen");
    $("#entry-form").elements.insulin.value = units;
    toast(num(units, 2) + " IE ins Formular übernommen");
  } catch (error) {
    toast(error.message, true);
  }
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
                (item.strong_outlier ? ' <span class="outlier-badge">Stark abweichend</span>' : '')
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
  });
  $("#entry-form").addEventListener("focusin", setDefaultMealTime);
  $("#entry-form").elements.carbs.addEventListener("input", updateEntrySportOption);
  $("#entry-form").elements.consider_sport.addEventListener("change", updateEntrySportOption);
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
  $("#dose-result").addEventListener("click", applyDoseSuggestion);
  $("#entry-form").elements.carbs.addEventListener("input", event => {
    event.target.setCustomValidity("");
    scheduleDoseHelp();
  });
  $("#entry-form").elements.insulin.addEventListener("input", () => {
    $("#entry-form").elements.carbs.setCustomValidity("");
  });
  $("#diary-view").addEventListener("click", event => {
    if (event.target.closest(".delete-treatment")) deleteTreatment(event);
    const edit = event.target.closest(".edit-entry");
    if (edit) editEntry(edit.dataset.id);
    if (event.target.closest(".sport-save, .sport-suggestion, .sport-remove")) saveSportMark(event);
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
