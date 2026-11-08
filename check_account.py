#!/usr/bin/env python3
"""Run the Phase 2 account read without printing credentials or identifiers."""

from __future__ import annotations

from ebay_app.account import AccountService
from ebay_app.config import ConfigError, load_config
from ebay_app.ebay_client import EbayError, EbaySandboxClient
from ebay_app.storage import AppDatabase, StorageError


def main() -> int:
    database = None
    try:
        config = load_config()
        database = AppDatabase(config.database_path, config.data_encryption_key)
        client = EbaySandboxClient(config, database)
        summary = AccountService(client, database).retrieve()
        print("eBay Sandbox account read: SUCCEEDED")
        print(f"Environment: {summary.environment}")
        print(f"Account type: {summary.account_type}")
        print(f"Registration marketplace: {summary.registration_marketplace}")
        print(f"Retrieved at: {summary.retrieved_at}")
        return 0
    except (ConfigError, StorageError, EbayError) as error:
        print(f"eBay Sandbox account read: FAILED — {error}")
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
