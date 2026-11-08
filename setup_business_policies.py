#!/usr/bin/env python3
"""One-time Phase 5 prerequisite: create the Sandbox merchant location and the
three business policies (fulfillment, payment, return) an unpublished Offer
needs, then print the resulting IDs for the owner to paste into .env.

This script performs real Sandbox mutations. It never runs automatically and
never retries a mutation whose outcome is uncertain. It requires:

  1. APP_FEATURE_STAGE=setup in .env (temporarily — revert to "drafts"
     afterwards; the OAuth grant itself keeps every scope it has already been
     given, so reverting the stage does not remove permission).
  2. A Sandbox OAuth grant that already includes Account-management scope
     (sell.account). If it does not yet, start the app with
     APP_FEATURE_STAGE=setup and go through the normal scope-upgrade consent
     flow once before running this script.

Order matters here: eBay Sandbox rejects every Business Policy read or write
with "User is not eligible for Business Policy" until the seller has opted
in to SELLING_POLICY_MANAGEMENT, so opt-in runs first, before any read.
After that, read-only preflight checks reuse anything that already exists
instead of creating a duplicate. Nothing is written to .env automatically —
review the printed values and add them yourself.
"""

from __future__ import annotations

from ebay_app.config import ConfigError, load_config
from ebay_app.ebay_client import EbayError, EbaySandboxClient
from ebay_app.storage import AppDatabase, StorageError

MERCHANT_LOCATION_KEY = "SANDBOX-WAREHOUSE-1"
LOCATION_NAME = "Sandbox Warehouse"
LOCATION_ADDRESS = {
    "address_line_1": "1 Sandbox Way",
    "city": "London",
    "state_or_province": "London",
    "postal_code": "SW1A 1AA",
    "country": "GB",
}
FULFILLMENT_POLICY_NAME = "Sandbox Standard Shipping"
PAYMENT_POLICY_NAME = "Sandbox Standard Payment"
RETURN_POLICY_NAME = "Sandbox 30 Day Returns"


def _print_error(error: EbayError) -> None:
    print(f"Error: {error}")
    if error.status is not None:
        print(f"  HTTP status: {error.status}")
    if error.code is not None:
        print(f"  Code: {error.code}")
    if error.details:
        for item in error.details:
            print(f"  eBay error: {item}")


def _confirm() -> bool:
    print(
        "\nThis will, in order: opt this Sandbox seller in to Business "
        "Policies (harmless if already done), then check what already "
        "exists, then create only whichever of the following are missing:"
    )
    print(f"  - Merchant location '{MERCHANT_LOCATION_KEY}'")
    print(f"  - Fulfillment policy '{FULFILLMENT_POLICY_NAME}'")
    print(f"  - Payment policy '{PAYMENT_POLICY_NAME}'")
    print(f"  - Return policy '{RETURN_POLICY_NAME}'")
    print(
        "\nType CREATE to proceed, or anything else to cancel without "
        "changing anything."
    )
    return input("> ").strip() == "CREATE"


def main() -> int:
    database = None
    try:
        config = load_config()
        if config.feature_stage != "setup":
            print(
                "Refusing to run: APP_FEATURE_STAGE must be 'setup' for this "
                "script (it is currently "
                f"'{config.feature_stage}'). Set APP_FEATURE_STAGE=setup in "
                ".env, restart the app, complete the Account-management "
                "consent upgrade, then rerun this script."
            )
            return 1

        database = AppDatabase(config.database_path, config.data_encryption_key)
        client = EbaySandboxClient(config, database)

        if not _confirm():
            print("Cancelled. Nothing was created.")
            return 0

        completed: list[str] = []
        with client.setup_mutation_guard():
            print("\nOpting in to Business Policies...")
            try:
                client.opt_in_to_selling_policy_management()
                completed.append("opt-in")
                print("  Opt-in call succeeded.")
            except EbayError as error:
                print(
                    "  Opt-in call returned an error. This is expected and "
                    "harmless if the seller was already opted in from a "
                    "previous run:"
                )
                _print_error(error)
                proceed = input(
                    "  Continue with location/policy creation anyway? [y/N] "
                ).strip().lower()
                if proceed != "y":
                    print("Stopped after opt-in. Nothing else was created.")
                    return 1

            print("\nChecking what already exists (read-only)...")
            try:
                existing_location = client.get_inventory_location(MERCHANT_LOCATION_KEY)
                existing_fulfillment = next(
                    (
                        p
                        for p in client.list_fulfillment_policies()
                        if p.get("name") == FULFILLMENT_POLICY_NAME
                    ),
                    None,
                )
                existing_payment = next(
                    (
                        p
                        for p in client.list_payment_policies()
                        if p.get("name") == PAYMENT_POLICY_NAME
                    ),
                    None,
                )
                existing_return = next(
                    (
                        p
                        for p in client.list_return_policies()
                        if p.get("name") == RETURN_POLICY_NAME
                    ),
                    None,
                )
            except EbayError as error:
                print(
                    f"\nStopped after completing: {', '.join(completed) or 'nothing'}."
                )
                _print_error(error)
                return 1

            try:
                location_key = (
                    existing_location["merchantLocationKey"]
                    if existing_location
                    else client.create_merchant_location(
                        MERCHANT_LOCATION_KEY,
                        name=LOCATION_NAME,
                        **LOCATION_ADDRESS,
                    )
                )
                if existing_location:
                    print(f"  Merchant location already existed, reusing it.")
                else:
                    completed.append("merchant location")
                    print(f"  Created merchant location.")

                fulfillment_id = (
                    existing_fulfillment["fulfillmentPolicyId"]
                    if existing_fulfillment
                    else client.create_fulfillment_policy(name=FULFILLMENT_POLICY_NAME)
                )
                if existing_fulfillment:
                    print(f"  Fulfillment policy already existed, reusing it.")
                else:
                    completed.append("fulfillment policy")
                    print(f"  Created fulfillment policy.")

                payment_id = (
                    existing_payment["paymentPolicyId"]
                    if existing_payment
                    else client.create_payment_policy(name=PAYMENT_POLICY_NAME)
                )
                if existing_payment:
                    print(f"  Payment policy already existed, reusing it.")
                else:
                    completed.append("payment policy")
                    print(f"  Created payment policy.")

                return_id = (
                    existing_return["returnPolicyId"]
                    if existing_return
                    else client.create_return_policy(name=RETURN_POLICY_NAME)
                )
                if existing_return:
                    print(f"  Return policy already existed, reusing it.")
                else:
                    completed.append("return policy")
                    print(f"  Created return policy.")
            except EbayError as error:
                print(
                    f"\nStopped after completing: {', '.join(completed) or 'nothing'}."
                )
                _print_error(error)
                print(
                    "Do not rerun blindly if the outcome of the last step is "
                    "uncertain — check the Sandbox account directly first."
                )
                return 1

        print("\nDone. Add these to .env:")
        print(f"EBAY_MERCHANT_LOCATION_KEY={location_key}")
        print(f"EBAY_FULFILLMENT_POLICY_ID={fulfillment_id}")
        print(f"EBAY_PAYMENT_POLICY_ID={payment_id}")
        print(f"EBAY_RETURN_POLICY_ID={return_id}")
        print(
            "\nAfter updating .env, set APP_FEATURE_STAGE back to 'drafts', "
            "stop and restart the app."
        )
        return 0
    except (ConfigError, StorageError, EbayError) as error:
        print(f"Sandbox setup: FAILED — {error}")
        if isinstance(error, EbayError):
            _print_error(error)
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
