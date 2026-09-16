// Calm output: the overlay shows where a run is and what needs a decision,
// and folds the rest. On unless the user turned it off in Settings.

import { useEffect, useState } from "react";
import { KALEO_STORAGE_KEYS } from "@/config/kaleo.constants";

const KEY = KALEO_STORAGE_KEYS.CALM_OUTPUT;

export function readCalm(): boolean {
  try {
    return localStorage.getItem(KEY) !== "0";
  } catch {
    return true;
  }
}

export function writeCalm(on: boolean): void {
  try {
    localStorage.setItem(KEY, on ? "1" : "0");
    window.dispatchEvent(new StorageEvent("storage", { key: KEY }));
  } catch {
    // A preference that cannot be saved stays on for this session.
  }
}

/** The preference, kept in step across windows. */
export function useCalm(): boolean {
  const [calm, setCalm] = useState(readCalm);
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key === null || event.key === KEY) setCalm(readCalm());
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);
  return calm;
}
