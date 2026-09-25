// The RevenueCat app user id, and the header that carries it to the engine.
//
// One UUID, minted on this desktop the first time purchases are configured
// and kept in the typed settings store under `purchases.appUserId`
// (`src/lib/settings/keys.ts`). It is what `Purchases.configure` gets as
// `appUserId` and what every `/steps` request carries as
// `X-Kaleo-App-User-Id`, so the service can ask RevenueCat about the same
// customer the desktop bought as.
//
// What it is not, each for a reason: not the engine's bearer token (a
// credential is not an identity, and it would leave for RevenueCat's API);
// not an `ada_` API key (`service/auth.py`, same reason); not RevenueCat's
// `$RCAnonymousID:` form, which `Purchases.generateRevenueCatAnonymousAppUserId`
// mints and `billing/accounts.py:31` refuses because it carries a `$`. A
// value that is not a UUID is refused by the store's validator, so a
// hand-edited `settings.json` cannot smuggle one in either.
//
// It is a claim, not a proof. The service trusts the header as sent, so the
// id is only as private as the desktop that holds it; a signed identity is
// not built (`docs/purchases.md`, "Not yet built").
//
// Exactly one window mints. The strip (Tauri label `main`, the one window in
// `tauri.conf.json`; `app/src-tauri/src/lib.rs` fetches it by that name) and
// the dashboard (label `dashboard`, pre-created at boot by the same file)
// both mount a `PurchasesProvider`, and both would otherwise read an empty
// store at the same moment and mint two ids for one slot. So the main window
// mints and the dashboard waits: it subscribes to the key and takes the id
// the main window writes, which reaches it through the store's own channels
// (the plugin's change event, or the localStorage mirror's `storage` event,
// `src/lib/settings/store.ts`). A window that cannot learn its label does
// not mint either. The wait is bounded, and running out is reported in words
// rather than answered with a second id. Outside Tauri there is one window,
// and it mints. The label is read the way `src/lib/notify/notify.ts` reads
// it (`currentWebviewLabel`): a dynamic import behind `isTauriRuntime()`, so
// Vitest never resolves the Tauri API.

import {
  getSetting,
  initSettings,
  isTauriRuntime,
  isUuid,
  setSetting,
  subscribe,
  type SettingsApi,
} from "@/lib/settings/store";

/** The header every `/steps` request carries when an id exists. */
export const APP_USER_ID_HEADER = "X-Kaleo-App-User-Id";

/** The Tauri label of the window that mints (`app/src-tauri/tauri.conf.json`). */
export const MAIN_WINDOW_LABEL = "main";

/** How long a non-minting window waits for the main window's id. */
export const APP_USER_ID_WAIT_MS = 20_000;

/** The sentence a window that waited in vain reports. */
export function waitTimedOutLine(waitMs: number): string {
  const seconds = Math.round(waitMs / 1000);
  return `No app user id arrived from the main window within ${seconds} s; this window does not mint one.`;
}

/**
 * A v4 UUID. `crypto.randomUUID` where the webview offers it (WKWebView does
 * on a secure origin; `tauri://localhost` is one), otherwise the same shape
 * from `getRandomValues`, the way `newRunId` in `src/lib/silkscreen/client.ts`
 * falls back. There is no third fallback: an id that is not random is worse
 * than no purchases, and `ensureAppUserId` throws instead.
 */
export function mintUuid(): string {
  const c = globalThis.crypto;
  if (typeof c?.randomUUID === "function") return c.randomUUID();
  if (typeof c?.getRandomValues !== "function") {
    throw new Error("This webview has no random source; an app user id cannot be minted.");
  }
  const bytes = c.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

let labelPromise: Promise<string | null> | null = null;

/** This webview's Tauri label, cached; null outside Tauri or when the API refuses. */
function currentLabel(): Promise<string | null> {
  if (!labelPromise) {
    labelPromise = (async () => {
      if (!isTauriRuntime()) return null;
      try {
        const { getCurrentWebviewWindow } = await import("@tauri-apps/api/webviewWindow");
        return getCurrentWebviewWindow().label;
      } catch {
        return null;
      }
    })();
  }
  return labelPromise;
}

/**
 * Does this window mint? The main window inside Tauri, and the only window
 * outside it. A Tauri window whose label cannot be read waits instead: a
 * second id is the one outcome this must not produce.
 */
export async function mintingWindow(): Promise<boolean> {
  if (!isTauriRuntime()) return true;
  return (await currentLabel()) === MAIN_WINDOW_LABEL;
}

/** The slice of the settings store the source needs. */
export type AppUserIdStore = Pick<SettingsApi, "getSetting" | "setSetting" | "subscribe" | "initSettings">;

export interface AppUserIdSourceOptions {
  /** Whether this window mints; the default is `mintingWindow()`. */
  mints?: () => boolean | Promise<boolean>;
  /** The non-minting window's wait; the default is `APP_USER_ID_WAIT_MS`. */
  waitMs?: number;
}

export interface AppUserIdSource {
  /** The id, minting or waiting for one the first time. Idempotent and serialised. */
  ensure(): Promise<string>;
  /** The stored id, or null when none exists (or the store is unreadable). */
  current(): string | null;
  /** The `/steps` header, or nothing at all when there is no id. */
  header(): Record<string, string>;
  /** Forget the in-flight ensure, so the next call reads the store again. */
  reset(): void;
}

/**
 * Wait for a UUID to appear under `purchases.appUserId`. Subscribes first
 * and reads second, so a write that lands between the two is not missed.
 */
function awaitStoredId(store: AppUserIdStore, waitMs: number): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    let done = false;
    let unsubscribe: () => void = () => {};
    let timer: ReturnType<typeof setTimeout> | null = null;
    const finish = (id: string) => {
      if (done) return;
      done = true;
      if (timer !== null) clearTimeout(timer);
      unsubscribe();
      resolve(id);
    };
    unsubscribe = store.subscribe("purchases.appUserId", (value) => {
      if (isUuid(value)) finish(value);
    });
    timer = setTimeout(() => {
      if (done) return;
      done = true;
      unsubscribe();
      reject(new Error(waitTimedOutLine(waitMs)));
    }, waitMs);
    const now = store.getSetting("purchases.appUserId");
    if (isUuid(now)) finish(now);
  });
}

/**
 * Build the source over one store. The module-level functions below are
 * this over the real store; tests build their own over two stores sharing
 * one backend, which is the two-window case.
 */
export function createAppUserIdSource(
  store: AppUserIdStore,
  options: AppUserIdSourceOptions = {}
): AppUserIdSource {
  const mints = options.mints ?? mintingWindow;
  const waitMs = options.waitMs ?? APP_USER_ID_WAIT_MS;
  let inFlight: Promise<string> | null = null;

  const current = (): string | null => {
    try {
      const stored = store.getSetting("purchases.appUserId");
      return isUuid(stored) ? stored : null;
    } catch {
      return null;
    }
  };

  const ensure = (): Promise<string> => {
    if (!inFlight) {
      inFlight = (async () => {
        await store.initSettings();
        const existing = current();
        if (existing) return existing;
        if (!(await mints())) return awaitStoredId(store, waitMs);
        // Read again after the label round trip: the other window may have
        // written in the meantime, and a second mint over its id is the bug.
        const meanwhile = current();
        if (meanwhile) return meanwhile;
        const fresh = mintUuid();
        await store.setSetting("purchases.appUserId", fresh);
        return fresh;
      })().catch((error) => {
        inFlight = null;
        throw error;
      });
    }
    return inFlight;
  };

  const header = (): Record<string, string> => {
    const id = current();
    return id ? { [APP_USER_ID_HEADER]: id } : {};
  };

  return {
    ensure,
    current,
    header,
    reset() {
      inFlight = null;
    },
  };
}

const defaultSource = createAppUserIdSource({ getSetting, setSetting, subscribe, initSettings });

/** The stored id, or null when none has been minted (or the store is unreadable). */
export function currentAppUserId(): string | null {
  return defaultSource.current();
}

/**
 * The id, minting one the first time in the main window, or waiting for the
 * main window's in any other. Idempotent and serialised: two callers in one
 * window (React StrictMode mounts twice) share one mint or one wait.
 */
export function ensureAppUserId(): Promise<string> {
  return defaultSource.ensure();
}

/** Test seam: forget the in-flight ensure and the cached label. */
export function resetAppUserIdForTests(): void {
  defaultSource.reset();
  labelPromise = null;
}

/**
 * The header for a `/steps` request: the stored id, or nothing at all when
 * there is none. An invented id would create a RevenueCat customer nobody
 * bought as, so the honest header for "no id" is no header.
 */
export function appUserIdHeader(): Record<string, string> {
  return defaultSource.header();
}
