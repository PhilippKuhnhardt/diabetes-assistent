// A sport session's effect, shown and set the same way wherever a session appears (user request
// 2026-11-23): the entry form, the entry card opened from "Heute" or "Verlauf", and Verlauf's sport card.
// The effect belongs to the session only (Standard of its kind, own value or none, plus how long it acts);
// the server spreads it over the meal windows it reaches and sends that split with the session
// (`sport_shares`, `sport_rest_mgdl`, `sport_effect_until`). These components only draw it.
import { html } from "./html.js";
import { num, glNum, names, timeOnly, sportNames, UNIT } from "./lib.js";
import { glInput, toMgdl, mmol } from "./units.js";
import { Segments } from "./meal-fields.js";
import { t } from "./i18n.js";

const fmt = (value, digits = 1) => num(value, digits);
const parse = text => Number(String(text ?? "").replace(",", ".").trim());
// A signed glucose change given in mg/dL, in the display unit (without the unit).
export const signedMgdl = value => (value > 0 ? "+" : value < 0 ? "−" : "±") + glNum(Math.abs(value), 0);
// The server stores a session's effect as a glucose drop; the page shows the signed change (−40 lowers).
const change = drop => -Number(drop || 0);

// The fields' starting values for a saved session (null: a new one that follows its kind).
export function sportFields(item) {
  const effect = item ? change(item.sport_effect_mgdl) : 0;
  return {
    effectMode: !item?.sport_effect_custom ? "standard" : effect === 0 ? "none" : "own",
    effect: item?.sport_effect_custom && effect !== 0 ? String(glInput(effect)) : "",
    minutesMode: item?.sport_minutes_custom ? "own" : "standard",
    hours: item?.sport_minutes_custom ? fmt(item.sport_minutes / 60) : "",
  };
}

// What the fields mean for the API: the signed change (null = Standard) and the minutes (null = Standard).
export function sportValues(fields) {
  // A typed value that is no number goes as "", so the server refuses it instead of taking the default.
  const own = parse(fields.effect);
  const effect = fields.effectMode === "own" ? (Number.isFinite(own) && String(fields.effect).trim() ? Math.round(toMgdl(own)) : "")
    : fields.effectMode === "none" ? 0 : null;
  const hours = parse(fields.hours);
  return {
    change: effect,
    minutes: fields.effectMode === "none" || fields.minutesMode !== "own" ? null
      : Number.isFinite(hours) && String(fields.hours).trim() ? Math.round(hours * 60) : "",
  };
}

// Effect and effect duration of one session. `defaults` = {effect: drop of its kind, minutes}.
export function SportEffectFields({ fields, defaults, onChange, id }) {
  const set = patch => onChange({ ...fields, ...patch });
  const standard = change(defaults?.effect);
  const minutes = Number(defaults?.minutes || 300);
  return html`<div class="card-group sport-effect" id=${id}>
    <div class="card-group-head"><b>${t("sport.effectTitle")}</b>
      <small>${fields.effectMode === "standard" ? t("sport.standardValue", { value: signedMgdl(standard) + " " + UNIT.glucose })
        : fields.effectMode === "none" ? t("sport.noEffect") : t("sport.signHint")}</small></div>
    <${Segments} label=${t("sport.effect")} value=${fields.effectMode} data="sport-effect" onChange=${effectMode => set({ effectMode })}
      options=${[["standard", t("sport.standard")], ["own", t("sport.ownEffect")], ["none", t("sport.none")]]} />
    ${fields.effectMode === "own" && html`<span class="card-number wide"><input name="sport_effect_mgdl" inputmode=${mmol ? "decimal" : "numeric"} required
      aria-label=${t("sport.effectIn")} placeholder=${signedMgdl(standard)} value=${fields.effect}
      onInput=${event => set({ effect: event.currentTarget.value.replace("−", "-") })} /><small>${UNIT.glucose}</small></span>`}
    ${fields.effectMode !== "none" && html`
      <div class="card-group-head"><b>${t("sport.durationTitle")}</b>
        <small>${fields.minutesMode === "standard" ? t("sport.standardValue", { value: fmt(minutes / 60) + " h" }) : t("sport.durationRange", { low: fmt(0.5), high: 24 })}</small></div>
      <${Segments} label=${t("sport.duration")} value=${fields.minutesMode} data="sport-minutes" onChange=${minutesMode => set({ minutesMode })}
        options=${[["standard", t("sport.standard")], ["own", t("sport.ownDuration")]]} />
      ${fields.minutesMode === "own" && html`<span class="card-number wide"><input name="sport_hours" inputmode="decimal" required
        aria-label=${t("sport.durationIn")} placeholder=${fmt(minutes / 60)} value=${fields.hours}
        onInput=${event => set({ hours: event.currentTarget.value })} /><small>h</small></span>`}`}
  </div>`;
}

// Where a saved session's effect lands: each meal of its day, the rest between or after the meals.
export function SportShares({ item }) {
  const total = change(item.sport_effect_mgdl);
  if (!total) return html`<p class="hint sport-shares-none">${t("sport.sharesNone")}</p>`;
  const rest = change(item.sport_rest_mgdl);
  return html`<div class="card-group sport-shares">
    <div class="card-group-head"><b>${t("sport.sharesTitle")}</b>
      ${item.sport_effect_until && html`<small>${t("sport.untilClock", { time: timeOnly(item.sport_effect_until) })}</small>`}</div>
    <ul>${(item.sport_shares || []).map(share => html`<li key=${share.meal_id}><span>${names[share.meal] || t("meal.meal")}</span>
        <b>${signedMgdl(change(share.effect_mgdl))}</b></li>`)}
      ${Math.round(rest) !== 0 && html`<li class="is-rest"><span>${t("sport.restBetween")}</span><b>${signedMgdl(rest)}</b></li>`}
      <li class="is-total"><span>${t("sport.total")}</span><b>${signedMgdl(total)} ${UNIT.glucose}</b></li></ul>
    <p class="hint">${t("sport.sharesHint")}</p></div>`;
}

// One line per session in lists ("Heute", Verlauf): kind, duration, effect and where it lands.
export function sportSummary(item) {
  const total = change(item.sport_effect_mgdl);
  return {
    title: (sportNames[item.sport_kind] || t("sport.sport")) + (Number(item.duration_minutes) ? " · " + fmt(item.duration_minutes, 0) + " min" : ""),
    effect: total ? signedMgdl(total) + " " + UNIT.glucose : t("sport.noEffectShort"),
    parts: total ? [...(item.sport_shares || []).map(share => ({ key: share.meal_id,
      text: (share.meal ? t("meal.short." + share.meal) : t("meal.meal")) + " " + signedMgdl(change(share.effect_mgdl)) })),
      ...(Math.round(change(item.sport_rest_mgdl)) !== 0 ? [{ key: "rest", rest: true, text: t("sport.between", { value: signedMgdl(change(item.sport_rest_mgdl)) }) }] : [])] : [],
  };
}
