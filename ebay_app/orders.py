"""Phase 4 service for a minimal, privacy-preserving order summary."""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Mapping, Tuple

from .config import ORDER_SCOPES, granted_scopes_cover
from .ebay_client import EbayError, EbaySandboxClient
from .storage import AppDatabase


ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9._!\-]{1,128}$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
MONEY_PATTERN = re.compile(r"^(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,4})?$")
MAX_PUBLIC_ORDERS = 20_000
MAX_LINE_ITEMS_PER_ORDER = 1_000

PAYMENT_STATUSES = {
    "FAILED": "Failed",
    "FULLY_REFUNDED": "Fully refunded",
    "PAID": "Paid",
    "PARTIALLY_REFUNDED": "Partially refunded",
    "PENDING": "Pending",
}
FULFILLMENT_STATUSES = {
    "FULFILLED": "Fulfilled",
    "IN_PROGRESS": "In progress",
    "NOT_STARTED": "Not started",
}
CANCEL_STATES = {
    "NONE_REQUESTED": "None requested",
    "IN_PROGRESS": "In progress",
    "CANCELED": "Cancelled",
}


def _public_text(
    value: Any,
    field: str,
    *,
    maximum: int,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        raise EbayError(
            f"eBay returned an invalid order {field}",
            code="INVALID_EBAY_RESPONSE",
        )
    return value


def _money(value: Any) -> str:
    raw = _public_text(value, "total", maximum=32, pattern=MONEY_PATTERN)
    try:
        parsed = Decimal(raw)
    except InvalidOperation as exc:
        raise EbayError(
            "eBay returned an invalid order total",
            code="INVALID_EBAY_RESPONSE",
        ) from exc
    if not parsed.is_finite() or parsed < 0 or parsed > Decimal("999999999999"):
        raise EbayError(
            "eBay returned an invalid order total",
            code="INVALID_EBAY_RESPONSE",
        )
    return format(parsed, "f")


def _timestamp(value: Any) -> str:
    raw = _public_text(value, "creation date", maximum=64)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EbayError(
            "eBay returned an invalid order creation date",
            code="INVALID_EBAY_RESPONSE",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EbayError(
            "eBay returned an invalid order creation date",
            code="INVALID_EBAY_RESPONSE",
        )
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _mapped_status(
    value: Any,
    field: str,
    allowed: Mapping[str, str],
) -> str:
    raw = _public_text(value, field, maximum=64)
    display = allowed.get(raw)
    if display is None:
        raise EbayError(
            f"eBay returned an unsupported order {field}",
            code="INVALID_EBAY_RESPONSE",
        )
    return display


@dataclass(frozen=True)
class RecentOrder:
    """The complete Phase 4 public field allowlist for one order."""

    order_id: str
    created_at: str
    payment_status: str
    fulfillment_status: str
    cancellation_state: str
    total_value: str
    currency: str
    line_item_count: int

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RecentOrderSummary:
    environment: str
    retrieved_at: str
    lookback_days: int
    orders: Tuple[RecentOrder, ...]

    def order_dicts(self) -> Tuple[Dict[str, Any], ...]:
        return tuple(order.as_dict() for order in self.orders)


class OrderService:
    """Retrieve recent orders without retaining raw buyer or delivery data."""

    def __init__(
        self,
        ebay: EbaySandboxClient,
        database: AppDatabase,
        lookback_days: int,
        environment: str = "Sandbox",
        now=time.time,
    ) -> None:
        self.ebay = ebay
        self.database = database
        self.lookback_days = lookback_days
        self.environment = environment
        self.now = now

    def retrieve(self) -> RecentOrderSummary:
        if not granted_scopes_cover(self.database.granted_scopes(), ORDER_SCOPES):
            raise EbayError(
                "Additional read-only order permission is required",
                status=409,
                code="ORDER_PERMISSION_REQUIRED",
            )
        binding = self.database.verified_connection_binding()
        if binding is None:
            raise EbayError(
                f"Verify the eBay {self.environment} connection before viewing orders",
                status=409,
                code="CONNECTION_NOT_VERIFIED",
            )

        minimized_records = self.ebay.read_verified_order_records(binding)
        if not isinstance(minimized_records, list):
            raise EbayError(
                "eBay returned an invalid order collection",
                code="INVALID_EBAY_RESPONSE",
            )

        public: list[RecentOrder] = []
        seen_order_ids: set[str] = set()
        for record in minimized_records:
            order = self._recent_order(record)
            if order.order_id in seen_order_ids:
                raise EbayError(
                    "eBay returned a duplicate order",
                    code="INVALID_EBAY_RESPONSE",
                )
            seen_order_ids.add(order.order_id)
            public.append(order)
            if len(public) > MAX_PUBLIC_ORDERS:
                raise EbayError(
                    "Order response exceeded its safety limit",
                    code="INVALID_EBAY_RESPONSE",
                )

        public.sort(key=lambda item: (item.created_at, item.order_id), reverse=True)
        retrieved_at = datetime.fromtimestamp(self.now(), timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
        return RecentOrderSummary(
            environment=self.environment,
            retrieved_at=retrieved_at,
            lookback_days=self.lookback_days,
            orders=tuple(public),
        )

    @staticmethod
    def _recent_order(record: Any) -> RecentOrder:
        if not isinstance(record, Mapping):
            raise EbayError(
                "eBay returned an invalid order record",
                code="INVALID_EBAY_RESPONSE",
            )

        line_item_count = record.get("line_item_count")
        if (
            isinstance(line_item_count, bool)
            or not isinstance(line_item_count, int)
            or line_item_count < 1
            or line_item_count > MAX_LINE_ITEMS_PER_ORDER
        ):
            raise EbayError(
                "eBay returned invalid order line items",
                code="INVALID_EBAY_RESPONSE",
            )

        return RecentOrder(
            order_id=_public_text(
                record.get("order_id"),
                "ID",
                maximum=128,
                pattern=ORDER_ID_PATTERN,
            ),
            created_at=_timestamp(record.get("creation_date")),
            payment_status=_mapped_status(
                record.get("payment_status"),
                "payment status",
                PAYMENT_STATUSES,
            ),
            fulfillment_status=_mapped_status(
                record.get("fulfillment_status"),
                "fulfilment status",
                FULFILLMENT_STATUSES,
            ),
            cancellation_state=_mapped_status(
                record.get("cancellation_state"),
                "cancellation state",
                CANCEL_STATES,
            ),
            total_value=_money(record.get("total_value")),
            currency=_public_text(
                record.get("currency"),
                "currency",
                maximum=3,
                pattern=CURRENCY_PATTERN,
            ),
            line_item_count=line_item_count,
        )
