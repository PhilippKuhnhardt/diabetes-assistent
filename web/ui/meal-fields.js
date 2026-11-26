// The fields a meal has wherever it is entered or changed: the entry form (web/ui/entry.js), a saved
// meal's card (web/ui/entry-card.js) and the meal chat (web/ui/agent-chat.js). KH-Sicherheit, the
// Fettreich/Schnell chips and the fat/protein surcharge choice are built once here, so creating and
// editing a meal cannot drift apart again. What they offer comes from the server;
// the dose recommendation stays in the entry form on purpose.
import { html } from "./html.js";
import { num } from "./lib.js";
import { t } from "./i18n.js";

// KH-Sicherheit is a three-stop slider (unsure, roughly known, weighed) that starts in the middle.
export const CERTAINTY_LEVELS = ["unsure", "known", "weighed"];
const CERTAINTY_SLIDER_LABELS = [t("certainty.slider.unsure"), t("certainty.slider.known"), t("certainty.slider.weighed")];

// onChange follows the slider; onCommit (optional) runs once it is let go.
export function CertaintySlider({ level, onChange, onCommit }) {
  return html`<span class="certainty-slider" data-level=${CERTAINTY_LEVELS[level]}>
    <input type="range" name="certainty_level" min="0" max="2" step="1" value=${level}
      aria-label=${t("certainty.sliderLabel")}
      onInput=${event => onChange(Number(event.currentTarget.value))} onChange=${() => onCommit?.()} />
    <output class="certainty-label">${CERTAINTY_SLIDER_LABELS[level]}</output>
  </span>`;
}

const FactorChip = ({ name, label, title, checked, onChange }) => html`
  <label class="chip chip-toggle factor-chip" title=${title}>
    <input type="checkbox" name=${name} aria-label=${label} checked=${checked} onChange=${event => onChange(event.currentTarget.checked)} />
    <span class=${"factor-icon factor-" + name} aria-hidden="true"></span><span>${label}</span>
  </label>`;

// Fettreich and Schnell; the caller places them (inside its own chip row).
export const FactorChips = ({ fat, fast, onFat, onFast }) => html`
  <${FactorChip} name="fat_protein" label=${t("factor.fat_protein")} title=${t("factor.fatTitle")} checked=${fat} onChange=${onFat} />
  <${FactorChip} name="fast" label=${t("factor.fast")} title=${t("factor.fastTitle")} checked=${fast} onChange=${onFast} />`;

// A segmented switch: one of a few options, the chosen one pressed. `data` names a data attribute
// (data-<data>=key) on each button for tests and styles.
export function Segments({ value, options, onChange, label, data, disabled }) {
  return html`<div class="segments" role="group" aria-label=${label}>${options.map(([key, text]) => html`
    <button type="button" aria-pressed=${value === key ? "true" : "false"} disabled=${disabled}
      ...${data ? { ["data-" + data]: key } : {}} onClick=${() => onChange(key)}>${text}</button>`)}</div>`;
}

// Static or calculated fat/protein surcharge (app/fpe.py). `calculated` is fpe.view from the server
// ({fpe, mgdl, minutes, fat_g, protein_g}); `staticMgdl` the static surcharge with the fat mark as set.
// The entry form adds that a split then gives the carbs now and the surcharge later.
export function SurchargeChoice({ mode, calculated, staticMgdl, fat, splitNote, onChange }) {
  return html`<div class="card-group meal-surcharge">
    <div class="card-group-head"><b>${t("surcharge.title")}</b>
      <small>${t("surcharge.detail", { fat: num(calculated.fat_g, 0), protein: num(calculated.protein_g, 0), fpe: num(calculated.fpe, 1), hours: num(calculated.minutes / 60, 1) })}</small></div>
    <${Segments} label=${t("surcharge.label")} value=${mode} data="surcharge-mode" onChange=${onChange}
      options=${[["static", t("surcharge.static") + (staticMgdl != null ? " " + num(staticMgdl, 0) + " mg/dL" : "") + (fat ? " " + t("surcharge.fatRich") : "")],
                 ["fpe", t("surcharge.calculated", { mgdl: num(calculated.mgdl, 0) })]]} />
    ${splitNote && mode === "fpe" && html`<small class="hint">${t("surcharge.splitNote")}</small>`}
  </div>`;
}
