// @vitest-environment jsdom
//
// The cross-window "open Settings at this pane" request: written by the
// strip, taken by the dashboard, stale after a minute, never a navigation in
// the writer's own window.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  PANE_REQUEST_KEY,
  PANE_REQUEST_MAX_AGE_MS,
  PRO_PANE_ID,
  clearPaneRequest,
  openSettingsPane,
  readPaneRequest,
  requestPane,
  settingsPathFor,
  subscribePaneRequests,
} from "./pane";

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("pane requests", () => {
  it("round-trips a pane through storage and clears", () => {
    expect(readPaneRequest()).toBeNull();
    expect(requestPane(PRO_PANE_ID, 1_000)).toBe(true);
    expect(readPaneRequest(1_500)).toBe("pro");
    clearPaneRequest();
    expect(readPaneRequest(1_500)).toBeNull();
  });

  it("a request nobody took within a minute is stale", () => {
    requestPane("pro", 1_000);
    expect(readPaneRequest(1_000 + PANE_REQUEST_MAX_AGE_MS)).toBe("pro");
    expect(readPaneRequest(1_001 + PANE_REQUEST_MAX_AGE_MS)).toBeNull();
    // A clock that went backwards by more than the window is stale too.
    expect(readPaneRequest(1_000 - PANE_REQUEST_MAX_AGE_MS - 1)).toBeNull();
  });

  it("refuses a pane id that is not a plain element id, in and out", () => {
    expect(requestPane("../etc", 1)).toBe(false);
    expect(requestPane("", 1)).toBe(false);
    expect(readPaneRequest()).toBeNull();
    window.localStorage.setItem(PANE_REQUEST_KEY, JSON.stringify({ pane: "<script>", at: 1 }));
    expect(readPaneRequest(2)).toBeNull();
    window.localStorage.setItem(PANE_REQUEST_KEY, "not json");
    expect(readPaneRequest(2)).toBeNull();
    window.localStorage.setItem(PANE_REQUEST_KEY, JSON.stringify({ pane: "pro" }));
    expect(readPaneRequest(2)).toBeNull();
  });

  it("names the settings route with the pane as its hash", () => {
    expect(settingsPathFor("pro")).toBe("/settings#pro");
  });

  it("does not live under the existing-user or settings-mirror prefixes", () => {
    expect(PANE_REQUEST_KEY.startsWith("silkscreen_")).toBe(false);
    expect(PANE_REQUEST_KEY.startsWith("kaleo.settings.")).toBe(false);
  });

  it("hears a request written by the other window, and only that key", () => {
    const heard: string[] = [];
    const off = subscribePaneRequests((pane) => heard.push(pane));
    const now = Date.now();
    window.dispatchEvent(
      new StorageEvent("storage", {
        key: PANE_REQUEST_KEY,
        newValue: JSON.stringify({ pane: "pro", at: now }),
      })
    );
    window.dispatchEvent(
      new StorageEvent("storage", { key: "silkscreen_tour", newValue: JSON.stringify({ pane: "pro", at: now }) })
    );
    window.dispatchEvent(
      new StorageEvent("storage", {
        key: PANE_REQUEST_KEY,
        newValue: JSON.stringify({ pane: "pro", at: now - PANE_REQUEST_MAX_AGE_MS - 1 }),
      })
    );
    expect(heard).toEqual(["pro"]);
    off();
    window.dispatchEvent(
      new StorageEvent("storage", { key: PANE_REQUEST_KEY, newValue: JSON.stringify({ pane: "pro", at: now }) })
    );
    expect(heard).toEqual(["pro"]);
  });
});

describe("openSettingsPane", () => {
  it("records the request before it opens the dashboard", async () => {
    const order: string[] = [];
    await openSettingsPane("pro", async () => {
      order.push(`open:${readPaneRequest() ?? "none"}`);
    });
    expect(order).toEqual(["open:pro"]);
    expect(readPaneRequest()).toBe("pro");
  });

  it("a dashboard that would not open is logged, and the request is kept", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    await expect(
      openSettingsPane("pro", async () => {
        throw new Error("no window");
      })
    ).resolves.toBeUndefined();
    expect(warn).toHaveBeenCalledTimes(1);
    expect(readPaneRequest()).toBe("pro");
  });
});

describe("paywall context", () => {
  it("carries a printable board name and a whole part count, and nothing else", async () => {
    const { requestPaywallContext, readPaywallContext, BOARD_NAME_MAX } = await import("./pane");
    const now = 1_000_000;
    requestPaywallContext({ board: "  LDO\u0007 boardé  ", parts: 7 }, now);
    expect(readPaywallContext(now)).toEqual({ board: "LDO board", parts: 7 });
    requestPaywallContext({ board: "x".repeat(200), parts: 2.5 }, now);
    expect(readPaywallContext(now)).toEqual({ board: "x".repeat(BOARD_NAME_MAX) });
    requestPaywallContext({ board: "", parts: 0 }, now);
    expect(readPaywallContext(now)).toBeNull();
  });

  it("is stale after the pane request's own minute", async () => {
    const { requestPaywallContext, readPaywallContext, PANE_REQUEST_MAX_AGE_MS } = await import("./pane");
    requestPaywallContext({ board: "blinker" }, 0);
    expect(readPaywallContext(PANE_REQUEST_MAX_AGE_MS)).toEqual({ board: "blinker" });
    expect(readPaywallContext(PANE_REQUEST_MAX_AGE_MS + 1)).toBeNull();
  });
});
