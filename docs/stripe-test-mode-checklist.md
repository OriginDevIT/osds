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
      claimed listing whose owner you can sign in as.
- [ ] `stripe login` is done and the CLI is in **test mode**.
- [ ] Note the tier's amount, currency and interval. You will make the Stripe
      Price match it exactly.

## 1. Create the prices

For each purchasable tier (here, `featured` at 49.00 USD per month):

```powershell
stripe products create --name "Featured"
stripe prices create --product prod_XXXX --unit-amount 4900 --currency usd -d "recurring[interval]=month"
```

- [ ] The command prints a `price_…` id for each tier.
- [ ] Amount, currency and interval equal the tier's in OSDS. A yearly tier is
      `recurring[interval]=year`. An interval count other than 1 is refused.

## 2. Configure the adapter

In the console, **Settings → Payments**:

- [ ] **Secret key**: your `sk_test_…` (or a restricted `rk_test_…` key that can
      write Checkout Sessions, Billing Portal Sessions and Subscriptions, and
      read Prices, Subscriptions and Customers).
- [ ] **Price ids**: `featured=price_XXXX, verified=price_YYYY`.
- [ ] A value in the wrong shape (a `pk_test_…` key, `featured=prod_…`) is refused
      with "not in the expected format" and the page does not echo it.
- [ ] The page shows this directory's webhook URL:
      `https://<your host>/_adapters/stripe/inbound/`.

## 3. Webhook signing secret

Choose one.

**Forwarding (local, no public URL):**

```powershell
stripe listen --forward-to https://acme.localhost/_adapters/stripe/inbound/ --skip-verify
```

The CLI prints `Ready! Your webhook signing secret is whsec_…`. That secret is
specific to this `listen` session.

**A real endpoint (a public URL):** Dashboard → Developers → Webhooks → Add
endpoint, with the URL above and these events:
`checkout.session.completed`, `invoice.paid`, `invoice.payment_failed`,
`customer.subscription.updated`, `customer.subscription.deleted`,
`charge.refunded`. Reveal and copy the endpoint's `whsec_…`.

- [ ] Paste it into **Webhook signing secret** and save. The page now says
      payments are available.
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
      change, the listing does not change twice (the command log shows a
      replay).
- [ ] Remove the webhook secret setting entirely: a delivery returns **503**.
      Restore it.

## 5. Purchase

Sign in as the listing's owner and open the listing's billing page.

- [ ] **Upgrade** redirects to a Stripe Checkout page. The page shows the right
      plan and price, and the owner's email.
- [ ] Pay with `4242 4242 4242 4242`, any future expiry, any CVC.
- [ ] The owner returns to OSDS and sees the "confirmation may take a moment"
      page. In `stripe listen`: `checkout.session.completed`,
      `invoice.paid`, others, all `200`.
- [ ] The listing is now on the tier, **active**, with a renewal date a month
      out. The operator console shows the entitlement with a `sub_…` reference.
- [ ] Repeat **Upgrade** with a Price whose amount differs from the tier's (edit
      the setting to point at the wrong price): OSDS refuses with a plain
      message and creates no session. Restore it.

## 6. Failed payment

- [ ] Start a checkout and pay with `4000 0000 0000 0341` (attaches, then
      fails on the first charge): the listing does not receive the tier.
- [ ] For a renewal failure, buy normally, then in the Dashboard open the
      subscription's customer and replace the card with `4000 0000 0000 0341`.
      Advance the clock (Dashboard → Test clocks) one billing period.
      `invoice.payment_failed` arrives; the listing becomes **past due**, the
      owner sees "Payment failed, update card" and receives the mail, and the
      portal link works.
- [ ] Fix the card in the portal. Stripe retries and `invoice.paid` arrives; the
      listing returns to **active**.

## 7. Customer portal

Dashboard → Settings → Billing → Customer portal (test mode): enable
cancellation and payment-method updates.

- [ ] The owner's **Update card** link opens the portal for the right customer
      and returns to OSDS afterwards.
- [ ] Cancelling from the portal sends `customer.subscription.updated` and the
      listing shows **Cancelled, active until {date}**.
- [ ] **Keep my plan** in the portal (or reactivating the subscription) sends
      the opposite update, and the listing returns to **active**.

## 8. Cancel from OSDS

- [ ] As the owner, **Cancel**. Stripe shows the subscription set to cancel at
      period end; the listing shows **Cancelled, active until {date}**.
- [ ] Cancel from `grace` (after a failed payment has run its course): Stripe's
      subscription is **ended now**, not at period end.
- [ ] Cancel a subscription already deleted in the Dashboard: OSDS treats it as
      done and does not error.
- [ ] Delete the subscription in the Dashboard: `customer.subscription.deleted`
      arrives and the listing moves to **canceled** immediately.

## 9. Refund

- [ ] Dashboard → Payments → the charge → **Refund** the full amount. In the
      stream: `charge.refunded` `200`, and the listing's entitlement is
      refunded.
- [ ] A **partial** refund is acknowledged and changes nothing.

## 10. Trial

If a tier has a trial (`trial_days`):

- [ ] Checkout shows the trial; a card is still collected.
- [ ] After completion the listing is **trialing**, with "Trial ends in N days".
      The first invoice is $0 and `invoice.paid` for it does **not** convert the
      trial.
- [ ] Advance the test clock past the trial end: a charge is made, `invoice.paid`
      arrives, and the listing is **active**.

## 11. Plan change

- [ ] In the Dashboard change the subscription's price to the other configured
      tier. `customer.subscription.updated` arrives and the listing changes
      tier. Changing it to a price that is **not** in the Price ids setting is
      acknowledged and changes nothing.

## 12. Afterwards

- [ ] No `sk_` or `whsec_` value appears in the application logs, the command
      log, or `tenant.settings_changed` events.
- [ ] Stop `stripe listen`. Remove or roll the test endpoint if you created one.
- [ ] Note the Stripe account's API version (Dashboard → Developers) and the
      date you ran this, in the PR or release notes.
