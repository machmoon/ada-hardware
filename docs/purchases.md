# Ada Pro purchases

Ada is an AI hardware engineer that works beside KiCad: describe a board, get
a schematic, a placed and routed board and a printable case, each checked by
KiCad's own ERC and DRC. For a beginner before the first dead board, and a
senior engineer before fab.

Ada Pro is the one capability behind a subscription: the **order** step
(Prepare fab order). Nothing else is gated on it; engine calls are metered
separately (`billing/`). This document is the desktop half, built 2026-09-24
over RevenueCat's `purchases-js` SDK; the service half (the gate itself) is
described under "The contract", with what it can and cannot enforce.

Status, stated plainly: the desktop is built and tested offline against a
recording fake of the SDK. It has not been run against a live RevenueCat
project from this checkout. The Test Store is the intended first live run.

## What is built

| Piece | Where | What it does |
| --- | --- | --- |
| SDK seam | `app/src/lib/purchases/client.ts` | `configurePurchases`, `fetchOfferings`, `buy`, `refreshCustomerInfo`, `verdictFrom`, `isTestStoreKey`; the `PurchasesSdk` interface the real SDK satisfies, and `nullPurchases()` for tests. The real SDK is loaded by dynamic import, so Vitest never resolves it. |
| App user id | `app/src/lib/purchases/app-user-id.ts` | One UUID minted with `crypto.randomUUID()` in the main window only (Tauri label `main`; the dashboard waits for it through the store, 20 s at most, and never mints one of its own) and kept in the settings store under `purchases.appUserId`; the `X-Kaleo-App-User-Id` header. |
| Provider | `app/src/contexts/purchases.context.tsx` | One per window, beside `RunProvider`: configures the SDK from `VITE_REVENUECAT_PUBLIC_KEY`, checks on mount, on window focus, and whenever the other window records a different verdict under `purchases.lastVerdict` (a purchase in the dashboard reaches the strip that way, without waiting for a focus event the strip's non-activating panel may never get); exposes `buy` and `refresh`; remembers the last verdict under `purchases.lastVerdict`. |
| Settings pane | `app/src/pages/settings/components/AdaPro.tsx` | "Ada Pro", above Billing: the offering's packages with a Buy each, the SDK's `CustomerInfo` verbatim, the Test Store sentence, or the not-configured sentence. |
| Strip gate | `app/src/pages/kaleo/components/StepPanel.tsx` | The order step's button while the verdict is `free` or the service answered 402: "Prepare fab order · Ada Pro", which opens Settings at the pane. |
| Engine client | `app/src/lib/silkscreen/client.ts` | Sends the header on every `/steps` request; maps a 402 with `reason: entitlement_required` to `ErrorKind` `entitlement`. |
| Pane request | `app/src/lib/purchases/pane.ts` | How the strip opens the other window at `/settings#pro`: one localStorage key plus the `storage` event, the tour's mechanism (`app/src/lib/tour.ts`). |
| Build guard | `app/vite.config.ts` | A production build with a Test Store key fails unless `ADA_ALLOW_TEST_STORE=1`. |
| Guards | `app/src/config/hardening.test.ts` | `.env.local` is git-ignored, no key literal in any source, the build guard's sentence, the two settings validators. |

## The contract

These names are shared between the desktop, the service and the RevenueCat
dashboard, and none of them may be renamed on one side alone.

| | |
| --- | --- |
| Product | Ada Pro |
| Entitlement identifier | `pro` |
| Product identifier | `ada_pro_monthly` (monthly subscription) |
| Offering | `default` |
| Gated step | `order` only. `case` and `sourcing` are prefetched by the service at `place` (`service/steps.py`), so a client gate on those would stop no model call. |
| Desktop env | `VITE_REVENUECAT_PUBLIC_KEY`, from `app/.env.local` (git-ignored), read through `import.meta.env`. |
| Service env | `REVENUECAT_SECRET_API_KEY` (an API v2 secret key) and `REVENUECAT_PROJECT_ID`. With either missing the gate is off and the step envelope says so. |
| Header | `X-Kaleo-App-User-Id: <uuid>` on every `/steps` request from the desktop; the service reads it only for the order step. |
| 402 body | `{"reason": "entitlement_required", "entitlement": "pro", "detail": "<one sentence>", "checked_at": "<ISO-8601 UTC>"}`. The desktop maps it to `ErrorKind` `entitlement`; the pre-existing 402 `insufficient_credit` keeps its handling. |
| Envelope, gate off | `"entitlement": {"checked": false, "reason": "not_configured", "detail": "Entitlement not checked: REVENUECAT_SECRET_API_KEY is not set, so the order step is not gated on this service."}` |
| Envelope, gate on and passed | `"entitlement": {"checked": true, "entitlement": "pro", "active": true, "checked_at": ...}` |
| Test Store copy | "Test Store: simulated purchase. No money moves." Shown whenever the key is a Test Store key or the SDK's `isSandbox()` is true. |
| App user id | A UUID minted once on the desktop; never the engine bearer token, never an `ada_` key (`service/auth.py`), never RevenueCat's `$RCAnonymousID:` form (`billing/accounts.py` refuses a `$`). |

The app user id is a claim, not a proof: the service trusts the header as
sent, so an entitled desktop's UUID works from any client that sends it, and
the service does not gate when RevenueCat cannot be reached (the root
`.env.example` says so: a dead or rate-limited RevenueCat fails open, with
the reason in the envelope). A signed identity (account sign-in) is [not yet
built].

### The three-state verdict

The desktop's verdict is `entitled`, `free` or `unknown` (`unknown` covers
no key, offline, and any SDK error; the provider also has a `checking` state
for the first check in flight, which the strip treats as `unknown`).

- `free` locks the order step behind "Prepare fab order · Ada Pro".
- `unknown` **never locks the step**. Ada is loopback-first, and an offline
  laptop must not lose a paid feature to a check that could not run. The
  button stays live and a note under it says "Ada Pro not checked" with the
  reason.
- `entitled` changes nothing.

The service's gate, where it is configured, is what refuses, within the two
limits stated above. A 402 from it locks the button the same way `free` does
until the desktop's own verdict turns `entitled`: the error stays on the run
until the next approval, and a person who bought Pro after the 402 gets the
button back without starting the run over.

Where money and identity go is summarised for people, not engineers, in
[privacy.md](privacy.md), which the paywall's Privacy link opens.

## Configuring it

1. In the RevenueCat dashboard, create a project (or use the existing one).
   A **Test Store** is provisioned automatically for a new project
   (RevenueCat docs, Test Store: "During the setup of a new RevenueCat
   project, a Test Store will be automatically created with products").
2. Create the product `ada_pro_monthly` as a monthly subscription, the
   entitlement `pro`, attach the product to it, and put the product in the
   `default` offering as its monthly package. This is the README's own
   sequence (`app/node_modules/@revenuecat/purchases-js/README.md`,
   Prerequisites).
   Then attach a **paywall** to the `default` offering (Paywalls in the
   dashboard) and define two custom variables on it, `board_name` and
   `part_count`, with defaults such as "your board" and "every". Use them in
   the copy as `{{ custom.board_name }}` and `{{ custom.part_count }}`: the
   desktop fills them with the board the person was ordering when the gate
   stopped them (`paywallVariables` in `app/src/lib/purchases/client.ts`).
   Without a paywall the pane lists the package with a Buy button instead
   and says so.
3. Copy the Test Store's public API key (it starts with `test_`) into
   `app/.env.local`:

   ```
   VITE_REVENUECAT_PUBLIC_KEY=test_...
   ```

   `app/.env.example` documents the name. `*.local` is git-ignored.
4. Start the app (`npm run dev` from `app/`, or `tauri dev`). Settings shows
   an "Ada Pro" pane above Billing with the Test Store sentence and
   RevenueCat's paywall ("See Ada Pro plans"). Pressing the locked "Prepare
   fab order · Ada Pro" on the strip opens this pane and the paywall at
   once, with that board's name. Once Pro is active the pane offers
   "Manage subscription", which opens `customerInfo.managementURL`.
5. For the service gate, set `REVENUECAT_SECRET_API_KEY` and
   `REVENUECAT_PROJECT_ID` in the service's environment. Without them the
   order step runs ungated and every envelope says so.

## What Test Store means

RevenueCat's Test Store is a store of its own, beside Stripe, the App Store
and Play. A purchase through it is made in a modal the SDK renders, without a
payment method, and RevenueCat records it like any other purchase: it updates
`CustomerInfo`, activates the entitlement, and appears in the dashboard as
sandbox data (RevenueCat docs, Test Store: test purchases "behave like real
purchases and subscriptions: they update CustomerInfo, trigger entitlements,
and appear in your RevenueCat dashboard"). No money moves. The pane says
exactly that, in the one sentence agreed for it, whenever the key is a Test
Store key or the SDK reports sandbox.

A Test Store key must never ship in a store build. RevenueCat says so
("Never submit an app to the App Store or Google Play that is configured with
a Test Store API key"), and `app/vite.config.ts` refuses a production build
that carries one: "Refusing to build a store bundle with a RevenueCat Test
Store key; set ADA_ALLOW_TEST_STORE=1 for a demo build." Dev mode is
unaffected.

Key formats, from the SDK's own `src/helpers/api-key-helper.ts`
(RevenueCat/purchases-js): Test Store `/^test_[a-zA-Z0-9_.-]+$/`, Web
Billing sandbox `rcb_sb_...`, Web Billing production `rcb_...`.

## The launch path

Web Billing is RevenueCat's own checkout over a connected Stripe account
(the README's first prerequisite: "Connect your Stripe account"). Going live
is a key swap, not a code change: a `rcb_sb_` sandbox key for rehearsal, a
`rcb_` production key for release, in the same `VITE_REVENUECAT_PUBLIC_KEY`.

What is planned and **[not yet built]**:

- **Webhook to the ledger.** RevenueCat signs every webhook with
  `X-RevenueCat-Webhook-Signature: t=<unix>,v1=<hex>`, an HMAC-SHA256 over
  `<t>.<raw body>` (RevenueCat docs, Webhooks), which is the same scheme
  `billing/webhook.py` already verifies for Stripe, raw bytes before any
  parse. A handler in the service would grant on `INITIAL_PURCHASE` and
  `RENEWAL`, revoke on `EXPIRATION` and `CANCELLATION`, and key idempotency on
  the event id. Not built this week: a desktop demo has no public URL for
  RevenueCat to call.
- **Paywall.** The SDK's `presentPaywall({ htmlTarget })` renders the
  dashboard-designed paywall; the pane lists packages directly instead.
- **Account identity.** The app user id is per desktop. Signing in (Google
  through `googleapps/auth.py`, or an `ada_` API key) and aliasing the id
  through `identifyUser` would carry Pro across machines.

## Not yet built

- [not yet built] The service webhook and the ledger grant (above).
- A live run of the paywall path, measured 2026-09-27 with the Test Store
  key and purchases-js 1.64.0 in a desktop browser: `getOfferings()` returned
  `default` with `hasPaywall: true`, `presentPaywall` drew the dashboard's
  "Ada Pro" paywall with `board_name` and `part_count` filled in and the price
  from the product ($12.00 per month), the Test Store checkout's "Test valid
  purchase" completed, and the returned `CustomerInfo` had `pro` active. The
  calls are the ones `realSdk()` makes (`presentPaywall` is copied from
  RevenueCat/purchases-js `afae8c7`
  `examples/webbilling-demo/src/pages/rc_paywall/index.tsx`).
- [not yet built] The same run inside the Tauri desktop app. It was not
  rebuilt that day (no Rust toolchain on the machine); the app's wiring of
  those calls is covered by the Vitest suite over `nullPurchases()`.
- [not yet built] The desktop reading the step envelope's `entitlement`
  block. The strip acts on the verdict and on the 402; the envelope field is
  for logs and the dashboard.
- [not yet built] Restoring a purchase on a second desktop.
- [not yet built] A signed identity. The app user id is a claim the desktop
  sends; the service trusts it as sent and does not gate when RevenueCat is
  unreachable.
- [not yet built] A Rust-side mint of the app user id under one lock. The
  JS designates the minting window by its Tauri label instead, and the
  dashboard waits for the main window's id rather than minting.

## References

- `app/node_modules/@revenuecat/purchases-js/README.md` (1.64.0, MIT): the
  Prerequisites sequence.
- `app/node_modules/@revenuecat/purchases-js/dist/Purchases.es.d.ts`: the
  public API this integration uses (`configure`, `isConfigured`,
  `getSharedInstance`, `getOfferings`, `purchase`, `getCustomerInfo`,
  `isSandbox`, `CustomerInfo.entitlements.active`, `PurchaseResult`).
- RevenueCat/purchases-js `src/helpers/api-key-helper.ts`: the key regexes.
- https://www.revenuecat.com/docs/web/web-billing/web-sdk: `Purchases.configure`,
  `getOfferings`, `purchase`.
- https://www.revenuecat.com/docs/test-and-launch/sandbox/test-store: what the
  Test Store is and the rule against shipping its key.
- https://www.revenuecat.com/docs/integrations/webhooks: the signature
  header and scheme.
