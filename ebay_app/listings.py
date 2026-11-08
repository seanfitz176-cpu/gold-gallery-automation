"""Phase 3 service for a minimal, read-only active-listing summary."""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Mapping, Sequence, Tuple

from .config import LISTING_SCOPES, granted_scopes_cover
from .ebay_client import EbayError, EbaySandboxClient
from .storage import AppDatabase


ACTIVE_LISTING_STATUSES = {"ACTIVE": "Active", "OUT_OF_STOCK": "Out of stock"}
MARKETPLACE_PATTERN = re.compile(r"^EBAY_[A-Z0-9_]{2,24}$")
LISTING_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
PRICE_PATTERN = re.compile(r"^(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,4})?$")
MAX_PUBLIC_LISTINGS = 20_000


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
            f"eBay returned an invalid active-listing {field}",
            code="INVALID_EBAY_RESPONSE",
        )
    return value


def _price(value: Any) -> str:
    raw = _public_text(value, "price", maximum=32, pattern=PRICE_PATTERN)
    try:
        parsed = Decimal(raw)
    except InvalidOperation as exc:  # Defensive: the regular expression already narrows input.
        raise EbayError(
            "eBay returned an invalid active-listing price",
            code="INVALID_EBAY_RESPONSE",
        ) from exc
    if not parsed.is_finite() or parsed < 0 or parsed > Decimal("999999999999"):
        raise EbayError(
            "eBay returned an invalid active-listing price",
            code="INVALID_EBAY_RESPONSE",
        )
    return format(parsed, "f")


@dataclass(frozen=True)
class ActiveListing:
    """The complete Phase 3 public field allowlist for one listing/SKU."""

    title: str
    sku: str
    listing_id: str
    listing_state: str
    marketplace: str
    price_value: str
    currency: str
    available_quantity: int

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ActiveListingSummary:
    environment: str
    retrieved_at: str
    listings: Tuple[ActiveListing, ...]

    def listing_dicts(self) -> Tuple[Dict[str, Any], ...]:
        return tuple(listing.as_dict() for listing in self.listings)


class ListingService:
    """Retrieve active Inventory listings without retaining raw eBay objects."""

    def __init__(
        self,
        ebay: EbaySandboxClient,
        database: AppDatabase,
        environment: str = "Sandbox",
        now=time.time,
    ) -> None:
        self.ebay = ebay
        self.database = database
        self.environment = environment
        self.now = now

    def retrieve(self) -> ActiveListingSummary:
        # Scope and verification checks happen before the client can make a request.
        if not granted_scopes_cover(self.database.granted_scopes(), LISTING_SCOPES):
            raise EbayError(
                "Additional read-only listing permission is required",
                status=409,
                code="LISTING_PERMISSION_REQUIRED",
            )
        binding = self.database.verified_connection_binding()
        if binding is None:
            raise EbayError(
                f"Verify the eBay {self.environment} connection before viewing listings",
                status=409,
                code="CONNECTION_NOT_VERIFIED",
            )

        raw_records = self.ebay.read_verified_listing_records(binding)
        if not isinstance(raw_records, list):
            raise EbayError(
                "eBay returned an invalid listing collection",
                code="INVALID_EBAY_RESPONSE",
            )

        public: list[ActiveListing] = []
        for record in raw_records:
            listing = self._active_listing(record)
            if listing is not None:
                public.append(listing)
                if len(public) > MAX_PUBLIC_LISTINGS:
                    raise EbayError(
                        "Active-listing response exceeded its safety limit",
                        code="INVALID_EBAY_RESPONSE",
                    )

        public.sort(key=lambda item: (item.title.casefold(), item.sku.casefold()))
        retrieved_at = datetime.fromtimestamp(self.now(), timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
        return ActiveListingSummary(
            environment=self.environment,
            retrieved_at=retrieved_at,
            listings=tuple(public),
        )

    @staticmethod
    def _active_listing(record: Any) -> ActiveListing | None:
        if not isinstance(record, Mapping):
            raise EbayError(
                "eBay returned an invalid listing record",
                code="INVALID_EBAY_RESPONSE",
            )
        offer = record.get("offer")
        if not isinstance(offer, Mapping) or offer.get("status") != "PUBLISHED":
            return None
        listing = offer.get("listing")
        if not isinstance(listing, Mapping):
            return None
        raw_status = listing.get("listingStatus")
        if raw_status not in ACTIVE_LISTING_STATUSES:
            return None

        inventory = record.get("inventory")
        if not isinstance(inventory, Mapping):
            raise EbayError(
                "eBay returned an invalid active-listing inventory item",
                code="INVALID_EBAY_RESPONSE",
            )
        product = inventory.get("product")
        if product is not None and not isinstance(product, Mapping):
            raise EbayError(
                "eBay returned an invalid active-listing product",
                code="INVALID_EBAY_RESPONSE",
            )
        raw_title = product.get("title") if isinstance(product, Mapping) else None
        title = (
            "Untitled"
            if raw_title is None or raw_title == ""
            else _public_text(raw_title, "title", maximum=200)
        )

        on_hold = listing.get("listingOnHold", False)
        if not isinstance(on_hold, bool):
            raise EbayError(
                "eBay returned an invalid active-listing hold state",
                code="INVALID_EBAY_RESPONSE",
            )

        pricing = offer.get("pricingSummary")
        price = pricing.get("price") if isinstance(pricing, Mapping) else None
        if not isinstance(price, Mapping):
            raise EbayError(
                "eBay returned an invalid active-listing price",
                code="INVALID_EBAY_RESPONSE",
            )
        # The Offer carries availableQuantity only when this app's own
        # create_offer() set it explicitly. An Offer eBay created itself --
        # e.g. via bulkMigrateListing, confirmed live on 2026-11-07 for
        # item 336629764656/SKU GG-336629764656 -- omits it entirely, and
        # eBay's documented behaviour is that the Inventory Item's own
        # availability then governs. Fall back to that before failing closed.
        quantity = offer.get("availableQuantity")
        if quantity is None:
            availability = inventory.get("availability")
            ship_to = (
                availability.get("shipToLocationAvailability")
                if isinstance(availability, Mapping)
                else None
            )
            quantity = (
                ship_to.get("quantity") if isinstance(ship_to, Mapping) else None
            )
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity < 0
            or quantity > 2_147_483_647
        ):
            raise EbayError(
                "eBay returned an invalid active-listing quantity",
                code="INVALID_EBAY_RESPONSE",
            )

        return ActiveListing(
            title=title,
            sku=_public_text(record.get("sku"), "SKU", maximum=50),
            listing_id=_public_text(
                listing.get("listingId"),
                "listing ID",
                maximum=128,
                pattern=LISTING_ID_PATTERN,
            ),
            listing_state="On hold" if on_hold else ACTIVE_LISTING_STATUSES[raw_status],
            marketplace=_public_text(
                offer.get("marketplaceId"),
                "marketplace",
                maximum=32,
                pattern=MARKETPLACE_PATTERN,
            ),
            price_value=_price(price.get("value")),
            currency=_public_text(
                price.get("currency"),
                "currency",
                maximum=3,
                pattern=CURRENCY_PATTERN,
            ),
            available_quantity=quantity,
        )
