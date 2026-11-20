# Stripe test-mode checklist

A manual run-through of the Stripe adapter against Stripe's **test mode**, using
the [Stripe CLI](https://docs.stripe.com/stripe-cli). The automated tests fake
Stripe's responses; this is the only check against the real API shapes. Run it
before enabling payments on a real directory, and again after any change to
`adapters/stripe/`.

Use a **test-mode** key (`sk_test_…`) throughout. Nothing here moves real money.

## 0. Prepare

- [ ] OSDS is running on a host with a **verified domain over HTTPS**. Payments
      are unavailable otherwise (decisions.md §4.11). For a laptop, expose it
      with a tunnel and use the tunnel's host as the tenant domain; or skip the
      public URL and forward with `stripe listen` (step 3).
- [ ] The directory has at least one purchasable tier with a price, and one
      claimed listing whose owner you can sign in as. The tier must be
      purchasable, have a rank above 0 and not be a slot tier, or checkout is
      refused with "That tier is not for sale." (`billing/payments.py:160`).
- [ ] **Mail is configured and the worker (`osds-worker`) is running.** Billing
      notices (payment failed, downgraded, expired) are queued by
      `billing/notices.py` and sent by the worker; without both, no mail
      arrives (`docs/install.md`, "Mail").
- [ ] `stripe login` is done and the CLI is in **test mode**.
- [ ] Note the tier's amount, currency and interval. You will make the Stripe
      Price match it exactly.

Limits to know about before you start: an owner can start at most **10
checkouts per hour** (`billing/payments.py:42`); the inbound webhook route
allows **300 requests per minute per IP** (`billing/inbound.py:39`) and rejects
bodies over **256 KB** with `413` (`billing/inbound.py:36,74-78`).

## 1. Create the prices

For each purchasable tier (here, `featured` at 49.00 USD per month):

```powershell
stripe products create --name "Featured"
stripe prices create --product prod_XXXX --unit-amount 4900 --currency usd -d "recurring[interval]=month"
```

- [ ] The command prints a `price_…` id for each tier.
- [ ] Amount, currency and interval equal the tier's in OSDS. A yearly tier is
      `recurring[interval]=year`. An `interval_count` other than 1 is refused
      (`adapters/stripe/provider.py:63`), and so is an **inactive** Price
      (`provider.py:57-58`).

## 2. Configure the adapter

**Order matters.** The webhook signing secret is a required field, so it has to
be saved *together with* the secret key and price ids: a first save without it
fails with "Webhook signing secret is required."
(`billing/settings_service.py:72-73`). Obtain the `whsec_…` first (step 3), then
save every field at once. The page shows the webhook URL before you save, once
the domain is verified (`billing/settings_views.py:73-76`); start
`stripe listen` first, or create the endpoint using that URL.

Admin home → **Payment settings** (`/admin/settings/payments/`). The page needs
the Admin role (`billing/settings_views.py:41`).

- [ ] **Secret key**: your `sk_test_…` (or a restricted `rk_test_…` key). The
      adapter calls only: read Prices (GET); write Checkout Sessions (POST);
      read and write Subscriptions (GET, POST, DELETE, including a list by
      customer, `adapters/stripe/events.py:245`); write Billing Portal
      Sessions (POST). It never reads Customers, so that permission is not
      needed.
- [ ] **Price ids**: `featured=price_XXXX, verified=price_YYYY`.
- [ ] A value in the wrong shape (a `pk_test_…` key, `featured=prod_…`) is
      refused with "<Field label> is not in the expected format."
      (`osds/setting_pattern.py:23`; patterns at `adapters/stripe/config.py:24,38`).
      The page does not echo the value.
- [ ] The page shows this directory's webhook URL:
      `https://<your host>/_adapters/stripe/inbound/`.

## 3. Webhook signing secret

Do this **before** saving the settings in step 2. Choose one.

**Forwarding (local, no public URL):**

```powershell
stripe listen --forward-to https://acme.localhost/_adapters/stripe/inbound/ --skip-verify
```

The CLI prints `Ready! Your webhook signing secret is whsec_…`. That secret is
specific to this `listen` session.

**A real endpoint (a public URL):** Dashboard → Developers → Webhooks → Add
endpoint, with the URL above and exactly these six events (the handlers in
`adapters/stripe/events.py:259-266`): `checkout.session.completed`,
`invoice.paid`, `invoice.payment_failed`, `customer.subscription.updated`,
`customer.subscription.deleted`, `charge.refunded`. Reveal and copy the
endpoint's `whsec_…`.

- [ ] Paste it into **Webhook signing secret** and save together with the secret
      key and price ids (step 2). The page now says payments are available.
- [ ] Keep `stripe listen` running (it prints each delivery and its response
      code) for the rest of this list.

## 4. Signature and replay

- [ ] `stripe trigger customer.created` is delivered and the CLI shows `200`
      (an event the adapter does not handle is acknowledged).
- [ ] Replace the signing secret in OSDS with a wrong `whsec_abcdef`, run
      `stripe trigger customer.created`: the response is **400**. Put the right
      secret back.
- [ ] Resend an event: Dashboard → the event → Resend (or
      `stripe events resend evt_XXXX`). It returns 200. If it carried a state
      change that was **applied**, the listing does not change twice and the
      command log shows a replay (`billing/reports.py:77-85`). An event
      recorded as "ignored" replays silently, with no log entry
      (`billing/reports.py:86-89`).
- [ ] The **503** path cannot be tested by removing the secret: a required
      secret cannot be cleared. Ticking "Remove the stored webhook signing
      secret" gives "Webhook signing secret is required."
      (`billing/settings_service.py:66-69`). A delivery returns 503
      ("not configured") only on a tenant that has **never** saved a secret
      (`billing/inbound.py:85-86`, `adapters/stripe/provider.py:129`). To test
      it, send a signed or unsigned POST to the webhook URL before the first
      save in step 2; skip it otherwise.

`stripe trigger` fixtures carry no OSDS metadata, so triggering invoice,
subscription or checkout events is acknowledged `200` and then **ignored**
(`adapters/stripe/events.py:111-112,154-155,189-191`). Only real purchase flows
exercise the adapter. The `200` response body is JSON,
`{"received":…, "applied":…, "ignored":…}`, which shows which.

## 5. Purchase

Sign in as the listing's owner and open the listing page. Billing is the
**Your plan** panel on that page; there is no separate billing page
(`billing/templates/billing/owner/_panel.html`).

- [ ] **Upgrade to <Tier> — 4900 USD / month** redirects to a Stripe Checkout
      page. (The panel shows the price in raw minor units.) The page shows the
      right plan and price, and the owner's email.
- [ ] Pay with `4242 4242 4242 4242`, any future expiry, any CVC.
- [ ] The owner returns to OSDS and sees "Thank you … Your payment is being
      confirmed. Your plan appears on your listing page within a few minutes;
      refresh it if it has not changed yet." (`billing/templates/billing/owner/return.html:7`).
      In `stripe listen`: `checkout.session.completed`, `invoice.paid`, others,
      all `200`.
- [ ] `invoice.paid` can arrive **before** `checkout.session.completed`. Then
      the second is reported `ignored` ("checkout_already_completed",
      `billing/reports.py:171-172,189-194`) and still returns `200`. The
      response JSON shows which one applied.
- [ ] The listing is now on the tier, **active**, with a renewal date a month
      out. The operator console's entitlement list and detail pages do **not**
      show the `sub_…` reference (they show Tier, Status, Billing, Started,
      Period ends, trial/dunning/grace dates and History). Verify the `sub_…`
      in the Stripe Dashboard: the subscription's metadata carries
      `attempt_id`, `listing_id` and `tier`.
- [ ] Repeat **Upgrade** with a Price whose amount differs from the tier's (edit
      the setting to point at the wrong price): OSDS refuses with the generic
      "The payment provider could not complete that. Nothing was changed; please
      try again." (`billing/payments.py:65`), not a specific message. A checkout
      attempt row is created and marked failed (`billing/payments.py:141`), and
      no Stripe session is created. Restore the setting.

## 6. Failed payment

- [ ] An owner whose listing is already **past due** or in **grace** cannot start
      a new checkout: they get "Update your payment details to fix this; a new
      purchase is not needed." (`billing/payments.py:165-166`). An active or
      trialing listing gets "This listing already has a paid plan."
      (`billing/payments.py:167-168`).
- [ ] Start a checkout and pay with `4000 0000 0000 0341` (attaches, then
      fails on the first charge): the listing does not receive the tier.
- [ ] For a renewal failure, buy normally, then in the Dashboard open the
      subscription's customer and replace the card with `4000 0000 0000 0341`.
      Advance the clock (Dashboard → Test clocks) one billing period.
      `invoice.payment_failed` arrives; the listing becomes **past due**, the
      owner sees "Payment failed, update card." with an **Update card** link
      (`billing/owner_state.py:59-61`), and the portal link works. The owner
      receives the mail only if mail is configured and the worker is running
      (`billing/notices.py`); a notice is dropped once the dunning window ends
      (its expiry is the dunning deadline).
      **Test clocks may not apply**: see the note below this step.
- [ ] Fix the card in the portal. Stripe retries and `invoice.paid` arrives; the
      listing returns to **active** (`billing/machine.py:138`).

**Dunning runs on OSDS's own clock, not Stripe's.** The 14-day dunning window
and the 30-day grace window (`billing/machine.py:39-40`) are advanced by the
worker tick against real time. Stripe test clocks do not move them. To reach
**grace** quickly, use the operator **Override** on the entitlement, which sets
Grace and keeps the `payment_ref` (`billing/entitlements.py:249`). The
alternative is waiting 14 real days.

**Test clocks versus Checkout-created customers (unverified).** The adapter
passes only `customer_email` (`adapters/stripe/provider.py:85`), so Stripe
creates the customer at Checkout, and a Checkout-created customer is not
attached to a test clock. Steps 6 (renewal failure) and 10 (advance past the
trial) rely on test clocks. This is Stripe-side behaviour that cannot be
confirmed from the code. Confirm it at run time, and if the clock does not move
the subscription, decide on another way to force a renewal.

## 7. Customer portal

Dashboard → Settings → Billing → Customer portal (test mode): enable
cancellation and payment-method updates.

- [ ] The owner's **Update card** link opens the portal for the right customer
      and returns to OSDS afterwards. The link appears **only** when the listing
      is **past due** (and **Restore** when it is in **grace**)
      (`billing/owner_state.py:59-66`). An active listing has no portal link. To
      test cancel and keep-plan from the portal on an active listing, open the
      portal from the Stripe Dashboard instead of from OSDS.
- [ ] Cancelling from the portal at period end sends
      `customer.subscription.updated` and the listing shows **Cancelled, active
      until YYYY-MM-DD.** (`adapters/stripe/events.py:198-204`,
      `billing/owner_state.py:67-69`). If the portal is set to cancel
      **immediately**, Stripe sends `customer.subscription.deleted` and the
      listing goes to **Expired**, not Cancelled.
- [ ] **Keep my plan** in the portal (or reactivating the subscription) sends
      the opposite update, and the listing returns to **active** (a cancelled
      listing plus a successful payment is active, `billing/machine.py:150`).
- [ ] Watch for this at run time (it cannot be confirmed from the code): if a
      portal cancel changes `cancel_at` without flipping `cancel_at_period_end`,
      the adapter ignores it (`adapters/stripe/events.py:198`).

## 8. Cancel from OSDS

- [ ] As the owner, **Cancel plan** (the browser asks "Cancel your plan?"; the
      flash reads "Your plan is cancelled."). Stripe shows the subscription set
      to cancel at period end (`adapters/stripe/provider.py:105-106`); the
      listing shows **Cancelled, active until {date}**.
- [ ] Cancel from `grace` (after a failed payment has run its course; see the
      dunning note in step 6 for reaching grace): Stripe's subscription is
      **ended now**, not at period end (`billing/payments.py:253`), and the
      listing goes to **Expired**, not Cancelled (`billing/machine.py:147`,
      rule G3).
- [ ] Cancel from **past due**: the listing goes to **Cancelled**, not Expired
      (`billing/machine.py:141`, rule G3), and the Stripe subscription is set
      to cancel at period end.
- [ ] Cancel a subscription already deleted in the Dashboard: OSDS treats it as
      done and does not error. This only arises if the `.deleted` webhook was
      **not** processed. If it was, the listing is already Expired and the
      owner sees "There is no subscription to cancel."
      (`billing/payments.py:64`, raised at `:244`). Stop `stripe listen`
      before deleting the subscription in the Dashboard to test the first case.
- [ ] Delete the subscription in the Dashboard (with `stripe listen` running):
      `customer.subscription.deleted` arrives and the listing moves to
      **Expired** immediately (`billing/machine.py:161`, rule G7). The owner
      gets the "…is back on the free listing" mail (mail and worker required).

- [ ] Resubscribe from **Cancelled**. Upgrade is gated by status: active or
      trialing is refused ("already subscribed"), past due or grace is told to
      use the portal, and Cancelled can buy again (rule G2,
      `billing/machine.py:150`). With the listing Cancelled and its period not
      yet ended, the **Your plan** panel offers **Upgrade**; complete a
      purchase as in step 5 and the listing returns to **active**.

## 9. Refund

- [ ] Dashboard → Payments → the charge → **Refund** the full amount. In the
      stream: `charge.refunded` `200`, and the listing moves to **Expired**,
      with an `entitlement.expired` event (cause "refund") and a
      `billing.refund_issued` event (`billing/machine.py:160`). There is no
      "refunded" status.
- [ ] A **partial** refund is acknowledged and changes nothing
      (`adapters/stripe/events.py:239-240`).
- [ ] The refund never cancels the Stripe subscription, so Stripe keeps billing
      it, and the next `invoice.paid` is ignored as "no_entitlement"
      (`billing/reports.py:189-192`). Cancel the subscription in the Dashboard
      after this step.

## 10. Trial

If a tier has a trial (`trial_days`):

- [ ] Checkout shows the trial; a card is still collected.
- [ ] After completion the listing is **trialing**, with "Trial ends in N days".
      The first invoice is $0 and `invoice.paid` for it does **not** convert the
      trial.
- [ ] Advance the test clock past the trial end: a charge is made, `invoice.paid`
      arrives, and the listing is **active**. This relies on test clocks; see
      the caveat in step 6.

## 11. Plan change

- [ ] In the Dashboard change the subscription's price to the other configured
      tier. `customer.subscription.updated` arrives and the listing changes
      tier (`adapters/stripe/events.py:212-219`). Changing it to a price that is
      **not** in the Price ids setting returns no report and is acknowledged
      with nothing changed.

## 12. Afterwards

- [ ] No `sk_` or `whsec_` value appears in the application logs, the command
      log, or `tenant.settings_changed` events. Settings saves record field
      names, never values (`billing/settings_service.py:91`;
      `tenant.settings_changed` is `audit/events.py:113`).
- [ ] Stop `stripe listen`. Remove or roll the test endpoint if you created one.
      Cancel any leftover test subscription in the Dashboard.
- [ ] Note the Stripe account's API version (Dashboard → Developers) and the
      date you ran this, in the PR or release notes.
