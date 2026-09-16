// The names a spoken sentence is likely to contain, for /transcribe to bias on.
//
// After wyoming-faster-whisper's `vocabulary.py` / `hass_api.py`
// (rhasspy/wyoming-faster-whisper): bias recognition with the names the
// speaker can actually mean *right now*, in priority order, and let the
// engine's budget cut the tail — never a big static glossary, which crowds out
// the names that matter and makes the model echo words nobody said. Their
// tiers are areas, then commandable entities, then the rest; ours are the open
// run's parts by name, then by reference, then a short fixed list of part
// families an engineer says before any run exists. Measured 2026-09-14:
// gemini-3.5-flash-lite wrote "ESP32" as "S32" on both tries without it.

import type { StepResponse } from "@/lib/silkscreen/types";

/** Said before a board exists, so they are last: the run's own names win. */
export const CORE_VOCABULARY: readonly string[] = Object.freeze([
  "KiCad",
  "ESP32",
  "STM32",
  "RP2040",
  "AMS1117",
  "LDO",
  "USB-C",
  "JLCPCB",
]);

/** Priority-ordered, de-duplicated names for the run in `history`. */
export function recognitionVocabulary(history: readonly StepResponse[]): string[] {
  const names: string[] = [];
  const refs: string[] = [];
  for (const response of history) {
    for (const part of response.schematic?.parts ?? []) {
      // Passives are named by type and value ("capacitor", "10uF"): ordinary
      // words a recognizer already spells. A device's id is its part number.
      if (part.kind === "device" && part.id) names.push(part.id);
      if (part.ref) refs.push(part.ref);
    }
    if (Array.isArray(response.parts)) {
      for (const part of response.parts) if (part.ref) refs.push(part.ref);
    }
  }
  const seen = new Set<string>();
  const out: string[] = [];
  for (const name of [...names, ...refs, ...CORE_VOCABULARY]) {
    const key = name.trim().toLowerCase();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push(name.trim());
  }
  return out;
}
