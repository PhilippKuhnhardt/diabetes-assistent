// Languages of the page (German and English). Every text the page shows lives in web/i18n/<lang>.js under
// a key; components call t("area.key", { name: value }). German is the default and the reference: a key
// missing in another language falls back to German, a key missing everywhere shows the key itself.
// The language is fixed while the page lives: a change in the settings stores it and reloads the page,
// so module-level labels (meal names, sport kinds) can be built once.
import de from "../i18n/de.js";
import en from "../i18n/en.js";
import { GLUCOSE_UNIT } from "./units.js";

const DICTIONARIES = { de, en };
export const DEFAULT_LANGUAGE = "de";
// Shown in the language switch, each in its own language.
export const LANGUAGES = [["de", "Deutsch"], ["en", "English"]];
// Number and date formats per language (24-hour clock and day before month in both).
const LOCALES = { de: "de-DE", en: "en-GB" };
const STORAGE_KEY = "language";

// The stored choice wins, else the first browser language the app speaks, else German.
function detectLanguage() {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (DICTIONARIES[stored]) return stored;
  } catch (_) { /* Private mode: follow the browser. */ }
  const wanted = typeof navigator === "undefined" ? [] : navigator.languages?.length ? navigator.languages : [navigator.language];
  for (const tag of wanted) {
    const base = String(tag || "").slice(0, 2).toLowerCase();
    if (DICTIONARIES[base]) return base;
  }
  return DEFAULT_LANGUAGE;
}

export const lang = detectLanguage();
export const locale = LOCALES[lang];
// Whether the language was picked by hand (settings show "automatic" otherwise).
export function storedLanguage() {
  try { return DICTIONARIES[localStorage.getItem(STORAGE_KEY)] ? localStorage.getItem(STORAGE_KEY) : null; } catch (_) { return null; }
}

// Stores the choice (null: follow the browser again) and reloads, so every text follows.
export function setLanguage(code) {
  try {
    if (code) localStorage.setItem(STORAGE_KEY, code);
    else localStorage.removeItem(STORAGE_KEY);
  } catch (_) { /* Without storage the choice cannot outlive the reload. */ }
  window.location.reload();
}

const plurals = new Intl.PluralRules(locale);
const lookup = (dictionary, key) => key.split(".").reduce((node, part) => node == null ? undefined : node[part], dictionary);

function entry(key, params) {
  let value = lookup(DICTIONARIES[lang], key);
  if (value == null) value = lookup(DICTIONARIES[DEFAULT_LANGUAGE], key);
  if (value == null) return key;
  // Plural forms: { one, other } chosen by params.count.
  if (typeof value === "object") value = value[plurals.select(Number(params?.count ?? 0))] ?? value.other;
  // Texts name the glucose unit as mg/dL; with mmol/L the callers pass converted numbers (web/ui/units.js).
  return String(value).replaceAll("mg/dL", GLUCOSE_UNIT);
}

// The text of a key with {name} placeholders filled in.
export function t(key, params) {
  const text = entry(key, params);
  return params ? text.replace(/\{(\w+)\}/g, (match, name) => params[name] == null ? match : String(params[name])) : text;
}

// The same for text with markup inside: placeholders may be Preact nodes; the result is an array to render.
export function tx(key, params = {}) {
  return entry(key, params).split(/\{(\w+)\}/).map((part, index) => index % 2 ? params[part] ?? "" : part).filter(part => part !== "");
}

// The static page (index.html): data-i18n sets the text, data-i18n-<attribute> an attribute
// (aria-label, title, placeholder), each with a key.
export function translateDocument(root = document) {
  document.documentElement.lang = lang;
  for (const node of root.querySelectorAll("[data-i18n]")) node.textContent = t(node.dataset.i18n);
  for (const attribute of ["aria-label", "title", "placeholder"]) {
    for (const node of root.querySelectorAll("[data-i18n-" + attribute + "]")) node.setAttribute(attribute, t(node.getAttribute("data-i18n-" + attribute)));
  }
}
