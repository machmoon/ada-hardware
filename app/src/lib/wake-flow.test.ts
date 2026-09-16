import { describe, expect, it } from "vitest";

import { proposalAnswer, wakeAction } from "./wake-flow";

describe("wakeAction", () => {
  it("talks to the orchestrator when nothing is open, rather than starting a board", () => {
    expect(
      wakeAction({ utterance: " make me a 3.3 V LDO board ", busy: false, stepsStatus: "idle" })
    ).toEqual({ kind: "converse", text: "make me a 3.3 V LDO board" });
  });

  it("\"can you hear me\" is a question, never a paid board", () => {
    expect(
      wakeAction({ utterance: "can you hear me?", busy: false, stepsStatus: "idle" })
    ).toEqual({ kind: "converse", text: "can you hear me?" });
  });

  it("a bare yes to a pending proposal starts exactly that proposal", () => {
    expect(
      wakeAction({
        utterance: "Yeah, do it.",
        busy: false,
        stepsStatus: "idle",
        pendingProposal: "A 3.3 V LDO board from USB 5 V",
      })
    ).toEqual({ kind: "start", intent: "A 3.3 V LDO board from USB 5 V" });
  });

  it("the pending proposal's yes wins over a stage waiting for approval", () => {
    expect(
      wakeAction({
        utterance: "yes",
        busy: false,
        stepsStatus: "waiting",
        pendingProposal: "a blinker",
      })
    ).toEqual({ kind: "start", intent: "a blinker" });
  });

  it("a no drops the proposal and spends nothing", () => {
    expect(
      wakeAction({
        utterance: "No thanks",
        busy: false,
        stepsStatus: "idle",
        pendingProposal: "a blinker",
      })
    ).toEqual({ kind: "decline" });
  });

  it("a yes with no proposal pending is conversation, not consent", () => {
    expect(wakeAction({ utterance: "yes", busy: false, stepsStatus: "idle" })).toEqual({
      kind: "converse",
      text: "yes",
    });
  });

  it("a qualified yes goes back to the orchestrator as a new sentence", () => {
    expect(
      wakeAction({
        utterance: "yes but make it 5 volts",
        busy: false,
        stepsStatus: "idle",
        pendingProposal: "a 3.3 V LDO board",
      })
    ).toEqual({ kind: "converse", text: "yes but make it 5 volts" });
  });
});

describe("proposalAnswer", () => {
  it("matches whole answers only", () => {
    expect(proposalAnswer("Build it!")).toBe("approve");
    expect(proposalAnswer("never mind")).toBe("decline");
    expect(proposalAnswer("don't")).toBe("decline");
    expect(proposalAnswer("yes, the second one")).toBeNull();
    expect(proposalAnswer("")).toBeNull();
  });
});

describe("wakeAction, open runs", () => {

  it("only the name, nothing open: listen for the sentence", () => {
    expect(wakeAction({ utterance: "", busy: false, stepsStatus: "idle" })).toEqual({
      kind: "listen",
    });
  });

  it("while a stage waits, the sentence is a reply to the run, never a second board", () => {
    for (const stepsStatus of ["waiting", "done", "error"] as const) {
      expect(wakeAction({ utterance: "approve", busy: false, stepsStatus })).toEqual({
        kind: "command",
        text: "approve",
      });
    }
    expect(wakeAction({ utterance: "", busy: false, stepsStatus: "waiting" })).toEqual({
      kind: "command",
      text: "",
    });
  });

  it("a run in flight takes the sentence as a command rather than dropping it", () => {
    // It used to answer `ignore` — "say it again when this run is done" —
    // which is the app refusing a sentence it heard perfectly well. The
    // guarantee that mattered is unchanged and is one layer up: a `command`
    // is interpreted, and a sentence that describes a board is held for a
    // human to confirm. Nothing here starts a run.
    expect(wakeAction({ utterance: "an LDO", busy: true, stepsStatus: "idle" })).toEqual({
      kind: "command",
      text: "an LDO",
    });
    expect(wakeAction({ utterance: "an LDO", busy: false, stepsStatus: "running" })).toEqual({
      kind: "command",
      text: "an LDO",
    });
    // And it is never `start`, which is the kind that POSTs /generate.
    for (const busy of [true, false]) {
      expect(
        wakeAction({ utterance: "a buck converter", busy, stepsStatus: "running" }).kind
      ).not.toBe("start");
    }
  });
});
