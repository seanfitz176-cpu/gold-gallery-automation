# Checkpoint: Phase 2, publish step for bulk-created drafts

## What changed
- **New `publish` stage**, a script-only island modelled on `migration`. It's wired
  into every place migration needed: `config.py` (valid stages, scope,
  `PRODUCTION_WRITE_OPT_INS["publish"]` = `APP_ALLOW_PRODUCTION_PUBLISH`),
  `ebay_client.py` (both scope-upgrade stage sets, the allowlist entry for exactly
  `POST /offer/{id}/publish`, and `publish_mutation_guard`), `web.py` (minimal
  routes plus the permission flag) and `views.py` (dashboard boundary card,
  permission-upgrade button and OAuth handoff text). Startup reports
  "PUBLISH CHANGES ENABLED -- DRY RUN / LIVE WRITES".
- **Client:** `check_offer_publishable` (read-only re-check) and `publish_offer`
  (re-check, then a POST with no body, a required listing ID, and a readback that
  must show PUBLISHED with the same listing ID).
- **`ebay_app/bulk_publish.py`:** read-only plan (exactly one UNPUBLISHED Offer per
  SKU), dry run, actions budget, and outcomes `published`, `dry_run`, `failed`,
  `unknown`, `already_published`, `no_offer`, `ambiguous` and `skipped_budget`.
- **`bulk_publish_listings.py`:** reads the `sku` column of your upload CSV and shows
  the plan and the mode. You type `PUBLISH n` (or `REHEARSE n` in a dry run). It
  prints results, saves a results CSV, and exits 3 with an "ACTION NEEDED" list if
  any row is `unknown`.
- The gate hook now also blocks Claude from running `bulk_publish_listings.py`.
- Docs: `docs/publish_design.md`, README row, `.env.production.example` comment.

## Tests
`python3 -m unittest discover -s tests` gives **684/685**. The only failure is the
known `PROJECT_STATUS.md` check, which lives outside the repository. There are 17
new tests in `tests/test_publish_phase.py`, and `publish` was added to the existing
every-stage regression loops (scope upgrade offered, scope upgrade completed, OAuth
handoff text).

## Untested, and needs you
1. **The real response shape from eBay.** The code assumes eBay's documented
   `{"listingId": ...}` reply and an Offer readback showing `listing.listingId`. If
   either differs, rows report `unknown` (fails safe) and I'll adjust.
2. **Sandbox pilot**, in this order:
   - Bulk-upload 1–2 rows with images (stage `images`, as before).
   - Set `APP_FEATURE_STAGE=publish` in `.env`, start the app once, and click
     "Authorize Sandbox listing publishing" if it asks.
   - Run `python3 bulk_publish_listings.py pilot.csv` and type `PUBLISH 2`.
   - Check that the listings show up in Sandbox.
3. Production isn't touched. Going live is your own edit to `.env.production`:
   `APP_FEATURE_STAGE=publish` plus the opt-in. It stays a dry run until you also set
   `APP_DRY_RUN=false`.

Approve with `python3 .claude/ctl.py approve 2`, or just tell me "approved".
