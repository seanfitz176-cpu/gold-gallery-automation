#!/usr/bin/env python3
"""One-off diagnostic tool: publish ONE existing Sandbox Offer so it can be
used as test data for Phase 6 (editing a published listing's price).

Why this script exists: Phase 5 deliberately only ever creates UNPUBLISHED
offers, and Phase 6's "Review price" control only appears next to a
PUBLISHED listing on the app's own /listings page. Without a published
Sandbox Offer there is nothing to point Phase 6 at.

The actual publish call is NOT part of the running application. It is never
imported by, called by, or reachable from app.py's web UI, and it does not
use EbaySandboxClient's guarded `_api_request` / `_assert_mutation_allowed`
machinery -- it is made directly, the same way a developer testing with curl
or Postman would. Every real feature stage the app can select still has no
code path that can publish anything; that guarantee is unchanged by this
script. (The one exception is the item-specifics fix below, which *does* go
through the app's own guarded, already-tested Inventory-item write -- the
same call Phase 5's draft flow already makes -- because that call is already
permitted under the "drafts"/"prices" stages.)

Publishing an Offer is a one-way action in Sandbox: it creates a listing
with a listing ID that cannot be turned back into a draft through the
public API. Only run this against a Sandbox Offer you are happy to have
listed (for example, the Phase 5 test offer).

Before publishing, this script asks eBay's own Taxonomy API which item
specifics (aspects) the Offer's category requires -- for example "Storage
Capacity" or "Colour" for a mobile phone -- and checks which of those the
Inventory item is still missing, rather than discovering them one at a time
from repeated failed publish attempts. If any are missing, it shows you the
whole list up front and asks for a value for each one before making a
single combined update and then publishing once.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from ebay_app.config import ConfigError, load_config
from ebay_app.ebay_client import EbayError, EbaySandboxClient
from ebay_app.storage import AppDatabase, StorageError


def _print_error(error: EbayError) -> None:
    print(f"Error: {error}")
    if error.status is not None:
        print(f"  HTTP status: {error.status}")
    if error.code is not None:
        print(f"  Code: {error.code}")
    if error.details:
        print(f"  eBay error: {error.details}")


def _missing_required_aspects(
    client: EbaySandboxClient, category_id: str, item: Dict[str, Any]
) -> List[str]:
    """Read-only: which required item specifics does this Inventory item lack?

    Asks eBay's Taxonomy API directly for the category's required aspects
    instead of guessing from a failed publish attempt's error text.
    """

    tree_id = client.get_default_category_tree_id()
    aspects = client.get_item_aspects_for_category(tree_id, category_id)
    existing = (item.get("product") or {}).get("aspects") or {}
    missing = []
    for aspect in aspects:
        constraint = aspect.get("aspectConstraint") or {}
        if not constraint.get("aspectRequired"):
            continue
        name = aspect.get("localizedAspectName")
        if not name:
            continue
        values = existing.get(name)
        if not values or not any(str(v).strip() for v in values):
            missing.append(name)
    return missing


def _update_aspects(
    client: EbaySandboxClient, sku: str, item: Dict[str, Any], new_values: Dict[str, str]
) -> None:
    """Add the given item specifics to the existing Inventory item.

    Reuses the app's own already-guarded, already-tested Inventory-item PUT
    (the same call Phase 5's draft flow makes) -- not a raw bypass call.
    Every other field on the item (title, description, condition, quantity)
    is carried over unchanged.
    """

    product = dict(item.get("product") or {})
    aspects = dict(product.get("aspects") or {})
    for name, value in new_values.items():
        aspects[name] = [value]
    product["aspects"] = aspects
    payload = {
        "availability": item.get("availability")
        or {"shipToLocationAvailability": {"quantity": 1}},
        "condition": item.get("condition", "NEW"),
        "product": product,
    }
    client._api_request(
        "PUT",
        f"/sell/inventory/v1/inventory_item/{urllib.parse.quote(sku, safe='')}",
        payload=payload,
        safe_read=False,
        expected_statuses=(204,),
    )


def _attempt_publish(
    client: EbaySandboxClient, config, offer_id: str
) -> Tuple[bool, Optional[str], Optional[dict]]:
    """Make the one raw publish call. Returns (success, listing_id, error_body)."""

    token = client.get_access_token()
    url = f"{config.api_origin}/sell/inventory/v1/offer/{offer_id}/publish"
    request = urllib.request.Request(
        url,
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Language": config.content_language,
            "X-EBAY-C-MARKETPLACE-ID": config.marketplace_id,
        },
    )
    try:
        with urllib.request.urlopen(
            request, timeout=config.request_timeout_seconds
        ) as response:
            body = response.read()
            result = json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        raw_body = error.read()
        try:
            parsed = json.loads(raw_body)
        except (ValueError, UnicodeDecodeError):
            parsed = {"_raw": repr(raw_body)}
        return False, None, {"status": error.code, "body": parsed}

    return True, result.get("listingId"), None


def main() -> int:
    database = None
    try:
        config = load_config()
        database = AppDatabase(config.database_path, config.data_encryption_key)
        client = EbaySandboxClient(config, database)

        offer_id = input("Offer ID to publish (e.g. 11743059010): ").strip()
        if not offer_id:
            print("No Offer ID entered. Nothing was done.")
            return 1

        print("\nReading the Offer (read-only)...")
        try:
            offer = client.get_offer(offer_id)
        except EbayError as error:
            print("Could not read that Offer:")
            _print_error(error)
            return 1

        if offer.get("offerId") != offer_id:
            print("eBay returned a different Offer than the one you typed. Stopping.")
            return 1

        sku = offer.get("sku", "(unknown)")
        status = offer.get("status")
        category_id = offer.get("categoryId")

        if status == "PUBLISHED":
            print(f"Offer {offer_id} (SKU {sku}) is already PUBLISHED. Nothing to do.")
            listing = offer.get("listing") or {}
            listing_id = listing.get("listingId") or offer.get("listingId")
            if listing_id:
                print(f"Listing ID: {listing_id}")
            return 0

        if status != "UNPUBLISHED":
            print(f"Offer {offer_id} has status '{status}', not UNPUBLISHED. Stopping.")
            return 1

        print(f"\nOffer {offer_id}   SKU: {sku}   status: {status}   category: {category_id}")

        item = client.get_inventory_item(sku)
        if item is None:
            print(f"Inventory item {sku} no longer exists. Stopping.")
            return 1

        if category_id:
            print("\nChecking eBay's required item specifics for this category (read-only)...")
            try:
                missing = _missing_required_aspects(client, str(category_id), item)
            except EbayError as error:
                print("Could not read category requirements:")
                _print_error(error)
                missing = []

            if missing:
                print(
                    f"\nThis category requires values for: {', '.join(missing)}. "
                    "None of these have been changed yet."
                )
                new_values: Dict[str, str] = {}
                for name in missing:
                    value = input(f"Enter a value for '{name}' (or leave blank to stop): ").strip()
                    if not value:
                        print("Stopping. Nothing was published or changed.")
                        return 1
                    new_values[name] = value

                print(f"\nThis will update the existing Inventory item {sku}, adding:")
                for name, value in new_values.items():
                    print(f"  '{name}': '{value}'")
                print(
                    "Nothing else on the item changes, and nothing is published yet."
                )
                print("Type ADD to proceed, or anything else to cancel.")
                if input("> ").strip() != "ADD":
                    print("Cancelled. Nothing was published or changed.")
                    return 0
                try:
                    _update_aspects(client, sku, item, new_values)
                except EbayError as error:
                    print("Could not update the Inventory item:")
                    _print_error(error)
                    return 1
                print("  Updated.")
            else:
                print("  All required item specifics are already present.")

        print(
            "\nPublishing creates a live-in-Sandbox listing. This cannot be turned "
            "back into a draft afterwards through the public API. This makes "
            "exactly this one write and nothing else."
        )
        print("Type PUBLISH to proceed, or anything else to cancel.")
        if input("> ").strip() != "PUBLISH":
            print("Cancelled. Nothing was published.")
            return 0

        print("\nPublishing...")
        success, listing_id, error_body = _attempt_publish(client, config, offer_id)

        if not success:
            status_code = (error_body or {}).get("status")
            print(f"Publish failed: HTTP {status_code}")
            print(f"  eBay error: {(error_body or {}).get('body')}")
            if status_code in (400, 404, 409):
                print(
                    "\nThis is a validation rejection: nothing was published and "
                    f"Offer {offer_id} is still UNPUBLISHED. Safe to fix and try "
                    "again -- rerun this script, it will re-check requirements "
                    "from scratch."
                )
            else:
                print(
                    "\nThe outcome of this write is uncertain -- check this Offer "
                    "in Sandbox directly before trying again."
                )
            return 1

        if not listing_id:
            print(
                "eBay accepted the request but did not return a listing ID. "
                "Check this Offer in Sandbox before doing anything else."
            )
            return 1

        print(f"\nDone. Offer {offer_id} is now PUBLISHED. Listing ID: {listing_id}")
        print(
            "You can now use this Offer's 'Review price' control on the app's "
            "/listings page (once APP_FEATURE_STAGE=prices)."
        )
        return 0
    except (ConfigError, StorageError) as error:
        print(f"FAILED - {error}")
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
