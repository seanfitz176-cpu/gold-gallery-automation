# GitHub Copilot repository instructions

This repository is a staged, local eBay **Sandbox** management application.
Preserve the existing modular Python design and safety boundaries.

## Mandatory working rules

1. Use eBay Sandbox by default. Do not add, accept, infer, or test Production
   credentials or Production endpoints unless the owner approves a separate
   Phase 9 design.
2. Never expose, echo, log, commit, fabricate, or request real credentials.
   This includes App IDs, Cert IDs, RuNames, access tokens, refresh tokens,
   passwords, encryption keys, Sandbox usernames, cookies, OAuth codes, and
   OAuth state values.
3. Never read or include `.env`, `instance/`, database files, logs, private
   keys, or customer/seller data in a prompt, diff, issue, test fixture, or
   commit.
4. Never perform a live eBay listing, price, stock, inventory, order,
   fulfillment, refund, image, or messaging change without explicit human
   approval immediately before that exact action.
5. Automated tests must use mocked transport and must not contact eBay.
6. Preserve working features and the modular Python structure. Do not rebuild
   the application, replace its architecture, or perform broad refactors unless
   there is a documented technical reason and prior approval.
7. Work on one controlled task at a time. Keep diffs narrowly scoped.
8. Run the complete relevant test suite before proposing changes. Report the
   exact commands, pass/fail totals, and any untested manual behavior.
9. Explain proposed and completed changes in plain English.
10. Stop safely when an unexpected error, ambiguous remote mutation, identity
    mismatch, scope mismatch, stale grant, storage error, or migration error
    occurs. Do not retry a potentially successful mutation automatically.
11. Ask for approval before destructive changes, migrations, data deletion,
    credential rotation, history rewriting, dependency upgrades with breaking
    changes, or removal of dormant/legacy files.
12. Keep Sandbox and any future Production credentials, endpoints, databases,
    callback URLs, sessions, and deployment configuration completely separate.
13. Do not weaken validation, CSRF, session binding, one-use capabilities,
    encryption, route allowlists, field minimization, mutation journaling, or
    fail-closed behavior to make a test pass.

## Current implementation boundary

- Phases 1–4 are implemented: connection verification, minimal account data,
  active Inventory API listings, and recent Fulfillment orders.
- Phase 5 is implemented: exact review and explicit confirmation for one
  unpublished Sandbox Inventory item and Offer.
- Phase 6 is implemented and live-verified: `PriceService` edits one
  published Offer's price via the same review/one-use-confirm/reconcile
  shape as Phase 5.
- Phase 7 is implemented and live-verified: `QuantityService` mirrors
  `PriceService` exactly for `availableQuantity`, cumulative over `prices`.
- Phase 8 is implemented and live-verified: `ImageService` adds exactly one
  seller-supplied, pre-validated image URL to an already-published listing
  (image-URL linking, not raw file upload -- eBay's Commerce Media
  upload/hosting API is very likely unsupported in Sandbox, so this phase
  was deliberately rescoped with explicit sign-off). `image_fetch.check_image_url`
  performs a live, read-only, HEAD-only check of the candidate URL before
  anything is stored; the one allowed write is a targeted PUT to the same
  `inventory_item` endpoint Phase 5 uses to create it. Cumulative over
  `quantities`.
- Double-submit confirmations (a genuine double-click, or a slow response
  the seller assumed had failed) always leave exactly one write, per the
  one-use claim invariant below; the losing submission's 409 error message
  is enriched (`AppDatabase.find_confirmed_action_id_by_token`, used by
  `WebApplication._confirmed_redirect_or_clear_error`) to say the token
  already completed, instead of the generic "expired, cancelled, or
  already used" wording -- never by redirecting or replaying the write.
  Applies uniformly to drafts/prices/quantities/images confirmation.
- Phase 9 (production preparation) has two slices landed, both dormant with
  respect to any real account: (1) `load_config()` can load
  `EBAY_ENVIRONMENT=production` only from a file literally named
  `.env.production` with an explicit `APP_ALLOW_PRODUCTION_ENVIRONMENT`
  opt-in, restricted to read-only feature stages; (2) a dedicated
  `run_production.py` entry point (separate from `run.py`, no `--env` flag)
  that only loads that same sibling file, plus a red "LIVE -- REAL EBAY
  ACCOUNT" banner stamped onto every page and a truthful `/health` when
  `config.environment == "production"`. No `.env.production` file exists in
  this repository, so none of this is reachable with a real account today.
  See `docs/phase9_production_design.md` and `docs/ROADMAP.md` item 9.
- Stock updates, bulk editing, templates, CSV import/export, reporting, and
  analytics are not implemented. Production write capability is not
  implemented and must not be added without a separately reviewed design.
- The internal `full` feature stage is for directly constructed test fixtures
  only. `load_config` must continue rejecting it.

Read `docs/ROADMAP.md` before changing feature boundaries.

## Invariants that require approval to change

- `EBAY_ENVIRONMENT=sandbox` enforcement and Production-key rejection (a
  real `EBAY_ENVIRONMENT=production` config may only ever load from a file
  literally named `.env.production` with its explicit
  `APP_ALLOW_PRODUCTION_ENVIRONMENT` opt-in, restricted to read-only
  feature stages, and `run.py` must never be the thing that makes it
  reachable without a separately reviewed production entry point).
- Minimum fixed OAuth scopes per phase.
- Same-seller checks on scope upgrades.
- Refresh-token encryption and memory-only access tokens.
- Session-bound, expiring, one-use OAuth state and confirmations.
- Loopback-only Python server.
- Stable Worker callback with one fixed redirect to
  `http://127.0.0.1:5000/oauth/callback`.
- Worker Preview URLs, persisted logs, traces, exports, and Cloudflare Access
  remain disabled.
- No raw eBay response, buyer PII, seller contact data, or credential display.
- No automatic mutation retry when the result is uncertain.
- No Phase 5 publish, delete, fulfillment, or refund route; no image upload
  or hosting of raw image bytes at any stage (only a pre-validated external
  URL is ever sent to eBay).

## Test commands

Run from the repository root:

```sh
python -m unittest discover -s tests -v
python -m pip check
```

Run the Worker tests separately:

```sh
cd oauth_relay_worker
node --test test/index.test.mjs
```

Do not claim a feature is complete only because mocked tests pass. Clearly list
any browser, Cloudflare, OAuth, or Sandbox behavior that still requires manual
verification.

## Repository hygiene

- `.env.example` may contain variable names and unmistakably fake placeholders
  only.
- `.env`, `instance/`, virtual environments, databases, caches, logs, key files,
  and certificates must remain ignored.
- Use generic fixtures. Never copy real account identifiers, callback
  subdomains, local usernames, absolute home-directory paths, or raw Sandbox
  responses into source or tests.
- Do not delete dormant or legacy files automatically. Document callers,
  coverage, replacement behavior, and an archive/removal recommendation first.
