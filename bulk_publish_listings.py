"""Publish UNPUBLISHED Offers created by bulk_upload_listings.py.

Reads the ``sku`` column of a CSV (the same file you uploaded is fine),
looks up each SKU's Offer (read-only), shows you exactly what it would do,
then -- after you type the exact confirmation -- publishes them one at a
time, re-verifying each Offer immediately before its own publish call and
reading it back afterwards.

DRY RUN: when APP_DRY_RUN is true (the default in production), every Offer
is re-verified exactly as a real run would, but nothing is sent to eBay.
BUDGET: at most APP_MAX_ACTIONS_PER_RUN Offers are attempted per run.

Requires APP_FEATURE_STAGE=publish. In production it also requires
APP_ALLOW_PRODUCTION_PUBLISH=yes-i-understand-the-risk-of-publishing-live-listings
and an OAuth grant with Inventory write permission (reconnect from the
dashboard if this reports a permission error).

A results CSV is written next to the input file so you keep a record.

Usage:
    python3 bulk_publish_listings.py <csv_path>                # Sandbox (.env)
    python3 bulk_publish_listings.py <csv_path> --production   # Production (.env.production)
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from ebay_app.bulk_publish import BulkPublishService, load_skus, summarize
from ebay_app.config import ConfigError, load_config
from ebay_app.ebay_client import EbayError, EbaySandboxClient
from ebay_app.storage import AppDatabase, StorageError

PRODUCTION_ENV_PATH = Path(__file__).resolve().parent / ".env.production"


def main(argv: List[str]) -> int:
    use_production = "--production" in argv
    positional = [arg for arg in argv if not arg.startswith("--")]
    if not positional:
        print(__doc__)
        return 2
    csv_path = Path(positional[0])
    if not csv_path.is_file():
        print(f"CSV file not found: {csv_path}")
        return 2

    database = None
    try:
        config = load_config(PRODUCTION_ENV_PATH) if use_production else load_config()
        if use_production and config.environment != "production":
            print("This script only runs against .env.production with --production.")
            return 2
        try:
            skus = load_skus(csv_path)
        except EbayError as error:
            print(f"Could not read the CSV file: {error}")
            return 2
        if not skus:
            print("No SKUs found in the CSV file.")
            return 1

        database = AppDatabase(
            config.database_path,
            config.data_encryption_key,
            environment=config.environment.capitalize(),
        )
        client = EbaySandboxClient(config, database)
        service = BulkPublishService(client, database, config)
        try:
            binding = service.verified_binding()
            print(f"Looking up {len(skus)} SKU(s) in {config.environment.capitalize()} (read-only)...\n")
            planned = service.plan(skus)
        except EbayError as error:
            print(f"Cannot proceed: {error}")
            return 1

        ready = [p for p in planned if p.status == "ready"]
        print(f"{'SKU':<28} {'Offer ID':<20} {'Status':<18} Detail")
        print("-" * 100)
        for item in planned:
            print(f"{item.sku:<28} {(item.offer_id or ''):<20} {item.status:<18} {item.detail}")
        if not ready:
            print("\nNothing to publish.")
            return 0

        budget = config.max_actions_per_run
        to_attempt = min(len(ready), budget)
        mode = (
            "DRY RUN -- nothing will be sent to eBay"
            if config.dry_run
            else f"LIVE -- {to_attempt} listing(s) will become VISIBLE TO BUYERS in "
                 f"{config.environment.capitalize()}"
        )
        print(f"\n{len(ready)} Offer(s) ready. This run will attempt {to_attempt} "
              f"(APP_MAX_ACTIONS_PER_RUN={budget}).")
        print(f"MODE: {mode}\n")
        word = "REHEARSE" if config.dry_run else "PUBLISH"
        try:
            confirm = input(f"Type {word} {to_attempt} to proceed, or anything else to cancel: ").strip()
        except EOFError:
            confirm = ""
        if confirm != f"{word} {to_attempt}":
            print("Confirmation did not match -- nothing was done.")
            return 1

        print()
        results = service.publish_batch(binding, planned)
    except (ConfigError, StorageError) as error:
        print(f"Could not start: {error}")
        return 1
    finally:
        if database is not None:
            database.close()

    print(f"{'SKU':<28} {'Status':<18} {'Listing ID':<16} Detail")
    print("-" * 110)
    for result in results:
        print(f"{result.sku:<28} {result.status:<18} {(result.listing_id or ''):<16} {result.detail}")
    print("\nSummary: " + ", ".join(f"{name}={count}" for name, count in summarize(results) if count))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = csv_path.with_name(f"{csv_path.stem}.publish-results-{stamp}.csv")
    with out_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["sku", "offer_id", "status", "listing_id", "detail"])
        for r in results:
            writer.writerow([r.sku, r.offer_id or "", r.status, r.listing_id or "", r.detail])
    print(f"Results saved to {out_path}")

    unknown = [r for r in results if r.status == "unknown"]
    if unknown:
        print("\nACTION NEEDED -- these listings MAY be live. Check each in Seller Hub before "
              "doing anything else:")
        for r in unknown:
            print(f"  {r.sku} (Offer {r.offer_id})")
        return 3
    return 1 if any(r.status == "failed" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
