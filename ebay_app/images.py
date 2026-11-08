"""Phase 8 service for explicitly confirmed published-Offer image additions.

Mirrors QuantityService's shape exactly (review -> one-use confirmation ->
journaled result), which itself mirrors PriceService's and DraftService's
shape but is proportionate to a single-mutation write. Two things make this
review step different from price/quantity review, both handled here rather
than in the client:

- the candidate image URL points at an untrusted, seller-supplied external
  host (never eBay's own API), so review performs a live, read-only check
  of that URL via ``image_fetch.check_image_url`` before anything is
  stored -- a bad or unreachable URL is rejected as a normal validation
  error, the same way an invalid price or quantity is
- the "expected" state being protected against drift is the inventory
  item's current list of image URLs (not a price or quantity scalar);
  ``add_inventory_item_image()`` on the client already re-fetches the
  Offer and Inventory item, re-verifies identity/SKU/status, and compares
  the stored "expected" image list against the live list immediately
  before writing -- raising ImagesChangedError on any drift. That
  re-verification is this feature's reconciliation safety net, exactly as
  it is for PriceService and QuantityService.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional

from .config import AppConfig, DRAFT_SCOPES, granted_scopes_cover
from .ebay_client import EbayError, EbaySandboxClient
from .image_fetch import ImageUrlError, check_image_url
from .storage import AppDatabase, ConnectionChangedStorageError
from .validation import ValidationError, validate_image_add_form

IMAGE_ACTION_KIND = "add_image"
IMAGE_PAYLOAD_VERSION = 1

UrlChecker = Callable[[str], Dict[str, Any]]

# Same logger name as web.py/drafts.py/prices.py/quantities.py, so this
# lands in the same server console output. Full eBay error detail
# (status/code/details) is deliberately logged here only -- never persisted
# to the database or shown in the browser -- so the owner can see exactly
# why a confirm failed by reading their own terminal, without that detail
# leaking anywhere else.
_logger = logging.getLogger("ebay_sandbox_app")


@dataclass(frozen=True)
class ImageReview:
    """Public review data returned to the web layer for the review screen."""

    action_token: str
    offer_id: str
    sku: str
    expected_image_urls: List[str]
    new_image_url: str
    content_type: str
    content_length: int


class ImageService:
    """Prepare and execute one-use listing-image-addition confirmations.

    Review performs a read-only Offer and Inventory-item lookup, a
    read-only live check of the candidate image URL, and local encrypted
    storage of the reviewed values. Confirmation is the sole path to the
    one allowed eBay mutation (a targeted Inventory-item PUT) this stage
    permits, appending exactly one image URL.
    """

    def __init__(
        self,
        ebay: EbaySandboxClient,
        database: AppDatabase,
        config: AppConfig,
        url_checker: UrlChecker = check_image_url,
    ) -> None:
        self.ebay = ebay
        self.database = database
        self.config = config
        self._url_checker = url_checker
        # Section 6 safety control, mirroring PriceService exactly: how many
        # image additions THIS PROCESS has actually confirmed so far.
        # Deliberately in-memory and reset on every restart -- "per run"
        # means per execution of run_production.py, not a rolling window, so
        # a restart is itself always a safe, visible way to reset the budget
        # (the owner has to deliberately restart the app). Shares one budget
        # with PriceService's and QuantityService's own counters in spirit
        # only, not in state: each service tracks its own count against the
        # same configured ceiling, consistent with each one-use confirmation
        # being a single action.
        self._actions_this_run = 0

    def _require_stage(self) -> None:
        if self.config.feature_stage != "images":
            raise EbayError(
                "Adding a listing image is locked in this application stage",
                status=404,
                code="IMAGE_STAGE_LOCKED",
            )
        if self.config.runtime_mode != "local":
            raise EbayError(
                "Image data and confirmations are disabled in OAuth authorization mode",
                status=409,
                code="IMAGE_LOCAL_MODE_REQUIRED",
            )
        if not granted_scopes_cover(self.database.granted_scopes(), DRAFT_SCOPES):
            raise EbayError(
                "Authorize and verify Inventory write permission before adding an image",
                status=409,
                code="IMAGE_PERMISSION_REQUIRED",
            )

    def _check_actions_budget(self) -> None:
        if self._actions_this_run >= self.config.max_actions_per_run:
            raise EbayError(
                "This run has reached its maximum number of automated image "
                "additions; restart the application to reset the budget",
                status=429,
                code="MAX_ACTIONS_PER_RUN_REACHED",
            )

    def prepare_review(
        self,
        form: Mapping[str, Any],
        session_id: str,
    ) -> ImageReview:
        self._require_stage()
        self._check_actions_budget()
        fields = validate_image_add_form(form)
        offer_id = fields["offer_id"]
        sku = fields["sku"]
        new_image_url = fields["image_url"]
        generation = self.database.connection_generation()
        if generation is None:
            raise EbayError(
                f"Connect an eBay {self.config.environment.capitalize()} seller account first",
                status=409,
                code="NOT_CONNECTED",
            )
        offer = self.ebay.get_offer(offer_id)
        if offer.get("offerId") != offer_id:
            raise EbayError(
                "eBay returned a different Offer than the one selected",
                status=409,
                code="OFFER_MISMATCH",
            )
        if offer.get("sku") != sku:
            raise EbayError(
                "The Offer does not match the selected SKU",
                status=409,
                code="OFFER_SKU_MISMATCH",
            )
        if offer.get("status") != "PUBLISHED":
            raise EbayError(
                "Only published Offers can use the targeted image-add operation",
                status=409,
                code="OFFER_NOT_PUBLISHED",
            )
        item = self.ebay.get_inventory_item(sku)
        if item is None:
            raise EbayError(
                "The Inventory item no longer exists",
                status=409,
                code="INVENTORY_ITEM_MISSING",
            )
        current = self.ebay.inventory_item_image_urls(item)
        if new_image_url in current:
            raise ValidationError(
                {"image_url": "This image is already attached to the listing."}
            )
        if len(current) >= 24:
            raise EbayError(
                "eBay allows at most 24 images on one listing",
                status=409,
                code="IMAGE_LIMIT_REACHED",
            )
        try:
            checked = self._url_checker(new_image_url)
        except ImageUrlError as error:
            raise ValidationError({"image_url": str(error)}) from error
        payload = {
            "version": IMAGE_PAYLOAD_VERSION,
            "offer_id": offer_id,
            "sku": sku,
            "expected_image_urls": current,
            "new_image_url": new_image_url,
        }
        try:
            token = self.database.create_pending_action(
                session_id,
                IMAGE_ACTION_KIND,
                payload,
                expected_generation=generation,
            )
        except ConnectionChangedStorageError as error:
            raise EbayError(
                f"The {self.config.environment.capitalize()} seller connection changed during review; "
                "review again",
                status=409,
                code="CONNECTION_CHANGED",
            ) from error
        return ImageReview(
            action_token=token,
            offer_id=offer_id,
            sku=sku,
            expected_image_urls=current,
            new_image_url=new_image_url,
            content_type=str(checked.get("content_type", "")),
            content_length=int(checked.get("content_length", 0)),
        )

    def confirm(self, token: str, session_id: str) -> Optional[str]:
        self._require_stage()
        self._check_actions_budget()
        action = self.database.claim_pending_action(
            token, session_id, expected_kind=IMAGE_ACTION_KIND
        )
        if action is None:
            return None
        payload = action.payload
        if not isinstance(payload, Mapping) or set(payload) != {
            "version",
            "offer_id",
            "sku",
            "expected_image_urls",
            "new_image_url",
        } or payload.get("version") != IMAGE_PAYLOAD_VERSION:
            self.database.complete_action(
                action.action_id,
                "failed",
                error_message="The saved image confirmation is invalid",
            )
            return action.action_id
        expected_image_urls = payload.get("expected_image_urls")
        if not isinstance(expected_image_urls, list) or not all(
            isinstance(url, str) for url in expected_image_urls
        ):
            self.database.complete_action(
                action.action_id,
                "failed",
                error_message="The saved image confirmation is invalid",
            )
            return action.action_id
        if self.config.dry_run:
            # The kill switch's other half (see AppConfig.dry_run), mirroring
            # PriceService.confirm() exactly: claim and journal the action as
            # normal, but never call eBay at all. The actions-per-run budget
            # still advances, so a dry run rehearses the same limits a real
            # run would hit.
            self.database.complete_action(
                action.action_id,
                "succeeded",
                result={
                    "dry_run": True,
                    "offer_id": str(payload["offer_id"]),
                    "sku": str(payload["sku"]),
                    "image_url": str(payload["new_image_url"]),
                },
            )
            self._actions_this_run += 1
            return action.action_id
        try:
            with self.ebay.image_mutation_guard():
                result = self.ebay.add_inventory_item_image(
                    offer_id=str(payload["offer_id"]),
                    sku=str(payload["sku"]),
                    expected_image_urls=list(expected_image_urls),
                    new_image_url=str(payload["new_image_url"]),
                    expected_generation=action.grant_generation,
                )
            self.database.complete_action(action.action_id, "succeeded", result=result)
            self._actions_this_run += 1
        except EbayError as error:
            _logger.warning(
                "Image confirmation failed code=%s status=%s details=%r",
                error.code,
                error.status,
                error.details,
            )
            status = "unknown" if error.outcome_unknown else "failed"
            error_result: Dict[str, Any] = {
                "safe_code": error.code,
                "reconciliation_required": bool(error.outcome_unknown),
            }
            self.database.complete_action(
                action.action_id,
                status,
                result=error_result,
                error_message=str(error),
            )
        except Exception:
            # If an unanticipated local failure occurs after a remote request
            # was started, never imply that retrying is safe.
            self.database.complete_action(
                action.action_id,
                "unknown",
                error_message=(
                    "The application failed after confirmation; reconcile in "
                    "eBay before retrying"
                ),
            )
            raise
        return action.action_id

    def cancel(self, token: str, session_id: str) -> bool:
        self._require_stage()
        return self.database.cancel_pending_action(
            token,
            session_id,
            expected_kind=IMAGE_ACTION_KIND,
        )
