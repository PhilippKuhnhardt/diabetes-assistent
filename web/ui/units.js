// Display units: glucose in mg/dL or mmol/L, the meal factor as IE per KE (10 g), IE per BE (12 g) or g per IE.
// The server, the API and every calculation stay in mg/dL and IE per KE; only what the page shows and reads
// from inputs is converted here. The settings (glucose_unit, carb_unit) are the source; the device keeps a
// copy so labels built at import time already know the unit, and app.js reloads the page once when the
// settings name other units than the copy.
const KEY = "units";
const RELOADED = "units-reloaded";
export const MGDL_PER_MMOL = 18.0182;
const GLUCOSE = ["mgdl", "mmol"];
const CARBS = ["ke", "be", "g"];
const GRAMS = { ke: 10, be: 12 };

function stored() {
  try {
    const value = JSON.parse(localStorage.getItem(KEY) || "{}");
    return { glucose: GLUCOSE.includes(value.glucose) ? value.glucose : "mgdl", carbs: CARBS.includes(value.carbs) ? value.carbs : "ke" };
  } catch (_) {
    return { glucose: "mgdl", carbs: "ke" };
  }
}

export const units = stored();
export const mmol = units.glucose === "mmol";
export const GLUCOSE_UNIT = mmol ? "mmol/L" : "mg/dL";

// Called with every /api/settings answer: stores other units and reloads once (never in a loop, also not
// when the device cannot store them; then the page keeps the defaults).
export function syncUnits(settings) {
  const wanted = { glucose: GLUCOSE.includes(settings?.glucose_unit) ? settings.glucose_unit : "mgdl",
    carbs: CARBS.includes(settings?.carb_unit) ? settings.carb_unit : "ke" };
  if (wanted.glucose === units.glucose && wanted.carbs === units.carbs) return;
  try {
    if (sessionStorage.getItem(RELOADED) === JSON.stringify(wanted)) return;
    localStorage.setItem(KEY, JSON.stringify(wanted));
    sessionStorage.setItem(RELOADED, JSON.stringify(wanted));
  } catch (_) {
    return;
  }
  window.location.reload();
}

// A glucose value or change given in mg/dL as a number in the display unit (mmol/L rounded to one decimal).
export const glValue = mgdl => mmol ? Math.round(Number(mgdl) / MGDL_PER_MMOL * 10) / 10 : Number(mgdl);
// A value the page shows in an input (display unit) and the mg/dL it stands for.
export const glInput = mgdl => mgdl == null || mgdl === "" ? "" : glValue(mgdl);
export const toMgdl = value => value === "" || value == null ? value : mmol ? Math.round(Number(value) * MGDL_PER_MMOL) : Number(value);
// Input attributes for a glucose field whose limits are given in mg/dL.
export const glField = (min, max, step = 1) => mmol
  ? { min: Math.round(min / MGDL_PER_MMOL * 10) / 10, max: Math.round(max / MGDL_PER_MMOL * 10) / 10, step: 0.1 }
  : { min, max, step };

// The meal factor (IE per KE from the server) in the display unit: IE/KE, IE/BE or g/IE.
export const gramsPerIe = units.carbs === "g";
export const factorValue = unitsPerKe => {
  const value = Number(unitsPerKe);
  if (gramsPerIe) return value > 0 ? 10 / value : NaN;
  return value * GRAMS[units.carbs] / 10;
};
// Back from the display unit to IE per KE (settings, I:C schedule).
export const factorToUnitsPerKe = value => gramsPerIe ? 10 / Number(value) : Number(value) * 10 / GRAMS[units.carbs];
// Grams of carbs in one carb unit (KE 10, BE 12; null for grams).
export const carbUnitGrams = GRAMS[units.carbs] || null;
