"""Pure formatting/derivation helpers for the exportable audit log report.

Kept separate from the CLI script (export_audit_log.py) and from
AppDatabase.export_actions() itself so the "what does this action's
before/after actually mean, per kind" logic is unit-tested directly,
matching this project's convention of putting any real logic inside
ebay_app/ rather than in a thin root-level script.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# One row per exportable column, in a fixed, stable order -- this is the
# contract a seller's spreadsheet/accounting tool can rely on not reordering
# under them between runs.
EXPORT_COLUMNS = (
    "action_id",
    "kind",
    "status",
    "sku",
    "offer_id",
    "before",
    "after",
    "currency",
    "dry_run",
    "created_at",
    "updated_at",
    "error_message",
)


def parse_date_to_timestamp(value: str, *, end_of_day: bool = False) -> int:
    """Parse a ``YYYY-MM-DD`` string to a UTC epoch timestamp.

    Raises ``ValueError`` (with a human-readable message, via datetime's own
    exception) on anything else -- a date filter should never silently
    degrade to "no filter" on a typo.
    """

    parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end_of_day:
        parsed = parsed.replace(hour=23, minute=59, second=59)
    return int(parsed.timestamp())


def _iso(timestamp: Optional[int]) -> str:
    if timestamp is None:
        return ""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _before_after(record: Dict[str, Any]) -> Tuple[str, str]:
    """Derive human "before"/"after" values for one action record, by kind.

    Deliberately kind-specific: each write-capable stage's payload uses its
    own field names (see quantities.py/images.py/prices.py/drafts.py), and
    guessing a generic field name would silently show blank for every kind
    but one.
    """

    kind = record.get("kind")
    payload = record.get("payload") or {}
    result = record.get("result") or {}

    if kind == "change_price":
        before = payload.get("expected_price")
        after = payload.get("new_price") if result.get("price") is None else result.get("price")
        return (
            "" if before is None else str(before),
            "" if after is None else str(after),
        )
    if kind == "change_quantity":
        before = payload.get("expected_quantity")
        after = (
            payload.get("new_quantity") if result.get("quantity") is None else result.get("quantity")
        )
        return (
            "" if before is None else str(before),
            "" if after is None else str(after),
        )
    if kind == "add_image":
        expected_urls = payload.get("expected_image_urls")
        before = ", ".join(expected_urls) if isinstance(expected_urls, list) else ""
        after = result.get("image_url") or payload.get("new_image_url") or ""
        return (before, str(after))
    if kind == "create_draft":
        # Nothing existed before a draft was created -- "after" is the new
        # Offer this action produced, if any.
        after = result.get("offer_id") or ""
        return ("", str(after))
    return ("", "")


def summarize_row(record: Dict[str, Any]) -> Dict[str, str]:
    """Flatten one ``AppDatabase.export_actions()`` record to export columns.

    Every value is already a plain string, ready for CSV or a printed table
    -- no further interpretation of ``payload``/``result`` should happen
    downstream of this function.
    """

    payload = record.get("payload") or {}
    result = record.get("result") or {}
    before, after = _before_after(record)
    sku = result.get("sku") or payload.get("sku") or ""
    offer_id = result.get("offer_id") or payload.get("offer_id") or ""
    currency = result.get("currency") or payload.get("currency") or ""
    dry_run = result.get("dry_run")
    if dry_run is True:
        dry_run_text = "true"
    elif dry_run is False:
        dry_run_text = "false"
    else:
        dry_run_text = ""
    return {
        "action_id": record.get("action_id") or "",
        "kind": record.get("kind") or "",
        "status": record.get("status") or "",
        "sku": str(sku),
        "offer_id": str(offer_id),
        "before": before,
        "after": after,
        "currency": str(currency),
        "dry_run": dry_run_text,
        "created_at": _iso(record.get("created_at")),
        "updated_at": _iso(record.get("updated_at")),
        "error_message": record.get("error_message") or "",
    }


def to_csv(records: List[Dict[str, Any]]) -> str:
    """Render a list of export_actions() records as a CSV string (header included)."""

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=EXPORT_COLUMNS)
    writer.writeheader()
    for record in records:
        writer.writerow(summarize_row(record))
    return buffer.getvalue()


def to_table(records: List[Dict[str, Any]]) -> str:
    """Render a list of export_actions() records as a simple fixed-width table."""

    if not records:
        return "No actions match the given filters."
    rows = [summarize_row(record) for record in records]
    widths = {
        column: max(len(column), max(len(row[column]) for row in rows))
        for column in EXPORT_COLUMNS
    }
    header = "  ".join(column.upper().ljust(widths[column]) for column in EXPORT_COLUMNS)
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append("  ".join(row[column].ljust(widths[column]) for column in EXPORT_COLUMNS))
    return "\n".join(lines)
