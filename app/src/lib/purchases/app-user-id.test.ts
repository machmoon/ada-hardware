// @vitest-environment jsdom
//
// The app user id: one UUID, minted once, in one window, and the header that
// carries it. The two-window case is driven with two settings stores over
// one backend, which is what the strip and the dashboard are.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  UUID_RE,
  createSettingsStore,
  getSetting,
  setSetting,
  type SettingKey,
  type SettingsBackend,
} from "@/lib/settings/store";
import {
  APP_USER_ID_HEADER,
  MAIN_WINDOW_LABEL,
  appUserIdHeader,
  createAppUserIdSource,
  currentAppUserId,
  ensureAppUserId,
  mintUuid,
  mintingWindow,
  resetAppUserIdForTests,
  waitTimedOutLine,
} from "./app-user-id";

let currentLabel = MAIN_WINDOW_LABEL;

/** The Tauri API, as far as this module reads it: the current window's label. */
vi.mock("@tauri-apps/api/webviewWindow", () => ({
  getCurrentWebviewWindow: () => ({ label: currentLabel }),
}));

beforeEach(async () => {
  window.localStorage.clear();
  resetAppUserIdForTests();
  await setSetting("purchases.appUserId", "");
});

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  currentLabel = MAIN_WINDOW_LABEL;
});

/**
 * One backend for two stores, with the change event the plugin gives every
 * window for a write made in the other. `sets` records who wrote.
 */
function sharedBackend() {
  const data = new Map<SettingKey, unknown>();
  const listeners = new Set<(key: string, value: unknown) => void>();
  const sets: SettingKey[] = [];
  const backend: SettingsBackend = {
    async load() {
      return Object.fromEntries(data) as Partial<Record<SettingKey, unknown>>;
    },
    async set(key, value) {
      data.set(key, value);
      sets.push(key);
      for (const cb of [...listeners]) cb(key, value);
    },
    async onChange(cb) {
      listeners.add(cb);
      return () => {
        listeners.delete(cb);
      };
    },
  };
  return { backend, data, sets };
}

const window_ = (backend: SettingsBackend) =>
  createSettingsStore(backend, { tauri: false, hasLegacyKeys: () => false });

const tick = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

describe("mintUuid", () => {
  it("mints a UUID, and a different one each time", () => {
    const a = mintUuid();
    const b = mintUuid();
    expect(a).toMatch(UUID_RE);
    expect(b).toMatch(UUID_RE);
    expect(a).not.toBe(b);
    // Never RevenueCat's anonymous form, which the ledger refuses.
    expect(a).not.toContain("$");
  });
});

describe("mintingWindow", () => {
  it("outside Tauri the only window mints", async () => {
    expect("__TAURI_INTERNALS__" in window).toBe(false);
    expect(await mintingWindow()).toBe(true);
  });

  it("inside Tauri only the main window mints; the dashboard does not", async () => {
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    currentLabel = "dashboard";
    expect(await mintingWindow()).toBe(false);
    resetAppUserIdForTests();
    currentLabel = "main";
    expect(await mintingWindow()).toBe(true);
    expect(MAIN_WINDOW_LABEL).toBe("main");
  });
});

describe("ensureAppUserId", () => {
  it("mints once, persists, and answers the same id afterwards", async () => {
    expect(currentAppUserId()).toBeNull();
    const first = await ensureAppUserId();
    expect(first).toMatch(UUID_RE);
    expect(getSetting("purchases.appUserId")).toBe(first);
    expect(await ensureAppUserId()).toBe(first);
    resetAppUserIdForTests();
    expect(await ensureAppUserId()).toBe(first);
  });

  it("two callers in flight share one mint", async () => {
    const [a, b] = await Promise.all([ensureAppUserId(), ensureAppUserId()]);
    expect(a).toBe(b);
  });

  it("keeps an id the store already holds", async () => {
    await setSetting("purchases.appUserId", "8f1c2b4e-3d5a-4f6b-9c7d-0e1f2a3b4c5d");
    expect(await ensureAppUserId()).toBe("8f1c2b4e-3d5a-4f6b-9c7d-0e1f2a3b4c5d");
  });
});

describe("two windows over one store", () => {
  it("the main window mints and the dashboard takes that id: one write, one id", async () => {
    const { backend, data, sets } = sharedBackend();
    const strip = createAppUserIdSource(window_(backend), { mints: () => true });
    const dashboard = createAppUserIdSource(window_(backend), { mints: () => false, waitMs: 2_000 });
    // The dashboard reaches its wait first (both read an empty store), then
    // the strip mints; the dashboard hears the write through the backend's
    // change event, since a same-window mirror write is not a storage event.
    const waited = dashboard.ensure();
    await tick();
    expect(dashboard.current()).toBeNull();
    const minted = await strip.ensure();
    expect(minted).toMatch(UUID_RE);
    expect(await waited).toBe(minted);
    expect(dashboard.current()).toBe(minted);
    expect(sets.filter((k) => k === "purchases.appUserId")).toHaveLength(1);
    expect(data.get("purchases.appUserId")).toBe(minted);
    expect(dashboard.header()).toEqual({ [APP_USER_ID_HEADER]: minted });
  });

  it("interleaved from boot, both windows still end with the one id the main window wrote", async () => {
    const { backend, sets } = sharedBackend();
    const strip = createAppUserIdSource(window_(backend), { mints: () => true });
    const dashboard = createAppUserIdSource(window_(backend), { mints: () => false, waitMs: 2_000 });
    const [a, b] = await Promise.all([strip.ensure(), dashboard.ensure()]);
    expect(a).toBe(b);
    expect(sets.filter((k) => k === "purchases.appUserId")).toHaveLength(1);
  });

  it("a dashboard that already finds the id on disk never waits", async () => {
    const { backend, sets } = sharedBackend();
    await backend.set("purchases.appUserId", "8f1c2b4e-3d5a-4f6b-9c7d-0e1f2a3b4c5d");
    const dashboard = createAppUserIdSource(window_(backend), { mints: () => false, waitMs: 20 });
    expect(await dashboard.ensure()).toBe("8f1c2b4e-3d5a-4f6b-9c7d-0e1f2a3b4c5d");
    expect(sets.filter((k) => k === "purchases.appUserId")).toHaveLength(1);
  });

  it("a window that does not mint gives up in words, never with a second id, and can ask again", async () => {
    const { backend, sets } = sharedBackend();
    const dashboard = createAppUserIdSource(window_(backend), { mints: () => false, waitMs: 30 });
    await expect(dashboard.ensure()).rejects.toThrow(waitTimedOutLine(30));
    expect(waitTimedOutLine(20_000)).toBe(
      "No app user id arrived from the main window within 20 s; this window does not mint one."
    );
    expect(sets).not.toContain("purchases.appUserId");
    expect(dashboard.current()).toBeNull();
    // The failure is not pinned: the next call waits again, and this time
    // the id is there.
    const strip = createAppUserIdSource(window_(backend), { mints: () => true });
    const minted = await strip.ensure();
    expect(await dashboard.ensure()).toBe(minted);
  });
});

describe("appUserIdHeader", () => {
  it("is no header at all until an id exists, then exactly the one header", async () => {
    expect(appUserIdHeader()).toEqual({});
    const id = await ensureAppUserId();
    expect(appUserIdHeader()).toEqual({ [APP_USER_ID_HEADER]: id });
    expect(APP_USER_ID_HEADER).toBe("X-Kaleo-App-User-Id");
  });

  it("refuses to store anything but a UUID, so the header can never carry a credential", async () => {
    await expect(setSetting("purchases.appUserId", "ada_8f1c2b4e3d5a4f6b9c7d0e1f2a3b4c5d")).rejects.toThrow(
      /invalid value/
    );
    expect(appUserIdHeader()).toEqual({});
  });
});
