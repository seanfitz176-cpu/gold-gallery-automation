"""Phase 6 service for explicitly confirmed published-Offer price changes.

Mirrors DraftService's shape (review -> one-use confirmation -> journaled
result), but is proportionate to a single-mutation write. change_price()
on the client already re-fetches the Offer, re-verifies its identity, SKU
and status, and compares the stored "expected" price against the live
price immediately before writing -- raising PriceChangedError on any
drift. That re-verification is this feature's reconciliation safety net,
in place of DraftService's seller/resource binding (there is no new
resource being created here to protect against a swapped-seller
collision; the target Offer already exists and is re-verified fresh).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Mapping, Optional

from .config import AppConfig, DRAFT_SCOPES, granted_scopes_cover
from .ebay_client import EbayError, EbaySandboxClient
from .storage import AppDatabase, ConnectionChangedStorageError
from .validation import ValidationError, validate_price_change_form

PRICE_ACTION_KIND = "change_price"
PRICE_PAYLOAD_VERSION = 1

# Same logger name as web.py/drafts.py, so this lands in the same server
# console output. Full eBay error detail (status/code/details) is
# deliberately logged here only -- never persisted to the database or shown
# in the browser -- so the owner can see exactly why a confirm failed by
# reading their own terminal, without that detail leaking anywhere else.
_logger = logging.getLogger("ebay_sandbox_app")


@dataclass(frozen=True)
class PriceReview:
    """Public review data returned to the web layer for the review screen."""

    action_token: str
    offer_id: str
    sku: str
    expected_price: str
    currency: str
    new_price: str


class PriceService:
    """Prepare and execute one-use price-change confirmations.

    Review performs only a read-only Offer lookup plus local encrypted
    storage of the reviewed values. Confirmation is the sole path to the
    one allowed eBay mutation (``bulk_update_price_quantity``).
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
        # Section 6 safety control: how many price changes THIS PROCESS has
        # actually confirmed so far. Deliberately in-memory and reset on every
        # restart -- "per run" means per execution of run_production.py, not a
        # rolling window, so a restart is itself always a safe, visible way to
        # reset the budget (the owner has to deliberately restart the app).
        self._actions_this_run = 0

    def _require_stage(self) -> None:
        # ``quantities`` and ``images`` are later, cumulative stages: each
        # retains every ``prices`` capability in addition to its own single
        # mutation, so PriceService stays reachable from both of them too.
        if self.config.feature_stage not in {"prices", "quantities", "images"}:
            raise EbayError(
                "Editing a listing price is locked in this application stage",
                status=404,
                code="PRICE_STAGE_LOCKED",
            )
        if self.config.runtime_mode != "local":
            raise EbayError(
                "Price data and confirmations are disabled in OAuth authorization mode",
                status=409,
                code="PRICE_LOCAL_MODE_REQUIRED",
            )
        if not granted_scopes_cover(self.database.granted_scopes(), DRAFT_SCOPES):
            raise EbayError(
                "Authorize and verify Inventory write permission before changing a price",
                status=409,
                code="PRICE_PERMISSION_REQUIRED",
            )

    def _check_actions_budget(self) -> None:
        if self._actions_this_run >= self.config.max_actions_per_run:
            raise EbayError(
                "This run has reached its maximum number of automated price changes; "
                "restart the application to reset the budget",
                status=429,
                code="MAX_ACTIONS_PER_RUN_REACHED",
            )

    def _check_price_safety_limits(self, sku: str, current_value: str, new_price: str) -> None:
        """Section 6 production price-safety controls. Applied once, here, at
        review time: both prices needed for the check are only available
        together at this point, and the existing expected-price re-verification
        in ``change_price`` already protects against the live price drifting
        before confirmation, so re-checking at confirm time would be redundant."""
        limit = self.database.get_price_limit(sku)
        if limit is None:
            if self.config.require_price_floor:
                raise EbayError(
                    f"Set a floor price for SKU {sku} before an automated price "
                    "change can be made",
                    status=409,
                    code="PRICE_FLOOR_REQUIRED",
                )
            cap_percent = self.config.max_price_change_percent
        else:
            override = limit.get("max_change_percent")
            cap_percent = Decimal(override) if override else self.config.max_price_change_percent
            try:
                floor = Decimal(str(limit["floor_price"]))
                candidate = Decimal(new_price)
            except InvalidOperation as exc:
                raise EbayError(
                    "The stored floor price could not be read", code="INVALID_EBAY_RESPONSE"
                ) from exc
            if candidate < floor:
                raise EbayError(
                    f"{new_price} is below the configured floor price "
                    f"({limit['floor_currency']} {limit['floor_price']}) for SKU {sku}",
                    status=409,
                    code="PRICE_BELOW_FLOOR",
                )
        try:
            current = Decimal(current_value)
            candidate = Decimal(new_price)
        except InvalidOperation as exc:
            raise EbayError(
                "The current Offer price could not be read", code="INVALID_EBAY_RESPONSE"
            ) from exc
        if current <= 0:
            raise EbayError(
                "The current Offer price is invalid", code="INVALID_EBAY_RESPONSE"
            )
        change_percent = abs(candidate - current) / current * 100
        if change_percent > cap_percent:
            raise EbayError(
                f"A {change_percent.quantize(Decimal('0.1'))}% price change exceeds the "
                f"{cap_percent}% limit for SKU {sku}",
                status=409,
                code="PRICE_CHANGE_EXCEEDS_LIMIT",
            )

    def prepare_review(
        self,
        form: Mapping[str, Any],
        session_id: str,
    ) -> PriceReview:
        self._require_stage()
        self._check_actions_budget()
        fields = validate_price_change_form(form)
        offer_id = fields["offer_id"]
        sku = fields["sku"]
        new_price = fields["new_price"]
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
                "Only published Offers can use the targeted price update",
                status=409,
                code="OFFER_NOT_PUBLISHED",
            )
        current = self.ebay.offer_price(offer)
        if not current["value"] or not current["currency"]:
            raise EbayError(
                "The current Offer price is missing",
                code="INVALID_EBAY_RESPONSE",
            )
        if current["value"] == new_price:
            raise ValidationError(
                {"new_price": "Enter a price different from the current Offer price."}
            )
        self._check_price_safety_limits(sku, current["value"], new_price)
        payload = {
            "version": PRICE_PAYLOAD_VERSION,
            "offer_id": offer_id,
            "sku": sku,
            "expected_price": current["value"],
            "currency": current["currency"],
            "new_price": new_price,
        }
        try:
            token = self.database.create_pending_action(
                session_id,
                PRICE_ACTION_KIND,
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
        return PriceReview(
            action_token=token,
            offer_id=offer_id,
            sku=sku,
            expected_price=current["value"],
            currency=current["currency"],
            new_price=new_price,
        )

    def confirm(self, token: str, session_id: str) -> Optional[str]:
        self._require_stage()
        self._check_actions_budget()
        action = self.database.claim_pending_action(
            token, session_id, expected_kind=PRICE_ACTION_KIND
        )
        if action is None:
            return None
        payload = action.payload
        if not isinstance(payload, Mapping) or set(payload) != {
            "version",
            "offer_id",
            "sku",
            "expected_price",
            "currency",
            "new_price",
        } or payload.get("version") != PRICE_PAYLOAD_VERSION:
            self.database.complete_action(
                action.action_id,
                "failed",
                error_message="The saved price confirmation is invalid",
            )
            return action.action_id
        if self.config.dry_run:
            # The kill switch's other half (see AppConfig.dry_run): claim and
            # journal the action exactly as normal, but never call eBay at
            # all. The actions-per-run budget still advances, so a dry run
            # rehearses the same limits a real run would hit.
            self.database.complete_action(
                action.action_id,
                "succeeded",
                result={
                    "dry_run": True,
                    "offer_id": str(payload["offer_id"]),
                    "sku": str(payload["sku"]),
                    "price": str(payload["new_price"]),
                    "currency": str(payload["currency"]),
                },
            )
            self._actions_this_run += 1
            return action.action_id
        try:
            with self.ebay.price_mutation_guard():
                result = self.ebay.change_price(
                    offer_id=str(payload["offer_id"]),
                    sku=str(payload["sku"]),
                    expected_price=str(payload["expected_price"]),
                    currency=str(payload["currency"]),
                    new_price=str(payload["new_price"]),
                    expected_generation=action.grant_generation,
                )
            self.database.complete_action(action.action_id, "succeeded", result=result)
            self._actions_this_run += 1
        except EbayError as error:
            _logger.warning(
                "Price confirmation failed code=%s status=%s details=%r",
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
            expected_kind=PRICE_ACTION_KIND,
        )
