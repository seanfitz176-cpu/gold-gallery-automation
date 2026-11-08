#!/usr/bin/env python3
"""Export the local action journal (audit log) -- read-only, no eBay call.

Fills the gap flagged in the Phase 9 production readiness report: the
journaled record of every confirmed draft/price/quantity/image action
already exists (encrypted, append-only, in the local database), but there
was previously no queryable or exportable report view for it. This script
reads that same local database directly -- it never calls eBay, never
needs a live OAuth connection, and is safe to run at any time, against
either environment's database.

Every price/quantity/SKU/image-URL value here was already seen and
confirmed by you in the dashboard at the time the action happened -- this
just lets you query and export the durable record of it afterward, for
your own compliance review, accounting, or troubleshooting.

Usage:
    python3 export_audit_log.py                       # Sandbox, table, all history
    python3 export_audit_log.py --production
    python3 export_audit_log.py --format csv --output audit.csv
    python3 export_audit_log.py --kind change_price --kind change_quantity
    python3 export_audit_log.py --status failed --status partial --status unknown
    python3 export_audit_log.py --since 2026-11-01 --until 2026-11-08
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

from ebay_app.audit_export import parse_date_to_timestamp, to_csv, to_table
from ebay_app.config import ConfigError, load_config
from ebay_app.storage import AppDatabase, StorageError

PRODUCTION_ENV_PATH = Path(__file__).resolve().parent / ".env.production"


def _parse_flag_value(argv: List[str], name: str) -> Optional[str]:
    for i, arg in enumerate(argv):
        if arg == name and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


def _parse_repeated_flag(argv: List[str], name: str) -> List[str]:
    values: List[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == name and i + 1 < len(argv):
            values.append(argv[i + 1])
            i += 2
            continue
        if arg.startswith(name + "="):
            values.append(arg.split("=", 1)[1])
            i += 1
            continue
        i += 1
    return values


def main(argv: List[str]) -> int:
    use_production = "--production" in argv
    output_format = _parse_flag_value(argv, "--format") or "table"
    if output_format not in ("table", "csv"):
        print(f"Unsupported --format: {output_format!r} -- use 'table' or 'csv'.")
        return 2

    kinds = _parse_repeated_flag(argv, "--kind") or None
    statuses = _parse_repeated_flag(argv, "--status") or None
    since_arg = _parse_flag_value(argv, "--since")
    until_arg = _parse_flag_value(argv, "--until")
    output_path = _parse_flag_value(argv, "--output")

    try:
        since = parse_date_to_timestamp(since_arg) if since_arg else None
        until = parse_date_to_timestamp(until_arg, end_of_day=True) if until_arg else None
    except ValueError as error:
        print(f"Invalid --since/--until date ({error}) -- use YYYY-MM-DD.")
        return 2

    database = None
    try:
        config = load_config(PRODUCTION_ENV_PATH) if use_production else load_config()
        database = AppDatabase(
            config.database_path,
            config.data_encryption_key,
            environment=config.environment.capitalize(),
        )
        records = database.export_actions(
            kinds=kinds, statuses=statuses, since=since, until=until
        )
    except (ConfigError, StorageError) as error:
        print(f"Could not read the audit log: {error}")
        return 1
    finally:
        if database is not None:
            database.close()

    rendered = to_csv(records) if output_format == "csv" else to_table(records)
    if output_path:
        Path(output_path).write_text(rendered, encoding="utf-8")
        print(f"Wrote {len(records)} action(s) to {output_path}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
