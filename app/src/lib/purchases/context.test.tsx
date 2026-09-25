// @vitest-environment jsdom
//
// The purchases provider over the null SDK: no key is "not configured",
// never a throw; a key configures once with a minted UUID and asks for the
// verdict; a purchase moves the verdict; a failed check is "unknown" with the
// reason and never "free".

import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  FOCUS_REFRESH_MIN_MS,
  PurchasesProvider,
  usePurchases,
  type PurchasesState,
} from "@/contexts/purchases.context";
import { mirrorKey } from "@/lib/settings/keys";
import { UUID_RE, getSetting, setSetting } from "@/lib/settings/store";
import {
  NOT_CONFIGURED_LINE,
  NOT_MOUNTED_LINE,
  customerInfoFixture,
  nullPurchases,
  offeringsFixture,
  packageFixture,
  setPurchasesSdk,
} from "./client";
import { resetAppUserIdForTests } from "./app-user-id";

let latest: PurchasesState | null = null;

const Probe = () => {
  const purchases = usePurchases();
  latest = purchases;
  return (
    <p data-testid="probe" data-status={purchases.status} data-configured={String(purchases.configured)}>
      {purchases.reason ?? ""}
    </p>
  );
};

const mount = (apiKey: string, refreshOnFocus = false) =>
  render(
    <PurchasesProvider apiKey={apiKey} refreshOnFocus={refreshOnFocus}>
      <Probe />
    </PurchasesProvider>
  );

const status = () => screen.getByTestId("probe").getAttribute("data-status");

beforeEach(async () => {
  window.localStorage.clear();
  latest = null;
  resetAppUserIdForTests();
  await setSetting("purchases.appUserId", "");
  await setSetting("purchases.lastVerdict", { verdict: "unknown", at: "" });
});

afterEach(() => {
  cleanup();
  setPurchasesSdk(null);
  vi.useRealTimers();
});

describe("PurchasesProvider", () => {
  it("with no key is not configured, unknown, and says why in one sentence", async () => {
    const fake = nullPurchases();
    setPurchasesSdk(fake);
    mount("");
    await waitFor(() => expect(screen.getByTestId("probe").textContent).toBe(NOT_CONFIGURED_LINE));
    expect(status()).toBe("unknown");
    expect(screen.getByTestId("probe").getAttribute("data-configured")).toBe("false");
    expect(fake.calls).toEqual([]);
  });

  it("with a Test Store key configures once with a minted UUID, then reads the verdict", async () => {
    const fake = nullPurchases({ offerings: offeringsFixture([packageFixture()]) });
    setPurchasesSdk(fake);
    mount("test_abc");
    await waitFor(() => expect(status()).toBe("free"));
    expect(fake.configured?.apiKey).toBe("test_abc");
    expect(fake.configured?.appUserId).toMatch(UUID_RE);
    expect(getSetting("purchases.appUserId")).toBe(fake.configured?.appUserId);
    expect(fake.calls.filter((c) => c === "configure")).toHaveLength(1);
    expect(latest?.sandbox).toBe(true);
    expect(latest?.configured).toBe(true);
    expect(latest?.appUserId).toBe(fake.configured?.appUserId);
    expect(latest?.reason).toBeNull();
    await waitFor(() => expect(latest?.offerings).toBeDefined());
    await waitFor(() => expect(getSetting("purchases.lastVerdict").verdict).toBe("free"));
  });

  it("an entitled customer is entitled, and a purchase moves free to entitled", async () => {
    const pkg = packageFixture();
    const fake = nullPurchases({ offerings: offeringsFixture([pkg]) });
    setPurchasesSdk(fake);
    mount("test_abc");
    await waitFor(() => expect(status()).toBe("free"));
    let result: Awaited<ReturnType<PurchasesState["buy"]>> | null = null;
    await act(async () => {
      result = await latest!.buy(pkg);
    });
    expect(fake.purchased).toEqual([pkg]);
    expect(status()).toBe("entitled");
    expect(latest?.lastTransaction?.storeTransactionId).toBe(result!.storeTransaction.storeTransactionId);
    await waitFor(() => expect(getSetting("purchases.lastVerdict").verdict).toBe("entitled"));
  });

  it("starts entitled when RevenueCat already says so", async () => {
    setPurchasesSdk(nullPurchases({ customerInfo: customerInfoFixture({ pro: true }) }));
    mount("rcb_sb_abc");
    await waitFor(() => expect(status()).toBe("entitled"));
    // Not a Test Store key; the SDK's own isSandbox() is the other source.
    expect(latest?.sandbox).toBe(false);
  });

  it("the SDK's isSandbox() also marks the build sandbox", async () => {
    setPurchasesSdk(nullPurchases({ sandbox: true }));
    mount("rcb_sb_abc");
    await waitFor(() => expect(status()).toBe("free"));
    expect(latest?.sandbox).toBe(true);
  });

  it("a check that fails is unknown with the reason, never free, and the last verdict stands", async () => {
    await setSetting("purchases.lastVerdict", { verdict: "entitled", at: "2026-09-24T10:00:00.000Z" });
    setPurchasesSdk(nullPurchases({ customerInfoError: new Error("Network request failed") }));
    mount("test_abc");
    await waitFor(() => expect(status()).toBe("unknown"));
    expect(screen.getByTestId("probe").textContent).toBe("Ada Pro could not be checked: Network request failed");
    expect(latest?.error).toBe("Network request failed");
    expect(latest?.configured).toBe(true);
    expect(getSetting("purchases.lastVerdict").verdict).toBe("entitled");
  });

  it("a configure the SDK refuses is unknown with the reason", async () => {
    setPurchasesSdk(nullPurchases({ configureError: new Error("Invalid API key") }));
    mount("test_abc");
    await waitFor(() =>
      expect(screen.getByTestId("probe").textContent).toBe("Purchases could not be configured: Invalid API key")
    );
    expect(status()).toBe("unknown");
    expect(latest?.configured).toBe(false);
  });

  it("refreshes on window focus, at most once per spacing", async () => {
    const fake = nullPurchases();
    setPurchasesSdk(fake);
    mount("test_abc", true);
    await waitFor(() => expect(status()).toBe("free"));
    const before = fake.calls.filter((c) => c === "getCustomerInfo").length;
    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    expect(fake.calls.filter((c) => c === "getCustomerInfo")).toHaveLength(before);
    vi.useFakeTimers();
    vi.setSystemTime(Date.now() + FOCUS_REFRESH_MIN_MS + 1);
    fake.setCustomerInfo(customerInfoFixture({ pro: true }));
    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    vi.useRealTimers();
    await waitFor(() => expect(status()).toBe("entitled"));
    expect(fake.calls.filter((c) => c === "getCustomerInfo")).toHaveLength(before + 1);
  });

  it("hears the other window's verdict through the settings store and asks RevenueCat again at once", async () => {
    const fake = nullPurchases();
    setPurchasesSdk(fake);
    mount("test_abc");
    await waitFor(() => expect(status()).toBe("free"));
    await waitFor(() => expect(getSetting("purchases.lastVerdict").verdict).toBe("free"));
    const count = () => fake.calls.filter((c) => c === "getCustomerInfo").length;
    // One check on mount; this window's own record did not make it ask twice.
    expect(count()).toBe(1);
    const hear = (verdict: "entitled" | "free") =>
      act(() => {
        // What the mirror's `storage` event carries when the other window
        // wrote `purchases.lastVerdict`; a same-window write is not an event.
        window.dispatchEvent(
          new StorageEvent("storage", {
            key: mirrorKey("purchases.lastVerdict"),
            newValue: JSON.stringify({ verdict, at: "2026-09-24T10:05:00.000Z" }),
          })
        );
      });
    // The same verdict is nothing new.
    hear("free");
    expect(count()).toBe(1);
    fake.setCustomerInfo(customerInfoFixture({ pro: true }));
    hear("entitled");
    // No ten-second spacing applies: the mount check was a moment ago.
    await waitFor(() => expect(status()).toBe("entitled"));
    expect(count()).toBe(2);
  });

  it("a configure that failed is tried again on focus", async () => {
    setPurchasesSdk(nullPurchases({ configureError: new Error("Invalid API key") }));
    mount("test_abc", true);
    await waitFor(() =>
      expect(screen.getByTestId("probe").textContent).toBe("Purchases could not be configured: Invalid API key")
    );
    const fake = nullPurchases();
    setPurchasesSdk(fake);
    vi.useFakeTimers();
    vi.setSystemTime(Date.now() + FOCUS_REFRESH_MIN_MS + 1);
    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    vi.useRealTimers();
    await waitFor(() => expect(status()).toBe("free"));
    expect(fake.configured?.apiKey).toBe("test_abc");
    expect(latest?.configured).toBe(true);
  });
});

describe("usePurchases outside a provider", () => {
  it("is unknown and says purchases are not mounted, rather than throwing", () => {
    render(<Probe />);
    expect(status()).toBe("unknown");
    expect(screen.getByTestId("probe").textContent).toBe(NOT_MOUNTED_LINE);
  });
});
