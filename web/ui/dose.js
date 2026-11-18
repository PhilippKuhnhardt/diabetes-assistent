// The dose help under the entry form: the server's suggestion (/api/entry/preview `dose`) as option
// buttons or, for a meal, the dose slider (user request 2026-11-15): one scale from little to much need,
// the recommendation flagged, and where glucose likely ends for the chosen dose (the expected value of the
// normal dose moved by the KF, with the corridor past doses showed around it: dark = every second case,
// line = 8 of 10). The component only draws; app.js keeps the choice, fills the insulin field and saves.
import { html } from "./html.js";
import { num, factorNum, signedChange, signedPercent, timeOnly, SPLIT_TIMING, Icon } from "./lib.js";

const LAND_AXIS = [40, 280];
const landX = value => Math.max(0, Math.min(100, (value - LAND_AXIS[0]) / (LAND_AXIS[1] - LAND_AXIS[0]) * 100));
const landClass = value => value < 90 ? "is-low" : value > 160 ? "is-high" : "is-ok";
const landColor = value => value < 70 ? "#d9534f" : value < 90 ? "#eb9b8f" : value <= 160 ? "#8fcaa6" : value <= 200 ? "#f2cf86" : "#e9b45a";
export const halfUnits = value => Math.round(value * 2) / 2;

// fast / slow: ends of a correction after a meal; the one the rise leans to is marked by colour only.
const doseLabels = { split: "2. Teil", fast: "Wenig", normal: "Normal", slow: "Viel", low: "Tiefer", cautious: "Vorsichtig", carbs: "Nur KH" };

export function sliderLanding(slider, units) {
  if (slider.expected_mgdl == null || slider.isf_mgdl_per_unit == null) return null;
  const center = Number(slider.expected_mgdl) + (Number(slider.normal_units) - units) * Number(slider.isf_mgdl_per_unit);
  return slider.corridor_mgdl.map(offset => Math.round(center + Number(offset)));
}

// The slider's dose: the dragged or typed one, else the recommendation. Own IE may lie outside the
// slider: unclamped they show as typed, the thumb stops at the end.
export function sliderUnits(slider, chosen, clamp = true) {
  const lo = Number(slider.min_units), hi = Number(slider.max_units);
  const value = chosen?.units ?? Number(slider.recommended_units);
  return clamp || chosen?.follow !== false ? Math.max(lo, Math.min(hi, value)) : value;
}

// With a split the first part is given now: the share of the total, in half units.
export function firstPart(slider, units) {
  return slider.first_percent != null ? halfUnits(units * Number(slider.first_percent) / 100) : null;
}

// The second split part's amount comes from the forecast; the note names the plan and when it fits.
function splitOptionNote(option) {
  const planned = "geplant " + num(option.planned_units, 1) + " IE um " + timeOnly(option.due_at);
  const when = option.state === "wait" ? SPLIT_TIMING.wait + " bis " + timeOnly(option.latest_at) : SPLIT_TIMING[option.state];
  return [option.from_forecast && Number(option.units) <= 0 ? "laut Prognose nicht nötig" : "", planned, when].filter(Boolean).join(" · ");
}

function DoseSlider({ slider, chosen, onSlide }) {
  const lo = Number(slider.min_units), hi = Number(slider.max_units), rec = Number(slider.recommended_units);
  const pos = value => hi > lo ? (value - lo) / (hi - lo) * 100 : 50;
  const units = sliderUnits(slider, chosen, false);
  const ends = [lo, hi].map(value => sliderLanding(slider, value));
  const land = sliderLanding(slider, units);
  const first = firstPart(slider, units);
  let track = null;
  if (ends[0]) {
    const stops = [];
    for (let i = 0; i <= 20; i += 1) stops.push(landColor(sliderLanding(slider, lo + (hi - lo) * i / 20)[2]) + " " + (i * 5) + "%");
    track = "linear-gradient(90deg," + stops.join(",") + ")";
  }
  const flagShift = pos(rec) < 15 ? -12 : pos(rec) > 85 ? -88 : -50;
  const span = (from, to) => ({ left: landX(from) + "%", width: (landX(to) - landX(from)) + "%" });
  return html`<div class="ds">
    <div class="ds-read"><div class="ds-dose"><strong>${num(units, 1)}<small>IE</small></strong>
      <span class="ds-split">${first != null ? "jetzt " + num(first, 1) + " + " + num(units - first, 1) + " um " + timeOnly(slider.later_at) : ""}</span></div>
      ${land && html`<div class="ds-land"><span>erwartet in 4 h</span><b class=${"ds-pill " + landClass(land[2])}>${land[2]}<small>mg/dL</small></b>
        <small class="ds-iqr">meist ${land[1]}–${land[3]}</small></div>`}</div>
    ${land && html`<div class="ds-cor" aria-hidden="true"><div class="ds-cor-axis">
      <div class="ds-cor-low" style=${{ width: landX(70) + "%" }}></div><div class="ds-cor-zone" style=${span(90, 160)}></div>
      <div class="ds-cor-w" style=${span(land[0], land[4])}></div><div class="ds-cor-iqr" style=${span(land[1], land[3])}></div>
      <div class="ds-cor-med" style=${{ left: landX(land[2]) + "%" }}></div></div>
      <div class="ds-cor-scale">${[70, 90, 160, 250].map(value => html`<span style=${{ left: landX(value) + "%" }}>${value}</span>`)}</div></div>`}
    <div class="ds-rail"><div class="ds-flag" style=${{ left: pos(rec) + "%", transform: "translateX(" + flagShift + "%)" }}><b>empfohlen ${num(rec, 1)}</b></div>
      <i class="ds-stem" style=${{ left: "calc(14px + (100% - 28px) * " + pos(rec) / 100 + ")" }}></i>
      <input type="range" class="ds-range" min=${lo} max=${hi} step="0.5" value=${sliderUnits(slider, chosen)} aria-label="Insulin in IE"
        style=${track ? { "--ds-track": track } : null} onInput=${event => onSlide(Number(event.currentTarget.value))} /></div>
    <div class="ds-ends"><span>${num(lo, 1)} IE${ends[0] ? " · " + ends[0][2] : ""}</span><span>${num(hi, 1)} IE${ends[1] ? " · " + ends[1][2] : ""}</span></div></div>`;
}

function DoseOption({ option, before, chosen, onChoose }) {
  const label = doseLabels[option.variant];
  return html`<button type="button" class=${"dose-option apply-dose" + (before ? " is-changed" : "") + (option.suggested ? " is-suggested" : "")}
    data-variant=${option.variant} aria-pressed=${String(chosen)} title=${label + ": " + num(option.units, 2) + " IE direkt in Nightscout speichern"}
    disabled=${!option.savable} onClick=${() => onChoose(option.variant)}>
    <span class="dose-label">${label}</span><strong>${num(option.units, 2)} <small>IE</small></strong>
    <span class="dose-expected">${option.expected_mgdl != null ? "→ " + num(option.expected_mgdl, 0) + " mg/dL" : "ohne Korrektur"}</span>
    ${option.later_units != null && html`<span class="dose-expected">+ ${num(option.later_units, 1)} IE um ${timeOnly(option.later_at)}</span>`}
    ${option.variant === "split" && html`<span class="dose-expected">${splitOptionNote(option)}</span>`}
    ${before && html`<span class="dose-was">vorher ${num(before.units, 2)} IE${before.later_units != null && Number(before.later_units) !== Number(option.later_units)
      ? " + " + num(before.later_units, 1) : ""}</span>`}</button>`;
}

// The factor all suggestions use; a stand-in is named while Automatik lacks meals.
function FactorLine({ data }) {
  if (data.units_per_ke == null) return null;
  const kf = "KF " + num(data.isf_mgdl_per_unit, 1) + " mg/dL/IE";
  if (!(Number(data.carbs_g) > 0)) return html`<p class="dose-factor">${kf}</p>`;
  const factor = value => html`<strong>${factorNum(value)} IE/KE</strong>`;
  const count = " (" + (data.auto_factor_count || 0) + "/" + data.auto_factor_min_meals + ")";
  const surcharge = data.auto_surcharge_units != null
    ? html` + Zuschlag <strong>${num(data.auto_surcharge_units, 2)} IE</strong>${data.auto_fat_surcharge_mgdl != null ? " (fettreich +" + num(data.auto_fat_surcharge_mgdl, 0) + " mg/dL)" : ""}${data.solo_snack ? " · Snack ohne Mahlzeit, Zuschlag wie Hauptmahlzeit" : ""}`
    : data.auto_method === "surcharge" && data.auto_units_per_ke != null ? " · Snack ohne Zuschlag" : "";
  // The place factor is already in the factor and surcharge shown.
  const place = data.place_factor;
  const placeText = !place || data.auto_units_per_ke == null ? ""
    : place.scale != null
      ? html` · Ort ${place.name} <strong>${signedPercent(place.scale)}</strong> (${place.count} Essen, ${place.days} T.)`
      : " · Ort " + place.name + ": noch kein Ortsfaktor (" + place.count + "/" + place.min_meals + " Essen)";
  const used = data.auto_units_per_ke != null
    ? html`Berechneter Faktor ${factor(data.auto_units_per_ke)}${count}${surcharge}${placeText}`
    : html`Berechneter Faktor –${count} · ersatzweise ${data.factor_source === "breakfast_day_type"
      ? "Frühstück " + (data.day_type === "free" ? "frei" : "Arbeit") : "Nightscout"} ${factor(data.units_per_ke)}`;
  return html`<p class="dose-factor">${used} · ${kf}</p>`;
}

// `help` is the preview's dose with options; without it `note` (a message, "wird berechnet", an error) shows.
export function DoseHelp({ help, note, changed = {}, choice, chosen, onChoose, onSlide }) {
  if (!help) return note || null;
  const withCarbs = Number(help.carbs_g) > 0;
  const splitPart = help.options.find(option => option.variant === "split");
  if (help.slider) changed = {};
  const anyChanged = Object.keys(changed).length > 0;
  const board = help.on_board;
  return html`<div class="dose-head"><span class="dose-heading">${withCarbs ? "Empfehlung" : "Korrektur ohne Essen"}</span></div>
    ${anyChanged && html`<p class="dose-changed" role="status"><${Icon} name="alert" /> Neu berechnet mit neueren Daten${help.glucose_at ? " (CGM " + timeOnly(help.glucose_at) + ")" : ""}. Markierte Werte haben sich geändert, bitte prüfen.</p>`}
    <${FactorLine} data=${help} />
    ${help.glucose_source === "finger" && help.glucose_fresh && html`<p class="dose-factor">Startwert blutig <strong>${num(help.glucose_mgdl, 0)} mg/dL</strong> um ${timeOnly(help.glucose_at)}</p>`}
    ${!help.slider && html`<div class="dose-options">${help.options.map(option => html`<${DoseOption} key=${option.variant} option=${option}
      before=${changed[option.variant]} chosen=${option.variant === choice} onChoose=${onChoose} />`)}</div>`}
    ${help.slider && html`<${DoseSlider} slider=${help.slider} chosen=${chosen} onSlide=${onSlide} />`}
    ${help.slider && splitPart && html`<p class="dose-factor">2. Teil: ${splitOptionNote(splitPart)}</p>`}
    ${Number(help.activity_change_mgdl) !== 0 && help.activity_units != null && html`<p class="dose-factor">Sport <strong>${signedChange(help.activity_change_mgdl, 0)}</strong> · ${(Number(help.activity_units) > 0 ? "+" : "") + num(help.activity_units, 2)} IE</p>`}
    ${board && (board.lowering_until || board.raising_until) && html`<p class="dose-factor">Wirkt noch <strong>−${num(board.lowering_mgdl, 0)} / +${num(board.raising_mgdl, 0)} mg/dL</strong> · eingerechnet</p>`}
    ${help.low_note && html`<p class="dose-factor">Tiefer (${num(help.low_target_mgdl, 0)} mg/dL) nicht angeboten: ${help.low_note}</p>`}
    ${help.reason && html`<p class="hint">${help.reason}</p>`}`;
}
