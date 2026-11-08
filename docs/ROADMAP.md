# Development roadmap

Only one stage is enabled at a time. A stage advances after its live Sandbox result is demonstrated and its failure cases are covered by mocked tests.

1. **Complete: Verify API connection** — identity-only OAuth, token refresh and Identity read.
2. **Complete: Retrieve Sandbox account information** — display only account type and registration marketplace; omit identifiers and contact data.
3. **Complete: Display active listings** — add exactly `commerce.identity.readonly` plus `sell.inventory.readonly`, safely upgrade the existing OAuth grant, and paginate Inventory items and offers. Display only `PUBLISHED` offers whose listing status is `ACTIVE` or `OUT_OF_STOCK`. This stage reads Inventory API-managed listings only; older Trading API or Seller Hub listings may require migration before they appear.
4. **Complete: Display orders** — add `sell.fulfillment.readonly` to the cumulative read-only grant, safely re-consent with the same seller, and paginate recent Fulfillment orders using row offsets. Display only order ID, creation time, mapped statuses, tax-inclusive total, currency and line-item count. Discard buyer identity, contact/delivery details, notes, item details, payment/refund/tracking data, tax breakdowns and raw responses.
5. **Complete: Create unpublished offers** — replace Inventory read-only with `sell.inventory`, preserve earlier reads, add a random app-owned Sandbox SKU suffix, perform a read-only preflight, show the exact payload/settings, and require a seller-bound, session-bound, CSRF-protected, five-minute one-use confirmation. Confirmation creates and reads back one Inventory item, creates and reads back one Offer, then proves the Offer is `UNPUBLISHED` and has no listing ID. There is no publish, delete, cleanup or automatic mutation retry.
6. **Complete: Edit listing prices** — a dedicated `PriceService` mirrors
   Phase 5's review/one-use-confirm/reconcile shape, proportionate to a single
   mutation: `change_price()` re-reads the Offer, re-verifies its identity/SKU/
   status and compares the live price against the one shown at review time
   immediately before the one allowed write, `bulk_update_price_quantity`.
   Reuses the Phase 5 Inventory-write scope, so no new OAuth consent is needed.
   Only a published Offer's "Review price" control on `/listings` can reach it.
7. **Complete: Update inventory quantities** — a dedicated `QuantityService`
   mirrors Phase 6's `PriceService` shape exactly: `change_quantity()` re-reads
   the Offer, re-verifies its identity/SKU/status and compares the live
   `availableQuantity` against the one shown at review time immediately before
   the one allowed write, the same `bulk_update_price_quantity` endpoint
   Phase 6 uses with a different field. Reuses the Phase 5 Inventory-write
   scope, so no new OAuth consent is needed. The `quantities` stage is
   cumulative over `prices` (both earlier capabilities stay reachable).
   Only a published Offer's "Review quantity" control on `/listings` can
   reach it. Live-verified in Sandbox: a quantity change on the same published
   test Offer used for Phase 6 (`11743059010`) succeeded and was confirmed
   through the app's own result page.
8. **Complete: Add listing images** — eBay's actual image-hosting API
   (Commerce Media, `createImageFromFile`/`createImageFromUrl`) is very
   likely unsupported in Sandbox, so this phase was deliberately rescoped
   (with explicit sign-off) to image-URL linking instead of file upload: the
   seller pastes a link to an already-hosted image. A dedicated
   `ImageService` mirrors Phase 7's `QuantityService` shape: `prepare_review()`
   performs a live, read-only check of the candidate URL
   (`image_fetch.check_image_url` — https-only, rejects loopback/private/
   link-local/reserved/multicast hosts, HEAD-only so the image bytes are
   never downloaded by this application, requires an allowlisted image
   Content-Type and a Content-Length at or under eBay's documented 12&nbsp;MB
   per-picture limit), then re-reads the Offer and Inventory item. Only the
   separate one-use confirmation calls `add_inventory_item_image()`, which
   re-verifies the Offer and re-compares the listing's live image list
   against the one shown at review time immediately before the one allowed
   write — a targeted PUT to the same `inventory_item` endpoint Phase 5
   already uses to create it, appending exactly one image URL. Reuses the
   Phase 5 Inventory-write scope, so no new OAuth consent is needed. The
   `images` stage is cumulative over `quantities` (draft creation, price
   changes and quantity changes all stay reachable). Only a published
   Offer's "Review image" control on `/listings` can reach it. Live-verified
   in Sandbox: an image URL was added to the same published test Offer used
   for Phases 6/7 (`11743059010`) and confirmed through the app's own result
   page; independently re-verified because the very next review of that
   same Offer correctly detected the URL already present via a fresh,
   live read of the Inventory item and refused to add it twice
   (`IMAGE_ALREADY_ATTACHED`), proving both the write and the
   drift/duplicate check work against real Sandbox data. One operational
   note from testing: `image_fetch.check_image_url` now sends a
   descriptive `User-Agent` header on its HEAD request, because some image
   hosts (Wikimedia among them) reject the default urllib one as a basic
   anti-bot measure; separately, Wikimedia's on-the-fly `/thumb/` resizing
   paths return 400 for HEAD requests specifically (only the original,
   already-rendered file path supports HEAD) -- a host quirk, not an app
   bug, worth knowing when picking a test image URL. A second, separate
   operational note also came out of that same live session: two
   near-simultaneous `/actions/confirm` submissions for the one-use token
   (a genuine double-click, or a slow first response the seller assumed
   had failed) always leave exactly one write and one journaled result --
   `claim_pending_action`'s atomic claim guarantees that -- but the losing
   submission used to get the same generic "expired, cancelled, or already
   used" wording as a stale or forged token, which read as if the click had
   done nothing. `find_confirmed_action_id_by_token` (storage.py) now lets
   the web layer recognise, for the *same* token in the *same* session,
   that it already reached a real terminal result, and the 409 error it
   raises says so plainly and points at the dashboard's recent completed
   confirmations -- without ever turning the duplicate submission into a
   second write, a redirect, or a silent success. A copied token replayed
   from a different session still gets the original, unenriched message,
   and a cancelled or expired token is never described as "already
   completed". Covered by a dedicated assertion in each phase's full-stack
   double-submit test (drafts/prices/quantities/images).
9. **In progress: Production preparation** — separate credentials/storage,
   explicit environment indicators, stronger deployment controls and a
   production readiness review. Design at `docs/phase9_production_design.md`.
   First slice landed: `load_config()` can now load a real `EBAY_ENVIRONMENT=production`
   configuration, but only through three independent, structural gates, all
   enforced in `ebay_app/config.py` -- (1) the credentials file must be
   literally named `.env.production`, never the ordinary `.env`; (2) that
   file must contain an explicit typed opt-in
   (`APP_ALLOW_PRODUCTION_ENVIRONMENT=yes-i-understand-the-risk`); (3) once
   in, `APP_FEATURE_STAGE` is restricted to the read-only stages
   (`connection`/`account`/`listings`/`orders`) -- `drafts`/`prices`/
   `quantities`/`images`/`setup` are rejected outright, with no flag able to
   override that yet. A production config also gets eBay's real endpoints
   (`api.ebay.com` etc., a new `AppConfig.environment` field and matching
   `api_origin`/`identity_base_url`/`consent_url`/`token_url`), the existing
   Sandbox-App-ID/Cert-ID shape checks flip direction for production
   (Sandbox-shaped credentials are now rejected, not required), and a
   same-directory Sandbox `.env`, if present, is read-only compared against
   to fail closed if the production database path or encryption key would
   collide with it. Critically, **none of this is reachable from the actual
   running application yet**: `run.py`'s `main()` always calls `load_config()`
   with no arguments, which always resolves to a file literally named
   `.env` next to the repo root -- never `.env.production` -- regardless of
   what any file's contents say. This is deliberately dormant, exactly like
   the pre-existing `full` feature stage, until a dedicated production entry
   point and the UI environment banners (the rest of the design doc) are
   built and reviewed as their own next slice. Covered by 16 new tests in
   `tests/test_config_validation.py` (`ProductionEnvironmentConfigTests`),
   including a regression test pinning that `run.py` cannot reach it.

   Second slice landed: a dedicated `run_production.py` entry point (never a
   `--env` flag on `run.py`, consistent with the existing one-permanent-
   configuration philosophy) that only ever loads a sibling file literally
   named `.env.production`, accepts no startup arguments, and refuses to
   start if the resulting config does not itself come back as
   `environment == "production"` (defense in depth on top of `config.py`'s
   own gates). Every page now gets an unmissable, high-contrast red banner
   ("LIVE -- REAL EBAY ACCOUNT") whenever `config.environment == "production"`,
   applied at the single WSGI response chokepoint in
   `WebApplication.__call__` -- covering every page and every error page
   alike, with no per-page wiring to forget -- rather than threaded through
   each of the ~20 individual page-rendering functions. `/health` now
   reports the real configured environment instead of a hardcoded
   `"sandbox"`. Covered by 10 new tests in
   `tests/test_production_environment.py`. **Still nothing is reachable
   with a real account**: no `.env.production` exists anywhere in this
   repository, `run_production.py` correctly refuses to start without one,
   and production write capability still does not exist in this codebase at
   all. All 329 tests pass; every pre-existing Sandbox test is unchanged and
   still passes untouched.

Bulk editing, reusable templates, CSV import/export, reporting and analytics are
not separate completed phases and have not been implemented. They should be
planned only after the controlled single-record operations are proven in
Sandbox.

Phase 5 permits only the two explicitly confirmed Sandbox creation calls needed for an unpublished Offer. No GET request changes eBay. Publishing, fulfilment, refunds, image uploads, price changes, quantity changes, updates and deletion remain locked. Ambiguous writes are journaled for reconciliation and never retried automatically.
