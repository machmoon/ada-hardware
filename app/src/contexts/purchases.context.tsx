import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type {
  CustomerInfo,
  Offerings,
  Package,
  PurchaseResult,
  StoreTransaction,
} from "@revenuecat/purchases-js";
import {
  NOT_CONFIGURED_LINE,
  NOT_MOUNTED_LINE,
  buy as buyPackage,
  configurePurchases,
  configuredPublicKey,
  fetchOfferings,
  isTestStoreKey,
  refreshCustomerInfo,
  sdkIsSandbox,
  verdictFrom,
} from "@/lib/purchases/client";
import { ensureAppUserId } from "@/lib/purchases/app-user-id";
import { recordVerdict } from "@/lib/purchases/verdict";
import { subscribe } from "@/lib/settings/store";

/**
 * `checking` is the first check in flight; every later refresh keeps the
 * verdict it has until the answer lands, so a focus flick cannot flicker the
 * order button between two labels.
 */
export type PurchasesStatus = "unknown" | "checking" | "entitled" | "free";

export interface PurchasesState {
  status: PurchasesStatus;
  /** A public key was present and the SDK accepted it. */
  configured: boolean;
  /** A Test Store key, or the SDK's own `isSandbox()`. Purchases are simulated. */
  sandbox: boolean;
  /** The UUID the SDK was configured with, once minted. */
  appUserId: string | null;
  customerInfo?: CustomerInfo;
  offerings?: Offerings;
  /** The last purchase's store transaction, from `PurchaseResult`. */
  lastTransaction?: StoreTransaction;
  /**
   * Why the verdict is `unknown` or `checking`, in one sentence: no key, no
   * network, an SDK error. Null once a verdict is known.
   */
  reason: string | null;
  /** The last SDK failure's message, verbatim, for the pane. */
  error?: string;
  buy(pkg: Package): Promise<PurchaseResult>;
  refresh(): Promise<void>;
}

/** Refreshes on focus are spaced at least this far apart. */
export const FOCUS_REFRESH_MIN_MS = 10_000;

const PurchasesContext = createContext<PurchasesState | undefined>(undefined);

/** Test seam: a test may supply a whole state to whatever it renders. */
export const PurchasesStateProvider = PurchasesContext.Provider;

export interface PurchasesProviderProps {
  children: ReactNode;
  /** The public key; defaults to `VITE_REVENUECAT_PUBLIC_KEY`. "" means not configured. */
  apiKey?: string;
  /** Refresh when the window regains focus. On by default. */
  refreshOnFocus?: boolean;
}

function messageOf(error: unknown): string {
  if (error instanceof Error) return error.message;
  return typeof error === "string" ? error : String(error ?? "unknown error");
}

/**
 * The single owner of "does this desktop have Ada Pro".
 *
 * One per window, mounted beside `RunProvider` in `src/routes/index.tsx`. It
 * configures the SDK once from the build's public key and the persisted app
 * user id, asks RevenueCat on mount and on window focus, and hands the strip
 * and the Settings pane one verdict. `unknown` is a real state and it never
 * locks the order step: Ada is loopback-first, and a laptop with no network
 * must not lose a paid feature to a check that could not run. The service's
 * gate, where it is configured, is what refuses; the id it checks is a claim
 * this desktop sends (`lib/purchases/app-user-id.ts`), not a proof.
 *
 * The two windows do not share React state, so a purchase made in the
 * dashboard's pane reaches the strip through the settings store: the record
 * `recordVerdict` writes under `purchases.lastVerdict` is heard here through
 * `subscribe`, and a verdict this window does not hold makes it ask
 * RevenueCat again at once. The strip is a non-activating panel
 * (`app/src-tauri/src/lib.rs`), so a window `focus` event is not something
 * it can be relied on to get.
 */
export const PurchasesProvider = ({
  children,
  apiKey = configuredPublicKey(),
  refreshOnFocus = true,
}: PurchasesProviderProps) => {
  const [state, setState] = useState<Omit<PurchasesState, "buy" | "refresh">>(() => ({
    status: "unknown",
    configured: false,
    sandbox: false,
    appUserId: null,
    reason: apiKey ? "Ada Pro has not been checked yet." : NOT_CONFIGURED_LINE,
  }));
  const configuredRef = useRef(false);
  const configuringRef = useRef<Promise<void> | null>(null);
  const mountedRef = useRef(true);
  const lastRefreshAt = useRef(0);
  // The verdict this provider last set. A ref, not state: the store
  // subscriber below fires synchronously inside `recordVerdict`'s write,
  // before React has flushed the matching `setState`, and it has to tell
  // this window's own record from the other window's.
  const verdictRef = useRef<PurchasesStatus>("unknown");

  const refresh = useCallback(async () => {
    if (!configuredRef.current) return;
    lastRefreshAt.current = Date.now();
    setState((s) => (s.status === "unknown" ? { ...s, status: "checking" } : s));
    try {
      const info = await refreshCustomerInfo();
      const verdict = verdictFrom(info);
      if (!mountedRef.current) return;
      verdictRef.current = verdict;
      setState((s) => ({ ...s, status: verdict, customerInfo: info, reason: null, error: undefined }));
      void recordVerdict(verdict);
    } catch (error) {
      if (!mountedRef.current) return;
      const message = messageOf(error);
      verdictRef.current = "unknown";
      setState((s) => ({
        ...s,
        status: "unknown",
        reason: `Ada Pro could not be checked: ${message}`,
        error: message,
      }));
    }
    try {
      const offerings = await fetchOfferings();
      if (!mountedRef.current) return;
      setState((s) => ({ ...s, offerings }));
    } catch (error) {
      if (!mountedRef.current) return;
      // The verdict stands; only the price list is missing, and the pane
      // says so with the SDK's words.
      setState((s) => ({ ...s, error: messageOf(error) }));
    }
  }, []);

  const buy = useCallback(async (pkg: Package): Promise<PurchaseResult> => {
    const result = await buyPackage(pkg);
    const verdict = verdictFrom(result.customerInfo);
    verdictRef.current = verdict;
    if (mountedRef.current) {
      setState((s) => ({
        ...s,
        status: verdict,
        customerInfo: result.customerInfo,
        lastTransaction: result.storeTransaction,
        reason: null,
        error: undefined,
      }));
    }
    void recordVerdict(verdict);
    return result;
  }, []);

  /**
   * Configure once, then check. Shared while in flight, so StrictMode's
   * second mount and a focus in the meantime join the same attempt rather
   * than starting another; a failed attempt (the dashboard's wait for the
   * main window's id ran out, the SDK refused the key) is reported in words
   * and may be tried again.
   */
  const configure = useCallback((): Promise<void> => {
    if (!apiKey || configuredRef.current) return Promise.resolve();
    if (configuringRef.current) return configuringRef.current;
    configuringRef.current = (async () => {
      try {
        const appUserId = await ensureAppUserId();
        await configurePurchases({ apiKey, appUserId });
        const sandbox = isTestStoreKey(apiKey) || (await sdkIsSandbox());
        if (!mountedRef.current) return;
        configuredRef.current = true;
        setState((s) => ({ ...s, configured: true, sandbox, appUserId }));
        await refresh();
      } catch (error) {
        if (!mountedRef.current) return;
        const message = messageOf(error);
        setState((s) => ({
          ...s,
          status: "unknown",
          reason: `Purchases could not be configured: ${message}`,
          error: message,
        }));
      } finally {
        configuringRef.current = null;
      }
    })();
    return configuringRef.current;
  }, [apiKey, refresh]);

  useEffect(() => {
    mountedRef.current = true;
    if (!apiKey) {
      setState((s) => ({ ...s, configured: false, status: "unknown", reason: NOT_CONFIGURED_LINE }));
    } else {
      void configure();
    }
    return () => {
      mountedRef.current = false;
    };
  }, [apiKey, configure]);

  useEffect(() => {
    if (!refreshOnFocus || typeof window === "undefined") return;
    const onFocus = () => {
      if (Date.now() - lastRefreshAt.current < FOCUS_REFRESH_MIN_MS) return;
      if (!configuredRef.current) {
        // Not configured yet: a failed attempt is tried again here, spaced
        // the same way a refresh is.
        lastRefreshAt.current = Date.now();
        void configure();
        return;
      }
      void refresh();
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [refreshOnFocus, refresh, configure]);

  useEffect(
    () =>
      subscribe("purchases.lastVerdict", (record) => {
        // The other window recorded a verdict this one does not hold: a
        // purchase in the dashboard while the strip still says free. Ask
        // RevenueCat again now, with no focus spacing. The record is a
        // trigger to re-check and never the verdict itself, so a remembered
        // `entitled` cannot unlock anything on its own. This window's own
        // record carries the verdict the ref already holds, and the same
        // verdict from the other window is nothing new; neither asks again.
        if (record.verdict === "unknown" || record.verdict === verdictRef.current) return;
        void refresh();
      }),
    [refresh]
  );

  const value = useMemo<PurchasesState>(() => ({ ...state, buy, refresh }), [state, buy, refresh]);
  return <PurchasesContext.Provider value={value}>{children}</PurchasesContext.Provider>;
};

/**
 * Outside a provider (a component test, a window that never mounted one)
 * the answer is `unknown` with the reason, not a throw: the strip's step
 * panel renders in places a purchases provider has no business being, and an
 * unknown verdict is the state that changes nothing.
 */
const NOT_MOUNTED: PurchasesState = Object.freeze({
  status: "unknown",
  configured: false,
  sandbox: false,
  appUserId: null,
  reason: NOT_MOUNTED_LINE,
  buy: () => Promise.reject(new Error(NOT_MOUNTED_LINE)),
  refresh: () => Promise.resolve(),
});

export const usePurchases = (): PurchasesState => useContext(PurchasesContext) ?? NOT_MOUNTED;
