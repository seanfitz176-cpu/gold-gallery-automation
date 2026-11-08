#!/usr/bin/env python3
"""One-off: bring an existing classic (non-Inventory-API) eBay listing under
this app's management, by calling eBay's bulkMigrateListing endpoint.

WHY THIS EXISTS: this app's ListingService/PriceService etc. only ever see
listings created through eBay's Sell Inventory API (the SKU + Offer model).
A listing created another way (eBay's website, Seller Hub) is invisible to
this app even though it's live on eBay. Migration converts one such listing
into the Inventory API's model -- a new Inventory Item and Offer, carrying
the SKU already on the listing -- without (per eBay's documentation)
republishing it or changing its eBay Item ID.

THIS APP CANNOT CHECK THE LISTING'S ELIGIBILITY FOR YOU. Confirm every one
of these yourself in Seller Hub before running this script:

  - Fixed Price, Good 'Til Cancelled (not an auction, not fixed-duration).
  - Uses your Business Policies for payment/return/shipping, not legacy
    per-listing settings.
  - Immediate Payment is required.
  - NOT a private listing.
  - NOT a Digital Gift Card listing.
  - Already has a seller-defined SKU (Custom Label) set on it.
  - Its PostalCode or Location (city) is set.

HONESTY ABOUT WHAT THIS SCRIPT DOES NOT KNOW: eBay's own public
documentation (as checked 2026-11-06) describes bulkMigrateListing's
eligibility rules in detail but does not publish this specific endpoint's
full field-level response schema. This script therefore never summarizes
the result in its own words -- it always prints eBay's complete raw
response so you can read it yourself. It is also unable to independently
verify, before or after the call, that the live listing's price and
visibility are genuinely unchanged -- eBay's documentation says this is
non-disruptive, but check the real listing in a browser immediately after
running this, exactly as every other step in this project's dry-run plan
asks you to.

THIS IS A REAL WRITE AGAINST YOUR LIVE PRODUCTION ACCOUNT. There is no dry
run for this specific call -- unlike the "prices"/"quantities"/"images"
stages, bulkMigrateListing has no rehearsal mode. One listing is migrated
per run, deliberately, never more.

PREREQUISITES in .env.production (see claude/listing_migration_plan.md):
  APP_FEATURE_STAGE=migration
  APP_ALLOW_PRODUCTION_LISTING_MIGRATION=yes-i-understand-the-risk-of-migrating-a-live-listing
...and a Production OAuth connection that already covers write (Inventory)
permission -- reconnect via the dashboard if this reports a permission
error. This uses the exact same sell.inventory scope the drafts/prices/
quantities/images stages already use, so no new OAuth consent screen
should be needed if you've reconnected for any of those already.

Usage:
    python3 migrate_listing.py --item-id <real eBay Item ID>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List, Optional

from ebay_app.config import ConfigError, load_config
from ebay_app.ebay_client import EbayError, EbaySandboxClient
from ebay_app.storage import AppDatabase, StorageError

PRODUCTION_ENV_PATH = Path(__file__).resolve().parent / ".env.production"

ELIGIBILITY_CHECKLIST = """
Before continuing, confirm EVERY one of these yourself in eBay Seller Hub --
this app has no way to check any of them automatically:

  [ ] Fixed Price, Good 'Til Cancelled (not an auction or fixed-duration).
  [ ] Uses your Business Policies for payment/return/shipping (not legacy
      per-listing settings).
  [ ] Immediate Payment is required on the listing.
  [ ] The listing is NOT a private listing.
  [ ] The listing is NOT a Digital Gift Card listing.
  [ ] The listing already has a seller-defined SKU (Custom Label) set.
  [ ] The listing's PostalCode or Location (city) is set.
"""


def _parse_item_id(argv: List[str]) -> Optional[str]:
    for i, arg in enumerate(argv):
        if arg == "--item-id" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--item-id="):
            return arg.split("=", 1)[1]
    return None


def main(argv: List[str]) -> int:
    item_id = _parse_item_id(argv)
    if not item_id or not item_id.strip():
        print(__doc__)
        return 2
    item_id = item_id.strip()

    print(ELIGIBILITY_CHECKLIST)
    print(
        "This is a REAL write against your live Production eBay account. There is\n"
        "no dry run for this specific call.\n"
    )
    try:
        confirm = input(
            f"Type the Item ID again to confirm you want to migrate it ({item_id}): "
        ).strip()
    except EOFError:
        confirm = ""
    if confirm != item_id:
        print("Item ID did not match -- nothing was done.")
        return 1

    database = None
    try:
        config = load_config(PRODUCTION_ENV_PATH)
        if config.environment != "production":
            print(
                "This script only runs against .env.production -- it refuses to "
                "touch a Sandbox account, since there is nothing real to migrate "
                "there."
            )
            return 1
        if config.feature_stage != "migration":
            print(
                "APP_FEATURE_STAGE must be 'migration' in .env.production for this "
                f"script to run (it is currently {config.feature_stage!r}). Set it, "
                "restart run_production.py once to confirm it starts cleanly, then "
                "run this again -- see claude/listing_migration_plan.md."
            )
            return 1
        database = AppDatabase(
            config.database_path,
            config.data_encryption_key,
            environment=config.environment.capitalize(),
        )
        client = EbaySandboxClient(config, database)
        generation = database.connection_generation()
        if generation is None:
            print(
                "No verified Production eBay connection found -- connect via the "
                "dashboard first. It also needs write/Inventory permission, not "
                "just read-only -- reconnect if the call below reports a "
                "permission error."
            )
            return 1

        print(f"\nCalling eBay to migrate Item ID {item_id} ...")
        with client.migration_mutation_guard():
            result = client.migrate_listing(item_id=item_id, expected_generation=generation)
    except (ConfigError, StorageError) as error:
        print(f"Could not start: {error}")
        return 1
    except EbayError as error:
        print(f"\neBay did not confirm the migration: {error}")
        status = getattr(error, "status", None)
        code = getattr(error, "code", None)
        if status is not None or code is not None:
            print(f"(HTTP status: {status}, code: {code})")
        details = getattr(error, "details", None)
        if details:
            print(f"Details: {json.dumps(details, indent=2)}")
        if getattr(error, "outcome_unknown", False):
            print(
                "\nThe outcome is UNKNOWN -- eBay's response could not be confirmed "
                "as a clean failure. Check the listing in Seller Hub/a browser "
                "before trying again; do not simply re-run this script."
            )
        return 1
    finally:
        if database is not None:
            database.close()

    print("\neBay's full raw response for this listing:")
    print(json.dumps(result["raw_response"], indent=2))
    print(
        "\nRead the response above yourself -- this app cannot fully interpret "
        "every field of it (see this script's own docstring). Next steps:\n"
        "  1. Check the real listing in a browser -- confirm its price, title and\n"
        "     visibility are unchanged.\n"
        "  2. Run: python3 find_pilot_candidate.py --production\n"
        "     The migrated SKU should now appear in that list.\n"
        "  3. Once confirmed, set APP_FEATURE_STAGE back to whatever stage you were\n"
        "     actually using (e.g. 'listings' or 'prices') in .env.production --\n"
        "     'migration' is a one-off stage, not meant to stay selected."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
