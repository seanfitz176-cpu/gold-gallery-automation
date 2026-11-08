"""Import per-SKU costs (and optional margins) into the local database.

Local-only: makes NO eBay call. It writes only the cost table the autonomy
engine reads (docs/autonomy_engine.md). Stop run.py / run_production.py
first -- the database allows one process at a time.

CSV columns (header row, any order, case-insensitive):
  sku            required
  item_cost      required, e.g. 42.00
  postage_cost   optional, default 0
  fee_percent    optional, default 0 (UK private sellers pay no selling fee)
  margin_percent optional; blank = use APP_ENGINE_DEFAULT_MARGIN_PERCENT if
                 set, otherwise this SKU is reported but never repriced

Usage:
    python3 import_costs.py costs.csv                # Sandbox (.env)
    python3 import_costs.py costs.csv --production   # Production (.env.production)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

from ebay_app.config import ConfigError, load_config
from ebay_app.engine_pricing import CostRowError, break_even_price, load_cost_csv
from ebay_app.storage import AppDatabase, StorageError

PRODUCTION_ENV_PATH = Path(__file__).resolve().parent / ".env.production"


def main(argv: List[str]) -> int:
    use_production = "--production" in argv
    positional = [a for a in argv if not a.startswith("--")]
    if not positional or not Path(positional[0]).is_file():
        print(__doc__)
        return 2
    database = None
    try:
        config = load_config(PRODUCTION_ENV_PATH) if use_production else load_config()
        if use_production and config.environment != "production":
            print("This script only runs against .env.production with --production.")
            return 2
        try:
            rows, errors = load_cost_csv(Path(positional[0]), config.currency)
        except CostRowError as error:
            print(f"Could not read the CSV file: {error}")
            return 2
        for line, sku, message in errors:
            print(f"  SKIPPED line {line} (sku={sku!r}): {message}")
        if not rows:
            print("No valid rows to import.")
            return 1
        print(f"\n{'SKU':<28} {'Item':>9} {'Post':>7} {'Fee%':>6} {'Margin%':>8} {'Break-even':>11}")
        for r in rows:
            print(f"{r.sku:<28} {r.item_cost:>9} {r.postage_cost:>7} {r.fee_percent:>6} "
                  f"{str(r.margin_percent or '-'):>8} {break_even_price(r):>11}")
        try:
            confirm = input(f"\nType IMPORT {len(rows)} to save these costs: ").strip()
        except EOFError:
            confirm = ""
        if confirm != f"IMPORT {len(rows)}":
            print("Confirmation did not match -- nothing was saved.")
            return 1
        database = AppDatabase(config.database_path, config.data_encryption_key,
                               environment=config.environment.capitalize())
        for r in rows:
            database.set_sku_cost(
                r.sku, item_cost=str(r.item_cost), postage_cost=str(r.postage_cost),
                fee_percent=str(r.fee_percent), currency=r.currency,
                margin_percent=str(r.margin_percent) if r.margin_percent is not None else None,
            )
        database.set_engine_state("reprice_requested", "1")
        print(f"Saved {len(rows)} cost row(s). Existing rows for the same SKUs were replaced.")
        print("If the engine is on, it will re-check prices within a minute of the app starting.")
        return 0
    except (ConfigError, StorageError) as error:
        print(f"Could not start: {error}")
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
