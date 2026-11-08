#!/usr/bin/env python3
"""Bulk-create many UNPUBLISHED Inventory items/Offers from a CSV file.

WHY THIS EXISTS: see ``ebay_app/bulk_listings.py``'s own module docstring
for the full account. In short: this reuses the exact same, already-tested,
already-live-verified per-item create+verify pipeline ``DraftService``'s
dashboard confirm step already uses for one item at a time -- this script
just drives it once per CSV row instead of once per web-form submission.
Every row gets the identical pre-write existence check and full post-write
field-by-field reconciliation against eBay's own stored data a single
hand-created draft already gets.

THIS SCRIPT NEVER PUBLISHES ANYTHING. Every item it creates is an
UNPUBLISHED Offer -- there is no code path anywhere in this app that can
publish, and that remains a separate, deliberate future decision. Images
are optional: an ``image_urls`` column of up to 24 https addresses separated
by ``|`` is sent with each Inventory item and checked exactly on readback.

CSV FORMAT: a header row naming (case-insensitively, any order) sku, title,
description, condition, quantity, price, category_id, and optionally
aspects (a JSON object, e.g. {"Brand": ["Acme"]}) and image_urls
(https://.../a.jpg|https://.../b.jpg, first = main picture). SKUs are used exactly as
given -- no automatic suffix is added, unlike the dashboard's own one-off
draft form. Every row uses the account's single configured currency.

Usage:
    python3 bulk_upload_listings.py <csv_path>               # Sandbox (.env)
    python3 bulk_upload_listings.py <csv_path> --production    # Production (.env.production)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

from ebay_app.bulk_listings import BulkListingService, load_and_validate_csv
from ebay_app.config import ConfigError, load_config
from ebay_app.ebay_client import EbayError, EbaySandboxClient
from ebay_app.storage import AppDatabase, StorageError

PRODUCTION_ENV_PATH = Path(__file__).resolve().parent / ".env.production"


def _parse_csv_path(argv: List[str]) -> str | None:
    positional = [arg for arg in argv if not arg.startswith("--")]
    return positional[0] if positional else None


def main(argv: List[str]) -> int:
    use_production = "--production" in argv
    csv_arg = _parse_csv_path(argv)
    if not csv_arg:
        print(__doc__)
        return 2
    csv_path = Path(csv_arg)
    if not csv_path.is_file():
        print(f"CSV file not found: {csv_path}")
        return 2

    database = None
    try:
        config = load_config(PRODUCTION_ENV_PATH) if use_production else load_config()
        if use_production and config.environment != "production":
            print(
                "This script only runs against .env.production with --production -- "
                "it refuses to touch a Sandbox account by accident."
            )
            return 2

        try:
            batch = load_and_validate_csv(csv_path, config.currency)
        except EbayError as error:
            print(f"Could not read the CSV file: {error}")
            return 2

        if batch.row_errors:
            print(f"{len(batch.row_errors)} row(s) failed local validation and will be SKIPPED:\n")
            for row_error in batch.row_errors:
                detail = "; ".join(f"{field}: {message}" for field, message in row_error.errors.items())
                print(f"  Line {row_error.row_number} (sku={row_error.sku_hint!r}): {detail}")
            print()

        if not batch.drafts:
            print("No valid rows to create. Fix the CSV and try again.")
            return 1

        print(f"{len(batch.drafts)} valid row(s) ready to create as UNPUBLISHED drafts in "
              f"{config.environment.capitalize()}:\n")
        for row_number, draft in batch.drafts:
            print(f"  Line {row_number}: SKU={draft['sku']!r}  {draft['title']!r}  "
                  f"{draft['currency']} {draft['price']}  qty={draft['quantity']}  "
                  f"images={len(draft.get('image_urls') or [])}")
        print(
            "\nThese become real Inventory items/Offers on your account, but none of them "
            "are published or visible to buyers -- every one is UNPUBLISHED, exactly like a "
            "draft created one at a time through the dashboard. Publishing is a separate, "
            "later step this script does not perform."
        )

        database = AppDatabase(
            config.database_path,
            config.data_encryption_key,
            environment=config.environment.capitalize(),
        )
        client = EbaySandboxClient(config, database)
        service = BulkListingService(client, database, config)
        try:
            binding = service.verified_binding()
        except EbayError as error:
            print(f"\nCannot proceed: {error}")
            return 1

        try:
            confirm = input(
                f"\nType CREATE {len(batch.drafts)} to proceed, or anything else to cancel: "
            ).strip()
        except EOFError:
            confirm = ""
        if confirm != f"CREATE {len(batch.drafts)}":
            print("Confirmation did not match -- nothing was done.")
            return 1

        print("\nCreating...\n")
        results = service.create_batch(binding, batch.drafts)

        created = [r for r in results if r.status == "created"]
        skipped = [r for r in results if r.status == "skipped_already_exists"]
        failed = [r for r in results if r.status == "failed"]
        partial = [r for r in results if r.status == "partial"]
        unknown = [r for r in results if r.status == "unknown"]

        print(f"{'Line':>5} {'SKU':<28} {'Status':<24} {'Offer ID':<18} Detail")
        print("-" * 110)
        for result in results:
            print(
                f"{result.row_number:>5} {result.sku:<28} {result.status:<24} "
                f"{(result.offer_id or ''):<18} {result.detail}"
            )

        print(
            f"\n{len(created)} created, {len(skipped)} skipped (already existed), "
            f"{len(failed)} failed, {len(partial)} partial, {len(unknown)} unknown."
        )
        if partial or unknown:
            print(
                "\n*** ACTION NEEDED: one or more rows above are 'partial' or "
                "'unknown' -- these are NOT the same as 'failed' and re-running this "
                "script will NOT fix or retry them. Check each of the following SKUs "
                "directly in Seller Hub or via the API before doing anything else "
                "with them: " + ", ".join(sorted({r.sku for r in partial + unknown})) +
                " ***"
            )
        if failed:
            print(
                "\nAny plain 'failed' row above made no eBay write at all -- fix the "
                "underlying issue (shown in its Detail column) and re-run this script "
                "with the same CSV; already-created and already-existing rows will "
                "simply be skipped again on the next pass, since this script never "
                "overwrites an existing SKU."
            )
        return 1 if (failed or partial or unknown) else 0
    except (ConfigError, StorageError, EbayError) as error:
        print(f"Could not run the bulk upload: {error}")
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
