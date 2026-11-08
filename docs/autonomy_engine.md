# Autonomy engine design (Phase 3)

The goal is for the account to run itself inside limits you set, with you
approving anything outside them and able to pause everything at any time.

Your decisions so far (2026-11-08):
- **Repricing:** a fixed margin over cost.
- **Relisting:** automatic when there is stock, otherwise flagged to you.
- **Alerts:** email.

## 1. Where it runs
`AppDatabase` takes an exclusive process lock, so a second process can't
open the database while the dashboard is running. The engine therefore runs
**inside the existing app process** (`run.py` / `run_production.py`) as one
background scheduler thread. It isn't a separate program. Stopping the app
stops the engine.

## 2. Jobs
| Job | Default cadence | Reads | May propose |
|---|---|---|---|
| Reprice | every 6 h | Offers, per-SKU cost table | price change |
| Relist / restock | every 1 h | Offers, Inventory quantities | quantity restore or republish |
| Order watch | every 30 min | Fulfillment orders | nothing; alerts only |
| Health check | every 6 h | token refresh, connection, error rate | nothing; alerts only |

Jobs only **propose** actions. Nothing is executed inside a job.

## 3. Proposals, policy, execution
1. **Proposal:** a row in a new `engine_proposals` table: SKU, kind,
   current value, proposed value, the reason in plain English, and a
   timestamp.
2. **Policy decision**, which is deterministic with no AI involved:
   - **auto** when every existing limit passes (per-SKU floor, max change
     %, actions per run) *and* the engine mode allows auto (see §6);
   - **queue** otherwise, for example when it's outside the limits, there's
     no cost recorded, the margin would fall below the floor, or the change
     is larger than the cap. The proposal waits for you.
3. **Execution** reuses the existing services (`PriceService`,
   `QuantityService`, publish), calling the same `prepare_review` →
   `confirm` methods with a fixed internal engine session. Every engine
   action is therefore journaled, drift-checked against live eBay data
   immediately before writing, bound by the dry-run switch and budget, and
   shown in the audit export, exactly like a manual action. No new write
   path to eBay is added.
4. **Outcome handling** is unchanged: `failed`, `partial` and `unknown`.
   An `unknown` result pauses the engine automatically and emails you.

## 4. Repricing: fixed margin over cost
- A new per-SKU **cost table** (unencrypted owner input, like
  `price_limits`): `sku`, `unit_cost`, optional `margin_percent` override,
  and an optional `include_postage` flag. It's editable on a dashboard page
  and importable from CSV.
- Target price = `unit_cost × (1 + margin%)`, rounded to the penny. Optional
  extras such as fees and postage depend on decision D2 below.
- The engine proposes a change when the live price differs from the target
  by more than a tolerance (default 1%). Each step is clamped to your max
  change %, so a large gap closes over several runs, never in one jump. It
  never goes below the floor.
- A SKU with **no cost recorded is never repriced.** It's listed once in a
  daily email instead.

## 5. Relisting when there's stock
- **Recommended setup:** turn on eBay's *out-of-stock option* for the account
  in Seller Hub. A GTC listing that hits zero then stays live but hidden,
  rather than ending. "Relisting" becomes a quantity restore through the
  existing `QuantityService`, a write that is already built, tested and
  gated.
- If a listing has actually ended, the engine queues a "republish" proposal
  for your approval, at least until the republish behaviour is confirmed in
  Sandbox. It never republishes an ended listing automatically.
- "Has stock" means the Inventory item's own quantity is above 0. The
  engine never invents stock.

## 6. Modes, pause and kill switches
`APP_ENGINE_MODE` has four modes, rolled out in this order:
1. `off`: the default, and today's behaviour.
2. `shadow`: jobs run and every proposal is queued. Nothing executes.
3. `auto-dry`: in-limit proposals "execute" with dry run forced on.
4. `auto`: in-limit proposals execute for real, still bound by
   `APP_DRY_RUN` and `APP_MAX_ACTIONS_PER_RUN`. It starts with a budget of 1.

Pausing works at three levels, and any one of them stops execution on the
next tick:
- a **"Pause engine" button** on the dashboard, which persists across
  restarts;
- an automatic pause on any `unknown` outcome, a failed token refresh, or 3
  consecutive eBay errors;
- `APP_DRY_RUN=true` or stopping the app entirely.

## 7. Approval queue
- A dashboard page, `/queue`, lists pending proposals with Approve, Reject
  and Approve all buttons.
- Approving runs the same execution path as auto, so the same checks apply.
- Proposals expire after 24 h, so stale prices are never applied.

## 8. Email alerts
- Uses the standard library's `smtplib` over TLS. The settings go in `.env`
  and are entered by you, never by Claude: `APP_SMTP_HOST`,
  `APP_SMTP_USER`, `APP_SMTP_APP_PASSWORD` and `APP_ALERT_EMAIL`. For iCloud
  that's `smtp.mail.me.com` with an app-specific password from your Apple
  Account settings.
- **Immediately:** an `unknown` outcome, an automatic pause, or a token or
  connection failure.
- **Digest:** proposals waiting for you, what was executed automatically,
  and SKUs with no cost. Sent at most every 6 h, and only if something
  changed.
- Emails contain SKUs, prices and counts only, never credentials or buyer
  details. Approvals happen in the local dashboard, not by replying to an
  email.

## 9. Build order (Phase 4)
1. Cost table, storage, dashboard page and CSV import.
2. Proposals table, scheduler thread, engine modes and the persistent pause.
3. Reprice job and policy decision (shadow mode only).
4. Approval queue page that executes through the existing services.
5. Relist/restock job.
6. Email alerts.
7. Order watch and health check.

Each step ships with tests and stays at `off` by default.

## 10. Decisions (answered and researched 2026-11-08)
- **D2. Cost** = item + postage + eBay fees. Research showed that UK private
  sellers pay **no** final value fee or regulatory fee (vehicles excepted).
  The buyer pays a Buyer Protection fee that never comes out of the seller's
  payout. So `fee_percent` **defaults to 0** for this account. It stays a
  per-SKU field so that a later switch to a business account, or a category
  with fees, can be represented. The one exception is a 3% international fee
  for a private seller delivering outside the UK, which isn't modelled
  because sales are domestic. Insertion fees (300 free listings a month,
  then 35p each) are per listing, not per sale, so they don't belong in the
  repricing formula. They do matter for the 300-listing bulk upload.
- **D3. Cost source:** entered or uploaded per SKU (`import_costs.py`).
- **D1. Margin: deliberately no global default.** The sources we found
  (reseller blogs quoting 20–50% for jewellery and resale) aren't a sound
  basis for one number across this account's mix. Instead:
  1. In `shadow` mode the engine reports each SKU's **implied current
     margin**: live price against item + postage, after fees.
  2. You then set `margin_percent` per SKU, or set
     `APP_ENGINE_DEFAULT_MARGIN_PERCENT`, from that real data.
  3. A SKU with no margin, from either source, is reported but **never
     repriced**.
  4. A **break-even guard** is always on, so the price never drops below
     (item + postage) / (1 − fee%).
- **D4. Cadence:** the eBay Inventory API default limit is 2 million calls a
  day, so API limits aren't a constraint at this account's size. With a
  fixed margin over a fixed cost, the target price **only changes when you
  edit the cost, margin or postage**. Repricing more often than daily
  achieves nothing and adds needless price changes. Defaults:
  `APP_ENGINE_REPRICE_HOURS=24`, plus one run straight after any cost
  import; relist check every 60 min (driven by sales); order watch every
  30 min.
- **Known limitation, recommended follow-up:** a fixed margin ignores the
  market. A target above comparable listings won't sell, and one below
  leaves money behind. The shadow report should later add a read-only
  comparison with similar live listings, using eBay's Browse API with the
  app token, so you can sanity-check each margin.

Sources: ebay.co.uk/help/selling/fees-credits-invoices/selling-fees?id=4364;
developer.ebay.com/develop/get-started/api-call-limits;
developer.ebay.com/api-docs/sell/static/inventory/bulk-updates.html.
