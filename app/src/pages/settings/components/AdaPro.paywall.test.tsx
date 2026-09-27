// @vitest-environment jsdom
//
// The pane's RevenueCat paywall: shown by itself when the locked order step
// sent the person here, with that board's name and part count as the
// paywall's custom variables; the package buttons standing in, and saying
// so, when the offering has no dashboard paywall; and "Manage subscription"
// opening RevenueCat's own management URL once Pro is active.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

const opened: string[] = [];
vi.mock("@tauri-apps/plugin-opener", () => ({
  openUrl: (url: string) => {
    opened.push(url);
    return Promise.resolve();
  },
}));

import { PurchasesProvider } from "@/contexts/purchases.context";
import { setSetting } from "@/lib/settings/store";
import {
  customerInfoFixture,
  nullPurchases,
  offeringsFixture,
  packageFixture,
  setPurchasesSdk,
} from "@/lib/purchases/client";
import { PAYWALL_CONTEXT_KEY, requestPaywallContext } from "@/lib/purchases/pane";
import { resetAppUserIdForTests } from "@/lib/purchases/app-user-id";
import { AdaPro, NO_PAYWALL_LINE, UNLOCKED_LINE } from "./AdaPro";

const mount = () =>
  render(
    <MemoryRouter>
      <PurchasesProvider apiKey="test_abc" refreshOnFocus={false}>
        <AdaPro />
      </PurchasesProvider>
    </MemoryRouter>
  );

beforeEach(async () => {
  window.localStorage.clear();
  opened.length = 0;
  resetAppUserIdForTests();
  await setSetting("purchases.appUserId", "");
  await setSetting("purchases.lastVerdict", { verdict: "unknown", at: "" });
});

afterEach(() => {
  cleanup();
  setPurchasesSdk(null);
});

describe("AdaPro paywall", () => {
  it("opens RevenueCat's paywall at once when the order step sent the person, naming that board", async () => {
    const fake = nullPurchases({ offerings: offeringsFixture([packageFixture()], "default", true) });
    setPurchasesSdk(fake);
    requestPaywallContext({ board: "a 3.3V LDO board powered by USB-C", parts: 7 });
    mount();
    await waitFor(() => expect(fake.calls).toContain("presentPaywall"));
    expect(fake.paywalls).toEqual([
      { board_name: "a 3.3V LDO board powered by USB-C", part_count: "7" },
    ]);
    await waitFor(() => expect(screen.getByTestId("pro-result").textContent).toBe(UNLOCKED_LINE));
    expect(screen.getByTestId("pro-verdict").getAttribute("data-status")).toBe("entitled");
    // Read once, then gone: a later Settings visit is not about this board.
    expect(window.localStorage.getItem(PAYWALL_CONTEXT_KEY)).toBeNull();
  });

  it("waits for a click, with the dashboard's own copy, when the person came from Settings", async () => {
    const fake = nullPurchases({ offerings: offeringsFixture([packageFixture()], "default", true) });
    setPurchasesSdk(fake);
    mount();
    await waitFor(() => expect(screen.getByTestId("pro-show-paywall")).toBeTruthy());
    expect(fake.calls).not.toContain("presentPaywall");
    expect(screen.queryByTestId("pro-packages")).toBeNull();
    fireEvent.click(screen.getByTestId("pro-show-paywall"));
    await waitFor(() => expect(fake.paywalls).toEqual([{}]));
  });

  it("lists the plans directly, and says why, when the offering has no dashboard paywall", async () => {
    const fake = nullPurchases({ offerings: offeringsFixture([packageFixture()], "default", false) });
    setPurchasesSdk(fake);
    requestPaywallContext({ board: "a blinker", parts: 5 });
    mount();
    await waitFor(() => expect(screen.getByTestId("pro-no-paywall").textContent).toBe(NO_PAYWALL_LINE));
    expect(screen.getAllByTestId("pro-package")).toHaveLength(1);
    expect(screen.queryByTestId("pro-paywall")).toBeNull();
    expect(fake.calls).not.toContain("presentPaywall");
  });

  it("offers Manage subscription at RevenueCat's own URL once Pro is active", async () => {
    const url = "https://api.revenuecat.com/rcbilling/v1/customerportal/abc";
    setPurchasesSdk(
      nullPurchases({
        offerings: offeringsFixture([packageFixture()], "default", true),
        customerInfo: customerInfoFixture({ pro: true, managementURL: url }),
      })
    );
    mount();
    await waitFor(() => expect(screen.getByTestId("pro-manage")).toBeTruthy());
    expect(screen.queryByTestId("pro-paywall")).toBeNull();
    fireEvent.click(screen.getByTestId("pro-manage"));
    expect(opened).toEqual([url]);
  });
});
