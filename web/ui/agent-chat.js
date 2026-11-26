// The meal agent as a chat (concept 2026-11-18): photos and words in, the agent's answers as bubbles,
// and at the end the user scales the agent's portion on a slider (carbs, fat and protein alike), sets how sure
// they are, fat-rich and fast, and takes that value over. Nothing is saved here: "übernehmen" only
// fills the form the chat was opened from, and the dose help calculates as always.
import { html, useState, useEffect, useRef } from "./html.js";
import { photoDataURL, canRecord, startRecording, recordingToWav } from "./media.js";
import { GlucoseBadge } from "./glucose.js";
import { api, toast, num } from "./lib.js";
import { t } from "./i18n.js";
import { CERTAINTY_LEVELS, CertaintySlider, FactorChips } from "./meal-fields.js";


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

// The portion to take over: the agent's carbs, fat and protein with their kcal, and
// a slider that scales all of them alike ("it was 0.8× the portion"), since the agent gets the mix right more
// often than the amount. It snaps to 1×; the agent's carb range shows as a hint.
export const portionValues = (estimate, portion) => {
  const scale = value => value == null ? null : Math.round(Number(value) * portion);
  const carbs = scale(estimate.carbs_g), fat = scale(estimate.fat_g), protein = scale(estimate.protein_g);
  return { carbs, fat, protein,
    kcal: fat == null || protein == null ? null : Math.round((4 * carbs + 9 * fat + 4 * protein) / 10) * 10 };
};

export function PortionPick({ estimate, portion, previous, onChange, children }) {
  const values = portionValues(estimate, portion);
  const ratio = grams => estimate.carbs_g > 0 && grams != null ? grams / estimate.carbs_g : 1;
  const low = Math.min(0.5, Math.floor(ratio(estimate.carbs_low_g) * 20) / 20);
  const high = Math.max(1.5, Math.ceil(ratio(estimate.carbs_high_g) * 20) / 20);
  const at = value => ((value - low) / (high - low) * 100).toFixed(1) + "%";
  const snap = value => Math.abs(value - 1) <= 0.03 ? 1 : Math.round(value * 20) / 20;
  const factor = value => num(value, 2) + "×";
  const note = [portion === 1 ? t("chat.estimate") : t("chat.ofEstimate", { factor: factor(portion) }),
    estimate.carbs_low_g != null ? t("chat.carbRange", { low: estimate.carbs_low_g, high: estimate.carbs_high_g }) : "",
    previous != null && previous !== "" && Number(previous) !== values.carbs ? t("chat.before", { grams: num(previous, 1) }) : ""].filter(Boolean);
  return html`<div class="carb-pick">
    <div class="carb-pick-value"><strong>${values.carbs} g</strong><span class="carb-note">${note.join(" · ")}</span>${children}</div>
    <p class="portion-values">${[values.fat != null ? t("chat.fat", { grams: values.fat }) : "", values.protein != null ? t("chat.protein", { grams: values.protein }) : "",
      values.kcal != null ? values.kcal + " kcal" : ""].filter(Boolean).join(" · ")}</p>
    <div class="carb-range">
      <input type="range" min=${low} max=${high} step="0.05" value=${portion} aria-label=${t("chat.portion")}
        style=${{ "--fill": at(portion) }} onInput=${event => onChange(snap(Number(event.currentTarget.value)))} />
      ${[[low, "start"], [1, "mid"], [high, "end"]].map(([value, align]) => html`<button type="button" class=${"carb-tick " + align}
        style=${align === "mid" ? { left: at(value) } : null} onClick=${() => onChange(value)}>
        ${align === "mid" ? html`<b>${factor(value)}</b>` : factor(value)}</button>`)}
    </div>
  </div>`;
}

function EstimateDetails({ estimate }) {
  const extra = estimate.items.length + (estimate.lookups?.length || 0) + (estimate.sources?.length || 0);
  return html`
    ${estimate.dish && html`<p class="chat-dish">${t("common.dish")}: <strong>${estimate.dish.name}</strong>
      ${estimate.dish.new && html` <span class="outlier-badge">${t("chat.new")}</span>`}
      ${estimate.dish.hint && html`<br /><small>${estimate.dish.hint}</small>`}</p>`}
    ${estimate.history_adjustment_g ? html`<p class="chat-note">${t("chat.historyAdjustment", { grams: (estimate.history_adjustment_g > 0 ? "+" : "−") + Math.abs(estimate.history_adjustment_g) })}</p>` : null}
    ${extra > 0 && html`<details>
      <summary>${[estimate.items.length ? t("chat.items", { count: estimate.items.length }) : "",
        estimate.lookups?.length ? t("chat.lookups", { count: estimate.lookups.length }) : ""].filter(Boolean).join(" · ") || t("chat.sources")}</summary>
      ${estimate.items.length > 0 && html`<ul class="chat-items">${estimate.items.map(item => html`
        <li><span>${item.name}${item.amount && html` <small>${item.amount}</small>`}</span><b>${num(Math.round(item.carbs_g), 1)} g</b></li>`)}</ul>`}
      ${estimate.lookups?.length > 0 && html`<ul class="chat-lookups">${estimate.lookups.map(line => html`<li>${line}</li>`)}</ul>`}
      ${estimate.sources?.length > 0 && html`<p class="chat-note">${t("chat.sources")}: ${estimate.sources.map((source, index) => html`${index ? ", " : ""}<a
        href=${source.url} target="_blank" rel="noopener noreferrer">${source.title || new URL(source.url).hostname}</a>`)}</p>`}
    </details>`}`;
}

// A sent photo as the page loads it, or a data URL while the message is still on its way.
const photoSrc = (session, photo) => typeof photo === "string" ? photo
  : "/api/agent/photo?session=" + encodeURIComponent(session) + "&n=" + photo;

function Bubbles({ conversation, estimate, busy, session }) {
  const lastAnswer = conversation.map(row => row.role).lastIndexOf("assistant");
  return html`<div class="chat-bubbles" aria-live="polite">
    ${!conversation.length && !busy && html`<p class="chat-empty">${t("chat.empty")}</p>`}
    ${conversation.map((row, index) => row.role === "user"
      ? html`<div class=${"chat-turn me" + (row.pending ? " pending" : "")}>
          ${row.photos?.length > 0 && html`<div class="chat-turn-photos">${row.photos.map((photo, n) => html`
            <img key=${n} src=${photoSrc(session, photo)} alt=${t("chat.photo", { n: n + 1 })} />`)}</div>`}
          ${row.text && html`<div class="bubble me">${row.text}</div>`}
        </div>`
      : index === lastAnswer
        ? html`<div class="bubble ai latest">
            <b>${row.carbs_g} g</b>${row.carbs_low_g != null && html`<span class="muted"> · ${row.carbs_low_g}–${row.carbs_high_g} g</span>`}
            ${row.reasoning && html`<p>${row.reasoning}</p>`}
            ${row.question && html`<p class="chat-question">${row.question}</p>`}
            ${estimate && html`<${EstimateDetails} estimate=${estimate} />`}
          </div>`
        : html`<div class="bubble ai earlier"><b>${row.carbs_g} g</b>${row.carbs_low_g != null
            && html`<span class="muted"> · ${row.carbs_low_g}–${row.carbs_high_g} g</span>`}</div>`)}
    ${busy && html`<div class="chat-typing">${t("chat.estimating")}</div>`}
  </div>`;
}

// One chat per opening, the same wherever it opens (correcting a saved meal and the
// first estimate must not differ). With `sessionId` (an estimate already taken over) or `eventId` (a
// saved meal's card) the earlier estimate continues.
export function MealChat({ info, eventId, sessionId, initialText, previousCarbs, context, onApply, onClose }) {
  // Photos picked for the next message (data URLs); sent ones live in the conversation rows.
  const [photos, setPhotos] = useState([]);
  const [sentPhotos, setSentPhotos] = useState(0);
  const [session, setSession] = useState(null);
  const [estimate, setEstimate] = useState(null);
  const [conversation, setConversation] = useState([]);
  const [text, setText] = useState(initialText || "");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(Boolean(sessionId || eventId));
  const [recording, setRecording] = useState(null);
  const [voiceStatus, setVoiceStatus] = useState("");
  const [portion, setPortion] = useState(null);
  const [level, setLevel] = useState(1);
  const [fat, setFat] = useState(false);
  const [fast, setFast] = useState(false);
  const scroller = useRef(null);
  const field = useRef(null);
  const perMessage = info?.limits?.photos || 4;
  const photoRoom = Math.min(perMessage - photos.length, (info?.limits?.session_photos || 8) - sentPhotos - photos.length);
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
    setPortion(result.portion ?? 1);
    setLevel(Math.max(0, CERTAINTY_LEVELS.indexOf(result.certainty)));
    setFat(Boolean(result.fat_protein));
    setFast(Boolean(result.fast));
  };

  // An entry the agent already estimated continues that conversation (photos, turns, last answer),
  // so a second plate is told as a correction instead of starting over.
  useEffect(() => {
    if (!sessionId && !eventId) return;
    let live = true;
    api("/api/agent/session?" + (sessionId ? "session_id=" + encodeURIComponent(sessionId)
      : "event_id=" + encodeURIComponent(eventId))).then(data => {
      const earlier = data.session;
      if (!live || !earlier?.can_continue) return;
      setSession(earlier.session_id);
      setSentPhotos(earlier.photos);
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
    const files = [...event.currentTarget.files].slice(0, Math.max(0, photoRoom));
    event.currentTarget.value = "";
    try {
      const added = [];
      for (const file of files) added.push(await photoDataURL(file));
      setPhotos(current => [...current, ...added].slice(0, perMessage));
    } catch (error) { toast(error.message, true); }
  }

  // A message shows up in the chat at once and stays there marked as
  // pending until the answer comes; a failed send puts the words and photos back into the field.
  async function send() {
    if (busy) return;
    const words = text.trim();
    const sending = photos;
    if (!words && !sending.length) {
      toast(followUp ? t("chat.needFollowUp") : t("chat.needInput"), true);
      return;
    }
    const { at, placeId } = context();
    const before = conversation;
    setConversation([...before, { role: "user", text: words, photos: sending, pending: true }]);
    setText("");
    setPhotos([]);
    setBusy(true);
    try {
      const data = await api("/api/agent/estimate", { method: "POST", body: JSON.stringify(followUp
        ? { session_id: session, text: words, photos: sending, at, place_id: placeId }
        : { text: words, photos: sending, at, place_id: placeId }) });
      setSession(data.session_id);
      setSentPhotos(data.photos || 0);
      takeEstimate(data.estimate, data.conversation || []);
    } catch (error) {
      setConversation(before);
      setText(current => current || words);
      setPhotos(current => [...sending, ...current].slice(0, perMessage));
      toast(error.message, true);
    }
    setBusy(false);
  }

  async function toggleVoice() {
    if (recording) { recording.stop(); setRecording(null); return; }
    try {
      const handle = await startRecording(async blob => {
        setRecording(null);
        if (!blob) { setVoiceStatus(""); return; }
        setVoiceStatus(t("chat.transcribing"));
        try {
          const data = await api("/api/agent/transcribe", { method: "POST",
            body: JSON.stringify({ audio: await recordingToWav(blob), format: "wav" }) });
          setText(current => (current.trim() ? current.trim() + " " : "") + data.text);
          setVoiceStatus(data.text ? "" : t("chat.notUnderstood"));
          field.current?.focus();
        } catch (error) {
          setVoiceStatus("");
          toast(error.message || t("chat.recordingFailed"), true);
        }
      });
      setRecording(handle);
      setVoiceStatus(t("chat.recording"));
    } catch (_) { toast(t("chat.micDenied"), true); }
  }

  const picked = estimate && portion != null ? portionValues(estimate, portion) : null;
  function apply() {
    onApply({ session, estimate, carbs: picked.carbs, portion, fat_g: picked.fat, protein_g: picked.protein,
      certainty: CERTAINTY_LEVELS[level], fat_protein: fat, fast });
  }

  const voice = info?.transcribe_available && canRecord();
  const placeholder = followUp ? t("chat.placeholderFollowUp") : t("chat.placeholder");
  const ready = Boolean(text.trim() || photos.length);
  const onKey = event => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); send(); }
  };

  return html`<div class="meal-chat">
    <div class="section-head chat-head">
      <div class="chat-title"><h2 id="agent-dialog-title">${t("entry.food")}</h2><${GlucoseBadge} /></div>
      <button type="button" class="quiet chat-close" aria-label=${t("common.close")} onClick=${onClose}>×</button>
    </div>
    <div class="chat-body" ref=${scroller}>
      ${loading ? html`<p class="chat-typing">${t("common.loading")}</p>`
        : html`<${Bubbles} conversation=${conversation} estimate=${estimate} busy=${busy} session=${session} />`}
    </div>
    <div class="chat-foot">
      ${picked && html`<div class="chat-pick">
        <${PortionPick} estimate=${estimate} portion=${portion} previous=${previousCarbs} onChange=${setPortion}>
          <span class="entry-chips">
            <${FactorChips} fat=${fat} fast=${fast} onFat=${setFat} onFast=${setFast} />
          </span>
        <//>
        <${CertaintySlider} level=${level} onChange=${setLevel} />
      </div>`}
      ${photos.length > 0 && html`<div class="chat-photos" aria-label=${t("chat.nextPhotos")}>
        ${photos.map((photo, index) => html`<figure key=${photo.slice(-24) + index}>
          <img src=${photo} alt=${t("chat.newPhoto", { n: index + 1 })} />
          <button type="button" class="quiet" aria-label=${t("chat.removePhoto", { n: index + 1 })}
            onClick=${() => setPhotos(photos.filter((_, other) => other !== index))}>×</button>
        </figure>`)}
      </div>`}
      <div class="chat-input">
        ${photoRoom > 0 && html`<label class="round-button" aria-label=${t("chat.addPhoto")} title=${t("chat.photoTitle")}>
          <${Icon} name="camera" /><input type="file" accept="image/*" multiple hidden onChange=${addPhotos} /></label>`}
        <textarea ref=${field} rows="1" maxlength="2000" placeholder=${placeholder} aria-label=${followUp ? t("chat.answerLabel") : t("card.description")}
          value=${text} onInput=${event => setText(event.currentTarget.value)} onKeyDown=${onKey}></textarea>
        ${estimate && ready
          ? html`<button type="button" class="round-button send" aria-label=${t("chat.send")} disabled=${busy} onClick=${send}><${Icon} name="send" /></button>`
          : voice && html`<button type="button" class="round-button" aria-label=${recording ? t("chat.stopRecording") : t("chat.speak")}
              aria-pressed=${recording ? "true" : "false"} onClick=${toggleVoice}><${Icon} name=${recording ? "stop" : "mic"} /></button>`}
      </div>
      ${voiceStatus && html`<p class="hint chat-voice" role="status">${voiceStatus}</p>`}
      ${estimate
        ? html`<button type="button" class="primary chat-apply" disabled=${busy} onClick=${apply}>${t("chat.apply", { grams: picked?.carbs })}</button>`
        : html`<button type="button" class="primary chat-apply" disabled=${busy || loading} onClick=${send}>${busy ? t("chat.estimating") : t("chat.estimateButton")}</button>`}
    </div>
  </div>`;
}
