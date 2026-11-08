"""Phase 7 service for explicitly confirmed published-Offer quantity changes.

Mirrors PriceService's shape exactly (review -> one-use confirmation ->
journaled result), which itself mirrors DraftService's shape but is
proportionate to a single-mutation write. change_quantity() on the client
already re-fetches the Offer, re-verifies its identity, SKU and status, and
compares the stored "expected" quantity against the live quantity
immediately before writing -- raising QuantityChangedError on any drift.
That re-verification is this feature's reconciliation safety net, exactly
as it is for PriceService.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from .config import AppConfig, DRAFT_SCOPES, granted_scopes_cover
from .ebay_client import EbayError, EbaySandboxClient
from .storage import AppDatabase, ConnectionChangedStorageError
from .validation import ValidationError, validate_quantity_change_form

QUANTITY_ACTION_KIND = "change_quantity"
QUANTITY_PAYLOAD_VERSION = 1

# Same logger name as web.py/drafts.py/prices.py, so this lands in the same
# server console output. Full eBay error detail (status/code/details) is
# deliberately logged here only -- never persisted to the database or shown
# in the browser -- so the owner can see exactly why a confirm failed by
# reading their own terminal, without that detail leaking anywhere else.
_logger = logging.getLogger("ebay_sandbox_app")


@dataclass(frozen=True)
class QuantityReview:
    """Public review data returned to the web layer for the review screen."""

    action_token: str
    offer_id: str
    sku: str
    expected_quantity: int
    new_quantity: int


class QuantityService:
    """Prepare and execute one-use quantity-change confirmations.

    Review performs only a read-only Offer lookup plus local encrypted
    storage of the reviewed values. Confirmation is the sole path to the
    one allowed eBay mutation (``bulk_update_price_quantity``) this stage
    permits, targeting the quantity field.
    """

    def __init__(
        self,
        ebay: EbaySandboxClient,
        database: AppDatabase,
        config: AppConfig,
    ) -> None:
        self.ebay = ebay
        self.database = database
        self.config = config
        # Section 6 safety control, mirroring PriceService exactly: how many
        # quantity changes THIS PROCESS has actually confirmed so far.
        # Deliberately in-memory and reset on every restart -- "per run"
        # means per execution of run_production.py, not a rolling window, so
        # a restart is itself always a safe, visible way to reset the budget
        # (the owner has to deliberately restart the app). Shares one budget
        # with PriceService's own counter in spirit only, not in state: each
        # service tracks its own count against the same configured ceiling,
        # consistent with each one-use confirmation being a single action.
        self._actions_this_run = 0

    def _require_stage(self) -> None:
        # ``images`` is a later, cumulative stage: it retains every
        # ``quantities`` capability in addition to adding a listing image,
        # so QuantityService stays reachable from it too.
        if self.config.feature_stage not in {"quantities", "images"}:
            raise EbayError(
                "Editing a listing quantity is locked in this application stage",
                status=404,
                code="QUANTITY_STAGE_LOCKED",
            )
        if self.config.runtime_mode != "local":
            raise EbayError(
                "Quantity data and confirmations are disabled in OAuth authorization mode",
                status=409,
                code="QUANTITY_LOCAL_MODE_REQUIRED",
            )
        if not granted_scopes_cover(self.database.granted_scopes(), DRAFT_SCOPES):
            raise EbayError(
                "Authorize and verify Inventory write permission before changing a quantity",
                status=409,
                code="QUANTITY_PERMISSION_REQUIRED",
            )

    def _check_actions_budget(self) -> None:
        if self._actions_this_run >= self.config.max_actions_per_run:
            raise EbayError(
                "This run has reached its maximum number of automated quantity "
                "changes; restart the application to reset the budget",
                status=429,
                code="MAX_ACTIONS_PER_RUN_REACHED",
            )

    def prepare_review(
        self,
        form: Mapping[str, Any],
        session_id: str,
    ) -> QuantityReview:
        self._require_stage()
        self._check_actions_budget()
        fields = validate_quantity_change_form(form)
        offer_id = fields["offer_id"]
        sku = fields["sku"]
        new_quantity = fields["new_quantity"]
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
                "Only published Offers can use the targeted quantity update",
                status=409,
                code="OFFER_NOT_PUBLISHED",
            )
        current = self.ebay.offer_quantity(offer)
        if current is None:
            raise EbayError(
                "The current Offer quantity is missing",
                code="INVALID_EBAY_RESPONSE",
            )
        if current == new_quantity:
            raise ValidationError(
                {"new_quantity": "Enter a quantity different from the current Offer quantity."}
            )
        payload = {
            "version": QUANTITY_PAYLOAD_VERSION,
            "offer_id": offer_id,
            "sku": sku,
            "expected_quantity": current,
            "new_quantity": new_quantity,
        }
        try:
            token = self.database.create_pending_action(
                session_id,
                QUANTITY_ACTION_KIND,
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
        return QuantityReview(
            action_token=token,
            offer_id=offer_id,
            sku=sku,
            expected_quantity=current,
            new_quantity=new_quantity,
        )

    def confirm(self, token: str, session_id: str) -> Optional[str]:
        self._require_stage()
        self._check_actions_budget()
        action = self.database.claim_pending_action(
            token, session_id, expected_kind=QUANTITY_ACTION_KIND
        )
        if action is None:
            return None
        payload = action.payload
        if not isinstance(payload, Mapping) or set(payload) != {
            "version",
            "offer_id",
            "sku",
            "expected_quantity",
            "new_quantity",
        } or payload.get("version") != QUANTITY_PAYLOAD_VERSION:
            self.database.complete_action(
                action.action_id,
                "failed",
                error_message="The saved quantity confirmation is invalid",
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
                    "quantity": str(payload["new_quantity"]),
                },
            )
            self._actions_this_run += 1
            return action.action_id
        try:
            with self.ebay.quantity_mutation_guard():
                result = self.ebay.change_quantity(
                    offer_id=str(payload["offer_id"]),
                    sku=str(payload["sku"]),
                    expected_quantity=int(payload["expected_quantity"]),
                    new_quantity=int(payload["new_quantity"]),
                    expected_generation=action.grant_generation,
                )
            self.database.complete_action(action.action_id, "succeeded", result=result)
            self._actions_this_run += 1
        except EbayError as error:
            _logger.warning(
                "Quantity confirmation failed code=%s status=%s details=%r",
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
            expected_kind=QUANTITY_ACTION_KIND,
        )
