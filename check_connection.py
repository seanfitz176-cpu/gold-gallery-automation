#!/usr/bin/env python3
"""Run the Phase 1 connection check without printing credentials or account data."""

from __future__ import annotations

from ebay_app.config import ConfigError, load_config
from ebay_app.connection import ConnectionVerifier
from ebay_app.ebay_client import EbayError, EbaySandboxClient
from ebay_app.storage import AppDatabase, StorageError


def main() -> int:
    database = None
    try:
        config = load_config()
        database = AppDatabase(config.database_path, config.data_encryption_key)
        client = EbaySandboxClient(config, database)
        result = ConnectionVerifier(client, database).verify()
        print("eBay Sandbox connection: VERIFIED")
        print(f"OAuth: {result.oauth}")
        print(f"Identity read: {result.identity_read}")
        print(f"Checked at: {result.checked_at}")
        return 0
    except (ConfigError, StorageError, EbayError) as error:
        print(f"eBay Sandbox connection: FAILED — {error}")
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
