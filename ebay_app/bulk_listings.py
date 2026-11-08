"""Bulk-create many unpublished Inventory items/Offers from a CSV file.

WHY THIS EXISTS: ``DraftService`` creates exactly one unpublished Inventory
item + Offer at a time, behind an interactive web review/confirm flow --
right-sized for a human adding one item through the dashboard, but not for
bringing dozens or hundreds of brand-new SKUs onto the account at once.

This module adds no new eBay capability at all. It drives the exact same,
already-tested, already-live-verified client methods ``DraftService``'s own
``confirm()`` step uses for a single item --
``create_verified_inventory_item``, ``verify_inventory_item``,
``create_verified_unpublished_offer``, ``verify_unpublished_offer`` -- once
per CSV row instead of once per web-form submission. Every item gets the
exact same pre-write existence check and full post-write field-by-field
reconciliation against eBay's own stored data that a single hand-created
draft already gets; nothing about the per-item safety or accuracy is
relaxed for bulk use. It reuses the dashboard's own ``validate_draft``
function for every row too, so a row that the web form would reject is
rejected here with the identical message.

WHAT THIS DELIBERATELY DOES NOT DO:
- It never publishes anything. Every item it creates is an UNPUBLISHED
  Offer, exactly like every other draft this app has ever created -- there
  is no code path anywhere in this app that can publish, and adding one is
  a separate, deliberate decision this module does not make implicitly.
- It never fetches, downloads, or hosts an image. An optional
  ``image_urls`` column lets each row name up to 24 seller-hosted https
  image addresses, which are sent in the Inventory item's own
  ``product.imageUrls`` field at creation time and reconciled exactly on
  readback, like every other field. The addresses are checked structurally
  only (https, no credentials or fragment, no localhost or private IP
  literal, length, count, no duplicates) -- loading the CSV still makes no
  network call. eBay itself fetches the images when the listing is later
  published.
- It never retries a failed row automatically, and one row's failure never
  stops any other row from being attempted -- each row is fully independent.

A row's eBay-side outcome is reported as one of four statuses, following the
exact same reasoning ``DraftService.confirm()`` already uses for a single
item (see that method's own except-clause): ``"created"`` (both the
Inventory item and the Offer were created and independently re-verified --
done, nothing further to check), ``"skipped_already_exists"`` (the SKU
already existed on the account before this row was attempted -- left
completely untouched), ``"partial"`` (the Inventory item write succeeded but
the Offer write or its verification did not -- this SKU now exists on the
account with NO Offer, and because the next run's own existence check will
see the SKU and treat it as already handled, re-running the same CSV will
NOT retry or complete it; the row must be checked and fixed by hand), and
``"unknown"`` (the Offer may or may not have actually been created -- its
own creation call's result could not be confirmed, so eBay must be checked
directly for this SKU before assuming anything either way). ``"partial"``
and ``"unknown"`` are never silently folded into ``"failed"``: a genuine
``"failed"`` row made no eBay write at all and is safe to simply fix in the
CSV and re-run.

CSV FORMAT: one row per listing. The header row must name every column in
``REQUIRED_COLUMNS`` (case-insensitive, any column order): sku, title,
description, condition, quantity, price, category_id. ``aspects`` is an
optional extra column; when present it must be a JSON object shaped exactly
like the dashboard's own draft form accepts, e.g. ``{"Brand": ["Acme"]}`` --
an empty cell means no aspects for that row. ``image_urls`` is a second
optional column: zero to 24 https image addresses separated by ``|`` (the
first is the main picture), e.g.
``https://example.com/a.jpg|https://example.com/b.jpg``; an empty cell means
no images for that row. There is no ``currency``
column: every row uses the account's single configured currency
(``AppConfig.currency``), matching every other part of this app. SKUs are
used exactly as given in the CSV -- unlike the dashboard's single-draft
form, no random suffix is appended, since a bulk upload needs predictable,
caller-chosen SKUs; eBay's own pre-write existence check still protects
against a collision with anything already on the account.
"""

from __future__ import annotations

import csv
import ipaddress
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Sequence, Set, Tuple

from .config import AppConfig, DRAFT_SCOPES, granted_scopes_cover
from .ebay_client import EbayError, EbaySandboxClient, SkuExistsError
from .storage import AppDatabase, VerifiedConnectionBinding
from .validation import ValidationError, validate_draft

REQUIRED_COLUMNS: Tuple[str, ...] = (
    "sku",
    "title",
    "description",
    "condition",
    "quantity",
    "price",
    "category_id",
)
OPTIONAL_COLUMNS: Tuple[str, ...] = ("aspects", "image_urls")
ALL_COLUMNS: Tuple[str, ...] = REQUIRED_COLUMNS + OPTIONAL_COLUMNS
# The columns handed to the dashboard's own ``validate_draft``. ``image_urls``
# is deliberately validated separately (``parse_image_urls``) because the
# dashboard's single-draft form intentionally does not accept images.
DRAFT_FORM_COLUMNS: Tuple[str, ...] = REQUIRED_COLUMNS + ("aspects",)

# eBay's documented maximum number of pictures per listing.
MAX_IMAGES_PER_LISTING = 24
# Matches ``image_fetch.MAX_URL_LENGTH``, the limit the published-listing
# image path already applies.
MAX_IMAGE_URL_LENGTH = 2000
IMAGE_URL_SEPARATOR = "|"


def parse_image_urls(raw: str) -> List[str]:
    """Split and structurally validate one row's ``image_urls`` cell.

    Returns the list of addresses (possibly empty). Raises ``ValueError``
    with a plain-English message on the first problem. Makes no network
    call: reachability and content are checked by eBay when it fetches the
    pictures at publish time.
    """

    if not raw.strip():
        return []
    urls = [part.strip() for part in raw.split(IMAGE_URL_SEPARATOR)]
    if any(not url for url in urls):
        raise ValueError("Image addresses must be separated by a single | with no empty entries.")
    if len(urls) > MAX_IMAGES_PER_LISTING:
        raise ValueError(f"A listing can have at most {MAX_IMAGES_PER_LISTING} images.")
    if len(set(urls)) != len(urls):
        raise ValueError("The same image address is listed more than once.")
    for url in urls:
        if len(url) > MAX_IMAGE_URL_LENGTH or any(
            ord(character) <= 0x20 or ord(character) == 0x7F for character in url
        ):
            raise ValueError("Each image address must be a plain https:// link with no spaces.")
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError as exc:
            raise ValueError("Each image address must be a plain https:// link.") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("Each image address must be a plain https:// link.")
        host = parsed.hostname.lower()
        if host == "localhost" or host.endswith(".localhost"):
            raise ValueError("Image addresses must point at a public host.")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("Image addresses must point at a public host.")
    return urls

# A generous but finite safety cap, matching this project's standing
# "fail closed rather than run away" principle -- not a tuned business
# number, just a ceiling nothing legitimate should ever hit by accident
# (a 300-item real batch is nowhere near it).
MAX_ROWS = 2000


@dataclass(frozen=True)
class RowError:
    """One CSV row that failed local validation -- no eBay call was made."""

    row_number: int  # 1-based, matching the file's own line numbering (header is line 1)
    sku_hint: str
    errors: Dict[str, str]


@dataclass(frozen=True)
class ValidatedBatch:
    drafts: Tuple[Tuple[int, Dict[str, Any]], ...]  # (row_number, draft) pairs
    row_errors: Tuple[RowError, ...]


@dataclass(frozen=True)
class RowResult:
    row_number: int
    sku: str
    status: str  # "created", "skipped_already_exists", "partial", "unknown", or "failed"
    offer_id: str | None
    detail: str


def load_and_validate_csv(path: Path, currency: str) -> ValidatedBatch:
    """Parse and validate every row. Makes no network call whatsoever.

    A malformed or incomplete row is collected as a ``RowError`` and never
    stops any other row from being parsed -- the same "one bad row never
    sinks the batch" principle the live run itself follows.
    """

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise EbayError(
                "The CSV file has no header row", code="BULK_LISTINGS_EMPTY_FILE"
            )
        normalized_fields = {
            name.strip().lower() for name in reader.fieldnames if name
        }
        missing = [name for name in REQUIRED_COLUMNS if name not in normalized_fields]
        if missing:
            raise EbayError(
                "The CSV file is missing required column(s): " + ", ".join(missing),
                code="BULK_LISTINGS_MISSING_COLUMNS",
            )

        drafts: List[Tuple[int, Dict[str, Any]]] = []
        row_errors: List[RowError] = []
        seen_skus: Set[str] = set()

        for index, raw_row in enumerate(reader):
            row_number = index + 2  # header is line 1; first data row is line 2
            if row_number - 1 > MAX_ROWS:
                raise EbayError(
                    f"The CSV file exceeds the {MAX_ROWS}-row safety limit",
                    code="BULK_LISTINGS_TOO_MANY_ROWS",
                )
            normalized_row = {
                (key or "").strip().lower(): (value or "").strip()
                for key, value in raw_row.items()
                if key is not None
            }
            sku_hint = normalized_row.get("sku", "")
            form: Dict[str, List[str]] = {
                column: [normalized_row.get(column, "")] for column in DRAFT_FORM_COLUMNS
            }
            errors: Dict[str, str] = {}
            draft: Dict[str, Any] | None = None
            try:
                draft = validate_draft(form, currency)
            except ValidationError as error:
                errors.update(error.errors)
            try:
                image_urls = parse_image_urls(normalized_row.get("image_urls", ""))
            except ValueError as error:
                errors["image_urls"] = str(error)
                image_urls = []
            if errors or draft is None:
                row_errors.append(RowError(row_number, sku_hint, errors))
                continue
            if image_urls:
                draft["image_urls"] = image_urls
            if draft["sku"] in seen_skus:
                row_errors.append(
                    RowError(
                        row_number,
                        sku_hint,
                        {"sku": "Duplicate SKU within this CSV file."},
                    )
                )
                continue
            seen_skus.add(draft["sku"])
            drafts.append((row_number, draft))

        return ValidatedBatch(tuple(drafts), tuple(row_errors))


class BulkListingService:
    """Creates many unpublished Inventory items/Offers, one fully-verified
    item at a time, reusing ``DraftService``'s own proven client methods and
    the account's existing default Business Policies/merchant location."""

    def __init__(
        self,
        ebay: EbaySandboxClient,
        database: AppDatabase,
        config: AppConfig,
    ) -> None:
        self.ebay = ebay
        self.database = database
        self.config = config

    def _require_stage(self) -> None:
        # Deliberately the exact same stage set DraftService itself requires
        # -- this is not a new capability, just a new (CSV-driven) way to
        # reach the same, already-gated one.
        if self.config.feature_stage not in {"drafts", "prices", "quantities", "images"}:
            raise EbayError(
                "Bulk unpublished-offer creation is locked in this application stage",
                status=404,
                code="DRAFT_STAGE_LOCKED",
            )
        if self.config.runtime_mode != "local":
            raise EbayError(
                "Draft data and confirmations are disabled in OAuth authorization mode",
                status=409,
                code="DRAFT_LOCAL_MODE_REQUIRED",
            )

    def _settings(self) -> Dict[str, Any]:
        return {
            "marketplace_id": self.config.marketplace_id,
            "format": "FIXED_PRICE",
            "listing_duration": "GTC",
            "merchant_location_key": self.config.merchant_location_key,
            "fulfillment_policy_id": self.config.fulfillment_policy_id,
            "payment_policy_id": self.config.payment_policy_id,
            "return_policy_id": self.config.return_policy_id,
        }

    def verified_binding(self) -> VerifiedConnectionBinding:
        self._require_stage()
        if not granted_scopes_cover(self.database.granted_scopes(), DRAFT_SCOPES):
            raise EbayError(
                "Authorize and verify Inventory write permission before bulk-creating listings",
                status=409,
                code="DRAFT_PERMISSION_REQUIRED",
            )
        binding = self.database.verified_connection_binding()
        if binding is None:
            raise EbayError(
                "Verify the eBay "
                f"{self.config.environment.capitalize()} connection before "
                "bulk-creating listings",
                status=409,
                code="CONNECTION_NOT_VERIFIED",
            )
        return binding

    def create_batch(
        self,
        binding: VerifiedConnectionBinding,
        drafts: Sequence[Tuple[int, Dict[str, Any]]],
    ) -> List[RowResult]:
        """Create each (row_number, draft) pair in file order.

        Never aborts the whole batch over one row's failure -- each row's
        outcome is recorded independently and the next row is still
        attempted. A row whose SKU already exists on the account is
        deliberately left untouched (reported, not overwritten) rather than
        replaced, exactly like DraftService's own existing-SKU guard. A
        mid-sequence failure is classified exactly the way
        ``DraftService.confirm()`` already classifies one for a single item
        -- see this module's own docstring for what "partial" and "unknown"
        mean and why they are never folded into a plain "failed".
        """

        self._require_stage()
        settings = self._settings()
        results: List[RowResult] = []
        for row_number, draft in drafts:
            sku = str(draft["sku"])
            inventory_created = False
            offer_created = False
            offer_id: str | None = None
            try:
                self.ebay.create_verified_inventory_item(binding, draft)
                inventory_created = True
                self.ebay.verify_inventory_item(binding, draft)
                offer_id = self.ebay.create_verified_unpublished_offer(
                    binding, draft, settings
                )
                offer_created = True
                self.ebay.verify_unpublished_offer(binding, offer_id, draft, settings)
            except SkuExistsError:
                results.append(
                    RowResult(
                        row_number,
                        sku,
                        "skipped_already_exists",
                        None,
                        "An Inventory item with this SKU already exists on the "
                        "account -- left untouched, nothing was overwritten.",
                    )
                )
                continue
            except EbayError as error:
                detail_suffix = (
                    f" ({error} status={error.status}, code={error.code}, "
                    f"outcome_unknown={error.outcome_unknown})"
                )
                if offer_created or error.outcome_unknown:
                    status = "unknown"
                    detail = (
                        "The Offer's own creation outcome could not be confirmed -- "
                        "it may or may not actually exist on the account. Check this "
                        "SKU directly in Seller Hub or via the API before assuming "
                        "either way; do not re-run this row as-is." + detail_suffix
                    )
                elif inventory_created:
                    status = "partial"
                    detail = (
                        "The Inventory item was created but the Offer was not. This "
                        "SKU now exists on the account with NO Offer, and will be "
                        "reported as already existing (not retried) on any future "
                        "run of this script. Check this SKU in Seller Hub and either "
                        "create its Offer manually or remove the Inventory item." +
                        detail_suffix
                    )
                else:
                    status = "failed"
                    detail = "No eBay write was made for this row." + detail_suffix
                results.append(
                    RowResult(
                        row_number,
                        sku,
                        status,
                        offer_id if offer_created else None,
                        detail,
                    )
                )
                continue
            results.append(
                RowResult(
                    row_number, sku, "created", offer_id, "Created and verified."
                )
            )
        return results
