# Ada Pro: what leaves your desktop

Ada Pro is bought through RevenueCat inside the Ada desktop app. This page says
what that purchase sends and to whom. It describes the code in this repository;
it is not a legal policy.

## To RevenueCat

- **An app user id**: a random UUID minted on your desktop the first time
  purchases are configured, stored in Ada's settings as `purchases.appUserId`
  (`app/src/lib/purchases/app-user-id.ts`). It is not your name, email or any
  account of yours.
- **The purchase**: which package you bought and when, recorded by RevenueCat's
  Web Billing. With a Test Store key, which is what this repository ships with,
  the purchase is simulated and no payment details are asked for or sent.
- **The paywall's two custom variables**: the board you were ordering when the
  paywall opened (the words of your request, at most 60 characters) and its part
  count, used only to fill the paywall's copy
  (`paywallVariables` in `app/src/lib/purchases/client.ts`).

## To Ada's own service

- The same app user id, as the `X-Kaleo-App-User-Id` header on each step
  request, so the service can ask RevenueCat whether that id has Ada Pro
  (`service/entitlements.py`). The service runs on your machine by default.

## Not sent

- No email, name or payment card is collected by Ada.
- Nothing about Ada Pro goes anywhere but the two places above. Designing a
  board is a separate matter: your request and any datasheets are sent to the
  model provider you configured (Gemini or Claude), because that is how the board
  is designed, with or without Ada Pro.

Manage or cancel a subscription from Settings > Ada Pro > Manage subscription,
which opens RevenueCat's own subscription page for your purchase.
