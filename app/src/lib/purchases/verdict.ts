// The last verdict, remembered. Shown in the Ada Pro pane as "last checked";
// never used to lock or unlock anything, because a remembered `free` on a
// laptop that has since bought would lock a paid feature, and a remembered
// `entitled` is not proof of anything today. It is also how a purchase in
// one window reaches the other: the strip and the dashboard share the
// settings store, so a record the dashboard writes is heard by the strip's
// provider, which then asks RevenueCat again
// (`src/contexts/purchases.context.tsx`). It re-asks; it does not adopt the
// record. The service's gate is what refuses; this is a receipt.

import { getSetting, setSetting, type PurchasesLastVerdict, type PurchasesVerdict } from "@/lib/settings/store";

/** Persist a verdict that was actually seen. `unknown` is never written: it is the absence of one. */
export async function recordVerdict(verdict: PurchasesVerdict, now = new Date()): Promise<void> {
  if (verdict === "unknown") return;
  const record: PurchasesLastVerdict = { verdict, at: now.toISOString() };
  try {
    await setSetting("purchases.lastVerdict", record);
  } catch (error) {
    console.warn("[ada purchases] could not remember the verdict:", error);
  }
}

/** The remembered verdict, or the never-seen default. */
export function lastVerdict(): PurchasesLastVerdict {
  try {
    return getSetting("purchases.lastVerdict");
  } catch {
    return { verdict: "unknown", at: "" };
  }
}
