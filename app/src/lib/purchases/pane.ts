// Opening the Settings window at one pane, from the other window.
//
// The strip and the dashboard are separate webviews running one bundle. The
// strip can ask Rust to show the dashboard (`open_dashboard`, which
// `src/pages/kaleo/index.tsx` already calls) but cannot tell it where to go:
// the command takes no route, and a window that already exists is only shown
// and focused. So the request crosses the same way the tour's caption and
// state do (`src/lib/tour.ts`: one localStorage key plus the `storage` event,
// which fires in every window but the writer). The dashboard reads the key on
// mount, in case it was only just created, and hears the event when it was
// already open, then navigates to `/settings#<pane>` and clears the key.
//
// The key is under `kaleo.` and not `silkscreen_`: any `silkscreen_*` key is
// how `initSettings()` recognises an existing user, and asking to see a pane
// must not make a fresh install look like one. It is not under
// `kaleo.settings.` either, which is the settings mirror's prefix.

import { safeLocalStorage } from "@/lib/storage/helper";
import type { PaywallContext } from "./client";

export const PANE_REQUEST_KEY = "kaleo.open_pane";
/**
 * The board the order step was for, beside the pane request, so the paywall
 * the pane shows can name it. Same crossing, same staleness rule; a separate
 * key so the pane request's shape (and every reader of it) is unchanged.
 */
export const PAYWALL_CONTEXT_KEY = "kaleo.paywall_context";
/** A board name longer than this is cut: it is paywall copy, not a record. */
export const BOARD_NAME_MAX = 60;
/** The Ada Pro pane's element id in Settings, and its `#hash`. */
export const PRO_PANE_ID = "pro";
/** A request older than this is stale: the other window never took it. */
export const PANE_REQUEST_MAX_AGE_MS = 60_000;

const PANE_ID = /^[a-z][a-z0-9-]{0,31}$/;

interface PaneRequest {
  pane: string;
  at: number;
}

function parse(raw: string | null, now: number): string | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Partial<PaneRequest> | null;
    if (!value || typeof value !== "object") return null;
    if (typeof value.pane !== "string" || !PANE_ID.test(value.pane)) return null;
    if (typeof value.at !== "number" || !Number.isFinite(value.at)) return null;
    if (now - value.at > PANE_REQUEST_MAX_AGE_MS || value.at - now > PANE_REQUEST_MAX_AGE_MS) return null;
    return value.pane;
  } catch {
    return null;
  }
}

/** Ask the dashboard to show `pane`. False when storage refused the write. */
export function requestPane(pane: string, now = Date.now()): boolean {
  if (!PANE_ID.test(pane)) return false;
  const request: PaneRequest = { pane, at: now };
  return safeLocalStorage.setItem(PANE_REQUEST_KEY, JSON.stringify(request));
}

/**
 * Record what the paywall should say about the board. Only a printable name
 * and a whole part count cross; anything else is dropped rather than shown.
 */
export function requestPaywallContext(context: PaywallContext, now = Date.now()): boolean {
  const clean: PaywallContext = {};
  const board = typeof context.board === "string" ? context.board.replace(/[^\x20-\x7e]/g, "").trim() : "";
  if (board) clean.board = board.slice(0, BOARD_NAME_MAX);
  if (typeof context.parts === "number" && Number.isInteger(context.parts) && context.parts > 0) {
    clean.parts = context.parts;
  }
  return safeLocalStorage.setItem(PAYWALL_CONTEXT_KEY, JSON.stringify({ ...clean, at: now }));
}

/** The board context for the paywall, or null when absent, stale or malformed. */
export function readPaywallContext(now = Date.now()): PaywallContext | null {
  const raw = safeLocalStorage.getItem(PAYWALL_CONTEXT_KEY);
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as (PaywallContext & { at?: unknown }) | null;
    if (!value || typeof value !== "object" || typeof value.at !== "number") return null;
    if (Math.abs(now - value.at) > PANE_REQUEST_MAX_AGE_MS) return null;
    const out: PaywallContext = {};
    if (typeof value.board === "string" && value.board) out.board = value.board.slice(0, BOARD_NAME_MAX);
    if (typeof value.parts === "number" && Number.isInteger(value.parts) && value.parts > 0) out.parts = value.parts;
    return out.board || out.parts ? out : null;
  } catch {
    return null;
  }
}

/** The pending pane, or null when there is none or it is stale or malformed. */
export function readPaneRequest(now = Date.now()): string | null {
  return parse(safeLocalStorage.getItem(PANE_REQUEST_KEY), now);
}

export function clearPaneRequest(): void {
  safeLocalStorage.removeItem(PANE_REQUEST_KEY);
}

/** Hear requests written by the other window. Same-window writes are not events. */
export function subscribePaneRequests(cb: (pane: string) => void): () => void {
  if (typeof window === "undefined") return () => {};
  const handler = (event: StorageEvent) => {
    if (event.key !== PANE_REQUEST_KEY) return;
    const pane = parse(event.newValue, Date.now());
    if (pane) cb(pane);
  };
  window.addEventListener("storage", handler);
  return () => window.removeEventListener("storage", handler);
}

/** The route the dashboard navigates to for a pane. */
export function settingsPathFor(pane: string): string {
  return `/settings#${pane}`;
}

async function showDashboard(): Promise<void> {
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("open_dashboard");
}

/**
 * Record the request, then bring the dashboard up. The order is the point:
 * a dashboard created by the command reads the key as it mounts, and one
 * that already exists hears the event before it is focused. A failed
 * `open_dashboard` is logged, not thrown: the request is still in storage
 * and the next dashboard to mount will take it.
 */
export async function openSettingsPane(
  pane: string,
  open: () => Promise<void> = showDashboard,
  context?: PaywallContext
): Promise<void> {
  if (context) requestPaywallContext(context);
  requestPane(pane);
  try {
    await open();
  } catch (error) {
    console.warn("[ada purchases] could not open the dashboard:", error);
  }
}
