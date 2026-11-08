# Checkpoint: Phase 1, images on bulk-created listings

## What changed
- `ebay_app/bulk_listings.py` adds an optional `image_urls` CSV column: 0–24 https
  addresses separated by `|`, with the first one as the main picture. Each address is
  checked structurally only: https, no username or password, no `#fragment`, not
  localhost or a private IP address, no spaces, at most 2000 characters, and no
  duplicates. Loading the CSV still makes no network call. Image errors and other
  field errors on the same row are reported together.
- `ebay_app/ebay_client.py`: `_inventory_payload` sends `product.imageUrls` when a
  draft has images, and `verify_inventory_item` requires them back exactly, in the
  same order. A mismatch reports the row as **unknown**, never as created. Dashboard
  drafts never carry images, so their payload and verification are unchanged.
- `bulk_upload_listings.py`: the pre-flight report shows each row's image count.
- README updated.

## Workflow added (same change set)
- `.claude/settings.json`, `.claude/hooks/gate.py`, `.claude/hooks/stop_gate.py`,
  `.claude/ctl.py`, `PLAN.md` and `tests/test_claude_hooks.py` (12 tests).

## Tests
`python3 -m unittest discover -s tests` gives **667/668 passing**. The one failure is
`test_project_documents_exist_and_have_no_absolute_home_paths`, because
`PROJECT_STATUS.md` lives outside the repository folder. It fails the same way on the
untouched GitHub copy and isn't caused by this change.

## Untested, and needs you
1. **Whether eBay stores image URLs exactly as sent.** If eBay rewrites them on the
   Inventory item before publishing, every row with images will come back `unknown`.
   That fails safe, but the check would then need relaxing. Only a real Sandbox run
   can tell.
2. **Pilot:** make a 1–2 row CSV with images hosted somewhere public, run
   `python3 bulk_upload_listings.py pilot.csv` (Sandbox), and confirm the rows come
   back `created`.

Approve with `python3 .claude/ctl.py approve 1` once you're happy.
