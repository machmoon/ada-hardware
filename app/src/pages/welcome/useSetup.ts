import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { SETUP_VERSION, getSetting, initSettings, setSetting } from "@/lib/settings/store";
import {
  INITIAL_STATE,
  hydrate,
  needsSetup as needsSetupFrom,
  progress,
  reduce,
  remaining,
  type SetupAction,
  type SetupCardId,
  type SetupState,
} from "@/lib/setup/machine";

/** What the Rust side says on `kaleo-setup-changed` (contract C3). */
interface SetupChanged {
  completed?: boolean;
  skipped?: string[];
}

/** `setup_status` (app/src-tauri/src/setup.rs): the shell's own gate. */
interface SetupStatus {
  completed?: boolean;
  in_setup?: boolean;
}

export interface UseSetup {
  state: SetupState;
  /** False once the gate says the wizard is over; the page then leaves. */
  needsSetup: boolean;
  /**
   * True once `needsSetup` is the shell's answer (or the store's, outside the
   * shell). Until then the page may draw a step but must not leave: the
   * synchronous store read falls back to a localStorage mirror, and a mirror
   * that still said "completed" from the last run sent a gated launch
   * straight to the workbench (observed 2026-09-16).
   */
  resolved: boolean;
  index: number;
  total: number;
  dispatch: (action: SetupAction) => void;
  /** Mark a card connected/enabled (or not), for the skipped list. */
  setCard: (card: SetupCardId, done: boolean) => void;
  /** `invoke("setup_finish")` with the cards still undone, then mark the store. */
  finish: () => Promise<void>;
}

function readState(): SetupState {
  try {
    return hydrate({
      step: getSetting("setup.step"),
      skipped: getSetting("setup.skipped"),
      remaining: getSetting("setup.remaining"),
    });
  } catch {
    return INITIAL_STATE;
  }
}

function readCompleted(): boolean {
  try {
    return getSetting("setup.completed") === true;
  } catch {
    return false;
  }
}

/**
 * The wizard's state, persisted after every reduce.
 *
 * `setup.step` is the resume point, `setup.skipped` the cards walked past
 * and `setup.remaining` the cards a closed window would leave undone — the
 * Rust close-mid-setup path copies the last into the middle. Persistence is
 * fire-and-forget: the store mirrors to localStorage synchronously and the
 * disk write trails by its autosave.
 */
export function useSetup(): UseSetup {
  const [state, setState] = useState<SetupState>(readState);
  const [completed, setCompleted] = useState<boolean>(readCompleted);
  const [resolved, setResolved] = useState(false);
  const stateRef = useRef(state);
  stateRef.current = state;
  const touchedRef = useRef(false);

  const persist = useCallback((next: SetupState) => {
    void setSetting("setup.step", next.step);
    void setSetting("setup.skipped", next.skipped);
    void setSetting("setup.remaining", remaining(next));
  }, []);

  const dispatch = useCallback(
    (action: SetupAction) => {
      const next = reduce(stateRef.current, action);
      if (next === stateRef.current) return;
      touchedRef.current = true;
      stateRef.current = next;
      setState(next);
      persist(next);
    },
    [persist],
  );

  const setCard = useCallback(
    (card: SetupCardId, done: boolean) => dispatch({ type: done ? "complete" : "uncomplete", card }),
    [dispatch],
  );

  // One source of truth for the gate: the shell reads `setup.completed` from
  // disk on every call (`setup_status`), and a shell in setup mode (`Run Setup
  // Again`) is in setup whatever the flag says. The store is the answer only
  // where there is no shell (a browser tab, a test). Cap's desktop does the
  // same -- `should_show_onboarding` in `apps/desktop/src-tauri/src/lib.rs`
  // decides in Rust and the page reads it through an async query, never a
  // synchronous shadow (CapSoftware/Cap @ 94bd7b4).
  useEffect(() => {
    let cancelled = false;
    invoke<SetupStatus>("setup_status")
      .then((status) => {
        if (cancelled) return;
        if (status && typeof status.completed === "boolean") {
          setCompleted(status.completed && status.in_setup !== true);
        } else {
          setCompleted(readCompleted());
        }
      })
      .catch(() => {
        if (!cancelled) setCompleted(readCompleted());
      })
      .finally(() => {
        if (!cancelled) setResolved(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // The first paint read the resume point from the localStorage mirror, which
  // is the last value this webview wrote, not the file the shell gated on.
  // Once the plugin has loaded `settings.json` (`initSettings` is idempotent
  // and returns the same promise `main.tsx` fired), read the step again --
  // unless the engineer has already moved, in which case theirs wins.
  useEffect(() => {
    let cancelled = false;
    initSettings()
      .then(() => {
        if (cancelled || touchedRef.current) return;
        const next = readState();
        stateRef.current = next;
        setState(next);
      })
      .catch(() => {
        // Outside the shell the mirror is all there is.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Re-hydrate when the shell finishes setup for us (window closed mid-way).
  useEffect(() => {
    let unlisten: (() => void) | undefined;
    let cancelled = false;
    listen<SetupChanged>("kaleo-setup-changed", (event) => {
      if (event.payload?.completed) setCompleted(true);
      const next = readState();
      stateRef.current = next;
      setState(next);
    })
      .then((fn) => {
        if (cancelled) fn();
        else unlisten = fn;
      })
      .catch(() => {
        // Outside Tauri (tests, a browser tab) there is no event bus.
      });
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, []);

  const finish = useCallback(async () => {
    const skipped = Array.from(new Set([...stateRef.current.skipped, ...remaining(stateRef.current)]));
    try {
      await invoke("setup_finish", { skipped });
    } catch (error) {
      // No shell (a browser tab, a test): the store is still the gate the
      // next launch reads, so mark it here rather than looping the wizard.
      console.warn("[kaleo setup] setup_finish:", error);
    }
    void setSetting("setup.skipped", skipped);
    void setSetting("setup.remaining", []);
    void setSetting("setup.completed", true);
    void setSetting("setup.version", SETUP_VERSION);
    void setSetting("setup.completedAt", Date.now());
    setCompleted(true);
  }, []);

  const { index, total } = useMemo(() => progress(state), [state]);

  return {
    state,
    needsSetup: needsSetupFrom({ completed }),
    resolved,
    index,
    total,
    dispatch,
    setCard,
    finish,
  };
}
