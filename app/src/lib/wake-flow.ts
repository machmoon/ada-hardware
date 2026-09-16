/**
 * What the page does with a wake-word detection, as data.
 *
 * "Ada, make me a 3.3 V LDO board" and "Ada, route it again" arrive through
 * the same ear, and the difference between them is a paid board and a reply
 * to the run already open. The decision lives here, pure, so the page cannot
 * quietly grow a path where a sentence spoken at the wrong moment starts a
 * second run.
 *
 * Not every sentence is about a board. "Ada, can you hear me" is a question to
 * a colleague, and it used to be `start` — a paid pipeline run for a greeting.
 * An idle sentence is now `converse`: it goes to the orchestrator, which
 * answers or *proposes* a board, the way OpenClaw forwards every voice-wake
 * transcript to its one agent session instead of classifying it
 * (`apps/macos/Sources/OpenClaw/VoiceWakeForwarder.swift`). A proposal is
 * spent only on a bare yes, and only while one is pending — Hermes Agent's
 * `_PLAINTEXT_APPROVAL_WORDS` in `gateway/run_busy.py`, gated on
 * `has_blocking_approval` so a conversational "yes" never fires anything.
 */

export interface WakeFlowInput {
  /** What followed the wake word; empty means the engineer only said "Ada". */
  utterance: string;
  /** A run is in flight in either state machine. */
  busy: boolean;
  /** The step-by-step flow's status. */
  stepsStatus: "idle" | "running" | "waiting" | "done" | "error";
  /** A board the orchestrator proposed out loud and nobody has answered. */
  pendingProposal?: string | null;
}

export type WakeAction =
  /**
   * The sentence cannot be acted on and is dropped. This is no longer the
   * answer to "you spoke while a run was in flight" — that is a `command`
   * now, which the page parks. It survives for the deictic case in
   * `ada-path.ts`, where there is genuinely nothing to point at yet.
   */
  | { kind: "ignore"; reason: string }
  /** A step run is open: the sentence is a reply to it (approve, restart). */
  | { kind: "command"; text: string }
  /** A yes to the pending proposal: that proposal is the intent of a new board. */
  | { kind: "start"; intent: string }
  /** Nothing is open: talk to the orchestrator, which may only propose a board. */
  | { kind: "converse"; text: string }
  /** A no to the pending proposal: drop it, spend nothing. */
  | { kind: "decline" }
  /** Only the name was said: open the bar and wait for the sentence. */
  | { kind: "listen" };

/**
 * Whole answers that settle a pending proposal, after punctuation and case are
 * dropped. Whole-utterance only, like Hermes' bare-word map: "yes, but make it
 * 5 V" is a new sentence for the orchestrator, not a yes.
 */
const APPROVE_ANSWERS = new Set([
  "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "approve",
  "yes please", "do it", "build it", "go ahead", "go for it", "sounds good",
  "yes do it", "yes build it", "yeah do it", "yeah build it", "please do",
]);
const DECLINE_ANSWERS = new Set([
  "no", "nope", "nah", "cancel", "deny", "stop", "never mind", "nevermind",
  "no thanks", "no thank you", "dont", "don t", "not now",
]);

export function proposalAnswer(utterance: string): "approve" | "decline" | null {
  const words = utterance.toLowerCase().replace(/[^a-z0-9 ]+/g, " ").split(/\s+/).filter(Boolean);
  const said = words.join(" ");
  if (APPROVE_ANSWERS.has(said)) return "approve";
  if (DECLINE_ANSWERS.has(said)) return "decline";
  return null;
}

export function wakeAction(input: WakeFlowInput): WakeAction {
  const text = input.utterance.trim();
  const proposal = input.pendingProposal?.trim();
  if (proposal) {
    // The pending question is answered before anything else, mid-run too:
    // it was asked out loud and "yes" is the answer to it, not to a stage.
    const answer = proposalAnswer(text);
    if (answer === "approve") return { kind: "start", intent: proposal };
    if (answer === "decline") return { kind: "decline" };
  }
  if (input.busy || input.stepsStatus === "running") {
    // Speaking while a run is in flight used to be dropped with "say it again
    // when this run is done", which is the app refusing a sentence it heard
    // perfectly well. It is a command now: the page parks it and says what it
    // will do with it when the stage lands. Nothing about that spends money —
    // a parked sentence still has to be confirmed — so the guard that
    // mattered (one paid run at a time) is unchanged.
    return { kind: "command", text };
  }
  if (
    input.stepsStatus === "waiting" ||
    input.stepsStatus === "done" ||
    input.stepsStatus === "error"
  ) {
    // Even an empty utterance is a reply here: the interpreter answers it
    // with the list of what could be said, which is the right prompt.
    return { kind: "command", text };
  }
  if (!text) return { kind: "listen" };
  return { kind: "converse", text };
}
