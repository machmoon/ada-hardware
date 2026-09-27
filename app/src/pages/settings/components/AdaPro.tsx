import { useCallback, useEffect, useRef, useState } from "react";
import { openUrl } from "@tauri-apps/plugin-opener";
import type { Package } from "@revenuecat/purchases-js";
import { Button, Header, Label } from "@/components";
import { usePurchases } from "@/contexts/purchases.context";
import {
  NOT_CONFIGURED_LINE,
  PRO_ENTITLEMENT,
  PRO_UNLOCKS_LINE,
  TEST_STORE_LINE,
  isUserCancelled,
  managementUrl,
  packagesOf,
  paywallVariables,
  proEntitlement,
  proOffering,
} from "@/lib/purchases/client";
import { PAYWALL_CONTEXT_KEY, PRO_PANE_ID, readPaywallContext } from "@/lib/purchases/pane";
import { safeLocalStorage } from "@/lib/storage/helper";
import { lastVerdict } from "@/lib/purchases/verdict";

/**
 * The Settings face of Ada Pro: what it unlocks, what it costs, and the
 * receipt RevenueCat holds for this desktop.
 *
 * The pane shows the SDK's `CustomerInfo` verbatim rather than a summary of
 * it, for the reason the sourcing table shows an MPN's status word rather
 * than a tick: the fields are the evidence, and a pane that said "you have
 * Pro" over a customer record that says otherwise would be the lie the
 * service's gate then catches at the worst moment. Buying goes through the
 * SDK's own checkout modal; with a Test Store key that modal is a simulated
 * purchase and the pane says so in the one sentence agreed for it.
 */

/** Said when the offering has no dashboard paywall and the buttons stand in. */
export const NO_PAYWALL_LINE =
  "No RevenueCat paywall is attached to this offering, so the plans are listed directly.";
/** Said once a purchase from either door has made Ada Pro active. */
export const UNLOCKED_LINE = "Ada Pro is active. Prepare fab order is unlocked on the strip.";

/** "Purchase cancelled" is the SDK's user-cancelled error, in one line. */
export const CANCELLED_LINE = "Purchase cancelled. Nothing was charged.";

const VERDICT_LINE: Record<string, string> = {
  entitled: "Ada Pro is active on this desktop.",
  free: "Ada Pro is not active on this desktop.",
  checking: "Checking with RevenueCat.",
  unknown: "Ada Pro is not known.",
};

/** The SDK's ISO 8601 period ("P1M") in words; an unknown one is shown as is. */
export function periodWords(period: string): string {
  const words: Record<string, string> = {
    P1W: "week",
    P1M: "month",
    P2M: "two months",
    P3M: "three months",
    P6M: "six months",
    P1Y: "year",
  };
  return words[period] ?? period;
}

function iso(date: Date | null | undefined): string {
  if (!date) return "none";
  const time = date instanceof Date ? date.getTime() : Number.NaN;
  return Number.isFinite(time) ? date.toISOString() : String(date);
}

interface AdaProProps {
  className?: string;
}

export const AdaPro = ({ className }: AdaProProps) => {
  const purchases = usePurchases();
  const [busy, setBusy] = useState<string | null>(null);
  const [result, setResult] = useState<string>("");
  const packages = packagesOf(purchases.offerings);
  const info = purchases.customerInfo;
  const pro = info ? proEntitlement(info) : null;
  const remembered = lastVerdict();
  const offering = proOffering(purchases.offerings);
  const paywallHost = useRef<HTMLDivElement>(null);
  const [paywallOpen, setPaywallOpen] = useState(false);
  const manage = managementUrl(info);

  const failed = (error: unknown) =>
    setResult(
      isUserCancelled(error)
        ? CANCELLED_LINE
        : `Could not purchase: ${(error as Error)?.message ?? "unknown error"}`
    );

  /**
   * RevenueCat's own paywall, designed in the dashboard and rendered into the
   * pane, the way RevenueCat's web sample does it (`presentPaywall` with an
   * offering, a target element and custom variables). The board context the
   * strip left is read once and removed, so a later visit to Settings shows
   * the dashboard's default copy rather than an old board's name.
   */
  const showPaywall = useCallback(async () => {
    if (!offering || !paywallHost.current) return;
    const context = readPaywallContext();
    safeLocalStorage.removeItem(PAYWALL_CONTEXT_KEY);
    paywallHost.current.innerHTML = "";
    setPaywallOpen(true);
    setBusy("paywall");
    setResult("");
    try {
      await purchases.paywall(offering, paywallHost.current, paywallVariables(context));
      setResult(UNLOCKED_LINE);
    } catch (error) {
      failed(error);
    } finally {
      setBusy(null);
      setPaywallOpen(false);
    }
  }, [offering, purchases]);

  // Opened from the locked order step: go straight to the paywall.
  const autoShown = useRef(false);
  useEffect(() => {
    if (autoShown.current || !offering?.hasPaywall || purchases.status !== "free") return;
    if (!readPaywallContext()) return;
    autoShown.current = true;
    void showPaywall();
  }, [offering, purchases.status, showPaywall]);

  const buy = async (pkg: Package) => {
    setBusy(pkg.identifier);
    setResult("");
    try {
      const outcome = await purchases.buy(pkg);
      setResult(
        `Purchased ${outcome.storeTransaction.productIdentifier}` +
          ` (transaction ${outcome.storeTransaction.storeTransactionId}).`
      );
    } catch (error) {
      failed(error);
    } finally {
      setBusy(null);
    }
  };

  const refresh = async () => {
    setBusy("refresh");
    setResult("");
    try {
      await purchases.refresh();
    } finally {
      setBusy(null);
    }
  };

  const body = !purchases.configured ? (
    <p className="text-xs text-muted-foreground" data-testid="pro-not-configured">
      {purchases.reason ?? NOT_CONFIGURED_LINE}
    </p>
  ) : (
    <>
      {purchases.sandbox ? (
        <p className="text-xs text-amber-600 dark:text-amber-400" data-testid="pro-sandbox">
          {TEST_STORE_LINE}
        </p>
      ) : null}

      <div className="flex items-center justify-between gap-3">
        <p className="text-xs" data-testid="pro-verdict" data-status={purchases.status}>
          {VERDICT_LINE[purchases.status]}
          {purchases.reason ? ` ${purchases.reason}` : ""}
          {remembered.at ? ` Last seen ${remembered.verdict} at ${remembered.at}.` : ""}
        </p>
        <Button
          size="sm"
          variant="outline"
          onClick={() => void refresh()}
          disabled={busy !== null}
          data-testid="pro-refresh"
        >
          {busy === "refresh" ? "Checking…" : "Refresh"}
        </Button>
      </div>

      {offering?.hasPaywall && purchases.status !== "entitled" ? (
        <div className="space-y-2" data-testid="pro-paywall">
          {paywallOpen ? null : (
            <Button
              size="sm"
              onClick={() => void showPaywall()}
              disabled={busy !== null}
              data-testid="pro-show-paywall"
            >
              See Ada Pro plans
            </Button>
          )}
          <div ref={paywallHost} data-testid="pro-paywall-host" />
        </div>
      ) : null}

      {manage && purchases.status === "entitled" ? (
        <Button
          size="sm"
          variant="outline"
          onClick={() => void openUrl(manage)}
          data-testid="pro-manage"
        >
          Manage subscription
        </Button>
      ) : null}

      {offering?.hasPaywall && purchases.status !== "entitled" ? null : packages.length ? (
        <div className="space-y-2" data-testid="pro-packages">
          {offering && !offering.hasPaywall ? (
            <p className="text-xs text-muted-foreground" data-testid="pro-no-paywall">
              {NO_PAYWALL_LINE}
            </p>
          ) : null}
          {packages.map((pkg) => {
            const product = pkg.webBillingProduct;
            return (
              <div
                key={pkg.identifier}
                className="flex items-center justify-between gap-3"
                data-testid="pro-package"
                data-package={pkg.identifier}
                data-product={product.identifier}
              >
                <div className="min-w-0">
                  <Label className="text-sm font-medium">{product.title}</Label>
                  <p className="text-xs text-muted-foreground">
                    {product.price.formattedPrice}
                    {product.normalPeriodDuration ? ` every ${periodWords(product.normalPeriodDuration)}` : ""}
                    {" · "}
                    <code>{product.identifier}</code>
                  </p>
                </div>
                <Button
                  size="sm"
                  onClick={() => void buy(pkg)}
                  disabled={busy !== null || purchases.status === "entitled"}
                  data-testid="pro-buy"
                  data-package={pkg.identifier}
                >
                  {busy === pkg.identifier
                    ? "Buying…"
                    : purchases.status === "entitled"
                      ? "Active"
                      : "Buy"}
                </Button>
              </div>
            );
          })}
        </div>
      ) : (
        <p className="text-xs text-muted-foreground" data-testid="pro-no-packages">
          {purchases.error
            ? `No packages: ${purchases.error}`
            : purchases.offerings
              ? "The default offering has no packages. Add ada_pro_monthly to it in the RevenueCat dashboard."
              : "Loading the offering."}
        </p>
      )}

      {info ? (
        <dl
          className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-xs"
          data-testid="pro-customer-info"
        >
          <dt className="text-muted-foreground">Entitlement {PRO_ENTITLEMENT}</dt>
          <dd data-testid="pro-entitlement-active">
            {info.entitlements.active[PRO_ENTITLEMENT] ? "active: yes" : "active: no"}
          </dd>
          <dt className="text-muted-foreground">Original app user id</dt>
          <dd className="break-all font-mono" data-testid="pro-original-app-user-id">
            {info.originalAppUserId}
          </dd>
          <dt className="text-muted-foreground">App user id</dt>
          <dd className="break-all font-mono" data-testid="pro-app-user-id">
            {purchases.appUserId ?? "none"}
          </dd>
          <dt className="text-muted-foreground">Latest purchase</dt>
          <dd data-testid="pro-latest-purchase">{iso(pro?.latestPurchaseDate)}</dd>
          <dt className="text-muted-foreground">Expires</dt>
          <dd data-testid="pro-expiration">{pro ? iso(pro.expirationDate) : "none"}</dd>
          <dt className="text-muted-foreground">Manage</dt>
          <dd data-testid="pro-management-url">
            {info.managementURL ? (
              <a className="underline" href={info.managementURL} target="_blank" rel="noreferrer">
                {info.managementURL}
              </a>
            ) : (
              "none"
            )}
          </dd>
          <dt className="text-muted-foreground">Last transaction</dt>
          <dd className="break-all font-mono" data-testid="pro-transaction">
            {purchases.lastTransaction?.storeTransactionId ?? "none this session"}
          </dd>
        </dl>
      ) : null}

      {result ? (
        <p className="text-xs" role="status" data-testid="pro-result">
          {result}
        </p>
      ) : null}
    </>
  );

  return (
    <div id={PRO_PANE_ID} className={`space-y-3 ${className ?? ""}`} data-testid="pro-settings">
      <Header isMainTitle title="Ada Pro" description={PRO_UNLOCKS_LINE} />
      <p className="text-xs" data-testid="pro-unlocks">
        Nothing else is gated on Ada Pro. Engine minutes are metered separately,
        under Usage metering below, and only when an operator turns that on.
      </p>
      {body}
    </div>
  );
};
