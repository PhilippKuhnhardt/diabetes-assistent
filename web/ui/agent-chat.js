// The meal agent as a chat (concept 2026-11-18): photos and words in, the agent's answers as bubbles,
// and at the end Philipp picks the carbs himself on a slider over the agent's range, sets how sure
// they are, fat-rich and fast, and takes that value over. Nothing is saved here: "übernehmen" only
// fills the form the chat was opened from, and the dose help calculates as always.
import { html, useState, useEffect, useRef } from "./html.js";
import { photoDataURL, canRecord, startRecording, recordingToWav } from "./media.js";
import { GlucoseBadge } from "./glucose.js";

export const CERTAINTY_LEVELS = ["unsure", "known", "weighed"];
const CERTAINTY_LABELS = ["KH unsicher", "KH halbwegs", "KH sicher"];
const fmt = value => Number(value).toLocaleString("de-DE", { maximumFractionDigits: 1 });

const Icon = ({ name }) => {
  const paths = {
    camera: "M4 8h3l2-3h6l2 3h3v11H4z M12 17a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
    mic: "M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3z M5 11a7 7 0 0 0 14 0 M12 18v3",
    stop: "M7 7h10v10H7z",
    send: "M12 19V5 M6 11l6-6 6 6",
  };
  return html`<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor"
    stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d=${paths[name]} /></svg>`;
};

// The carbs to take over: a slider from the agent's low to its high value that snaps to low,
// estimate and high, so the usual picks need no fiddling.
export function CarbRange({ low, estimate, high, value, previous, onChange, children }) {
  const span = Math.max(1, high - low);
  const at = grams => ((grams - low) / span * 100).toFixed(1) + "%";
  const snap = grams => {
    const near = Math.max(1, Math.round(span / 25));
    return [estimate, low, high].find(stop => Math.abs(grams - stop) <= near) ?? grams;
  };
  const note = [value === estimate ? "Schätzung" : "Schätzung " + estimate + " g",
    previous != null && previous !== "" && Number(previous) !== value ? "vorher " + fmt(previous) + " g" : ""].filter(Boolean);
  const ticks = [[low, "start"], [estimate, "mid"], [high, "end"]].filter(([grams], index, all) =>
    all.findIndex(([other]) => other === grams) === index);
  return html`<div class="carb-pick">
    <div class="carb-pick-value"><strong>${value} g</strong><span class="carb-note">${note.join(" · ")}</span>${children}</div>
    ${high > low && html`<div class="carb-range">
      <input type="range" min=${low} max=${high} step="1" value=${value} aria-label="KH wählen"
        style=${{ "--fill": at(value) }} onInput=${event => onChange(snap(Number(event.currentTarget.value)))} />
      ${ticks.map(([grams, align]) => html`<button type="button" class=${"carb-tick " + align}
        style=${align === "mid" ? { left: at(grams) } : null} onClick=${() => onChange(grams)}>
        ${align === "mid" ? html`<b>${grams}</b>` : grams}</button>`)}
    </div>`}
  </div>`;
}

export function CertaintySlider({ level, onChange }) {
  return html`<span class="certainty-slider" data-level=${CERTAINTY_LEVELS[level]}>
    <input type="range" min="0" max="2" step="1" value=${level}
      aria-label="KH-Sicherheit: unsicher, halbwegs sicher, sicher (gewogen oder genormt)"
      onInput=${event => onChange(Number(event.currentTarget.value))} />
    <output class="certainty-label">${CERTAINTY_LABELS[level]}</output>
  </span>`;
}

export const FactorChip = ({ name, label, title, checked, onChange }) => html`
  <label class="chip chip-toggle factor-chip" title=${title}>
    <input type="checkbox" name=${name} aria-label=${label} checked=${checked} onChange=${event => onChange(event.currentTarget.checked)} />
    <span class=${"factor-icon factor-" + name} aria-hidden="true"></span><span>${label}</span>
  </label>`;

function EstimateDetails({ estimate }) {
  const extra = estimate.items.length + (estimate.lookups?.length || 0) + (estimate.sources?.length || 0);
  return html`
    ${estimate.dish && html`<p class="chat-dish">Gericht: <strong>${estimate.dish.name}</strong>
      ${estimate.dish.new && html` <span class="outlier-badge">neu</span>`}
      ${estimate.dish.hint && html`<br /><small>${estimate.dish.hint}</small>`}</p>`}
    ${estimate.history_adjustment_g ? html`<p class="chat-note">Darin ${estimate.history_adjustment_g > 0 ? "+" : "−"}${Math.abs(estimate.history_adjustment_g)} g
      aus dem Verlauf des Gerichts.</p>` : null}
    ${extra > 0 && html`<details>
      <summary>${[estimate.items.length ? estimate.items.length + " Posten" : "",
        estimate.lookups?.length ? estimate.lookups.length + " nachgeschlagen" : ""].filter(Boolean).join(" · ") || "Quellen"}</summary>
      ${estimate.items.length > 0 && html`<ul class="chat-items">${estimate.items.map(item => html`
        <li><span>${item.name}${item.amount && html` <small>${item.amount}</small>`}</span><b>${fmt(Math.round(item.carbs_g))} g</b></li>`)}</ul>`}
      ${estimate.lookups?.length > 0 && html`<ul class="chat-lookups">${estimate.lookups.map(line => html`<li>${line}</li>`)}</ul>`}
      ${estimate.sources?.length > 0 && html`<p class="chat-note">Quellen: ${estimate.sources.map((source, index) => html`${index ? ", " : ""}<a
        href=${source.url} target="_blank" rel="noopener noreferrer">${source.title || new URL(source.url).hostname}</a>`)}</p>`}
    </details>`}`;
}

function Bubbles({ conversation, estimate, busy }) {
  const lastAnswer = conversation.map(row => row.role).lastIndexOf("assistant");
  return html`<div class="chat-bubbles" aria-live="polite">
    ${!conversation.length && !busy && html`<p class="chat-empty">Foto, Sprache oder Text, z. B. „Porridge wie immer, dazu eine Banane“.</p>`}
    ${conversation.map((row, index) => row.role === "user"
      ? html`<div class="bubble me">${row.text === "(nur Fotos)" ? "Nur Fotos" : row.text}</div>`
      : html`<div class=${"bubble ai" + (index === lastAnswer ? " latest" : "")}>
          <b>${row.carbs_g} g</b>${row.carbs_low_g != null && html`<span class="muted"> · ${row.carbs_low_g}–${row.carbs_high_g} g</span>`}
          ${row.reasoning && html`<p>${row.reasoning}</p>`}
          ${row.question && html`<p class="chat-question">${row.question}</p>`}
          ${index === lastAnswer && estimate && html`<${EstimateDetails} estimate=${estimate} />`}
        </div>`)}
    ${busy && html`<div class="chat-typing">Schätzt …</div>`}
  </div>`;
}

// One chat per opening. `target` is "entry" (the entry form) or "edit" (an existing meal, where an
// earlier estimate continues: "Nachbessern"). `api` and `toast` come from the page.
export function MealChat({ info, target, eventId, initialText, previousCarbs, context, api, toast, onApply, onClose }) {
  const [photos, setPhotos] = useState([]);
  const [session, setSession] = useState(null);
  const [estimate, setEstimate] = useState(null);
  const [conversation, setConversation] = useState([]);
  const [text, setText] = useState(initialText || "");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(Boolean(eventId));
  const [recording, setRecording] = useState(null);
  const [voiceStatus, setVoiceStatus] = useState("");
  const [pick, setPick] = useState(null);
  const [level, setLevel] = useState(1);
  const [fat, setFat] = useState(false);
  const [fast, setFast] = useState(false);
  const scroller = useRef(null);
  const field = useRef(null);
  const photoLimit = info?.limits?.photos || 4;
  const followUp = Boolean(session);
  // The field grows with the text; Safari has no field-sizing yet, so set the height by hand there.
  useEffect(() => {
    const node = field.current;
    if (!node || CSS.supports("field-sizing", "content")) return;
    node.style.height = "auto";
    node.style.height = Math.min(node.scrollHeight + 2, 140) + "px";
  }, [text]);

  const takeEstimate = (result, rows) => {
    setEstimate(result);
    setConversation(rows);
    setPick(result.carbs_g);
    setLevel(Math.max(0, CERTAINTY_LEVELS.indexOf(result.certainty)));
    setFat(Boolean(result.fat_protein));
    setFast(Boolean(result.fast));
  };

  // An entry the agent already estimated continues that conversation (photos, turns, last answer),
  // so a second plate is told as a correction instead of starting over.
  useEffect(() => {
    if (!eventId) return;
    let live = true;
    api("/api/agent/session?event_id=" + encodeURIComponent(eventId)).then(data => {
      const earlier = data.session;
      if (!live || !earlier?.can_continue) return;
      setSession(earlier.session_id);
      setPhotos(Array.from({ length: earlier.photos }, (_, index) =>
        "/api/agent/photo?session=" + encodeURIComponent(earlier.session_id) + "&n=" + index));
      takeEstimate(earlier.estimate, earlier.conversation || []);
      setText("");
    }).catch(error => toast(error.message, true)).finally(() => live && setLoading(false));
    return () => { live = false; };
  }, []);

  // Closing the chat drops a running recording.
  const live = useRef(null);
  live.current = recording;
  useEffect(() => () => live.current?.stop(true), []);
  useEffect(() => {
    const node = scroller.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [conversation.length, busy]);

  async function addPhotos(event) {
    const files = [...event.currentTarget.files].slice(0, photoLimit - photos.length);
    event.currentTarget.value = "";
    try {
      const added = [];
      for (const file of files) added.push(await photoDataURL(file));
      setPhotos(current => [...current, ...added].slice(0, photoLimit));
    } catch (error) { toast(error.message, true); }
  }

  async function send() {
    if (busy) return;
    const words = text.trim();
    if (!words && (followUp || !photos.length)) {
      toast(followUp ? "Bitte eine Antwort oder Korrektur eingeben" : "Bitte ein Foto aufnehmen oder das Essen beschreiben", true);
      return;
    }
    const { at, placeId } = context();
    setBusy(true);
    try {
      const data = await api("/api/agent/estimate", { method: "POST", body: JSON.stringify(followUp
        ? { session_id: session, text: words, at, place_id: placeId }
        : { text: words, photos, at, place_id: placeId }) });
      setSession(data.session_id);
      takeEstimate(data.estimate, data.conversation || []);
      setText("");
    } catch (error) { toast(error.message, true); }
    setBusy(false);
  }

  async function toggleVoice() {
    if (recording) { recording.stop(); setRecording(null); return; }
    try {
      const handle = await startRecording(async blob => {
        setRecording(null);
        if (!blob) { setVoiceStatus(""); return; }
        setVoiceStatus("Wird transkribiert …");
        try {
          const data = await api("/api/agent/transcribe", { method: "POST",
            body: JSON.stringify({ audio: await recordingToWav(blob), format: "wav" }) });
          setText(current => (current.trim() ? current.trim() + " " : "") + data.text);
          setVoiceStatus(data.text ? "" : "Nichts verstanden");
          field.current?.focus();
        } catch (error) {
          setVoiceStatus("");
          toast(error.message || "Aufnahme konnte nicht verarbeitet werden", true);
        }
      });
      setRecording(handle);
      setVoiceStatus("Aufnahme läuft …");
    } catch (_) { toast("Mikrofon nicht freigegeben", true); }
  }

  function apply() {
    onApply({ session, estimate, carbs: pick, certainty: CERTAINTY_LEVELS[level], fat_protein: fat, fast });
  }

  const voice = info?.transcribe_available && canRecord();
  const title = target === "edit" ? (followUp ? "Nachbessern" : "Essen einschätzen") : "Essen";
  const placeholder = followUp ? (target === "edit" ? "z. B. ich hatte noch einen zweiten Teller" : "Korrektur oder Antwort …")
    : "Was und wie viel?";
  const onKey = event => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); send(); }
  };

  return html`<div class="meal-chat">
    <div class="section-head chat-head">
      <div class="chat-title"><h2 id="agent-dialog-title">${title}</h2><${GlucoseBadge} /></div>
      <button type="button" class="quiet" aria-label="Schließen" onClick=${onClose}>×</button>
    </div>
    <div class="chat-body" ref=${scroller}>
      ${photos.length > 0 && html`<div class=${"chat-photos" + (conversation.length ? " small" : "")}>
        ${photos.map((photo, index) => html`<figure key=${photo.slice(-24) + index}>
          <img src=${photo} alt=${"Foto " + (index + 1)} />
          ${!followUp && html`<button type="button" class="quiet" aria-label=${"Foto " + (index + 1) + " entfernen"}
            onClick=${() => setPhotos(photos.filter((_, other) => other !== index))}>×</button>`}
        </figure>`)}
      </div>`}
      ${loading ? html`<p class="chat-typing">Lädt …</p>` : html`<${Bubbles} conversation=${conversation} estimate=${estimate} busy=${busy} />`}
    </div>
    <div class="chat-foot">
      ${estimate && pick != null && html`
        <${CarbRange} low=${estimate.carbs_low_g} estimate=${estimate.carbs_g} high=${estimate.carbs_high_g}
          value=${pick} previous=${previousCarbs} onChange=${setPick}>
          <span class="entry-chips">
            <${FactorChip} name="fat_protein" label="Fettreich" title="Fett- oder eiweißreiche Mahlzeit" checked=${fat} onChange=${setFat} />
            <${FactorChip} name="fast" label="Schnell" title="Wirkt schnell (z. B. Saft, Weißbrot)" checked=${fast} onChange=${setFast} />
          </span>
        <//>
        <${CertaintySlider} level=${level} onChange=${setLevel} />`}
      <div class="chat-input">
        ${!followUp && photos.length < photoLimit && html`<label class="round-button" aria-label="Foto hinzufügen" title="Foto">
          <${Icon} name="camera" /><input type="file" accept="image/*" multiple hidden onChange=${addPhotos} /></label>`}
        <textarea ref=${field} rows="1" maxlength="2000" placeholder=${placeholder} aria-label=${followUp ? "Antwort oder Korrektur" : "Beschreibung"}
          value=${text} onInput=${event => setText(event.currentTarget.value)} onKeyDown=${onKey}></textarea>
        ${estimate && text.trim()
          ? html`<button type="button" class="round-button send" aria-label="Senden" disabled=${busy} onClick=${send}><${Icon} name="send" /></button>`
          : voice && html`<button type="button" class="round-button" aria-label=${recording ? "Aufnahme beenden" : "Sprechen"}
              aria-pressed=${recording ? "true" : "false"} onClick=${toggleVoice}><${Icon} name=${recording ? "stop" : "mic"} /></button>`}
      </div>
      ${voiceStatus && html`<p class="hint chat-voice" role="status">${voiceStatus}</p>`}
      ${estimate
        ? html`<button type="button" class="primary chat-apply" disabled=${busy} onClick=${apply}>${pick} g übernehmen</button>`
        : html`<button type="button" class="primary chat-apply" disabled=${busy || loading} onClick=${send}>${busy ? "Schätzt …" : "Einschätzen"}</button>`}
    </div>
  </div>`;
}
