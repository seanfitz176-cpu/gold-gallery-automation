#!/usr/bin/env python3
"""Read-only pilot-SKU candidate finder.

Lists every PUBLISHED, active listing (via ListingService -- the exact same
read the dashboard's own "Active listings" page already performs) sorted by
price ascending, to help pick a low-stakes SKU for the supervised dry-run
pilot described in claude/supervised_dry_run_plan.md.

IMPORTANT LIMITATION, found while building this: this app's OrderService
deliberately minimizes order data for privacy (see ebay_app/orders.py) --
a RecentOrder record has no per-SKU breakdown at all, only an order-level
line-item COUNT. Real sales velocity per SKU cannot be computed from data
this app already has. This script therefore ranks by price alone, and
shows available_quantity as a loose, non-authoritative hint only -- a SKU
with unusually high stock MIGHT be slower-moving, but this is not a real
velocity measurement. For actual sales-velocity information, check eBay
Seller Hub directly, or your own records.

Makes the exact same read-only eBay API call ListingService.retrieve()
already makes for the dashboard's "Active listings" page -- no new
capability, no write, no new permission required. Safe to run at any time
once the connection/listings stages are already verified.

Usage:
    python3 find_pilot_candidate.py               # Sandbox (.env)
    python3 find_pilot_candidate.py --production    # Production (.env.production)
"""

from __future__ import annotations

import sys
from pathlib import Path

from ebay_app.config import ConfigError, load_config
from ebay_app.ebay_client import EbayError, EbaySandboxClient
from ebay_app.listings import ListingService
from ebay_app.storage import AppDatabase, StorageError

PRODUCTION_ENV_PATH = Path(__file__).resolve().parent / ".env.production"


def main(argv: list[str]) -> int:
    use_production = "--production" in argv
    database = None
    try:
        config = load_config(PRODUCTION_ENV_PATH) if use_production else load_config()
        database = AppDatabase(
            config.database_path,
            config.data_encryption_key,
            environment=config.environment.capitalize(),
        )
        client = EbaySandboxClient(config, database)
        service = ListingService(client, database, environment=config.environment.capitalize())
        summary = service.retrieve()
    except (ConfigError, StorageError, EbayError) as error:
        print(f"Could not retrieve listings: {error}")
        return 1
    finally:
        if database is not None:
            database.close()

    if not summary.listings:
        print(f"No active {summary.environment} listings found.")
        return 0

    ranked = sorted(
        summary.listings,
        key=lambda item: (float(item.price_value), item.available_quantity),
    )

    print(f"Active {summary.environment} listings, retrieved {summary.retrieved_at}")
    print("Ranked by price (ascending) -- lowest-price listings are usually the safest pilot candidates.")
    print(
        "Available quantity is shown as a loose hint only -- NOT a real sales-velocity\n"
        "measurement (this app's order data is deliberately minimized and has no\n"
        "per-SKU sales history -- see this script's own docstring)."
    )
    print()
    print(f"{'SKU':<24} {'Price':>10} {'Currency':<8} {'Qty avail':>9}  Title")
    print("-" * 100)
    for listing in ranked:
        print(
            f"{listing.sku:<24} {listing.price_value:>10} {listing.currency:<8} "
            f"{listing.available_quantity:>9}  {listing.title[:50]}"
        )

    print(
        "\nPick from near the top of this list (lowest price), then separately confirm it's"
        " slow-moving and replaceable using your own knowledge or eBay Seller Hub -- that"
        " information isn't available from this app's own data."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
