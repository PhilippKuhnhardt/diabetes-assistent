// Preact + htm without a build step: components are plain ES modules that write markup as
// tagged templates (html`<div class=${name}>…</div>`). Vendored in web/vendor (see LICENSES.txt).
import { h, render } from "../vendor/preact.js";
import htm from "../vendor/htm.js";

export { h, render };
export { useState, useEffect, useLayoutEffect, useRef, useMemo, useCallback } from "../vendor/preact-hooks.js";
export const html = htm.bind(h);
