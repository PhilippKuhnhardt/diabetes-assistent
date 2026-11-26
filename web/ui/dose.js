// The dose help under the entry form: the server's suggestion (/api/entry/preview `dose`) as option
// buttons or, for a meal, the dose slider: one scale from little to much need,
// the recommendation flagged, and where glucose likely ends for the chosen dose (the expected value of the
// normal dose moved by the KF, with the corridor past doses showed around it: dark = every second case,
// line = 8 of 10). The component only draws; app.js keeps the choice, fills the insulin field and saves.
import { html } from "./html.js";
import { num, glNum, gl, signedChange, timeOnly, SPLIT_TIMING, UNIT, Icon } from "./lib.js";
import { t, tx } from "./i18n.js";
import { prefs } from "./prefs.js";

const LAND_AXIS = [40, 280];
const landX = value => Math.max(0, Math.min(100, (value - LAND_AXIS[0]) / (LAND_AXIS[1] - LAND_AXIS[0]) * 100));
const landClass = value => value < 90 ? "is-low" : value > 160 ? "is-high" : "is-ok";
const landColor = value => value < prefs.lowMgdl ? "#d9534f" : value < 90 ? "#eb9b8f" : value <= 160 ? "#8fcaa6" : value <= 200 ? "#f2cf86" : "#e9b45a";
export const halfUnits = value => Math.round(value * 2) / 2;

// fast / slow: ends of a correction after a meal; the one the rise leans to is marked by colour only.
const doseLabels = Object.fromEntries(["split", "fast", "normal", "slow", "cautious", "carbs"].map(key => [key, t("dose.variants." + key)]));

export function sliderLanding(slider, units) {
  if (slider.expected_mgdl == null || slider.isf_mgdl_per_unit == null) return null;
  const center = Number(slider.expected_mgdl) + (Number(slider.normal_units) - units) * Number(slider.isf_mgdl_per_unit);
  return slider.corridor_mgdl.map(offset => Math.round(center + Number(offset)));
}

// The slider's dose: the dragged or typed one, else the recommendation. Own IE may lie outside the
// slider: unclamped they show as typed, the thumb stops at the end.
export function sliderUnits(slider, chosen, clamp = true) {
  // The "0" button beside the slider: no insulin, e.g. for a small snack.
  if (chosen?.zero) return 0;
  const lo = Number(slider.min_units), hi = Number(slider.max_units);
  const value = chosen?.units ?? Number(slider.recommended_units);
  return clamp || chosen?.follow !== false ? Math.max(lo, Math.min(hi, value)) : value;
}

// With a split the first part is given now: the share of the total, in half units.
export function firstPart(slider, units) {
  // Calculated fat/protein surcharge: the surcharge comes later, the rest now.
  if (slider.later_units != null) return Math.max(0, units - Number(slider.later_units));
  return slider.first_percent != null ? halfUnits(units * Number(slider.first_percent) / 100) : null;
}

// The second split part's amount comes from the forecast; the note names the plan and when it fits.
function splitOptionNote(option) {
  const planned = t("dose.planned", { units: num(option.planned_units, 1), unit: UNIT.insulin, time: timeOnly(option.due_at) });
  const when = option.state === "wait" ? t("today.waitUntil", { wait: SPLIT_TIMING.wait, time: timeOnly(option.latest_at) }) : SPLIT_TIMING[option.state];
  return [option.from_forecast && Number(option.units) <= 0 ? t("dose.notNeeded") : "", planned, when].filter(Boolean).join(" · ");
}

function DoseSlider({ slider, chosen, onSlide, onZero }) {
  const lo = Number(slider.min_units), hi = Number(slider.max_units), rec = Number(slider.recommended_units);
  const pos = value => hi > lo ? (value - lo) / (hi - lo) * 100 : 50;
  const units = sliderUnits(slider, chosen, false);
  const ends = [lo, hi].map(value => sliderLanding(slider, value));
  const land = sliderLanding(slider, units);
  const first = firstPart(slider, units);
  const zero = Boolean(chosen?.zero);
  let track = null;
  if (ends[0]) {
    const stops = [];
    for (let i = 0; i <= 20; i += 1) stops.push(landColor(sliderLanding(slider, lo + (hi - lo) * i / 20)[2]) + " " + (i * 5) + "%");
    track = "linear-gradient(90deg," + stops.join(",") + ")";
  }
  const flagShift = pos(rec) < 15 ? -12 : pos(rec) > 85 ? -88 : -50;
  const span = (from, to) => ({ left: landX(from) + "%", width: (landX(to) - landX(from)) + "%" });
  return html`<div class="ds">
    <div class="ds-read"><div class="ds-dose"><strong>${num(units, 1)}<small>${UNIT.insulin}</small></strong>
      <span class="ds-split">${zero ? t("dose.noInsulinShort") : first != null ? t("dose.splitNow", { first: num(first, 1), rest: num(units - first, 1), time: timeOnly(slider.later_at) }) : ""}</span></div>
      ${land && html`<div class="ds-land"><span>${t("dose.expectedIn4h")}</span><b class=${"ds-pill " + landClass(land[2])}>${glNum(land[2])}<small>${UNIT.glucose}</small></b>
        <small class="ds-iqr">${t("dose.mostly", { low: glNum(land[1]), high: glNum(land[3]) })}</small></div>`}</div>
    ${land && html`<div class="ds-cor" aria-hidden="true"><div class="ds-cor-axis">
      <div class="ds-cor-low" style=${{ width: landX(prefs.lowMgdl) + "%" }}></div><div class="ds-cor-zone" style=${span(90, 160)}></div>
      <div class="ds-cor-w" style=${span(land[0], land[4])}></div><div class="ds-cor-iqr" style=${span(land[1], land[3])}></div>
      <div class="ds-cor-med" style=${{ left: landX(land[2]) + "%" }}></div></div>
      <div class="ds-cor-scale">${[prefs.lowMgdl, 90, 160, prefs.highMgdl].map(value => html`<span style=${{ left: landX(value) + "%" }}>${glNum(value)}</span>`)}</div></div>`}
    <div class=${"ds-rail" + (zero ? " is-zero" : "")}><button type="button" class="ds-zero" aria-pressed=${String(zero)} aria-label=${t("dose.noInsulin")}
      title=${t("dose.noInsulinTitle", { unit: UNIT.insulin })} onClick=${onZero}>0<small>${UNIT.insulin}</small></button>
      <div class="ds-slide"><div class="ds-flag" style=${{ left: pos(rec) + "%", transform: "translateX(" + flagShift + "%)" }}><b>${t("dose.recommended", { units: num(rec, 1) })}</b></div>
      <i class="ds-stem" style=${{ left: "calc(14px + (100% - 28px) * " + pos(rec) / 100 + ")" }}></i>
      <input type="range" class="ds-range" min=${lo} max=${hi} step="0.5" value=${zero ? Number(slider.recommended_units) : sliderUnits(slider, chosen)} aria-label=${t("dose.insulinIn", { unit: UNIT.insulin })}
        style=${track ? { "--ds-track": track } : null} onInput=${event => onSlide(Number(event.currentTarget.value))} /></div></div>
    <div class="ds-ends"><span>${num(lo, 1)} ${UNIT.insulin}${ends[0] ? " · " + glNum(ends[0][2]) : ""}</span><span>${num(hi, 1)} ${UNIT.insulin}${ends[1] ? " · " + glNum(ends[1][2]) : ""}</span></div></div>`;
}

function DoseOption({ option, before, chosen, onChoose }) {
  const label = doseLabels[option.variant];
  return html`<button type="button" class=${"dose-option apply-dose" + (before ? " is-changed" : "") + (option.suggested ? " is-suggested" : "")}
    data-variant=${option.variant} aria-pressed=${String(chosen)} title=${t("dose.optionTitle", { label, units: num(option.units, 2), unit: UNIT.insulin })}
    disabled=${!option.savable} onClick=${() => onChoose(option.variant)}>
    <span class="dose-label">${label}</span><strong>${num(option.units, 2)} <small>${UNIT.insulin}</small></strong>
    <span class="dose-expected">${option.expected_mgdl != null ? "→ " + gl(option.expected_mgdl, 0) : t("dose.noCorrection")}</span>
    ${option.later_units != null && html`<span class="dose-expected">${t("dose.later", { units: num(option.later_units, 1), unit: UNIT.insulin, time: timeOnly(option.later_at) })}</span>`}
    ${option.variant === "split" && html`<span class="dose-expected">${splitOptionNote(option)}</span>`}
    ${before && html`<span class="dose-was">${t("dose.before", { units: num(before.units, 2), unit: UNIT.insulin })}${before.later_units != null && Number(before.later_units) !== Number(option.later_units)
      ? " + " + num(before.later_units, 1) : ""}</span>`}</button>`;
}

// `help` is the preview's dose with options; without it `note` (a message, "wird berechnet", an error) shows.
export function DoseHelp({ help, note, changed = {}, choice, chosen, onChoose, onSlide, onZero }) {
  if (!help) return note || null;
  const withCarbs = Number(help.carbs_g) > 0;
  const splitPart = help.options.find(option => option.variant === "split");
  if (help.slider) changed = {};
  const anyChanged = Object.keys(changed).length > 0;
  const board = help.on_board;
  return html`<div class="dose-head"><span class="dose-heading">${withCarbs ? t("dose.recommendation") : t("dose.correctionOnly")}</span></div>
    ${anyChanged && html`<p class="dose-changed" role="status"><${Icon} name="alert" /> ${t("dose.changed", { cgm: help.glucose_at ? " (CGM " + timeOnly(help.glucose_at) + ")" : "" })}</p>`}
    ${help.glucose_source === "finger" && help.glucose_fresh && html`<p class="dose-factor">${tx("dose.fingerStart", { value: html`<strong>${gl(help.glucose_mgdl, 0)}</strong>`, time: timeOnly(help.glucose_at) })}</p>`}
    ${!help.slider && html`<div class="dose-options">${help.options.map(option => html`<${DoseOption} key=${option.variant} option=${option}
      before=${changed[option.variant]} chosen=${option.variant === choice} onChoose=${onChoose} />`)}</div>`}
    ${help.slider && html`<${DoseSlider} slider=${help.slider} chosen=${chosen} onSlide=${onSlide} onZero=${onZero} />`}
    ${help.slider && splitPart && html`<p class="dose-factor">${t("dose.secondPart", { note: splitOptionNote(splitPart) })}</p>`}
    ${withCarbs && help.split_plan && html`<p class="dose-factor">${t("dose.openPart", { units: num(help.split_plan.units, 1), unit: UNIT.insulin, time: timeOnly(help.split_plan.due_at) })}</p>`}
    ${Number(help.activity_change_mgdl) !== 0 && help.activity_units != null && html`<p class="dose-factor">${t("sport.sport")} <strong>${signedChange(help.activity_change_mgdl, 0)}</strong> · ${(Number(help.activity_units) > 0 ? "+" : "") + num(help.activity_units, 2)} ${UNIT.insulin}</p>`}
    ${board && (board.lowering_until || board.raising_until) && html`<p class="dose-factor">${tx("dose.onBoard", { value: html`<strong>−${glNum(board.lowering_mgdl, 0)} / +${glNum(board.raising_mgdl, 0)} ${UNIT.glucose}</strong>` })}</p>`}
    ${help.reason && html`<p class="hint">${help.reason}</p>`}`;
}
