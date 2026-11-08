#!/usr/bin/env python3
"""Read-only pilot-readiness check for claude/supervised_dry_run_plan.md.

Standalone diagnostic script, in the same spirit as check_connection.py /
check_account.py / find_category.py already in this folder. Safe to run at
any time: it makes NO network call, performs NO database access, and NEVER
edits .env.production. It only reads the plain, non-secret feature-stage
and opt-in toggles the dry-run plan's own "Section 0: Preconditions"
checklist asks about, and reports their status in plain language.

Credential values (EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, EBAY_RUNAME,
APP_ADMIN_PASSWORD, APP_DATA_ENCRYPTION_KEY) are never printed -- only
whether each one is present at all, by name.

Usage:
    python3 check_pilot_preconditions.py
    python3 check_pilot_preconditions.py .env            # check Sandbox instead
"""

from __future__ import annotations

import sys
from pathlib import Path

DEFAULT_ENV_FILENAME = ".env.production"

NON_SECRET_KEYS = (
    "EBAY_ENVIRONMENT",
    "APP_FEATURE_STAGE",
    "APP_ALLOW_PRODUCTION_ENVIRONMENT",
    "APP_ALLOW_PRODUCTION_PRICE_WRITES",
    "APP_ALLOW_PRODUCTION_QUANTITY_WRITES",
    "APP_ALLOW_PRODUCTION_IMAGE_WRITES",
    "APP_DRY_RUN",
    "APP_MAX_ACTIONS_PER_RUN",
    "APP_MAX_PRICE_CHANGE_PERCENT",
    "APP_REQUIRE_PRICE_FLOOR",
)

SECRET_KEYS = (
    "EBAY_CLIENT_ID",
    "EBAY_CLIENT_SECRET",
    "EBAY_RUNAME",
    "APP_ADMIN_PASSWORD",
    "APP_DATA_ENCRYPTION_KEY",
)


def parse_env(path: Path) -> dict:
    """Minimal KEY=value parser -- deliberately simple, not config.py's real
    parser (which also handles quoting/escaping). Good enough for a status
    report; never used to actually load the application's configuration."""
    values: dict = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def describe(values: dict, key: str, expected: str | None = None) -> str:
    if key not in values or values[key] == "":
        return "NOT SET"
    value = values[key]
    if expected is not None:
        if value == expected:
            return "SET (matches the exact value the app requires)"
        return f"SET, but to an unexpected value ({value!r}) -- check this"
    return f"SET (value: {value!r})"


def main(argv: list[str]) -> int:
    env_path = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent / DEFAULT_ENV_FILENAME

    if not env_path.exists():
        print(f"No file found at {env_path} -- nothing to check.")
        return 1

    values = parse_env(env_path)

    print(f"Pilot-readiness check for {env_path.name} (read-only; no secret values shown)")
    print("=" * 70)

    print("\n-- Base production gate --")
    print(f"  EBAY_ENVIRONMENT:                     {describe(values, 'EBAY_ENVIRONMENT')}")
    print(f"  APP_ALLOW_PRODUCTION_ENVIRONMENT:     {describe(values, 'APP_ALLOW_PRODUCTION_ENVIRONMENT', 'yes-i-understand-the-risk')}")
    print(f"  APP_FEATURE_STAGE:                    {describe(values, 'APP_FEATURE_STAGE')}")

    print("\n-- Write-capable stage opt-ins (none of these should be set yet) --")
    print(f"  APP_ALLOW_PRODUCTION_PRICE_WRITES:    {describe(values, 'APP_ALLOW_PRODUCTION_PRICE_WRITES')}")
    print(f"  APP_ALLOW_PRODUCTION_QUANTITY_WRITES: {describe(values, 'APP_ALLOW_PRODUCTION_QUANTITY_WRITES')}")
    print(f"  APP_ALLOW_PRODUCTION_IMAGE_WRITES:    {describe(values, 'APP_ALLOW_PRODUCTION_IMAGE_WRITES')}")

    print("\n-- Kill switch and safety-control defaults --")
    dry_run_status = describe(values, "APP_DRY_RUN")
    print(f"  APP_DRY_RUN:                           {dry_run_status}  (NOT SET or 'true' is the safe starting state)")
    print(f"  APP_MAX_ACTIONS_PER_RUN:               {describe(values, 'APP_MAX_ACTIONS_PER_RUN')}  (the plan recommends 1 for the first pilot runs)")
    print(f"  APP_MAX_PRICE_CHANGE_PERCENT:          {describe(values, 'APP_MAX_PRICE_CHANGE_PERCENT')}")
    print(f"  APP_REQUIRE_PRICE_FLOOR:               {describe(values, 'APP_REQUIRE_PRICE_FLOOR')}")

    present_secrets = sorted(k for k in SECRET_KEYS if values.get(k))
    missing_secrets = sorted(k for k in SECRET_KEYS if not values.get(k))
    print("\n-- Credentials (names only -- values are never read or shown) --")
    print(f"  Present: {', '.join(present_secrets) or 'none'}")
    if missing_secrets:
        print(f"  Missing: {', '.join(missing_secrets)}")

    unknown_keys = sorted(set(values) - set(NON_SECRET_KEYS) - set(SECRET_KEYS))
    other_known_keys = {
        "APP_DATABASE_PATH",
        "HOST",
        "PORT",
        "EBAY_REQUEST_TIMEOUT_SECONDS",
        "EBAY_MARKETPLACE_ID",
        "EBAY_CONTENT_LANGUAGE",
        "EBAY_CURRENCY",
        "EBAY_MERCHANT_LOCATION_KEY",
        "EBAY_FULFILLMENT_POLICY_ID",
        "EBAY_PAYMENT_POLICY_ID",
        "EBAY_RETURN_POLICY_ID",
        "ACCOUNT_DELETION_WORKER_URL",
        "ACCOUNT_DELETION_STATUS_SECRET",
        "APP_ORDER_LOOKBACK_DAYS",
    }
    unknown_keys = sorted(set(unknown_keys) - other_known_keys)
    if unknown_keys:
        print(f"\n  (Other keys present, not checked by this script: {', '.join(unknown_keys)})")

    print("\n" + "=" * 70)
    print(
        "This only checks the toggles -- it does not verify OAuth connection\n"
        "status, run the test suite, or confirm a pilot SKU has a price limit\n"
        "set. See claude/supervised_dry_run_plan.md's Section 0 checklist for\n"
        "the full list of preconditions."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
