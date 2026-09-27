// The purchases seam, with the real SDK never loaded: key formats the SDK
// itself uses, the verdict read off a CustomerInfo, and the one-configure
// rule under the double mount StrictMode gives every provider.

import { afterEach, describe, expect, it } from "vitest";
import {
  PRO_ENTITLEMENT,
  PRO_PRODUCT,
  buy,
  configurePurchases,
  configuredPublicKey,
  customerInfoFixture,
  fetchOfferings,
  isTestStoreKey,
  isUserCancelled,
  isWebBillingSandboxKey,
  keyKind,
  nullPurchases,
  offeringsFixture,
  packageFixture,
  packagesOf,
  proEntitlement,
  refreshCustomerInfo,
  setPurchasesSdk,
  verdictFrom,
} from "./client";

afterEach(() => {
  setPurchasesSdk(null);
});

describe("key formats", () => {
  it("recognises a Test Store key by the SDK's own regex", () => {
    expect(isTestStoreKey("test_AbC.d-e_f")).toBe(true);
    expect(isTestStoreKey("test_")).toBe(false);
    expect(isTestStoreKey("test_with space")).toBe(false);
    expect(isTestStoreKey("rcb_sb_abc")).toBe(false);
    expect(isTestStoreKey(undefined)).toBe(false);
    expect(isTestStoreKey(null)).toBe(false);
  });

  it("tells the three public key kinds apart", () => {
    expect(isWebBillingSandboxKey("rcb_sb_abc")).toBe(true);
    expect(isWebBillingSandboxKey("rcb_abc")).toBe(false);
    expect(keyKind("test_abc")).toBe("test_store");
    expect(keyKind("rcb_sb_abc")).toBe("web_billing_sandbox");
    expect(keyKind("rcb_abc")).toBe("web_billing");
    expect(keyKind("sk_live_abc")).toBe("unknown");
    expect(keyKind("")).toBe("unknown");
  });

  it("reads no key at all from the test environment", () => {
    expect(configuredPublicKey()).toBe("");
  });
});

describe("verdictFrom", () => {
  it("is entitled exactly when pro is among the active entitlements", () => {
    expect(verdictFrom(customerInfoFixture({ pro: true }))).toBe("entitled");
    expect(verdictFrom(customerInfoFixture({ pro: false }))).toBe("free");
  });

  it("an inactive pro record is still a record, and still free", () => {
    const info = customerInfoFixture({ pro: false });
    expect(proEntitlement(info)?.identifier).toBe(PRO_ENTITLEMENT);
    expect(proEntitlement(info)?.isActive).toBe(false);
    expect(verdictFrom(info)).toBe("free");
  });

  it("lists the current offering's packages, and none without an offering", () => {
    const pkg = packageFixture();
    expect(packagesOf(offeringsFixture([pkg]))).toEqual([pkg]);
    expect(packagesOf(undefined)).toEqual([]);
    expect(packagesOf(offeringsFixture([]))).toEqual([]);
    expect(pkg.webBillingProduct.identifier).toBe(PRO_PRODUCT);
  });

  it("knows the SDK's user-cancelled code and nothing else", () => {
    expect(isUserCancelled({ errorCode: 1 })).toBe(true);
    expect(isUserCancelled({ errorCode: 10 })).toBe(false);
    expect(isUserCancelled(new Error("x"))).toBe(false);
    expect(isUserCancelled(null)).toBe(false);
  });
});

describe("configurePurchases", () => {
  it("configures once, and a second call under StrictMode does not configure again", async () => {
    const fake = nullPurchases();
    setPurchasesSdk(fake);
    const config = { apiKey: "test_abc", appUserId: "8f1c2b4e-3d5a-4f6b-9c7d-0e1f2a3b4c5d" };
    await Promise.all([configurePurchases(config), configurePurchases(config)]);
    await configurePurchases(config);
    expect(fake.calls.filter((c) => c === "configure")).toEqual(["configure"]);
    expect(fake.configured).toEqual(config);
  });

  it("a refused configure is reported and does not pin the failure", async () => {
    setPurchasesSdk(nullPurchases({ configureError: new Error("Invalid API key") }));
    await expect(
      configurePurchases({ apiKey: "nope", appUserId: "8f1c2b4e-3d5a-4f6b-9c7d-0e1f2a3b4c5d" })
    ).rejects.toThrow("Invalid API key");
    const fake = nullPurchases();
    setPurchasesSdk(fake);
    await configurePurchases({ apiKey: "test_abc", appUserId: "8f1c2b4e-3d5a-4f6b-9c7d-0e1f2a3b4c5d" });
    expect(fake.configured?.apiKey).toBe("test_abc");
  });

  it("the operations go through the seam", async () => {
    const pkg = packageFixture();
    const fake = nullPurchases({ offerings: offeringsFixture([pkg]) });
    setPurchasesSdk(fake);
    await configurePurchases({ apiKey: "test_abc", appUserId: "8f1c2b4e-3d5a-4f6b-9c7d-0e1f2a3b4c5d" });
    expect(verdictFrom(await refreshCustomerInfo())).toBe("free");
    expect(packagesOf(await fetchOfferings())).toEqual([pkg]);
    const result = await buy(pkg);
    expect(fake.purchased).toEqual([pkg]);
    expect(result.storeTransaction.productIdentifier).toBe(PRO_PRODUCT);
    expect(verdictFrom(result.customerInfo)).toBe("entitled");
    expect(verdictFrom(await refreshCustomerInfo())).toBe("entitled");
  });
});

describe("paywall helpers", () => {
  it("names the paywall's custom variables the way the dashboard paywall uses them", async () => {
    const { paywallVariables } = await import("./client");
    expect(paywallVariables({ board: "a blinker", parts: 5 })).toEqual({
      board_name: "a blinker",
      part_count: "5",
    });
    expect(paywallVariables(null)).toEqual({});
  });

  it("offers only an https management URL", async () => {
    const { managementUrl, customerInfoFixture } = await import("./client");
    expect(managementUrl(customerInfoFixture({ pro: true, managementURL: "https://x.test/m" }))).toBe(
      "https://x.test/m"
    );
    expect(managementUrl(customerInfoFixture({ pro: true, managementURL: "javascript:alert(1)" }))).toBeNull();
    expect(managementUrl(customerInfoFixture({ pro: true, managementURL: null }))).toBeNull();
  });
});
