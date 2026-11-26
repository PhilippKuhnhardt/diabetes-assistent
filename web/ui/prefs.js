// Settings the whole page draws with (glucose limits), set from /api/settings by app.js loadSettings
// before the first render. Components read them at render time.
import { setSportKinds } from "./lib.js";

export const prefs = { lowMgdl: 70, highMgdl: 250 };

export function setPrefs(settings) {
  if (!settings) return;
  prefs.lowMgdl = Number(settings.low_limit_mgdl ?? 70);
  prefs.highMgdl = Number(settings.high_limit_mgdl ?? 250);
  setSportKinds(settings);
}
