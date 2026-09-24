"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const state = { day: new Date().toLocaleDateString("sv-SE"), view: "diary", settings: null, dayData: null, pendingMealId: null, pendingSportId: null };
const views = new Set(["diary", "analysis", "settings"]);
const names = { breakfast: "Frühstück", lunch: "Mittagessen", dinner: "Abendessen" };
const num = (value, digits = 2) => Number(value).toLocaleString("de-DE", { maximumFractionDigits: digits, minimumFractionDigits: 0 });
const signedChange = (value, digits = 1) => value == null ? "—" : (Number(value) > 0 ? "+" : "") + num(value, digits) + " mg/dL";
const glucoseChangeHTML = value => value == null ? '—' :
  '<span class="glucose-change ' + (Number(value) > 0 ? 'rising' : Number(value) < 0 ? 'falling' : 'steady') + '">' +
  escapeHTML(signedChange(value)) + '</span>';
const timeOnly = (value) => new Date(value).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
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
  return {
    view: views.has(requestedView) ? requestedView : "diary",
    day: validDay(requestedDay) ? requestedDay : new Date().toLocaleDateString("sv-SE"),
  };
}

function syncTabLinks(url = new URL(window.location.href)) {
  url.searchParams.set("date", state.day);
  $$(".tab").forEach(link => {
    const linkURL = new URL(url);
    linkURL.searchParams.set("view", link.dataset.view);
    link.href = linkURL.href;
  });
}

function updateLocation(replace = false) {
  const url = new URL(window.location.href);
  url.searchParams.set("view", state.view);
  url.searchParams.set("date", state.day);
  syncTabLinks(url);
  window.history[replace ? "replaceState" : "pushState"]({ view: state.view, day: state.day }, "", url);
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
}

function changeDay(day, options = {}) {
  if (!validDay(day) || day === state.day) return;
  state.day = day;
  $("#selected-day").value = day;
  setDefaultMealTime();
  setDefaultSportTime();
  if (options.updateHistory !== false) updateLocation(Boolean(options.replaceHistory));
  if (options.load !== false) loadDay();
}

function setDefaultMealTime() {
  const now = new Date();
  $("#entry-time").value = state.day === now.toLocaleDateString("sv-SE")
    ? localInputTime(now).slice(11, 16) : "12:00";
}

function setDefaultSportTime() {}
function setSportDuration() {}

function minuteClock(minute) {
  return String(Math.floor(minute / 60)).padStart(2, "0") + ":" + String(minute % 60).padStart(2, "0");
}

function clockMinute(value) {
  const [hours, minutes] = value.split(":").map(Number);
  return hours * 60 + minutes;
}

function updateEntryFields() {
  const kind = $("#entry-kind").value;
  const form = $("#entry-form");
  $("#entry-carbs-field").hidden = !["meal", "snack", "carbs"].includes(kind);
  $("#entry-insulin-field").hidden = ["carbs", "exercise"].includes(kind);
  $("#entry-sport-field").hidden = kind !== "exercise";
  $("#entry-duration-field").hidden = kind !== "exercise";
  form.elements.carbs.required = ["meal", "snack", "carbs"].includes(kind);
  form.elements.insulin.required = kind === "correction";
  form.elements.duration_minutes.required = kind === "exercise";
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
  for (const kind of ["jogging", "gym", "light_cardio"]) {
    form.elements["sport_" + kind].value = data.settings.sport_effects_mgdl[kind];
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
      return '<label class="ic-row"><span>' + time + '</span><input type="number" min="0.001" max="1000" step="any" required data-start-minute="' +
        escapeHTML(start) + '" value="' + escapeHTML(10 / Number(row.grams_per_unit)) + '" aria-label="Insulinfaktor ab ' + time + ' in IE je KE"></label>';
    }).join("") + '</div>';
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
      [isIc ? "ic_schedule" : "isf_schedule"]: inputs.map(input => ({ start_minute: Number(input.dataset.startMinute), [key]: input.value })),
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
  const badge = '<div class="factor' + (valid ? '' : ' unavailable') + '"><span>Insulinfaktor</span><strong>' +
    (valid ? escapeHTML(num(result.factor_units_per_ke, 3)) + ' <small>IE/KE</small>' : '—') +
    '</strong><small>' + escapeHTML(num(result.carbs_total)) + ' g KH im Mahlzeitenfenster</small></div>';
  const endMinutes = num(result.actual_window_minutes, 1);
  const extended = result.end_reason === "after_last_injection"
    ? '<p class="calc-note">Endpunkt wegen einer späteren Injektion verschoben. Jede schnell wirksame Dosis im Fenster erhält ' + escapeHTML(num(result.window_minutes, 0)) + ' Minuten Beobachtungszeit.</p>' : '';
  const shortened = result.end_reason === "before_next_meal"
    ? '<p class="calc-note">Endpunkt auf den letzten CGM-Wert vor der nächsten Hauptmahlzeit um ' +
      escapeHTML(timeOnly(result.next_main_meal_at)) + ' verkürzt.</p>' : '';
  const reason = result.reason
    ? '<p class="calc-note">Kein Faktor: ' + escapeHTML(result.reason) + '.</p>' : '';
  const glucose = '<div class="calc-step"><span>Glukose von ' + escapeHTML(timeOnly(result.start)) + ' bis ' + escapeHTML(timeOnly(result.end)) +
    ' (+' + escapeHTML(endMinutes) + ' min)</span><b>' + escapeHTML(num(result.g0, 3)) + ' → ' +
    escapeHTML(num(result.g3, 3)) + ' mg/dL · Δ ' + escapeHTML(num(result.delta_mgdl, 3)) + '</b></div>';
  const insulin = '<div class="calc-step"><span>Schnell wirksames Insulin im Fenster</span><b>' +
    escapeHTML(num(result.rapid_units_in_window, 2)) + ' IE vollständig berücksichtigt · ' +
    escapeHTML(num(result.insulin_effect_mgdl, 3)) + ' mg/dL rechnerischer Insulineffekt</b></div>';
  const carbs = '<div class="calc-step"><span>KH × Korrekturfaktor</span><b>' +
    (result.extra_carb_count ? 'Σ(KH × KF) = ' : escapeHTML(num(result.carbs_total)) + ' g × ' + escapeHTML(num(result.meal_isf)) + ' mg/dL/IE = ') +
    escapeHTML(num(result.carbs_times_isf_total, 3)) + '</b></div>';
  const formula = valid
    ? '<p class="formula">IE/KE = 10 × (' + escapeHTML(num(result.delta_mgdl, 3)) +
      (Number(result.sport_effect_mgdl) ? ' + ' + escapeHTML(num(result.sport_effect_mgdl, 3)) + ' Sport' : '') +
      ' + ' + escapeHTML(num(result.insulin_effect_mgdl, 3)) + ') ÷ ' +
      escapeHTML(num(result.carbs_times_isf_total, 3)) + ' = ' + escapeHTML(num(result.factor_units_per_ke, 3)) + '</p>'
    : '';
  const profileInfo = result.nightscout_profile
    ? ' · aktuelles Nightscout-Profil „' + escapeHTML(result.nightscout_profile.profile_name) + '“'
    : '';
  const doseNames = { meal: "Mahlzeit", "meal-dose": "Mahlzeitenbolus", correction: "Korrektur", "Snack Bolus": "Snack-Bolus", "Meal Bolus": "Mahlzeitenbolus", "Correction Bolus": "Korrektur", Bolus: "Bolus" };
  const doseRows = result.doses.map(dose => '<tr><td>' + escapeHTML(timeOnly(dose.at)) + '</td><td>' +
    escapeHTML(dose.event_type === "Snack Bolus" ? "Snack-Bolus" : doseNames[dose.role] || dose.role) + '</td><td>' + escapeHTML(num(dose.units)) + ' IE</td><td>' +
    escapeHTML(num(dose.isf)) + '</td><td>' +
    escapeHTML(num(dose.effect_mgdl, 3)) + '</td></tr>').join("");
  const carbRows = result.carb_events.map(item => '<tr><td>' + escapeHTML(timeOnly(item.at)) + '</td><td>' +
    escapeHTML(item.kind === "meal" ? "Mahlzeit" : item.kind === "snack" ? "Snack" : "Weitere KH") + '</td><td>' +
    escapeHTML(num(item.carbs)) + ' g</td><td>' + escapeHTML(num(item.isf)) + '</td><td>' +
    escapeHTML(num(item.carbs_times_isf, 3)) + '</td></tr>').join("");
  return '<div class="calc-block">' + badge + '<div class="calc-steps">' + glucose + insulin + carbs + '</div>' +
    formula + extended + shortened + reason +
    '<details class="math"><summary>Alle Dosen, KH und Rohwerte anzeigen</summary>' +
    '<h4>KH-Ereignisse</h4><div class="table-wrap"><table><thead><tr><th>Zeit</th><th>Art</th><th>KH</th><th>KF mg/dL/IE</th><th>KH × KF</th></tr></thead><tbody>' + carbRows + '</tbody></table></div>' +
    '<h4>Insulineffekt</h4><div class="table-wrap"><table><thead><tr><th>Zeit</th><th>Art</th><th>Dosis</th><th>KF mg/dL/IE</th><th>mg/dL</th></tr></thead><tbody>' + doseRows + '</tbody></table></div>' +
    '<p class="hint">Rohwerte: gemessenes Δ ' + escapeHTML(result.delta_mgdl) +
      ' mg/dL · bestätigte Sport-Senkung ' + escapeHTML(result.sport_effect_mgdl || "0") +
      ' mg/dL · bereinigtes Δ ' + escapeHTML(result.adjusted_delta_mgdl || result.delta_mgdl) +
      ' mg/dL · rechnerischer Insulineffekt ' +
      escapeHTML(result.insulin_effect_mgdl) + ' mg/dL · Summe ' + escapeHTML(result.denominator_mgdl) +
      ' mg/dL' + (valid ? ' · IE/KE ' + escapeHTML(result.factor_units_per_ke) + ' · Nightscout-I:C ' +
      escapeHTML(result.ratio_g_per_unit) + ' g/IE' : '') + '.</p>' +
    '<p class="hint">CGM-Stützpunkte: ' + escapeHTML(result.g0_sources.join(" / ")) + ' und ' +
      escapeHTML(result.g3_sources.join(" / ")) + '. Modell ' + escapeHTML(result.algorithm) +
      ' · Einstellungen ' + escapeHTML(result.settings_version) + profileInfo + '.</p>' +
    '<p class="hint">Annahmen: ' + result.assumptions.map(escapeHTML).join(" · ") + '</p></details></div>';
}

function mealHTML(meal, entries) {
  const sport = meal.sport || {};
  const status = sport.status || "auto";
  const suggested = sport.suggested
    ? "Sport zuvor · Vorschlag " + num(sport.suggested_effect_mgdl, 0) + " mg/dL Senkung"
    : "";
  return '<article class="meal-card">' +
    '<div class="meal-top"><div><span class="meal-type">' + escapeHTML(names[meal.meal]) +
    '</span><h3>' + escapeHTML(timeOnly(meal.at)) + ' <small>· ' + escapeHTML(num(meal.carbs)) + ' g KH</small></h3></div></div>' +
    '<div class="sport-review">' + (suggested ? '<span>' + escapeHTML(suggested) + '</span>' : '') +
    '<label>Einfluss <select class="sport-status" data-meal-id="' + escapeHTML(meal.id) +
    '" data-day="' + escapeHTML(state.day) + '">' +
    '<option value="auto"' + (status === "auto" ? " selected" : "") + '>Noch nicht bewertet</option>' +
    '<option value="affected"' + (status === "affected" ? " selected" : "") + '>Sport wirkte mit</option>' +
    '<option value="unaffected"' + (status === "unaffected" ? " selected" : "") + '>Kein Einfluss</option></select></label>' +
    '<label>Senkung im Fenster <span>mg/dL</span><input class="sport-effect" type="number" min="0" max="500" step="1" value="' +
    escapeHTML(status === "affected" ? sport.effect_mgdl : sport.suggested_effect_mgdl || 0) + '"></label>' +
    '<button type="button" class="save-sport quiet">Übernehmen</button></div>' +
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
        ? ({ jogging: "Joggen", gym: "Gym", light_cardio: "Leichtes Cardio" })[item.sport_kind] || "Sport"
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
let doseSaving = false;
let pendingDoseEventId = null;
let pendingDoseKey = null;

function scheduleDoseHelp() {
  clearTimeout(helpTimer);
  helpTimer = setTimeout(updateDoseHelp, 350);
}

async function updateDoseHelp() {
  const requestId = ++helpRequestId;
  const output = $("#dose-result");
  output.textContent = "Berechne …";
  doseHelp = null;
  try {
    const data = await api("/api/dose-help", { method: "POST",
      body: JSON.stringify({ carbs: $("#help-carbs").value || 0 }) });
    if (requestId !== helpRequestId) return null;
    doseHelp = data;
    const glucose = data.glucose_mgdl == null ? "Kein CGM-Wert" :
      num(data.glucose_mgdl, 0) + " mg/dL um " + timeOnly(data.glucose_at) +
      (data.trend_delta_mgdl == null ? "" : " · " +
       (Number(data.trend_delta_mgdl) > 0 ? "↗ " : Number(data.trend_delta_mgdl) < 0 ? "↘ " : "→ ") +
       signedChange(data.trend_delta_mgdl, 0) + " in " + num(data.trend_minutes, 0) + " min");
    const today = new Date().toLocaleDateString("sv-SE", { timeZone: state.settings?.timezone || "Europe/Berlin" });
    const canRecord = state.day === today;
    const option = (label, variant, target, correction, total, units) => {
      if (total == null) return "";
      const valid = Number(units) > 0 || Number(data.carbs_g) > 0;
      const allowed = valid && Number(units) <= 100 && canRecord;
      return '<div class="dose-option"><span>' + escapeHTML(label) + ' · Ziel ' + escapeHTML(num(target, 0)) +
        ' mg/dL</span><small>Korrektur: (' + escapeHTML(num(data.glucose_mgdl, 0)) + ' − ' +
        escapeHTML(num(target, 0)) + ') ÷ ' + escapeHTML(num(data.isf_mgdl_per_unit, 1)) + ' = ' +
        escapeHTML(num(correction, 2)) + ' IE</small><strong>Gesamt: ' + escapeHTML(num(total, 2)) +
        ' IE</strong><button type="button" class="record-dose" data-variant="' + variant + '"' +
        (allowed && !doseSaving ? '' : ' disabled') + '>' +
        (Number(units) > 0 ? escapeHTML(num(units, 2)) + ' IE jetzt in Nightscout eintragen' :
          'KH ohne Insulin jetzt in Nightscout eintragen') + '</button></div>';
    };
    output.innerHTML = '<div class="dose-glucose"><span>Aktueller Blutzucker</span><strong>' +
      escapeHTML(glucose) + '</strong></div><div class="dose-math">' +
      (data.carb_units == null ? '' : '<span>KH: ' + escapeHTML(num(data.carbs_g)) + ' g × ' +
      escapeHTML(num(data.units_per_ke, 3)) + ' IE/KE = <b>' +
      escapeHTML(num(data.carb_units, 2)) + ' IE</b></span>') + '</div>' +
      '<div class="dose-options">' +
      option("Normal", "normal", data.target_mgdl, data.correction_units, data.total_units, data.entry_units) +
      option("Vorsichtig", "cautious", data.cautious_target_mgdl, data.cautious_correction_units,
        data.cautious_total_units, data.cautious_entry_units) + '</div>' +
      (!canRecord ? '<p class="hint">Direktes Eintragen ist nur für den heutigen Tag möglich.</p>' : '') +
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

async function saveDoseSuggestion(event) {
  const button = event.target.closest(".record-dose");
  if (!button || doseSaving || !doseHelp) return;
  const variant = button.dataset.variant;
  const previous = doseHelp;
  doseSaving = true;
  $$(".record-dose").forEach(item => item.disabled = true);
  try {
    clearTimeout(helpTimer);
    const current = await updateDoseHelp();
    if (!current) throw new Error("Vorschlag konnte nicht aktualisiert werden");
    const keys = ["carbs_g", "glucose_at", "glucose_mgdl", "isf_mgdl_per_unit", "target_mgdl",
      "cautious_target_mgdl", "entry_units", "cautious_entry_units"];
    if (keys.some(key => previous[key] !== current[key]) ||
        JSON.stringify(previous.recent_rapid_doses) !== JSON.stringify(current.recent_rapid_doses)) {
      pendingDoseEventId = null;
      pendingDoseKey = null;
      toast("Vorschlag geändert. Bitte den neuen Wert prüfen und erneut klicken.", true);
      return;
    }
    const units = variant === "cautious" ? current.cautious_entry_units : current.entry_units;
    if (units == null || Number(units) > 100 || (Number(units) <= 0 && Number(current.carbs_g) <= 0))
      throw new Error("Kein gültiger Eintrag aus diesem Vorschlag möglich");
    const today = new Date().toLocaleDateString("sv-SE", { timeZone: state.settings?.timezone || "Europe/Berlin" });
    if (state.day !== today) throw new Error("Zum direkten Eintragen den heutigen Tag öffnen");
    if (Number($("#help-carbs").value || 0) !== Number(current.carbs_g))
      throw new Error("Kohlenhydrate geändert. Bitte neuen Vorschlag prüfen");
    const doseKey = JSON.stringify([variant, current.carbs_g, current.glucose_at,
      current.glucose_mgdl, current.isf_mgdl_per_unit, current.target_mgdl, units]);
    if (pendingDoseKey !== doseKey) {
      pendingDoseKey = doseKey;
      pendingDoseEventId = crypto.randomUUID();
    }
    const result = await api("/api/treatment", { method: "POST", body: JSON.stringify({
      kind: Number(current.carbs_g) > 0 ? (Number(units) > 0 ? "meal" : "carbs") : "correction",
      at: new Date().toISOString(), carbs: current.carbs_g, insulin: units,
      event_id: pendingDoseEventId,
    }) });
    pendingDoseEventId = null;
    pendingDoseKey = null;
    state.day = result.day;
    updateLocation(true);
    toast("Vorschlag in Nightscout eingetragen");
    await loadDay();
  } catch (error) {
    toast(error.message, true);
  } finally {
    doseSaving = false;
    await updateDoseHelp();
  }
}

function resetEntryForm() {
  state.editing = null;
  state.pendingEventId = null;
  const form = $("#entry-form");
  form.reset();
  setDefaultMealTime();
  updateEntryFields();
  $("#entry-title").textContent = "Eintrag hinzufügen";
  $("#cancel-edit").hidden = true;
}

function editEntry(id) {
  const item = state.dayData?.timeline.find(row => row.id === id);
  if (!item || item.kind === "other") return;
  state.editing = item;
  const form = $("#entry-form");
  form.elements.kind.value = item.kind;
  form.elements.at.value = timeOnly(item.at);
  form.elements.carbs.value = Number(item.carbs) || "";
  form.elements.insulin.value = Number(item.insulin) || "";
  form.elements.sport_kind.value = item.sport_kind || "jogging";
  form.elements.duration_minutes.value = Number(item.duration_minutes) || "";
  form.elements.notes.value = item.notes || "";
  updateEntryFields();
  $("#entry-title").textContent = "Eintrag bearbeiten";
  $("#cancel-edit").hidden = false;
  form.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function saveEntry(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const kind = form.elements.kind.value;
  const payload = {
    kind, at: toInstant(state.day, form.elements.at.value),
    carbs: ["meal", "snack", "carbs"].includes(kind) ? form.elements.carbs.value : 0,
    insulin: ["meal", "snack", "correction"].includes(kind) ? form.elements.insulin.value || 0 : 0,
    sport_kind: form.elements.sport_kind.value,
    duration_minutes: form.elements.duration_minutes.value || 0,
    notes: form.elements.notes.value,
    id: state.editing?.id || null, original_day: state.day,
    event_id: state.pendingEventId || crypto.randomUUID(),
  };
  state.pendingEventId = payload.event_id;
  const button = $('button[type="submit"]', form);
  button.disabled = true;
  try {
    const result = await api("/api/treatment", { method: "POST", body: JSON.stringify(payload) });
    resetEntryForm();
    state.day = result.day;
    updateLocation(true);
    toast("Eintrag in Nightscout gespeichert");
    await loadDay();
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
  const block = event.target.closest(".sport-review");
  if (!block) return;
  const select = $(".sport-status", block);
  const effect = $(".sport-effect", block);
  const button = event.target;
  button.disabled = true;
  try {
    await api("/api/analysis/sport", { method: "POST", body: JSON.stringify({
      meal_id: select.dataset.mealId, day: select.dataset.day,
      status: select.value, effect_mgdl: effect.value || 0,
    }) });
    toast("Sporteinfluss gespeichert");
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
    sport_effects_mgdl: Object.fromEntries(["jogging", "gym", "light_cardio"]
      .map(kind => [kind, form.elements["sport_" + kind].value])),
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

async function loadSummary() {
  $("#summary").innerHTML = '<div class="empty">Analyse wird geladen …</div>';
  $("#night-summary-cards").innerHTML = '<div class="empty">Nachtübersicht wird geladen …</div>';
  $("#night-records").innerHTML = "";
  $("#calculations").innerHTML = "";
  try {
    const days = $("#summary-days").value;
    const data = await api("/api/summary?days=" + days);
    renderNightSummary(data.night || { count: 0, unavailable_count: Number(days), nights: [] });
    if (data.profile_sync_error) {
      $("#sync-progress").textContent = "Korrekturfaktor-Profil nicht aktualisiert: " + data.profile_sync_error;
    }
    const breakfastSubcards = () => '<div class="breakfast-subcards">' + ["work", "free"].map(dayType => {
      const subset = data.groups.breakfast.by_day_type[dayType];
      return '<div class="breakfast-subcard"><span>' + (dayType === "work" ? "Arbeit" : "Frei") + '</span>' +
        '<strong>' + (subset.count ? escapeHTML(num(subset.mean_units_per_ke, 3)) : '—') +
        ' <small>IE/KE</small></strong>' +
        '<small>Median: ' + (subset.count ? escapeHTML(num(subset.median_units_per_ke, 3)) + ' IE/KE' : '—') +
        ' · ' + subset.count + ' Werte</small></div>';
    }).join("") + '</div>';
    $("#summary").innerHTML = ["breakfast", "lunch", "dinner"].map(kind => {
      const row = data.groups[kind];
      const hasValues = row.count > 0;
      const value = field => hasValues ? escapeHTML(num(row[field], 3)) + ' <small>IE/KE</small>' : "—";
      const typical = row.q1_units_per_ke != null
        ? '<div class="summary-metric"><span>Mittlere 50 % der berücksichtigten Werte</span><strong>' +
          escapeHTML(num(row.q1_units_per_ke, 3)) + '–' + escapeHTML(num(row.q3_units_per_ke, 3)) +
          ' <small>IE/KE</small></strong></div>'
        : '';
      const range = hasValues
        ? '<p class="summary-range">Berücksichtigter Bereich: ' + escapeHTML(num(row.min_units_per_ke, 3)) +
          '–' + escapeHTML(num(row.max_units_per_ke, 3)) + ' IE/KE</p>'
        : '';
      const trend = row.trend_units_per_ke == null ? '' :
        '<p class="summary-range">Letzte 3 vs. vorherige 3 · Median ' +
        escapeHTML(num(row.recent_median_units_per_ke, 3)) + ' statt ' +
        escapeHTML(num(row.previous_median_units_per_ke, 3)) + ' IE/KE · Änderung ' +
        escapeHTML((Number(row.trend_units_per_ke) > 0 ? '+' : '') + num(row.trend_units_per_ke, 3)) +
        ' IE/KE</p>';
      return '<div class="summary-card"><span class="meal-type">' + escapeHTML(names[kind]) + '</span>' +
        '<div class="summary-primary"><span>Durchschnitt</span><strong>' + value("mean_units_per_ke") + '</strong></div>' +
        '<div class="summary-metric"><span>Median</span><strong>' + value("median_units_per_ke") + '</strong></div>' +
        (hasValues ? typical : '') + range + trend +
        '<p>' + row.count + ' von ' + row.valid_count + ' berechenbaren Mahlzeiten berücksichtigt · ' +
        row.excluded_count + ' ausgeklammert · ' + row.blocked_count + ' nicht berechenbar</p>' +
        (kind === "breakfast" ? breakfastSubcards() : '') + '</div>';
    }).join("");
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
      ? '<div class="table-wrap"><table class="analysis-table"><thead><tr><th>Uhrzeit</th><th>Mahlzeit</th><th>Insulinfaktor</th><th>KH</th><th>Insulin</th><th>Gemessen</th><th>Erwartet</th><th>Delta</th><th>Endpunkt</th><th>Sporteffekt</th><th>Ausklammern</th><th></th></tr></thead><tbody>' +
        (() => {
          let previousDay = "";
          return calculations.map(item => {
            const newDay = item.day !== previousDay;
            previousDay = item.day;
            const dayHeading = newDay
              ? '<tr class="analysis-day-row"><th colspan="12" scope="colgroup">' +
                escapeHTML(new Date(item.day + "T12:00:00").toLocaleDateString("de-DE", {
                  weekday: "long", day: "numeric", month: "long", year: "numeric",
                })) + ' <label class="day-type-label">Tag: <select class="day-type" data-day="' +
                escapeHTML(item.day) + '"><option value="work"' + (item.day_type === "work" ? " selected" : "") +
                '>Arbeit</option><option value="free"' + (item.day_type === "free" ? " selected" : "") +
                '>Frei</option></select></label></th></tr>'
              : '';
            const factor = item.units_per_ke != null
              ? escapeHTML(num(item.units_per_ke, 3)) + ' IE/KE' +
                (item.strong_outlier ? ' <span class="outlier-badge">Stark abweichend</span>' : '')
              : '<span class="summary-unavailable">Nicht berechenbar: ' + escapeHTML(item.reason || "Kein Faktor") + '</span>';
            const excluded = item.units_per_ke != null && item.excluded;
            const sport = item.sport || {};
            const status = sport.status || "auto";
            const sportReview = '<div class="sport-review sport-review-table">' +
              (sport.suggested ? '<small>Vorschlag: ' + escapeHTML(num(sport.suggested_effect_mgdl, 0)) + ' mg/dL</small>' : '') +
              '<select class="sport-status" data-meal-id="' + escapeHTML(item.id) + '" data-day="' + escapeHTML(item.day) + '">' +
              '<option value="auto"' + (status === "auto" ? " selected" : "") + '>Offen</option>' +
              '<option value="affected"' + (status === "affected" ? " selected" : "") + '>Wirkte mit</option>' +
              '<option value="unaffected"' + (status === "unaffected" ? " selected" : "") + '>Kein Einfluss</option></select>' +
              '<input class="sport-effect" type="number" min="0" max="500" step="1" aria-label="Sporteinfluss in mg/dL" value="' +
              escapeHTML(status === "affected" ? sport.effect_mgdl : sport.suggested_effect_mgdl || 0) + '">' +
              '<button type="button" class="save-sport quiet">Übernehmen</button></div>';
            return dayHeading + '<tr class="' + (excluded ? "analysis-excluded " : "") +
              (item.strong_outlier ? "analysis-outlier" : "") + '">' +
              '<td>' + escapeHTML(timeOnly(item.at)) + '</td>' +
            '<td>' + escapeHTML(names[item.kind]) + '</td><td><b>' + factor + '</b></td>' +
            '<td>' + (item.carbs_total == null ? "—" : escapeHTML(num(item.carbs_total)) + ' g') + '</td>' +
            '<td>' + (item.rapid_units_in_window == null ? "—" : escapeHTML(num(item.rapid_units_in_window)) + ' IE') + '</td>' +
            '<td>' + glucoseChangeHTML(item.delta_mgdl) + '</td>' +
            '<td>' + glucoseChangeHTML(item.expected_delta_mgdl) + '</td>' +
            '<td>' + glucoseChangeHTML(item.deviation_mgdl) + '</td>' +
            '<td>' + (item.end ? escapeHTML(timeOnly(item.end)) : "—") + '</td>' +
            '<td>' + sportReview + '</td>' +
            '<td>' + (item.units_per_ke == null ? "—" : '<label class="exclude-label"><input type="checkbox" class="exclude-meal" data-meal-id="' +
              escapeHTML(item.id) + '" data-day="' + escapeHTML(item.day) + '"' + (excluded ? " checked" : "") +
              '> <span>Ausklammern</span></label>') + '</td>' +
              '<td><button type="button" class="quiet open-meal-day" data-day="' + escapeHTML(item.day) + '">Tagebuch</button></td></tr>';
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
  const input = event.target;
  if (!input.matches(".exclude-meal")) return;
  input.disabled = true;
  try {
    await api("/api/analysis/exclusion", {
      method: "POST",
      body: JSON.stringify({ meal_id: input.dataset.mealId, day: input.dataset.day, excluded: input.checked }),
    });
    await loadSummary();
  } catch (error) {
    input.checked = !input.checked;
    input.disabled = false;
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
    state.day = nextLocation.day;
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
  $("#entry-kind").addEventListener("change", updateEntryFields);
  $("#cancel-edit").addEventListener("click", resetEntryForm);
  $("#help-carbs").addEventListener("input", scheduleDoseHelp);
  $("#dose-result").addEventListener("click", saveDoseSuggestion);
  $("#entry-form").elements.carbs.addEventListener("input", event => {
    if (["meal", "snack", "carbs"].includes($("#entry-kind").value)) {
      $("#help-carbs").value = event.target.value || 0;
      scheduleDoseHelp();
    }
  });
  $("#diary-view").addEventListener("click", event => {
    if (event.target.closest(".delete-treatment")) deleteTreatment(event);
    const edit = event.target.closest(".edit-entry");
    if (edit) editEntry(edit.dataset.id);
    if (event.target.closest(".save-sport")) saveSportMark(event);
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
  $("#calculations").addEventListener("change", saveAnalysisExclusion);
  $("#calculations").addEventListener("change", saveDayType);
  $("#calculations").addEventListener("click", event => {
    if (event.target.closest(".save-sport")) {
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
  try {
    await loadSettings();
    await loadDay();
  } catch (error) {
    toast(error.message, true);
  }
});
