"""Phase 5 service for explicitly confirmed, unpublished Sandbox offers."""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from typing import Any, Dict, Mapping

from .config import AppConfig, DRAFT_SCOPES, granted_scopes_cover
from .ebay_client import (
    EbayError,
    EbaySandboxClient,
    SkuExistsError,
)
from .storage import (
    AppDatabase,
    ClaimedAction,
    ConnectionChangedStorageError,
    StorageError,
    VerifiedConnectionBinding,
)
from .validation import ValidationError, validate_draft


DRAFT_ACTION_KIND = "create_draft"
DRAFT_PAYLOAD_VERSION = 1
_SKU_LABEL_MAX_LENGTH = 21

# Same logger name as web.py, so this lands in the same server console output.
# Full eBay error detail (status/code/details) is deliberately logged here only --
# never persisted to the database or shown in the browser -- so the owner can see
# exactly why a confirm failed by reading their own terminal, without that detail
# leaking anywhere else.
_logger = logging.getLogger("ebay_sandbox_app")


@dataclass(frozen=True)
class DraftReview:
    """Public review data; seller identity and grant internals stay private."""

    action_token: str
    draft: Dict[str, Any]
    settings: Dict[str, Any]


class DraftService:
    """Prepare and execute one-use unpublished-offer confirmations.

    Review performs only local encrypted storage plus a read-only SKU check.
    Confirmation is the sole path to the two allowed eBay mutations.
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

    def _require_local_stage(self) -> None:
        # ``prices``, ``quantities``, and ``images`` are later, cumulative
        # stages: each retains every ``drafts`` capability in addition to
        # its own single mutation, so DraftService stays reachable from all
        # of them too.
        if self.config.feature_stage not in {"drafts", "prices", "quantities", "images"}:
            raise EbayError(
                "Unpublished offer creation is locked in this application stage",
                status=404,
                code="DRAFT_STAGE_LOCKED",
            )
        if self.config.runtime_mode != "local":
            raise EbayError(
                "Draft data and confirmations are disabled in OAuth authorization mode",
                status=409,
                code="DRAFT_LOCAL_MODE_REQUIRED",
            )

    def _verified_binding(self) -> VerifiedConnectionBinding:
        self._require_local_stage()
        if not granted_scopes_cover(self.database.granted_scopes(), DRAFT_SCOPES):
            raise EbayError(
                "Authorize and verify Inventory write permission before creating a draft",
                status=409,
                code="DRAFT_PERMISSION_REQUIRED",
            )
        binding = self.database.verified_connection_binding()
        if binding is None:
            raise EbayError(
                f"Verify the eBay {self.config.environment.capitalize()} connection before creating a draft",
                status=409,
                code="CONNECTION_NOT_VERIFIED",
            )
        return binding

    def _settings(self) -> Dict[str, Any]:
        return {
            "environment": self.config.environment.capitalize(),
            "marketplace_id": self.config.marketplace_id,
            "currency": self.config.currency,
            "format": "FIXED_PRICE",
            "listing_duration": "GTC",
            "merchant_location_key": self.config.merchant_location_key,
            "fulfillment_policy_id": self.config.fulfillment_policy_id,
            "payment_policy_id": self.config.payment_policy_id,
            "return_policy_id": self.config.return_policy_id,
        }

    def prepare_review(
        self,
        form: Mapping[str, list[str]],
        session_id: str,
    ) -> DraftReview:
        binding = self._verified_binding()
        draft = validate_draft(form, self.config.currency)
        # eBay exposes only a create-or-replace PUT, not an atomic create-only
        # operation. Add an app-owned 96-bit suffix so a user-entered SKU cannot
        # collide with an existing or concurrently created seller record. The
        # final value is shown on the exact review screen before confirmation.
        # The marker itself reflects the real running environment -- this was
        # found hardcoded to "SBX" unconditionally until 2026-11-05, which
        # would have embedded a literally false "-SBX-" into a real Inventory
        # SKU the moment "prices" (which DraftService is reachable from, being
        # a cumulative capability) was ever used against Production.
        sku_marker = "PRD" if self.config.environment == "production" else "SBX"
        sku_label = draft["sku"]
        if len(sku_label) > _SKU_LABEL_MAX_LENGTH:
            raise ValidationError(
                {
                    "sku": (
                        "Use a short SKU label of 21 characters or fewer; the app adds "
                        "a unique identifying suffix before review."
                    )
                }
            )
        draft["sku"] = f"{sku_label}-{sku_marker}-{secrets.token_hex(12).upper()}"
        if self.ebay.verified_inventory_item_exists(binding, draft["sku"]):
            raise SkuExistsError(draft["sku"])
        settings = self._settings()
        payload = {
            "version": DRAFT_PAYLOAD_VERSION,
            "seller_user_id": binding.user_id,
            "draft": draft,
            "settings": settings,
        }
        try:
            token = self.database.create_pending_action(
                session_id,
                DRAFT_ACTION_KIND,
                payload,
                expected_generation=binding.grant_generation,
                expected_user_id=binding.user_id,
                resource_key=draft["sku"],
            )
        except ConnectionChangedStorageError as error:
            raise EbayError(
                f"The verified {self.config.environment.capitalize()} seller changed during review; review again",
                status=409,
                code="CONNECTION_CHANGED",
            ) from error
        except StorageError as error:
            if "active draft reservation" in str(error):
                raise EbayError(
                    "This SKU already has a confirmed or unresolved draft action",
                    status=409,
                    code="DRAFT_SKU_RESERVED",
                ) from error
            raise
        return DraftReview(token, draft, settings)

    def _validated_action(
        self,
        action: ClaimedAction,
        binding: VerifiedConnectionBinding,
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        payload = action.payload
        if not isinstance(payload, Mapping) or set(payload) != {
            "version",
            "seller_user_id",
            "draft",
            "settings",
        }:
            raise EbayError(
                "The saved draft confirmation is invalid",
                code="INVALID_SAVED_DRAFT",
            )
        if payload.get("version") != DRAFT_PAYLOAD_VERSION:
            raise EbayError(
                "The saved draft confirmation uses an unsupported version",
                code="INVALID_SAVED_DRAFT",
            )
        saved_user_id = payload.get("seller_user_id")
        if (
            not isinstance(saved_user_id, str)
            or not secrets.compare_digest(saved_user_id, binding.user_id)
            or not secrets.compare_digest(
                action.grant_generation, binding.grant_generation
            )
        ):
            raise EbayError(
                f"The verified {self.config.environment.capitalize()} seller changed after review",
                status=409,
                code="CONNECTION_CHANGED",
            )

        saved_draft = payload.get("draft")
        if not isinstance(saved_draft, Mapping) or set(saved_draft) != {
            "sku",
            "title",
            "description",
            "condition",
            "quantity",
            "price",
            "currency",
            "category_id",
            "aspects",
        }:
            raise EbayError(
                "The saved draft fields are invalid",
                code="INVALID_SAVED_DRAFT",
            )
        validation_form = {
            "csrf_token": ["stored"],
            "sku": [saved_draft.get("sku")],
            "title": [saved_draft.get("title")],
            "description": [saved_draft.get("description")],
            "condition": [saved_draft.get("condition")],
            "quantity": [str(saved_draft.get("quantity"))],
            "price": [saved_draft.get("price")],
            "category_id": [saved_draft.get("category_id")],
            "aspects": [
                json.dumps(saved_draft.get("aspects"), separators=(",", ":"))
            ],
        }
        try:
            normalized = validate_draft(validation_form, self.config.currency)
        except (ValidationError, TypeError, ValueError) as error:
            raise EbayError(
                "The saved draft fields failed validation",
                code="INVALID_SAVED_DRAFT",
            ) from error
        if normalized != dict(saved_draft):
            raise EbayError(
                "The saved draft fields no longer match their reviewed values",
                code="INVALID_SAVED_DRAFT",
            )

        saved_settings = payload.get("settings")
        current_settings = self._settings()
        if not isinstance(saved_settings, Mapping) or dict(saved_settings) != current_settings:
            raise EbayError(
                "Application listing settings changed after review; review again",
                status=409,
                code="DRAFT_SETTINGS_CHANGED",
            )
        return normalized, current_settings

    @staticmethod
    def _failure_result(
        *,
        sku: str,
        error: EbayError,
        stage: str,
        inventory_created: bool,
        offer_created: bool,
        offer_id: str | None,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "sku": sku,
            "safe_code": error.code,
            "failure_stage": stage,
            "inventory_created": inventory_created,
            "offer_created": offer_created,
            "reconciliation_required": bool(
                error.outcome_unknown or inventory_created or offer_created
            ),
        }
        if offer_id:
            result["offer_id"] = offer_id
        if isinstance(error, SkuExistsError):
            result["existing_inventory"] = True
        return result

    def confirm(self, token: str, session_id: str) -> str | None:
        binding = self._verified_binding()
        action = self.database.claim_pending_action(
            token,
            session_id,
            expected_kind=DRAFT_ACTION_KIND,
        )
        if action is None:
            return None

        inventory_created = False
        offer_created = False
        offer_id: str | None = None
        stage = "confirmed"
        try:
            # Claim and current binding are checked independently so a grant or
            # seller change cannot convert an old preview into a new seller write.
            current_binding = self._verified_binding()
            draft, settings = self._validated_action(action, current_binding)
            sku = draft["sku"]
            with self.ebay.draft_mutation_guard():
                stage = "inventory_request_started"
                self.database.update_action_checkpoint(
                    action.action_id,
                    stage,
                    {"sku": sku},
                )
                self.ebay.create_verified_inventory_item(
                    current_binding,
                    draft,
                )
                inventory_created = True
                stage = "inventory_created"
                self.database.update_action_checkpoint(
                    action.action_id,
                    stage,
                    {"sku": sku, "inventory_created": True},
                )

                self.ebay.verify_inventory_item(current_binding, draft)
                stage = "inventory_verified"
                self.database.update_action_checkpoint(
                    action.action_id,
                    stage,
                    {
                        "sku": sku,
                        "inventory_created": True,
                        "inventory_verified": True,
                    },
                )

                stage = "offer_request_started"
                self.database.update_action_checkpoint(action.action_id, stage)
                offer_id = self.ebay.create_verified_unpublished_offer(
                    current_binding,
                    draft,
                    settings,
                )
                offer_created = True
                stage = "offer_created"
                self.database.update_action_checkpoint(
                    action.action_id,
                    stage,
                    {
                        "sku": sku,
                        "offer_id": offer_id,
                        "offer_created": True,
                    },
                )

                result = self.ebay.verify_unpublished_offer(
                    current_binding,
                    offer_id,
                    draft,
                    settings,
                )
                stage = "verified_unpublished"
                safe_result = {
                    "sku": result["sku"],
                    "offer_id": result["offer_id"],
                    "offer_status": result["status"],
                    "marketplace_id": result["marketplace"],
                    "inventory_created": True,
                    "inventory_verified": True,
                    "offer_created": True,
                    "verified_unpublished": True,
                    "reconciliation_required": False,
                }
                self.database.update_action_checkpoint(
                    action.action_id,
                    stage,
                    safe_result,
                )
                self.database.complete_action(
                    action.action_id,
                    "succeeded",
                    result=safe_result,
                )
        except EbayError as error:
            sku = "unknown"
            saved_draft = action.payload.get("draft")
            if isinstance(saved_draft, Mapping) and isinstance(saved_draft.get("sku"), str):
                sku = saved_draft["sku"]
            if offer_created or error.outcome_unknown:
                status = "unknown"
                event = "reconciliation_required"
            elif inventory_created:
                status = "partial"
                event = "offer_request_failed"
            else:
                status = "failed"
                event = "inventory_request_failed"
            _logger.warning(
                "Draft confirmation failed at stage=%s code=%s status=%s details=%r",
                stage,
                error.code,
                error.status,
                error.details,
            )
            safe_result = self._failure_result(
                sku=sku,
                error=error,
                stage=stage,
                inventory_created=inventory_created,
                offer_created=offer_created,
                offer_id=offer_id,
            )
            self.database.append_action_event(
                action.action_id,
                event,
                ebay_error_id=error.code,
                result=safe_result,
            )
            self.database.complete_action(
                action.action_id,
                status,
                result=safe_result,
                ebay_error_id=error.code,
            )
        except Exception:
            # If an unanticipated local failure occurs after a remote request was
            # started, never imply that retrying is safe.
            status = "unknown" if stage != "confirmed" else "failed"
            self.database.complete_action(
                action.action_id,
                status,
                result={
                    "sku": str(
                        (action.payload.get("draft") or {}).get("sku", "unknown")
                    ),
                    "failure_stage": stage,
                    "reconciliation_required": status == "unknown",
                    "safe_code": "LOCAL_WORKFLOW_ERROR",
                },
            )
            raise
        return action.action_id

    def cancel(self, token: str, session_id: str) -> bool:
        self._require_local_stage()
        return self.database.cancel_pending_action(
            token,
            session_id,
            expected_kind=DRAFT_ACTION_KIND,
        )
