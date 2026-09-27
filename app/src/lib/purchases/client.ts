// The one place Ada talks to RevenueCat.
//
// A thin module over `@revenuecat/purchases-js` 1.64.0 (MIT), the SDK
// RevenueCat ships for Web Billing and its Test Store. What is used of it is
// exactly the surface its README points at
// (`node_modules/@revenuecat/purchases-js/README.md`: get the sandbox or
// production API key, create products, an offering with packages, and the
// entitlements linked to those products) and its public types
// (`node_modules/@revenuecat/purchases-js/dist/Purchases.es.d.ts`):
// `Purchases.configure({ apiKey, appUserId })`, `Purchases.isConfigured()`,
// `Purchases.getSharedInstance()`, `getOfferings()`, `purchase({ rcPackage })`,
// `getCustomerInfo()`, `isSandbox()`, `getAppUserId()` and
// `CustomerInfo.entitlements.active`.
//
// The seam is this repository's `Transport` convention (`googleapps/
// transport.py`, `billing/transport.py`: one interface the real thing
// satisfies, a recorded fake for tests, no socket in the suite). Here it is
// `PurchasesSdk`. The real SDK is loaded by dynamic import behind it, the way
// `src/lib/settings/store.ts` loads the store plugin behind `isTauriRuntime()`,
// so Vitest never resolves the package at all. `nullPurchases()` is the fake.
//
// The key formats are the SDK's own (`src/helpers/api-key-helper.ts` in
// RevenueCat/purchases-js, read 2026-09-24, and the same regex in the built
// `dist/Purchases.es.js`): a Test Store key matches /^test_[a-zA-Z0-9_.-]+$/,
// a Web Billing sandbox key starts with `rcb_sb_`, a production key with
// `rcb_`. Nothing here logs a key, and nothing here ever reads one from disk:
// the only key is `VITE_REVENUECAT_PUBLIC_KEY`, inlined by Vite at build time
// from `app/.env.local` (git-ignored). A public key is what the SDK is
// designed to carry; the secret key lives on the service and never here.

import type {
  CustomerInfo,
  EntitlementInfo,
  Offering,
  Offerings,
  Package,
  PaywallPurchaseResult,
  PurchaseResult,
  StoreTransaction,
} from "@revenuecat/purchases-js";

/**
 * The board the person was about to order when the gate stopped them, carried
 * from the strip to the Settings pane (`pane.ts`) so the paywall can name it.
 */
export interface PaywallContext {
  board?: string;
  parts?: number;
}

// ----------------------------------------------------------- the contract

/** The entitlement the order step is gated on. Shared with the service. */
export const PRO_ENTITLEMENT = "pro";
/** The monthly subscription product that grants it. */
export const PRO_PRODUCT = "ada_pro_monthly";
/** The offering the pane lists. */
export const PRO_OFFERING = "default";

/** What the strip's order button reads while the verdict is `free`. */
export const PRO_BUTTON_LABEL = "Prepare fab order · Ada Pro";
/** The one line the pane leads with. */
export const PRO_UNLOCKS_LINE = "Ada Pro unlocks Prepare fab order.";
/** Shown whenever the key is a Test Store key or the SDK says sandbox. */
export const TEST_STORE_LINE = "Test Store: simulated purchase. No money moves.";
/** The pane's whole content when the build carries no key. */
export const NOT_CONFIGURED_LINE =
  "Purchases are not configured on this build: VITE_REVENUECAT_PUBLIC_KEY is not set.";
/** The `usePurchases` fallback outside a provider. */
export const NOT_MOUNTED_LINE = "Purchases are not mounted in this window.";

/** The three-state verdict. `unknown` never locks anything. */
export type Verdict = "entitled" | "free" | "unknown";

/**
 * `ErrorCode.UserCancelledError` in `Purchases.es.d.ts` (the enum's value is
 * 1). Compared by number rather than by importing the enum, because a runtime
 * import of the SDK is exactly what this module keeps out of the test suite.
 */
export const USER_CANCELLED_CODE = 1;

// ---------------------------------------------------------------- the keys

const TEST_STORE_KEY = /^test_[a-zA-Z0-9_.-]+$/;
const WEB_BILLING_KEY = /^rcb_[a-zA-Z0-9_.-]+$/;

/** A RevenueCat Test Store public key (`isSimulatedStoreApiKey` upstream). */
export function isTestStoreKey(key: string | null | undefined): boolean {
  return typeof key === "string" && TEST_STORE_KEY.test(key);
}

/** A Web Billing sandbox key (`isWebBillingSandboxApiKey` upstream). */
export function isWebBillingSandboxKey(key: string | null | undefined): boolean {
  return typeof key === "string" && key.startsWith("rcb_sb_");
}

export type KeyKind = "test_store" | "web_billing_sandbox" | "web_billing" | "unknown";

/** Which kind of public key this is, for the pane's one-word label. */
export function keyKind(key: string | null | undefined): KeyKind {
  if (isTestStoreKey(key)) return "test_store";
  if (isWebBillingSandboxKey(key)) return "web_billing_sandbox";
  if (typeof key === "string" && WEB_BILLING_KEY.test(key)) return "web_billing";
  return "unknown";
}

/**
 * The public key this build was made with, or "" when it carries none.
 * `import.meta.env` is Vite's build-time substitution; in Vitest it is the
 * test process's environment, which sets nothing.
 */
export function configuredPublicKey(): string {
  const env = (import.meta as { env?: Record<string, unknown> }).env;
  const value = env?.VITE_REVENUECAT_PUBLIC_KEY;
  return typeof value === "string" ? value.trim() : "";
}

// ---------------------------------------------------------------- the seam

/** What a purchases backend has to provide: the subset of `Purchases` used. */
export interface PurchasesSdk {
  isConfigured(): boolean;
  configure(config: { apiKey: string; appUserId: string }): void;
  getOfferings(): Promise<Offerings>;
  purchase(params: { rcPackage: Package }): Promise<PurchaseResult>;
  getCustomerInfo(): Promise<CustomerInfo>;
  isSandbox(): boolean;
  getAppUserId(): string;
  /**
   * `Purchases.presentPaywall`: render the paywall designed in the RevenueCat
   * dashboard for `offering` into `htmlTarget` and run its checkout.
   * `variables` are plain strings here; the adapter wraps each in the SDK's
   * `CustomVariableValue.string`, so this seam (and the fake) never needs
   * RevenueCat's UI package.
   */
  presentPaywall(params: PaywallParams): Promise<PaywallPurchaseResult>;
}

/** What Ada passes to a paywall. */
export interface PaywallParams {
  offering: Offering;
  htmlTarget: HTMLElement;
  variables?: Record<string, string>;
}

/**
 * The real SDK, behind the interface. Every instance method goes through
 * `getSharedInstance()`, which throws `UninitializedPurchasesError` before
 * `configure` has run; callers reach it only after `configurePurchases`.
 */
async function realSdk(): Promise<PurchasesSdk> {
  const { Purchases } = await import("@revenuecat/purchases-js");
  return {
    isConfigured: () => Purchases.isConfigured(),
    configure: (config) => {
      Purchases.configure(config);
    },
    getOfferings: () => Purchases.getSharedInstance().getOfferings(),
    purchase: (params) => Purchases.getSharedInstance().purchase(params),
    getCustomerInfo: () => Purchases.getSharedInstance().getCustomerInfo(),
    isSandbox: () => Purchases.getSharedInstance().isSandbox(),
    getAppUserId: () => Purchases.getSharedInstance().getAppUserId(),
    // RevenueCat's own sample does exactly this
    // (RevenueCat/purchases-js afae8c7,
    // examples/webbilling-demo/src/pages/rc_paywall/index.tsx): the offering,
    // a target element, the custom variables, then the `PurchaseResult`.
    presentPaywall: async ({ offering, htmlTarget, variables }) => {
      const { CustomVariableValue } = await import("@revenuecat/purchases-js");
      const customVariables = Object.fromEntries(
        Object.entries(variables ?? {}).map(([name, value]) => [
          name,
          CustomVariableValue.string(value),
        ])
      );
      return Purchases.getSharedInstance().presentPaywall({
        offering,
        htmlTarget,
        customVariables,
      });
    },
  };
}

let sdkPromise: Promise<PurchasesSdk> | null = null;

/**
 * Replace the backend. Tests pass a `nullPurchases()`; `null` puts the real
 * SDK back. Also forgets any configure in flight, so one test's SDK cannot
 * leak into the next.
 */
export function setPurchasesSdk(sdk: PurchasesSdk | null): void {
  sdkPromise = sdk ? Promise.resolve(sdk) : null;
  configuring = null;
}

function sdk(): Promise<PurchasesSdk> {
  if (!sdkPromise) sdkPromise = realSdk();
  return sdkPromise;
}

// ----------------------------------------------------------- the operations

let configuring: Promise<void> | null = null;

/**
 * Configure the SDK once. `isConfigured()` is the guard the SDK itself
 * offers ("You should only call this once"), and the shared promise covers
 * the gap the guard cannot: React StrictMode mounts the provider twice, and
 * two calls that both pass `isConfigured()` before either has configured
 * would configure twice. Rejects with the SDK's own `PurchasesError` for a
 * malformed key or user id; the caller reports it, in words.
 */
export function configurePurchases(config: { apiKey: string; appUserId: string }): Promise<void> {
  if (!configuring) {
    configuring = (async () => {
      const api = await sdk();
      if (api.isConfigured()) return;
      api.configure(config);
    })().catch((error) => {
      // A failed configure must not pin the failure forever: the next call
      // tries again (a different key, a minted user id).
      configuring = null;
      throw error;
    });
  }
  return configuring;
}

/** True once `configurePurchases` has succeeded in this process. */
export async function purchasesConfigured(): Promise<boolean> {
  return (await sdk()).isConfigured();
}

/** The dashboard's offerings for this user. Requires a configured SDK. */
export async function fetchOfferings(): Promise<Offerings> {
  return (await sdk()).getOfferings();
}

/**
 * Buy one package. The SDK renders its own checkout (a modal at the root of
 * the page when no `htmlTarget` is given); with a Test Store key that modal
 * is a simulated purchase and no money moves. Rejects with a `PurchasesError`
 * whose `errorCode` is `USER_CANCELLED_CODE` when the person closed it.
 */
export async function buy(pkg: Package): Promise<PurchaseResult> {
  return (await sdk()).purchase({ rcPackage: pkg });
}

/**
 * Show the offering's dashboard paywall inside `htmlTarget` and buy from it.
 * Resolves with the `PurchaseResult` (plus the package chosen); rejects with
 * the SDK's user-cancelled error when the person backs out.
 */
export async function showPaywall(params: PaywallParams): Promise<PaywallPurchaseResult> {
  return (await sdk()).presentPaywall(params);
}

/** The offering the pane sells from: the current one, else `default`, else none. */
export function proOffering(offerings: Offerings | undefined): Offering | null {
  if (!offerings) return null;
  return offerings.current ?? offerings.all?.[PRO_OFFERING] ?? null;
}

/**
 * The paywall's custom variables, named as the dashboard paywall uses them
 * (`{{ custom.board_name }}`, `{{ custom.part_count }}`): the board the person
 * was about to order when they met the gate, so the paywall talks about that
 * board rather than about a product in general. Absent context sends none and
 * the dashboard's defaults show.
 */
export function paywallVariables(context: PaywallContext | null): Record<string, string> {
  if (!context) return {};
  const out: Record<string, string> = {};
  if (context.board) out.board_name = context.board;
  if (typeof context.parts === "number") out.part_count = String(context.parts);
  return out;
}

/** Where the customer manages the subscription RevenueCat holds, when it says. */
export function managementUrl(info: CustomerInfo | undefined): string | null {
  const url = info?.managementURL ?? null;
  return url && /^https:\/\//.test(url) ? url : null;
}

/** The latest `CustomerInfo` from RevenueCat. Requires a configured SDK. */
export async function refreshCustomerInfo(): Promise<CustomerInfo> {
  return (await sdk()).getCustomerInfo();
}

/** The SDK's own answer to "is this a sandbox key". Requires a configured SDK. */
export async function sdkIsSandbox(): Promise<boolean> {
  return (await sdk()).isSandbox();
}

/**
 * The verdict a `CustomerInfo` carries. `entitlements.active` is the SDK's
 * map of currently active entitlements by identifier, so presence is the
 * answer; nothing here computes an expiry of its own.
 */
export function verdictFrom(info: CustomerInfo): Exclude<Verdict, "unknown"> {
  const active = info?.entitlements?.active ?? {};
  return active[PRO_ENTITLEMENT] ? "entitled" : "free";
}

/** The `pro` entitlement's record, active or not, when the customer has ever had one. */
export function proEntitlement(info: CustomerInfo): EntitlementInfo | null {
  return info?.entitlements?.all?.[PRO_ENTITLEMENT] ?? null;
}

/** The packages the pane lists: the current offering's, else `default`'s, else none. */
export function packagesOf(offerings: Offerings | undefined): Package[] {
  return proOffering(offerings)?.availablePackages ?? [];
}

/** True for the SDK's user-cancelled error; a closed modal is not a failure. */
export function isUserCancelled(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    (error as { errorCode?: unknown }).errorCode === USER_CANCELLED_CODE
  );
}

// --------------------------------------------------------------- the fake

export interface NullPurchasesOptions {
  offerings?: Offerings;
  customerInfo?: CustomerInfo;
  /** What `purchase` answers; an Error is thrown instead. */
  purchase?: PurchaseResult | Error;
  sandbox?: boolean;
  /** Thrown by `getCustomerInfo` when set. */
  customerInfoError?: Error;
  /** Thrown by `getOfferings` when set. */
  offeringsError?: Error;
  /** Thrown by `configure` when set. */
  configureError?: Error;
  /** What `presentPaywall` answers; an Error is thrown instead. */
  paywall?: PaywallPurchaseResult | Error;
}

/** A recording fake: every call is remembered, nothing reaches a network. */
export interface NullPurchases extends PurchasesSdk {
  readonly calls: string[];
  readonly purchased: Package[];
  /** Every `presentPaywall` call's variables, in order. */
  readonly paywalls: Record<string, string>[];
  configured: { apiKey: string; appUserId: string } | null;
  /** Change what the next `getCustomerInfo` answers. */
  setCustomerInfo(info: CustomerInfo): void;
}

export function nullPurchases(options: NullPurchasesOptions = {}): NullPurchases {
  const calls: string[] = [];
  const purchased: Package[] = [];
  const paywalls: Record<string, string>[] = [];
  let customerInfo = options.customerInfo ?? customerInfoFixture({ pro: false });
  const fake: NullPurchases = {
    calls,
    purchased,
    paywalls,
    configured: null,
    isConfigured() {
      calls.push("isConfigured");
      return fake.configured !== null;
    },
    configure(config) {
      calls.push("configure");
      if (options.configureError) throw options.configureError;
      fake.configured = { ...config };
    },
    async getOfferings() {
      calls.push("getOfferings");
      if (options.offeringsError) throw options.offeringsError;
      return options.offerings ?? offeringsFixture([]);
    },
    async purchase({ rcPackage }) {
      calls.push("purchase");
      purchased.push(rcPackage);
      const answer = options.purchase;
      if (answer instanceof Error) throw answer;
      if (answer) {
        customerInfo = answer.customerInfo;
        return answer;
      }
      customerInfo = customerInfoFixture({ pro: true, appUserId: fake.configured?.appUserId });
      return purchaseResultFixture(customerInfo, rcPackage);
    },
    async getCustomerInfo() {
      calls.push("getCustomerInfo");
      if (options.customerInfoError) throw options.customerInfoError;
      return customerInfo;
    },
    isSandbox() {
      calls.push("isSandbox");
      return options.sandbox ?? false;
    },
    getAppUserId() {
      return fake.configured?.appUserId ?? "";
    },
    async presentPaywall({ offering, variables }) {
      calls.push("presentPaywall");
      paywalls.push({ ...(variables ?? {}) });
      const answer = options.paywall;
      if (answer instanceof Error) throw answer;
      if (answer) {
        customerInfo = answer.customerInfo;
        return answer;
      }
      const pkg = offering.availablePackages[0] ?? packageFixture();
      customerInfo = customerInfoFixture({ pro: true, appUserId: fake.configured?.appUserId });
      return { ...purchaseResultFixture(customerInfo, pkg), selectedPackage: pkg };
    },
    setCustomerInfo(info) {
      customerInfo = info;
    },
  };
  return fake;
}

// ------------------------------------------------------------ the fixtures
//
// Shapes the tests share. They follow `Purchases.es.d.ts` field for field
// where a test reads the field and are cast where the SDK's type carries more
// than any test looks at (a `Product` has fourteen fields; the pane reads two).

export interface CustomerInfoFixtureOptions {
  pro: boolean;
  appUserId?: string;
  originalAppUserId?: string;
  latestPurchaseDate?: Date;
  expirationDate?: Date | null;
  managementURL?: string | null;
}

export function customerInfoFixture(options: CustomerInfoFixtureOptions): CustomerInfo {
  const appUserId = options.appUserId ?? "00000000-0000-4000-8000-000000000000";
  const latest = options.latestPurchaseDate ?? new Date("2026-09-24T10:00:00Z");
  const expires =
    options.expirationDate === undefined ? new Date("2026-10-24T10:00:00Z") : options.expirationDate;
  const entitlement: EntitlementInfo = {
    identifier: PRO_ENTITLEMENT,
    isActive: options.pro,
    willRenew: options.pro,
    store: "test_store",
    latestPurchaseDate: latest,
    originalPurchaseDate: latest,
    expirationDate: expires,
    productIdentifier: PRO_PRODUCT,
    productPlanIdentifier: null,
    unsubscribeDetectedAt: null,
    billingIssueDetectedAt: null,
    isSandbox: true,
    periodType: "normal",
    ownershipType: "PURCHASED",
  };
  const info = {
    entitlements: {
      all: { [PRO_ENTITLEMENT]: entitlement },
      active: options.pro ? { [PRO_ENTITLEMENT]: entitlement } : {},
    },
    allExpirationDatesByProduct: { [PRO_PRODUCT]: expires },
    allPurchaseDatesByProduct: { [PRO_PRODUCT]: latest },
    activeSubscriptions: new Set(options.pro ? [PRO_PRODUCT] : []),
    managementURL:
      options.managementURL === undefined
        ? "https://api.revenuecat.com/rcbilling/v1/customerportal/example"
        : options.managementURL,
    requestDate: new Date("2026-09-24T10:00:05Z"),
    firstSeenDate: new Date("2026-09-24T09:00:00Z"),
    originalPurchaseDate: null,
    originalAppUserId: options.originalAppUserId ?? appUserId,
    nonSubscriptionTransactions: [],
    subscriptionsByProductIdentifier: {},
  };
  return info as unknown as CustomerInfo;
}

export interface PackageFixtureOptions {
  identifier?: string;
  productIdentifier?: string;
  title?: string;
  formattedPrice?: string;
}

export function packageFixture(options: PackageFixtureOptions = {}): Package {
  const product = {
    identifier: options.productIdentifier ?? PRO_PRODUCT,
    displayName: options.title ?? "Ada Pro",
    title: options.title ?? "Ada Pro",
    description: "Prepare fab order.",
    productType: "subscription",
    normalPeriodDuration: "P1M",
    price: {
      amount: 900,
      amountMicros: 9_000_000,
      currency: "USD",
      formattedPrice: options.formattedPrice ?? "$9.00",
    },
  };
  return {
    identifier: options.identifier ?? "$rc_monthly",
    rcBillingProduct: product,
    webBillingProduct: product,
    webCheckoutURL: null,
    packageType: "$rc_monthly",
  } as unknown as Package;
}

export function offeringsFixture(
  packages: Package[],
  identifier = PRO_OFFERING,
  hasPaywall = false
): Offerings {
  const offering = {
    identifier,
    serverDescription: "",
    metadata: null,
    webCheckoutURL: null,
    packagesById: Object.fromEntries(packages.map((p) => [p.identifier, p])),
    availablePackages: packages,
    lifetime: null,
    annual: null,
    sixMonth: null,
    threeMonth: null,
    twoMonth: null,
    monthly: packages[0] ?? null,
    weekly: null,
    hasPaywall,
  };
  return { all: { [identifier]: offering }, current: offering } as unknown as Offerings;
}

export function purchaseResultFixture(
  customerInfo: CustomerInfo,
  pkg: Package,
  storeTransactionId = "txn_test_0001"
): PurchaseResult {
  const storeTransaction: StoreTransaction = {
    storeTransactionId,
    productIdentifier: pkg.webBillingProduct.identifier,
    purchaseDate: new Date("2026-09-24T10:00:00Z"),
  };
  return {
    customerInfo,
    redemptionInfo: null,
    operationSessionId: "op_test_0001",
    storeTransaction,
  } as PurchaseResult;
}
