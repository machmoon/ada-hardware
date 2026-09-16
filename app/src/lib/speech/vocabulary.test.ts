import { describe, expect, it } from "vitest";
import type { StepResponse } from "@/lib/silkscreen/types";
import { CORE_VOCABULARY, recognitionVocabulary } from "./vocabulary";

const propose = {
  step: "propose",
  schematic: {
    parts: [
      { id: "ESP32-WROOM-32E", ref: "U1", kind: "device" },
      { id: "c_bulk", ref: "C1", kind: "capacitor", value: "10uF" },
      { id: "AMS1117-3.3", ref: "U2", kind: "device" },
    ],
  },
} as unknown as StepResponse;

const place = {
  step: "place",
  parts: [
    { ref: "U1", footprint: "x" },
    { ref: "J1", footprint: "y" },
  ],
} as unknown as StepResponse;

describe("recognitionVocabulary", () => {
  it("puts the run's part numbers first, then its refs, then the core list", () => {
    const names = recognitionVocabulary([propose, place]);
    expect(names.slice(0, 5)).toEqual(["ESP32-WROOM-32E", "AMS1117-3.3", "U1", "C1", "U2"]);
    expect(names).toContain("J1");
    expect(names.slice(-CORE_VOCABULARY.length)).toEqual([...CORE_VOCABULARY]);
  });

  it("never lists a passive's internal id, and never repeats a name", () => {
    const names = recognitionVocabulary([propose, place, propose]);
    expect(names).not.toContain("c_bulk");
    expect(new Set(names.map((n) => n.toLowerCase())).size).toBe(names.length);
  });

  it("offers only the core list before any run exists", () => {
    expect(recognitionVocabulary([])).toEqual([...CORE_VOCABULARY]);
  });
});
