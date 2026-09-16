// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { safeLocalStorage } from "./helper";

describe("safeLocalStorage", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("reports a stored write as true and a refused one as false, never throwing", () => {
    expect(safeLocalStorage.setItem("k", "v")).toBe(true);
    expect(safeLocalStorage.getItem("k")).toBe("v");
    expect(safeLocalStorage.removeItem("k")).toBe(true);
    expect(safeLocalStorage.getItem("k")).toBeNull();

    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("quota", "QuotaExceededError");
    });
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(safeLocalStorage.setItem("k", "v")).toBe(false);
    expect(safeLocalStorage.removeItem("k")).toBe(false);
    // Each refusal is logged with the key, so a support thread can name it.
    expect(warn).toHaveBeenCalledTimes(2);
    expect(String(warn.mock.calls[0][0])).toContain("k");
  });
});
