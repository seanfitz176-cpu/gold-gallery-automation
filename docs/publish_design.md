# Publish step design (Phase 2)

## Purpose
`bulk_upload_listings.py` creates UNPUBLISHED Inventory items and Offers.
Nothing is visible to buyers until each Offer is published. This step does
that, and it's gated as heavily as every other write in the app.

## Gating
| Layer | What it does |
|---|---|
| Feature stage | `APP_FEATURE_STAGE=publish`. This is its own island, like `migration`, and isn't cumulative with prices/quantities/images. An island means only a few places had to change, and trust earned elsewhere never unlocks publishing by accident. |
| Production opt-in | `APP_ALLOW_PRODUCTION_PUBLISH=yes-i-understand-the-risk-of-publishing-live-listings`, on top of `APP_ALLOW_PRODUCTION_ENVIRONMENT`. No other stage's opt-in unlocks it (tested). |
| OAuth scope | Same `sell.inventory` write scope as drafts, so no new consent screen is needed. |
| Mutation allowlist | Only `POST /sell/inventory/v1/offer/{offerId}/publish`, in the `publish` stage, in local mode. Withdraw, delete and bulk publish all stay locked. |
| Guard | `publish_mutation_guard()` serialises the batch under the account lock. |
| Dry run | `APP_DRY_RUN`, which defaults to true in production. Each Offer is re-read and re-verified exactly as in a real run, but nothing is posted. |
| Budget | At most `APP_MAX_ACTIONS_PER_RUN` Offers are attempted per run, real or dry run. The rest are `skipped_budget`. |
| Confirmation | You type `PUBLISH <n>`, or `REHEARSE <n>` in a dry run, where n is the exact number this run will attempt. |

## Per-Offer sequence
1. **Plan (read-only):** `list_offers(sku)`. The SKU must have exactly one
   Offer, and it must be UNPUBLISHED.
2. **Re-check immediately before writing:** `get_offer(offerId)`. It must
   still have the same SKU and still be UNPUBLISHED. If it's already
   published it's reported as such and never posted.
3. **Publish:** POST with no body. A non-empty `listingId` is required.
4. **Readback:** `get_offer(offerId)` must show `status=PUBLISHED` and the same
   `listing.listingId`.

## Outcomes
| Status | Meaning | What to do |
|---|---|---|
| `published` | Steps 3 and 4 both confirmed | Nothing |
| `dry_run` | Re-verified, nothing sent | Rerun with `APP_DRY_RUN=false` when ready |
| `failed` | eBay cleanly rejected it with a 4xx response, e.g. a missing item specific or no picture. eBay's own error detail is shown. | Fix and rerun. It's safe to rerun because nothing went live. |
| `unknown` | The response was lost or a 5xx came back, there was no listing ID, or the readback didn't confirm | **The listing may be live.** Check Seller Hub before anything else. |
| `already_published`, `no_offer`, `ambiguous` | Found at planning or re-check time | Nothing changed |
| `skipped_budget` | Budget used up | Rerun |

Nothing is retried automatically, and one row's failure never stops the
others. Each run writes a results CSV next to the input file.

## Not built (deliberately)
- **Journaling into the encrypted action table.** That would need a new
  action kind and a schema migration. The results CSV is the record for
  now, and wiring publish into the audit log can come with the autonomy
  engine.
- **Withdraw or end listing.** No path exists to take a listing back down.
