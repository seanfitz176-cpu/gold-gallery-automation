"""Publish many already-created UNPUBLISHED Offers, one verified Offer at a time.

WHY THIS EXISTS: ``bulk_listings`` creates UNPUBLISHED Inventory items and
Offers. Nothing buyers can see exists until each Offer is published. This
module is the separate, deliberately gated step that does that.

SAFETY MODEL (mirrors the rest of the app):
- Its own feature stage, ``publish`` -- an island like ``migration``, not
  cumulative with prices/quantities/images. In production it also needs its
  own opt-in, ``APP_ALLOW_PRODUCTION_PUBLISH``.
- Honours ``APP_DRY_RUN`` (default true in production): a dry run re-reads
  and re-verifies every Offer exactly as a real run would, but never calls
  eBay's publish endpoint.
- Honours ``APP_MAX_ACTIONS_PER_RUN``: at most that many Offers are
  attempted (real or dry run) per run; the rest are reported
  ``skipped_budget`` and untouched.
- Planning (``plan``) is read-only. Every Offer is re-verified again
  immediately before its own publish call.
- Never retries automatically. One row's failure never stops the others.

ROW OUTCOMES:
- ``published``: eBay returned a listing ID and a readback confirmed the
  Offer is PUBLISHED with that listing ID.
- ``dry_run``: the Offer was re-verified as publishable; nothing was sent.
- ``already_published`` / ``no_offer`` / ``ambiguous``: found at planning or
  re-check time; nothing was changed.
- ``failed``: eBay cleanly rejected the publish (e.g. a missing item
  specific or no image). Nothing was published -- fix and re-run.
- ``unknown``: the response was lost or could not be verified. The listing
  MAY be live. Check Seller Hub before doing anything else for this SKU.
- ``skipped_budget``: not attempted this run because the actions budget ran out.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .config import AppConfig, DRAFT_SCOPES, granted_scopes_cover
from .ebay_client import EbayError, EbaySandboxClient
from .storage import AppDatabase, VerifiedConnectionBinding

MAX_SKUS = 2000


@dataclass(frozen=True)
class PlannedOffer:
    sku: str
    offer_id: Optional[str]
    status: str  # "ready", "already_published", "no_offer", "ambiguous"
    detail: str


@dataclass(frozen=True)
class PublishResult:
    sku: str
    offer_id: Optional[str]
    status: str
    listing_id: Optional[str]
    detail: str


def load_skus(path: Path) -> List[str]:
    """Read the ``sku`` column of a CSV (e.g. the same file used for bulk
    upload). Blank and duplicate SKUs are dropped, order is preserved. No
    network call."""

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = {(name or "").strip().lower(): name for name in (reader.fieldnames or [])}
        if "sku" not in fields:
            raise EbayError("The CSV file has no 'sku' column", code="BULK_PUBLISH_NO_SKU_COLUMN")
        skus: List[str] = []
        seen = set()
        for row in reader:
            sku = (row.get(fields["sku"]) or "").strip()
            if sku and sku not in seen:
                seen.add(sku)
                skus.append(sku)
            if len(skus) > MAX_SKUS:
                raise EbayError(
                    f"The CSV file exceeds the {MAX_SKUS}-SKU safety limit",
                    code="BULK_PUBLISH_TOO_MANY_ROWS",
                )
        return skus


class BulkPublishService:
    def __init__(self, ebay: EbaySandboxClient, database: AppDatabase, config: AppConfig) -> None:
        self.ebay = ebay
        self.database = database
        self.config = config

    def _require_stage(self) -> None:
        if self.config.feature_stage != "publish":
            raise EbayError(
                "Publishing is locked in this application stage; set APP_FEATURE_STAGE=publish",
                status=404,
                code="PUBLISH_STAGE_LOCKED",
            )
        if self.config.runtime_mode != "local":
            raise EbayError(
                "Publishing is disabled in OAuth authorization mode",
                status=409,
                code="PUBLISH_LOCAL_MODE_REQUIRED",
            )

    def verified_binding(self) -> VerifiedConnectionBinding:
        self._require_stage()
        if not granted_scopes_cover(self.database.granted_scopes(), DRAFT_SCOPES):
            raise EbayError(
                "Authorize and verify Inventory write permission before publishing",
                status=409,
                code="DRAFT_PERMISSION_REQUIRED",
            )
        binding = self.database.verified_connection_binding()
        if binding is None:
            raise EbayError(
                f"Verify the eBay {self.config.environment.capitalize()} connection before publishing",
                status=409,
                code="CONNECTION_NOT_VERIFIED",
            )
        return binding

    def plan(self, skus: Sequence[str]) -> List[PlannedOffer]:
        """Read-only: find each SKU's single Offer and its current state."""

        self._require_stage()
        planned: List[PlannedOffer] = []
        for sku in skus:
            offers = self.ebay.list_offers(sku)
            if not offers:
                planned.append(PlannedOffer(sku, None, "no_offer", "No Offer exists for this SKU."))
                continue
            if len(offers) != 1:
                planned.append(
                    PlannedOffer(sku, None, "ambiguous",
                                 f"{len(offers)} Offers exist for this SKU; publish it by hand.")
                )
                continue
            offer = offers[0]
            offer_id = offer.get("offerId") if isinstance(offer.get("offerId"), str) else None
            status = offer.get("status")
            if offer_id is None:
                planned.append(PlannedOffer(sku, None, "ambiguous", "eBay returned an Offer with no ID."))
            elif status == "PUBLISHED":
                planned.append(PlannedOffer(sku, offer_id, "already_published", "Already live."))
            elif status == "UNPUBLISHED":
                planned.append(PlannedOffer(sku, offer_id, "ready", "Will be published."))
            else:
                planned.append(PlannedOffer(sku, offer_id, "ambiguous", f"Unexpected Offer status {status!r}."))
        return planned

    def publish_batch(
        self,
        binding: VerifiedConnectionBinding,
        planned: Sequence[PlannedOffer],
    ) -> List[PublishResult]:
        self._require_stage()
        budget = self.config.max_actions_per_run
        attempted = 0
        results: List[PublishResult] = []
        with self.ebay.publish_mutation_guard():
            for item in planned:
                if item.status != "ready" or item.offer_id is None:
                    results.append(PublishResult(item.sku, item.offer_id, item.status, None, item.detail))
                    continue
                if attempted >= budget:
                    results.append(
                        PublishResult(item.sku, item.offer_id, "skipped_budget", None,
                                      f"Not attempted: APP_MAX_ACTIONS_PER_RUN={budget} reached.")
                    )
                    continue
                attempted += 1
                results.append(self._publish_one(binding, item))
        return results

    def _publish_one(self, binding: VerifiedConnectionBinding, item: PlannedOffer) -> PublishResult:
        assert item.offer_id is not None
        try:
            if self.config.dry_run:
                self.ebay.check_offer_publishable(binding, offer_id=item.offer_id, sku=item.sku)
                return PublishResult(item.sku, item.offer_id, "dry_run", None,
                                     "DRY RUN -- re-verified as publishable; nothing was sent to eBay.")
            outcome = self.ebay.publish_offer(binding, offer_id=item.offer_id, sku=item.sku)
        except EbayError as error:
            suffix = f" ({error} status={error.status}, code={error.code})"
            if error.details:
                suffix += f" details={error.details}"
            if error.code == "OFFER_ALREADY_PUBLISHED":
                return PublishResult(item.sku, item.offer_id, "already_published", None,
                                     "Already live; nothing was changed." + suffix)
            if error.outcome_unknown:
                return PublishResult(
                    item.sku, item.offer_id, "unknown", None,
                    "The publish outcome could not be confirmed -- this listing MAY be live. "
                    "Check it in Seller Hub before doing anything else; do not re-run it as-is." + suffix,
                )
            return PublishResult(item.sku, item.offer_id, "failed", None,
                                 "eBay rejected the publish; nothing was published." + suffix)
        detail = "Published and verified."
        if outcome["warnings"]:
            detail += f" eBay warnings: {outcome['warnings']}"
        return PublishResult(item.sku, item.offer_id, "published", outcome["listing_id"], detail)


def summarize(results: Sequence[PublishResult]) -> Tuple[Tuple[str, int], ...]:
    order = ("published", "dry_run", "already_published", "failed", "unknown",
             "skipped_budget", "no_offer", "ambiguous")
    return tuple((name, sum(1 for r in results if r.status == name)) for name in order)
