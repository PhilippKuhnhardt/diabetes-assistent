// What every part of the page shares: the API call, the toast, German number and time formats, the
// names of meals and sport kinds, and a few small components (icons, badges, the exclusion button).
// Components only draw what the server sends (AGENTS.md: "web/ only draws"); no domain rule lives here.
import { html } from "./html.js";

export const MINUTE = 60000;
export const names = { breakfast: "Frühstück", lunch: "Mittagessen", dinner: "Abendessen" };
export const CERTAINTY_LABELS = { weighed: "KH sicher", known: "Halbwegs sicher", unsure: "KH unsicher" };
// Meal factors picked on a carb entry; shown as icons wherever the meal or entry appears.
export const MEAL_FACTORS = { alcohol: "Alkohol", fat_protein: "Fettreich", fast: "Schnell" };
export const sportKinds = ["jogging", "gym", "light_cardio", "racing"];
export const sportNames = { jogging: "Joggen", gym: "Gym", light_cardio: "Leichtes Cardio", racing: "Rennsport" };
// When the second part of a split dose is due, as the dose help and "Wirkt noch" word it.
export const SPLIT_TIMING = { early: "jetzt: BZ steigt schnell", due: "jetzt fällig", wait: "BZ tief oder fallend: lieber warten", later: "" };

// ---- API ----
// The app version the page was loaded with; a different one after a deploy marks an update as waiting.
let appVersion = null;
let updateWaiting = false;
export const appUpdateWaiting = () => updateWaiting;

export async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, cache: "no-store", ...options });
  const version = response.headers.get("X-App-Version");
  if (version && !appVersion) appVersion = version;
  else if (version && version !== appVersion) updateWaiting = true;
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || "Anfrage fehlgeschlagen");
    error.code = data.code || null;
    throw error;
  }
  return data;
}

export const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body) });

// ---- Toast ----
let toastTimeout;
// An optional action (e.g. "Rückgängig") adds a button and keeps the toast visible longer.
export function toast(message, error = false, action = null) {
  const node = document.getElementById("toast");
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

// ---- Numbers and times ----
export const num = (value, digits = 2) => Number(value).toLocaleString("de-DE", { maximumFractionDigits: digits, minimumFractionDigits: 0 });
export const factorNum = value => Number(value).toLocaleString("de-DE", { minimumFractionDigits: 1, maximumFractionDigits: 2 });
export const signedChange = (value, digits = 1) => value == null ? "—" : (Number(value) > 0 ? "+" : "") + num(value, digits) + " mg/dL";
// The API stores a sport effect as glucose drop; the UI shows the signed glucose change.
export const sportChange = effect => -Number(effect || 0);
export const timeOnly = value => new Date(value).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
// A place factor as a signed percentage: 0.85 -> "−15 %".
export function signedPercent(scale) {
  const value = Math.round((Number(scale) - 1) * 100);
  return (value > 0 ? "+" : value < 0 ? "−" : "±") + Math.abs(value) + " %";
}
export const dayDate = (day, options) => new Date(day + "T12:00:00").toLocaleDateString("de-DE", options);
export const minuteClock = minute => String(Math.floor(minute / 60)).padStart(2, "0") + ":" + String(minute % 60).padStart(2, "0");
export function clockMinute(value) {
  const [hours, minutes] = value.split(":").map(Number);
  return hours * 60 + minutes;
}

export const localInputTime = date => {
  const d = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return d.toISOString().slice(0, 16);
};

export const toInstant = (date, clock) => {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !/^([01]\d|2[0-3]):[0-5]\d$/.test(clock)) throw new Error("Zeit bitte im 24-Stunden-Format HH:MM eingeben");
  const parsed = new Date(date + "T" + clock + ":00");
  if (!Number.isFinite(parsed.getTime()) || localInputTime(parsed) !== date + "T" + clock) throw new Error("Dieser lokale Zeitpunkt existiert nicht");
  return parsed.toISOString();
};

// Keep an existing fractional value valid while number-input arrows use the requested step.
export function pickerMinimum(value, step, positive = false) {
  const remainder = Number((((value % step) + step) % step).toFixed(2));
  return positive && remainder === 0 ? step : remainder;
}

const trendSymbols = { up_fast: "↑", up: "↗", flat: "→", down: "↘", down_fast: "↓" };
export const trendSymbol = direction => trendSymbols[direction] || "";

// The browser position for the place suggestion and for adding a spot to a place.
export function currentPosition() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) { reject({ code: 0 }); return; }
    navigator.geolocation.getCurrentPosition(position => resolve({
      lat: position.coords.latitude, lon: position.coords.longitude,
      accuracy_m: Math.round(position.coords.accuracy), captured_at: new Date(position.timestamp).toISOString(),
    }), reject, { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 });
  });
}

// ---- Small components ----
const ICONS = {
  diary: html`<path d="M4 4h14a2 2 0 0 1 2 2v14H6a2 2 0 0 1-2-2V4Z"/><path d="M8 4v16M11 9h6M11 13h6"/>`,
  check: html`<path d="m4 12 5 5L20 6"/>`,
  exclude: html`<circle cx="12" cy="12" r="9"/><path d="m8 8 8 8"/>`,
  plus: html`<path d="M12 5v14M5 12h14"/>`,
  remove: html`<path d="M5 12h14"/>`,
  alert: html`<path d="M12 3 2.5 20h19L12 3Z"/><path d="M12 10v4M12 17h.01"/>`,
};
export const Icon = ({ name }) => html`<svg class="ui-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
  stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;

export const GlucoseChange = ({ value }) => value == null ? "—" : html`<span class=${"glucose-change " +
  (Number(value) > 0 ? "rising" : Number(value) < 0 ? "falling" : "steady")}>${signedChange(value)}</span>`;

export function FactorIcons({ marks }) {
  const on = Object.keys(MEAL_FACTORS).filter(key => marks?.[key]);
  if (!on.length) return null;
  const label = on.map(key => MEAL_FACTORS[key]).join(", ");
  return html`<span class="factor-icons" title=${label} role="img" aria-label=${label}>${on.map(key =>
    html`<span class=${"factor-icon factor-" + key}></span>`)}</span>`;
}

export const DishBadge = ({ dish }) => dish ? html`<span class="place-badge dish-badge" title="Gericht">${dish.name}</span>` : null;
export const PlaceBadge = ({ place }) => place ? html`<span class="place-badge" title="Ort">${place.name}</span>` : null;
export const CertaintyBadge = ({ level }) => level
  ? html`<span class=${"outlier-badge certainty-" + level}>${CERTAINTY_LABELS[level]}</span>` : null;

// Ausklammern: the server decides which meals can be excluded; the button only flips the mark.
export function ExcludeButton({ mealId, day, excluded, onDone }) {
  const label = excluded ? "Mahlzeit wieder berücksichtigen" : "Mahlzeit ausklammern";
  async function flip(event) {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await post("/api/analysis/exclusion", { meal_id: mealId, day, excluded: !excluded });
      await onDone?.();
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  }
  return html`<button type="button" class=${"exclude-meal" + (excluded ? " is-excluded" : "")} aria-pressed=${String(excluded)}
    aria-label=${label} title=${label} onClick=${flip}><${Icon} name=${excluded ? "exclude" : "check"} /><span>${excluded ? "Ausgeklammert" : "Berücksichtigt"}</span></button>`;
}

// Disables a button while its async action runs; errors become a toast.
export async function busy(event, action) {
  const button = event?.currentTarget;
  if (button) button.disabled = true;
  try {
    return await action();
  } catch (error) {
    toast(error.message, true);
    return undefined;
  } finally {
    if (button) button.disabled = false;
  }
}
