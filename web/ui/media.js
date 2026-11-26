// Photos and voice notes for the meal agent, kept apart from the components that show them.
import { t } from "./i18n.js";

export const PHOTO_SIDE = 1568;
export const RECORD_SECONDS = 120;

// A photo as a JPEG data URL, scaled down so the longer side has at most PHOTO_SIDE pixels.
export function photoDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(t("chat.photoUnreadable")));
    reader.onload = () => {
      const image = new Image();
      image.onerror = () => reject(new Error(t("chat.photoUnreadable")));
      image.onload = () => {
        const scale = Math.min(1, PHOTO_SIDE / Math.max(image.naturalWidth, image.naturalHeight));
        const canvas = document.createElement("canvas");
        canvas.width = Math.round(image.naturalWidth * scale);
        canvas.height = Math.round(image.naturalHeight * scale);
        canvas.getContext("2d").drawImage(image, 0, 0, canvas.width, canvas.height);
        resolve(canvas.toDataURL("image/jpeg", 0.82));
      };
      image.src = reader.result;
    };
    reader.readAsDataURL(file);
  });
}

export const canRecord = () => Boolean(navigator.mediaDevices?.getUserMedia && window.MediaRecorder);

// 16 kHz mono PCM WAV as base64: every transcription model reads that.
function wavBase64(samples, rate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const text = (offset, value) => [...value].forEach((char, index) => view.setUint8(offset + index, char.charCodeAt(0)));
  text(0, "RIFF"); view.setUint32(4, 36 + samples.length * 2, true); text(8, "WAVEfmt ");
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true); view.setUint16(32, 2, true);
  view.setUint16(34, 16, true); text(36, "data"); view.setUint32(40, samples.length * 2, true);
  for (let index = 0; index < samples.length; index++) {
    const value = Math.max(-1, Math.min(1, samples[index]));
    view.setInt16(44 + index * 2, value < 0 ? value * 0x8000 : value * 0x7fff, true);
  }
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000)
    binary += String.fromCharCode.apply(null, bytes.subarray(offset, offset + 0x8000));
  return btoa(binary);
}

export async function recordingToWav(blob) {
  const Context = window.AudioContext || window.webkitAudioContext;
  const context = new Context();
  const decoded = await context.decodeAudioData(await blob.arrayBuffer());
  context.close();
  const rate = 16000;
  const offline = new OfflineAudioContext(1, Math.max(1, Math.ceil(decoded.duration * rate)), rate);
  const source = offline.createBufferSource();
  source.buffer = decoded;
  source.connect(offline.destination);
  source.start();
  const rendered = await offline.startRendering();
  return wavBase64(rendered.getChannelData(0), rate);
}

// Starts recording; resolves to a handle whose stop(discard) ends it. onDone gets the blob
// (or nothing when discarded). Stops by itself after RECORD_SECONDS.
export async function startRecording(onDone) {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const chunks = [];
  const recorder = new MediaRecorder(stream);
  let discard = false;
  const timer = setTimeout(() => stop(), RECORD_SECONDS * 1000);
  recorder.addEventListener("dataavailable", event => { if (event.data.size) chunks.push(event.data); });
  recorder.addEventListener("stop", () => {
    stream.getTracks().forEach(track => track.stop());
    onDone(!discard && chunks.length ? new Blob(chunks, { type: recorder.mimeType }) : null);
  });
  function stop(drop = false) {
    clearTimeout(timer);
    discard = drop;
    if (recorder.state !== "inactive") recorder.stop();
  }
  recorder.start();
  return { stop };
}
