// @vitest-environment jsdom
//
// The Ada Pro pane over the null SDK: the offering's packages with a Buy
// each, the SDK's CustomerInfo verbatim after a purchase or a refresh, the
// Test Store sentence for a test_ key, and the not-configured state.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { PurchasesProvider } from "@/contexts/purchases.context";
import { setSetting } from "@/lib/settings/store";
import {
  NOT_CONFIGURED_LINE,
  TEST_STORE_LINE,
  customerInfoFixture,
  nullPurchases,
  offeringsFixture,
  packageFixture,
  setPurchasesSdk,
} from "@/lib/purchases/client";
import { resetAppUserIdForTests } from "@/lib/purchases/app-user-id";
import { AdaPro, CANCELLED_LINE, periodWords } from "./AdaPro";

const mount = (apiKey: string) =>
  render(
    <MemoryRouter>
      <PurchasesProvider apiKey={apiKey} refreshOnFocus={false}>
        <AdaPro />
      </PurchasesProvider>
    </MemoryRouter>
  );

beforeEach(async () => {
  window.localStorage.clear();
  resetAppUserIdForTests();
  await setSetting("purchases.appUserId", "");
  await setSetting("purchases.lastVerdict", { verdict: "unknown", at: "" });
});

afterEach(() => {
  cleanup();
  setPurchasesSdk(null);
});

describe("AdaPro", () => {
  it("leads with what Pro unlocks and lists the offering's packages with a Buy each", async () => {
    const monthly = packageFixture({ identifier: "$rc_monthly", title: "Ada Pro", formattedPrice: "$9.00" });
    setPurchasesSdk(nullPurchases({ offerings: offeringsFixture([monthly]) }));
    mount("test_abc");
    expect(screen.getByTestId("pro-settings").id).toBe("pro");
    // The header says what Pro unlocks; the body says what it does not, once.
    expect(screen.getByText("Ada Pro unlocks Prepare fab order.")).toBeTruthy();
    expect(screen.getByTestId("pro-unlocks").textContent).toContain("Nothing else is gated on Ada Pro.");
    await waitFor(() => expect(screen.getAllByTestId("pro-package")).toHaveLength(1));
    const row = screen.getByTestId("pro-package");
    expect(row.getAttribute("data-product")).toBe("ada_pro_monthly");
    expect(row.textContent).toContain("Ada Pro");
    expect(row.textContent).toContain("$9.00");
    // The fixture's P1M is the SDK's ISO 8601 period; the row says it in words.
    expect(row.textContent).toContain("$9.00 every month");
    expect(screen.getByTestId("pro-buy").textContent).toBe("Buy");
    expect(screen.getByTestId("pro-verdict").getAttribute("data-status")).toBe("free");
  });

  it("Buy purchases that package and then shows the SDK's CustomerInfo verbatim", async () => {
    const monthly = packageFixture();
    const fake = nullPurchases({ offerings: offeringsFixture([monthly]) });
    setPurchasesSdk(fake);
    mount("test_abc");
    await waitFor(() => expect(screen.getByTestId("pro-buy")).toBeTruthy());
    // Before any purchase the record still shows: the free customer's own.
    expect(screen.getByTestId("pro-entitlement-active").textContent).toBe("active: no");
    fireEvent.click(screen.getByTestId("pro-buy"));
    await waitFor(() => expect(screen.getByTestId("pro-result").textContent).toContain("txn_test_0001"));
    expect(fake.purchased).toEqual([monthly]);
    expect(screen.getByTestId("pro-verdict").getAttribute("data-status")).toBe("entitled");
    expect(screen.getByTestId("pro-entitlement-active").textContent).toBe("active: yes");
    expect(screen.getByTestId("pro-original-app-user-id").textContent).toBe(fake.configured?.appUserId);
    expect(screen.getByTestId("pro-app-user-id").textContent).toBe(fake.configured?.appUserId);
    expect(screen.getByTestId("pro-latest-purchase").textContent).toBe("2026-09-24T10:00:00.000Z");
    expect(screen.getByTestId("pro-expiration").textContent).toBe("2026-10-24T10:00:00.000Z");
    const manage = screen.getByTestId("pro-management-url").querySelector("a");
    expect(manage?.getAttribute("href")).toBe("https://api.revenuecat.com/rcbilling/v1/customerportal/example");
    expect(screen.getByTestId("pro-transaction").textContent).toBe("txn_test_0001");
    // Bought: the button no longer offers a second subscription.
    expect(screen.getByTestId("pro-buy").textContent).toBe("Active");
    expect(screen.getByTestId("pro-buy").hasAttribute("disabled")).toBe(true);
  });

  it("a closed checkout is reported as cancelled, with nothing charged", async () => {
    const cancelled = Object.assign(new Error("User cancelled"), { errorCode: 1 });
    setPurchasesSdk(nullPurchases({ offerings: offeringsFixture([packageFixture()]), purchase: cancelled }));
    mount("test_abc");
    await waitFor(() => expect(screen.getByTestId("pro-buy")).toBeTruthy());
    fireEvent.click(screen.getByTestId("pro-buy"));
    await waitFor(() => expect(screen.getByTestId("pro-result").textContent).toBe(CANCELLED_LINE));
    expect(screen.getByTestId("pro-verdict").getAttribute("data-status")).toBe("free");
  });

  it("Refresh re-reads CustomerInfo from the SDK", async () => {
    const fake = nullPurchases({ offerings: offeringsFixture([packageFixture()]) });
    setPurchasesSdk(fake);
    mount("test_abc");
    await waitFor(() => expect(screen.getByTestId("pro-verdict").getAttribute("data-status")).toBe("free"));
    fake.setCustomerInfo(customerInfoFixture({ pro: true, originalAppUserId: "someone-else" }));
    fireEvent.click(screen.getByTestId("pro-refresh"));
    await waitFor(() => expect(screen.getByTestId("pro-verdict").getAttribute("data-status")).toBe("entitled"));
    expect(screen.getByTestId("pro-original-app-user-id").textContent).toBe("someone-else");
  });

  it("says Test Store for a test_ key", async () => {
    setPurchasesSdk(nullPurchases());
    mount("test_abc");
    await waitFor(() => expect(screen.getByTestId("pro-sandbox").textContent).toBe(TEST_STORE_LINE));
    expect(screen.getByTestId("pro-sandbox").textContent).toBe("Test Store: simulated purchase. No money moves.");
  });

  it("says Test Store when the SDK itself says sandbox", async () => {
    setPurchasesSdk(nullPurchases({ sandbox: true }));
    mount("rcb_sb_abc");
    await waitFor(() => expect(screen.getByTestId("pro-sandbox")).toBeTruthy());
  });

  it("not configured: the one sentence, no packages, no buy", async () => {
    const fake = nullPurchases({ offerings: offeringsFixture([packageFixture()]) });
    setPurchasesSdk(fake);
    mount("");
    await waitFor(() => expect(screen.getByTestId("pro-not-configured").textContent).toBe(NOT_CONFIGURED_LINE));
    expect(screen.queryByTestId("pro-buy")).toBeNull();
    expect(screen.queryByTestId("pro-sandbox")).toBeNull();
    expect(fake.calls).toEqual([]);
  });

  it("a failed check is shown as not known, with the reason, and Buy stays offered", async () => {
    setPurchasesSdk(
      nullPurchases({
        offerings: offeringsFixture([packageFixture()]),
        customerInfoError: new Error("Network request failed"),
      })
    );
    mount("test_abc");
    await waitFor(() => expect(screen.getByTestId("pro-verdict").getAttribute("data-status")).toBe("unknown"));
    expect(screen.getByTestId("pro-verdict").textContent).toContain("Network request failed");
    expect(screen.getByTestId("pro-buy").textContent).toBe("Buy");
  });
});

describe("periodWords", () => {
  it("says the SDK's ISO 8601 period in words and leaves an unknown one as is", () => {
    expect(periodWords("P1W")).toBe("week");
    expect(periodWords("P1M")).toBe("month");
    expect(periodWords("P3M")).toBe("three months");
    expect(periodWords("P1Y")).toBe("year");
    // Not in the table: shown as the SDK gave it rather than guessed at.
    expect(periodWords("P2W")).toBe("P2W");
  });
});
