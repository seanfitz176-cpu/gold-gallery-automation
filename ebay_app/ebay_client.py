from __future__ import annotations

import base64
import json
import math
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from contextlib import contextmanager
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .config import (
    AppConfig,
    CONNECTION_SCOPES,
    DRAFT_SCOPES,
    LISTING_SCOPES,
    ORDER_SCOPES,
    SETUP_SCOPES,
    granted_scopes_cover,
)
from .storage import (
    AppDatabase,
    ConnectionChangedStorageError,
    VerifiedConnectionBinding,
)


MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_PAGES = 1000
MAX_INVENTORY_RECORDS = 10_000
MAX_OFFERS_PER_SKU = 100
MAX_LISTING_RECORDS = 20_000
MAX_ORDER_RECORDS = 20_000
MAX_ORDER_LINE_ITEMS = 1_000


class EbayError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        code: str = "EBAY_ERROR",
        details: Optional[Any] = None,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details
        self.outcome_unknown = outcome_unknown


class NotConnectedError(EbayError):
    def __init__(self, environment: str = "Sandbox") -> None:
        super().__init__(
            f"Connect an eBay {environment} seller account first",
            status=409,
            code="NOT_CONNECTED",
        )


class SkuExistsError(EbayError):
    def __init__(self, sku: str) -> None:
        super().__init__(
            f"SKU {sku} already exists; draft creation will not replace it",
            status=409,
            code="SKU_ALREADY_EXISTS",
        )


class DraftPartialError(EbayError):
    def __init__(self, sku: str, cause: EbayError) -> None:
        super().__init__(
            f"Inventory item {sku} was created, but the unpublished Offer was not created",
            status=cause.status,
            code="DRAFT_PARTIAL",
            details={"sku": sku, "offer_error": cause.code},
            outcome_unknown=cause.outcome_unknown,
        )
        self.cause = cause


class UnexpectedInventoryReplacementError(EbayError):
    def __init__(self, sku: str) -> None:
        super().__init__(
            "eBay reported that the SKU already existed and was replaced; "
            "offer creation was stopped and the SKU must be reconciled",
            status=409,
            code="UNEXPECTED_INVENTORY_REPLACEMENT",
            details={"sku": sku},
            outcome_unknown=True,
        )


class PriceChangedError(EbayError):
    def __init__(self) -> None:
        super().__init__(
            "The Offer price changed after review; review the latest price before trying again",
            status=409,
            code="PRICE_CHANGED",
        )


class QuantityChangedError(EbayError):
    def __init__(self) -> None:
        super().__init__(
            "The Offer quantity changed after review; review the latest quantity before "
            "trying again",
            status=409,
            code="QUANTITY_CHANGED",
        )


class ImagesChangedError(EbayError):
    def __init__(self) -> None:
        super().__init__(
            "The listing's images changed after review; review the latest images before "
            "trying again",
            status=409,
            code="IMAGES_CHANGED",
        )


class UnsupportedMarketplaceError(EbayError):
    def __init__(self, marketplace_id: str) -> None:
        super().__init__(
            "No verified domestic shipping service is configured for marketplace "
            f"'{marketplace_id}'. eBay does not publish valid shippingCarrierCode/"
            "shippingServiceCode values for the REST Account API, so a pair must be "
            "confirmed working (e.g. via the legacy Trading API GeteBayDetails call "
            "with DetailName=ShippingServiceDetails) and added to "
            "_DOMESTIC_SHIPPING_SERVICE_BY_MARKETPLACE in ebay_client.py before a "
            "fulfillment policy can be created for this marketplace.",
            status=422,
            code="UNSUPPORTED_MARKETPLACE",
            details={"marketplaceId": marketplace_id},
        )


# Verified (shippingCarrierCode, shippingServiceCode) pairs for flat-rate domestic
# shipping, keyed by eBay marketplace ID. These values are NOT documented by eBay's
# REST Account API -- they come from the legacy Trading API's ShippingServiceCodeType/
# ShippingCarrierCodeType enumerations, cross-checked against eBay's own carrier
# descriptions ("RoyalMail" / "Parcelforce": "Available in US, UK, DE"). Only add an
# entry here once it is confirmed to work; an unverified guess fails policy creation
# with an opaque eBay error (20402/20400).
_DOMESTIC_SHIPPING_SERVICE_BY_MARKETPLACE: Dict[str, Tuple[str, str]] = {
    "EBAY_GB": ("RoyalMail", "UK_RoyalMailFirstClassStandard"),
    "EBAY_US": ("USPS", "USPSPriority"),
}


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class UrllibTransport:
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def http_error_302(self, req, fp, code, msg, headers):
            raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

        http_error_301 = http_error_302
        http_error_303 = http_error_302
        http_error_307 = http_error_302
        http_error_308 = http_error_302

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(self._NoRedirect())

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Optional[bytes],
        timeout: int,
    ) -> HttpResponse:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                response_body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(response_body) > MAX_RESPONSE_BYTES:
                    raise EbayError("eBay returned an oversized response", code="RESPONSE_TOO_LARGE")
                return HttpResponse(
                    response.status,
                    {name.lower(): value for name, value in response.headers.items()},
                    response_body,
                )
        except urllib.error.HTTPError as error:
            try:
                response_body = error.read(MAX_RESPONSE_BYTES + 1)
            except (OSError, TimeoutError, socket.timeout) as exc:
                raise EbayError(
                    "Could not read eBay's response",
                    code="EBAY_CONNECTION_ERROR",
                ) from exc
            if len(response_body) > MAX_RESPONSE_BYTES:
                response_body = b""
            return HttpResponse(
                error.code,
                {
                    name.lower(): value
                    for name, value in (error.headers.items() if error.headers else [])
                },
                response_body,
            )
        except (urllib.error.URLError, OSError, TimeoutError, socket.timeout) as error:
            raise EbayError(
                "Could not connect to eBay",
                code="EBAY_CONNECTION_ERROR",
            ) from error


def _json_body(response: HttpResponse, mutation: bool = False) -> Dict[str, Any]:
    if not response.body:
        return {}
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EbayError(
            "eBay returned malformed JSON",
            status=response.status,
            code="INVALID_EBAY_RESPONSE",
            outcome_unknown=mutation and 200 <= response.status < 300,
        ) from exc
    if not isinstance(payload, dict):
        raise EbayError(
            "eBay returned an unexpected response",
            status=response.status,
            code="INVALID_EBAY_RESPONSE",
            outcome_unknown=mutation and 200 <= response.status < 300,
        )
    return payload


def _error_details(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return []
    return [
        {
            "errorId": item.get("errorId"),
            "category": item.get("category"),
            "message": item.get("message"),
            "longMessage": item.get("longMessage"),
        }
        for item in errors[:10]
        if isinstance(item, dict)
    ]


class EbaySandboxClient:
    def __init__(
        self,
        config: AppConfig,
        database: AppDatabase,
        transport=None,
        now=time.time,
    ) -> None:
        self.config = config
        self.database = database
        self.transport = transport or UrllibTransport()
        self.now = now
        self._account_lock = threading.RLock()
        self._access_token: Optional[str] = None
        self._access_token_expires_at = 0.0
        # A separate, unrelated cache for the *application* access token (client
        # credentials grant). This never touches the user's refresh token or the
        # stored grant -- it exists only for public reference-data reads (e.g.
        # Taxonomy) that need no user consent at all.
        self._app_access_token: Optional[str] = None
        self._app_access_token_expires_at = 0.0
        if config.initial_refresh_token and not database.get_refresh_token():
            database.save_refresh_token(config.initial_refresh_token, new_grant=True)

    def authorization_url(self, session_id: str) -> str:
        with self._account_lock:
            status = self.database.connection_status()
            intent = "connect"
            expected_generation = None
            if status["connected"]:
                if (
                    self.config.feature_stage
                    not in {
                        "listings", "orders", "drafts", "prices", "setup",
                        "quantities", "images", "migration",
                    }
                    or self.database.has_granted_scopes(self.config.scopes)
                ):
                    raise EbayError(
                        f"A {self.config.environment.capitalize()} seller is already connected",
                        status=409,
                        code="ALREADY_CONNECTED",
                    )
                binding = self.database.verified_connection_binding()
                if binding is None:
                    raise EbayError(
                        f"Verify the existing {self.config.environment.capitalize()} connection "
                        "before adding permissions",
                        status=409,
                        code="UPGRADE_REQUIRES_VERIFICATION",
                    )
                expected_generation = binding.grant_generation
                intent = "scope_upgrade"
            state = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
            self.database.save_oauth_state(
                state,
                session_id,
                intent=intent,
                expected_grant_generation=expected_generation,
            )
            query = urllib.parse.urlencode(
                {
                    "client_id": self.config.client_id,
                    "redirect_uri": self.config.runame,
                    "response_type": "code",
                    "scope": " ".join(self.config.scopes),
                    "state": state,
                }
            )
            return f"{self.config.consent_url}?{query}"

    def complete_authorization(
        self,
        state: str,
        code: str,
        session_id: str,
    ) -> Dict[str, Any]:
        with self._account_lock:
            state_record = (
                self.database.consume_oauth_state_record(state, session_id) if state else None
            )
            if state_record is None:
                raise EbayError(
                    "OAuth state is invalid, expired, or already used",
                    code="INVALID_STATE",
                )
            # eBay documents authorization codes as at most 1,024 characters.
            # Keep the local exchange boundary identical to the public relay.
            if not code or len(code) > 1024:
                raise EbayError("The eBay authorization code is invalid", code="INVALID_CODE")

            status = self.database.connection_status()
            upgrading = state_record.intent == "scope_upgrade"
            existing_user_id: Optional[str] = None
            if upgrading:
                if self.config.feature_stage not in {
                    "listings", "orders", "drafts", "prices", "setup",
                    "quantities", "images", "migration",
                }:
                    raise EbayError(
                        "Additional permission cannot be added in this application stage",
                        status=409,
                        code="INVALID_OAUTH_INTENT",
                    )
                expected_generation = state_record.expected_grant_generation or ""
                binding = self.database.verified_connection_binding()
                if (
                    binding is None
                    or not secrets.compare_digest(
                        binding.grant_generation, expected_generation
                    )
                ):
                    raise EbayError(
                        f"The verified {self.config.environment.capitalize()} connection is no "
                        "longer available",
                        status=409,
                        code="CONNECTION_CHANGED",
                    )
                existing_user_id = binding.user_id
            elif status["connected"]:
                raise EbayError(
                    f"A {self.config.environment.capitalize()} seller was connected before this "
                    "authorization completed",
                    status=409,
                    code="ALREADY_CONNECTED",
                )

            token = self._token_request(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.config.runame,
                }
            )
            refresh_token = token.get("refresh_token")
            refresh_expires = token.get("refresh_token_expires_in")
            if (
                not isinstance(refresh_token, str)
                or not refresh_token
                or len(refresh_token) > 16_384
                or isinstance(refresh_expires, bool)
                or not isinstance(refresh_expires, (int, float))
                or refresh_expires <= 0
                or not math.isfinite(float(refresh_expires))
                or refresh_expires > 315_360_000
            ):
                raise EbayError(
                    "eBay did not return a valid refresh token",
                    code="INVALID_TOKEN_RESPONSE",
                )

            access_token = str(token["access_token"])
            identity = self.get_user(access_token=access_token)
            user_id = identity.get("userId")
            if (
                not isinstance(user_id, str)
                or not user_id.strip()
                or len(user_id) > 512
                or any(ord(character) < 0x20 or ord(character) == 0x7F for character in user_id)
            ):
                raise EbayError(
                    f"eBay did not return a valid {self.config.environment.capitalize()} seller "
                    "identity",
                    code="INVALID_IDENTITY_RESPONSE",
                )
            user_id = user_id.strip()
            if existing_user_id and existing_user_id != user_id:
                raise EbayError(
                    "Additional permission was granted by a different "
                    f"{self.config.environment.capitalize()} seller",
                    status=409,
                    code="SELLER_MISMATCH",
                )

            # The old grant stays in place throughout the remote exchange. Re-check
            # immediately before the single local replacement so concurrent changes
            # cannot bind the new token to the wrong seller.
            if not upgrading and self.database.connection_generation() is not None:
                raise EbayError(
                    f"A {self.config.environment.capitalize()} seller was connected before this "
                    "authorization completed",
                    status=409,
                    code="ALREADY_CONNECTED",
                )

            refresh_expires_at = int(self.now() + float(refresh_expires))
            if upgrading:
                try:
                    self.database.replace_refresh_token_for_scope_upgrade(
                        refresh_token,
                        refresh_expires_at,
                        user_id,
                        self.config.scopes,
                        expected_generation=state_record.expected_grant_generation or "",
                        expected_user_id=existing_user_id or "",
                    )
                except ConnectionChangedStorageError as exc:
                    raise EbayError(
                        f"The {self.config.environment.capitalize()} connection changed during "
                        "the permission upgrade",
                        status=409,
                        code="CONNECTION_CHANGED",
                    ) from exc
            else:
                self.database.save_refresh_token(
                    refresh_token,
                    refresh_expires_at,
                    user_id,
                    new_grant=True,
                    granted_scopes=self.config.scopes,
                )
            self._adopt_access_token(token)
            return {"user_id": user_id, "upgraded": upgrading}

    def _token_request(self, values: Mapping[str, str]) -> Dict[str, Any]:
        credentials = base64.b64encode(
            f"{self.config.client_id}:{self.config.client_secret}".encode("utf-8")
        ).decode("ascii")
        body = urllib.parse.urlencode(values).encode("utf-8")
        response = self.transport(
            "POST",
            self.config.token_url,
            {
                "Accept": "application/json",
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body,
            self.config.request_timeout_seconds,
        )
        payload = _json_body(response)
        if not 200 <= response.status < 300:
            oauth_error = payload.get("error")
            environment_label = self.config.environment.capitalize()
            if oauth_error == "invalid_client":
                message = f"eBay rejected the {environment_label} App ID or Cert ID"
                code = "EBAY_INVALID_CLIENT"
            elif oauth_error == "invalid_grant":
                message = "The eBay authorization is invalid, expired, or revoked"
                code = "EBAY_REAUTH_REQUIRED"
            elif oauth_error == "invalid_scope":
                message = f"eBay rejected the requested {environment_label} permission scope"
                code = "EBAY_INVALID_SCOPE"
            elif oauth_error == "invalid_request":
                message = (
                    f"eBay rejected the {environment_label} OAuth request; verify that the "
                    f"RuName belongs to this {environment_label} App ID and is OAuth-enabled"
                )
                code = "EBAY_INVALID_OAUTH_REQUEST"
            elif response.status == 429:
                message = "eBay temporarily rate-limited the connection request"
                code = "EBAY_RATE_LIMITED"
            else:
                message = f"eBay {environment_label} OAuth failed"
                code = "EBAY_AUTH_ERROR"
            raise EbayError(
                message,
                status=response.status,
                code=code,
                details=_error_details(payload),
            )
        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if (
            not isinstance(access_token, str)
            or not access_token
            or len(access_token) > 16_384
            or isinstance(expires_in, bool)
            or not isinstance(expires_in, (int, float))
            or expires_in <= 0
            or not math.isfinite(float(expires_in))
            or expires_in > 31_536_000
        ):
            raise EbayError("eBay returned an invalid token response", code="INVALID_TOKEN_RESPONSE")
        return payload

    def _adopt_access_token(self, token: Mapping[str, Any]) -> str:
        self._access_token = str(token["access_token"])
        self._access_token_expires_at = self.now() + float(token["expires_in"])
        return self._access_token

    def get_access_token(self) -> str:
        with self._account_lock:
            if self._access_token and self._access_token_expires_at > self.now() + 60:
                return self._access_token
            return self._refresh_access_token_locked()

    def _refresh_access_token_locked(self) -> str:
        refresh_token = self.database.get_refresh_token()
        if not refresh_token:
            raise NotConnectedError(self.config.environment.capitalize())
        recorded_scopes = self.database.granted_scopes()
        if recorded_scopes:
            refresh_scopes = recorded_scopes
        elif self.config.feature_stage == "full":
            # Backwards compatibility for dormant test fixtures. Real runtime
            # configuration never permits the broad ``full`` stage.
            refresh_scopes = self.config.scopes
        else:
            # Schema-v3 grants predate scope recording, but the only live stages
            # that created them requested Identity read access. Never infer the new
            # Inventory or Fulfillment permission from such a legacy grant.
            refresh_scopes = CONNECTION_SCOPES
        try:
            token = self._token_request(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": " ".join(refresh_scopes),
                }
            )
        except EbayError as error:
            if error.code == "EBAY_REAUTH_REQUIRED":
                self.database.clear_refresh_token()
            raise
        if "refresh_token" in token:
            replacement = token.get("refresh_token")
            if (
                not isinstance(replacement, str)
                or not replacement
                or len(replacement) > 16_384
            ):
                raise EbayError(
                    "eBay returned an invalid refresh-token rotation",
                    code="INVALID_TOKEN_RESPONSE",
                )
            expires_in = token.get("refresh_token_expires_in")
            if expires_in is None:
                expires_at = None
            elif (
                isinstance(expires_in, bool)
                or not isinstance(expires_in, (int, float))
                or not math.isfinite(float(expires_in))
                or expires_in <= 0
                or expires_in > 315_360_000
            ):
                raise EbayError(
                    "eBay returned an invalid refresh-token expiry",
                    code="INVALID_TOKEN_RESPONSE",
                )
            else:
                expires_at = int(self.now() + float(expires_in))
            # Validation happens before this single encrypted replacement, so an
            # invalid optional rotation cannot destroy the known-good old grant.
            self.database.save_refresh_token(replacement, expires_at)
        return self._adopt_access_token(token)

    def verify_connection_identity(self, expected_generation: str) -> Dict[str, str]:
        """Force a token refresh, then make one read-only Identity API request."""
        with self._account_lock:
            self._assert_grant_generation(expected_generation)
            self._access_token = None
            self._access_token_expires_at = 0
            access_token = self._refresh_access_token_locked()
            self._assert_grant_generation(expected_generation)
            identity = self.get_user(access_token=access_token)
            user_id = identity.get("userId")
            if (
                not isinstance(user_id, str)
                or not user_id.strip()
                or len(user_id) > 512
                or any(ord(character) < 0x20 or ord(character) == 0x7F for character in user_id)
            ):
                raise EbayError(
                    "eBay Identity did not return a valid "
                    f"{self.config.environment.capitalize()} user",
                    code="INVALID_IDENTITY_RESPONSE",
                )
            user_id = user_id.strip()
            self._assert_grant_generation(expected_generation)
            return {"user_id": user_id}

    def _assert_verified_binding(self, expected: VerifiedConnectionBinding) -> None:
        current = self.database.verified_connection_binding()
        if (
            current is None
            or not secrets.compare_digest(
                current.grant_generation, expected.grant_generation
            )
            or current.user_id != expected.user_id
        ):
            raise EbayError(
                f"The verified {self.config.environment.capitalize()} connection changed; "
                "verify it again",
                status=409,
                code="CONNECTION_CHANGED",
            )

    def read_verified_account_identity(
        self, binding: VerifiedConnectionBinding
    ) -> Dict[str, Any]:
        """Force a fresh token and return only Phase 2's two public fields."""
        with self._account_lock:
            self._assert_verified_binding(binding)
            self._access_token = None
            self._access_token_expires_at = 0
            access_token = self._refresh_access_token_locked()
            self._assert_verified_binding(binding)
            identity = self.get_user(access_token=access_token)
            returned_user_id = identity.get("userId")
            if not isinstance(returned_user_id, str) or not returned_user_id.strip():
                raise EbayError(
                    "eBay Identity did not return a valid "
                    f"{self.config.environment.capitalize()} user",
                    code="INVALID_IDENTITY_RESPONSE",
                )
            if returned_user_id.strip() != binding.user_id:
                raise EbayError(
                    "eBay Identity returned a different "
                    f"{self.config.environment.capitalize()} account",
                    status=409,
                    code="SELLER_MISMATCH",
                )
            self._assert_verified_binding(binding)
            return {
                "account_type": identity.get("accountType"),
                "registration_marketplace": identity.get(
                    "registrationMarketplaceId"
                ),
            }

    def read_verified_listing_records(
        self, binding: VerifiedConnectionBinding
    ) -> List[Dict[str, Any]]:
        """Return raw Inventory records only behind Phase 3's verified scope gate.

        The dedicated listing service converts these records to a small public DTO;
        callers must never render or persist the raw response.
        """

        with self._account_lock:
            self._assert_listing_permission()
            self._assert_verified_binding(binding)
            self._access_token = None
            self._access_token_expires_at = 0
            self._refresh_access_token_locked()
            self._assert_listing_permission()
            self._assert_verified_binding(binding)
            records = self.list_listings()
            self._assert_listing_permission()
            self._assert_verified_binding(binding)
            return records

    def _assert_listing_permission(self) -> None:
        if not granted_scopes_cover(self.database.granted_scopes(), LISTING_SCOPES):
            raise EbayError(
                "Additional read-only listing permission is required",
                status=409,
                code="LISTING_PERMISSION_REQUIRED",
            )

    def read_verified_order_records(
        self, binding: VerifiedConnectionBinding
    ) -> List[Dict[str, Any]]:
        """Return minimized order records behind Phase 4's verified scope gate.

        ``list_orders`` discards buyer, delivery, item and payment detail on each
        page before accumulating results. The order service then validates and
        converts the remaining allowlisted values to its public DTO.
        """

        with self._account_lock:
            self._assert_order_permission()
            self._assert_verified_binding(binding)
            self._access_token = None
            self._access_token_expires_at = 0
            self._refresh_access_token_locked()
            self._assert_order_permission()
            self._assert_verified_binding(binding)
            records = self.list_orders()
            self._assert_order_permission()
            self._assert_verified_binding(binding)
            return records

    def _assert_order_permission(self) -> None:
        if not granted_scopes_cover(self.database.granted_scopes(), ORDER_SCOPES):
            raise EbayError(
                "Additional read-only order permission is required",
                status=409,
                code="ORDER_PERMISSION_REQUIRED",
            )

    def _assert_draft_permission(self) -> None:
        if not granted_scopes_cover(self.database.granted_scopes(), DRAFT_SCOPES):
            raise EbayError(
                "Inventory write permission is required for unpublished "
                f"{self.config.environment.capitalize()} offers",
                status=409,
                code="DRAFT_PERMISSION_REQUIRED",
            )

    def _assert_mutation_allowed(self, method: str, path: str) -> None:
        """Fail closed around the mutations permitted in real stages.

        The dormant ``full`` fixture remains available to older unit tests, but
        ``load_config`` cannot select it for a real process. No real stage can
        reach publish or delete. The ``prices`` stage is cumulative over
        ``drafts`` (it retains draft creation) and adds exactly one further
        write: the targeted bulk price-update call. The ``quantities`` stage
        is cumulative over ``prices`` in turn (it retains draft creation and
        price changes) and adds exactly one further write: the targeted bulk
        quantity-update call, using the same underlying endpoint with a
        different field. The ``images`` stage is cumulative over
        ``quantities`` in turn (it retains draft creation, price changes, and
        quantity changes) and adds exactly one further write: a targeted PUT
        to an existing inventory item that appends one seller-supplied,
        pre-validated image URL, using the same inventory-item endpoint
        ``drafts`` already uses to create that item. The one-time ``setup``
        stage separately permits exactly the five one-off Sandbox
        account/location/policy creation calls it needs, and nothing else —
        no read of an arbitrary account endpoint becomes a mutation, and no
        other write becomes reachable from any of these stages either. The
        one-time ``migration`` stage is also its own separate island, not
        cumulative with prices/quantities/images: it permits exactly one
        further write, the bulk-migrate-listing call that brings an existing
        classic listing under this app's management.
        """

        if method in {"GET", "HEAD"}:
            return
        if self.config.feature_stage == "full":
            return
        inventory_prefix = "/sell/inventory/v1/inventory_item/"
        inventory_suffix = (
            path[len(inventory_prefix) :] if path.startswith(inventory_prefix) else ""
        )
        allowed_inventory_create = (
            self.config.feature_stage in {"drafts", "prices", "quantities", "images"}
            and self.config.runtime_mode == "local"
            and method == "PUT"
            and bool(inventory_suffix)
            and "/" not in inventory_suffix
            and len(inventory_suffix) <= 200
        )
        allowed_offer_create = (
            self.config.feature_stage in {"drafts", "prices", "quantities", "images"}
            and self.config.runtime_mode == "local"
            and method == "POST"
            and path == "/sell/inventory/v1/offer"
        )
        # A single endpoint serves both price and quantity updates; this
        # allowlist check is stage-gated but cannot distinguish between the
        # two by payload, so it covers whichever field the ``prices`` or
        # ``quantities`` stage's own service actually sets. The ``images``
        # stage retains this endpoint too, since it is cumulative over both.
        allowed_price_or_quantity_mutation = (
            self.config.feature_stage in {"prices", "quantities", "images"}
            and self.config.runtime_mode == "local"
            and method == "POST"
            and path == "/sell/inventory/v1/bulk_update_price_quantity"
        )
        location_prefix = "/sell/inventory/v1/location/"
        location_suffix = (
            path[len(location_prefix) :] if path.startswith(location_prefix) else ""
        )
        allowed_migration_mutation = (
            self.config.feature_stage == "migration"
            and self.config.runtime_mode == "local"
            and method == "POST"
            and path == "/sell/inventory/v1/bulk_migrate_listing"
        )
        allowed_setup_mutation = (
            self.config.feature_stage == "setup"
            and self.config.runtime_mode == "local"
            and method == "POST"
            and (
                path == "/sell/account/v1/program/opt_in"
                or path == "/sell/account/v1/fulfillment_policy"
                or path == "/sell/account/v1/payment_policy"
                or path == "/sell/account/v1/return_policy"
                or (
                    bool(location_suffix)
                    and "/" not in location_suffix
                    and len(location_suffix) <= 200
                )
            )
        )
        if not (
            allowed_inventory_create
            or allowed_offer_create
            or allowed_price_or_quantity_mutation
            or allowed_migration_mutation
            or allowed_setup_mutation
        ):
            raise EbayError(
                "This eBay mutation is locked in the current "
                f"{self.config.environment.capitalize()} stage",
                status=403,
                code="MUTATION_LOCKED",
            )

    def _api_request(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, Any]] = None,
        payload: Optional[Mapping[str, Any]] = None,
        safe_read: bool,
        access_token: Optional[str] = None,
        expected_statuses: Optional[Sequence[int]] = None,
        include_raw_payload_on_error: bool = False,
    ) -> Dict[str, Any]:
        _status, response_payload = self._api_request_with_status(
            method,
            path,
            query=query,
            payload=payload,
            safe_read=safe_read,
            access_token=access_token,
            expected_statuses=expected_statuses,
            include_raw_payload_on_error=include_raw_payload_on_error,
        )
        return response_payload

    def _api_request_with_status(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, Any]] = None,
        payload: Optional[Mapping[str, Any]] = None,
        safe_read: bool,
        access_token: Optional[str] = None,
        expected_statuses: Optional[Sequence[int]] = None,
        include_raw_payload_on_error: bool = False,
    ) -> tuple[int, Dict[str, Any]]:
        mutation = method not in {"GET", "HEAD"}
        self._assert_mutation_allowed(method, path)
        if mutation and safe_read:
            raise EbayError(
                "A mutation cannot use automatic read retry behavior",
                code="INVALID_CLIENT_OPERATION",
            )
        url = f"{self.config.api_origin}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        request_body = (
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None
        )
        authorization_retried = False
        while True:
            token = access_token or self.get_access_token()
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "Content-Language": self.config.content_language,
                "X-EBAY-C-MARKETPLACE-ID": self.config.marketplace_id,
            }
            if request_body is not None:
                headers["Content-Type"] = "application/json"
            try:
                response = self.transport(
                    method,
                    url,
                    headers,
                    request_body,
                    self.config.request_timeout_seconds,
                )
            except EbayError as error:
                if mutation:
                    error.outcome_unknown = True
                raise
            definitive_failure = response.status in {400, 401, 403, 404, 409, 429}
            try:
                response_payload = _json_body(response, mutation=mutation)
            except EbayError as error:
                if mutation and not definitive_failure:
                    error.outcome_unknown = True
                raise
            if response.status == 401 and not access_token:
                if self._access_token == token:
                    self._access_token = None
                    self._access_token_expires_at = 0
                if safe_read and not authorization_retried:
                    authorization_retried = True
                    continue
            if not 200 <= response.status < 300:
                error_details = _error_details(response_payload)
                if (
                    not error_details
                    and include_raw_payload_on_error
                    and isinstance(response_payload, dict)
                ):
                    # Some endpoints' error bodies do not follow the usual
                    # {"errors": [...]} shape _error_details expects (this is
                    # documented as a known schema uncertainty for
                    # bulk_migrate_listing specifically -- see
                    # _migrate_listing's own docstring). Rather than silently
                    # show the caller nothing, fall back to eBay's raw
                    # response body so they are never left blind about why a
                    # real write was rejected.
                    error_details = [response_payload]
                raise EbayError(
                    f"eBay {self.config.environment.capitalize()} rejected the request",
                    status=response.status,
                    code="EBAY_RATE_LIMITED" if response.status == 429 else "EBAY_API_ERROR",
                    details=error_details,
                    outcome_unknown=mutation and not definitive_failure,
                )
            if expected_statuses is not None and response.status not in expected_statuses:
                raise EbayError(
                    "eBay returned an unexpected success status",
                    status=response.status,
                    code="INVALID_EBAY_RESPONSE",
                    outcome_unknown=mutation,
                )
            return response.status, response_payload

    def get_user(self, access_token: Optional[str] = None) -> Dict[str, Any]:
        # Identity uses the apiz host rather than the Sell API host.
        url = f"{self.config.identity_base_url}/user/"
        token = access_token or self.get_access_token()
        response = self.transport(
            "GET",
            url,
            {"Accept": "application/json", "Authorization": f"Bearer {token}"},
            None,
            self.config.request_timeout_seconds,
        )
        payload = _json_body(response)
        if response.status != 200:
            environment_label = self.config.environment.capitalize()
            if response.status in {401, 403}:
                message = f"eBay rejected the {environment_label} user authorization"
                code = "EBAY_IDENTITY_UNAUTHORIZED"
            elif response.status == 429:
                message = "eBay temporarily rate-limited the Identity check"
                code = "EBAY_RATE_LIMITED"
            elif response.status >= 500:
                message = f"eBay {environment_label} Identity is temporarily unavailable"
                code = "EBAY_IDENTITY_UNAVAILABLE"
            else:
                message = f"Could not read the {environment_label} seller identity"
                code = "EBAY_IDENTITY_ERROR"
            raise EbayError(
                message,
                status=response.status,
                code=code,
                details=_error_details(payload),
            )
        return payload

    @staticmethod
    def _pagination_metadata(response: Dict[str, Any]) -> tuple[Optional[int], bool]:
        total = response.get("total")
        if total is not None and (
            isinstance(total, bool) or not isinstance(total, int) or total < 0
        ):
            raise EbayError(
                "eBay returned invalid pagination metadata",
                code="INVALID_EBAY_RESPONSE",
            )
        next_value = response.get("next")
        if next_value is not None and next_value != "" and (
            not isinstance(next_value, str)
            or len(next_value) > 4096
            or any(
                ord(character) < 0x20 or ord(character) == 0x7F
                for character in next_value
            )
        ):
            raise EbayError(
                "eBay returned invalid pagination metadata",
                code="INVALID_EBAY_RESPONSE",
            )
        return total, bool(next_value)

    @classmethod
    def _collection(cls, response: Dict[str, Any], field: str) -> Sequence[Dict[str, Any]]:
        total, has_next = cls._pagination_metadata(response)
        value = response.get(field)
        if isinstance(value, list):
            if any(not isinstance(item, dict) for item in value):
                raise EbayError(
                    f"eBay returned an invalid {field} member",
                    code="INVALID_EBAY_RESPONSE",
                )
            if not value and has_next:
                raise EbayError(
                    f"eBay returned an empty {field} page with a next link",
                    code="INVALID_EBAY_RESPONSE",
                )
            if not value and total not in {None, 0}:
                raise EbayError(
                    f"eBay returned an incomplete {field} collection",
                    code="INVALID_EBAY_RESPONSE",
                )
            if value and total == 0:
                raise EbayError(
                    f"eBay returned inconsistent {field} pagination",
                    code="INVALID_EBAY_RESPONSE",
                )
            return value
        if total == 0 and not has_next:
            return []
        raise EbayError(f"eBay returned an invalid {field} collection", code="INVALID_EBAY_RESPONSE")

    @staticmethod
    def _check_page_progress(
        response: Dict[str, Any],
        accumulated: int,
        expected_total: Optional[int],
        field: str,
    ) -> tuple[Optional[int], bool]:
        page_total, has_next = EbaySandboxClient._pagination_metadata(response)
        if page_total is not None:
            if expected_total is not None and page_total != expected_total:
                raise EbayError(
                    f"eBay changed the {field} total during pagination",
                    code="INVALID_EBAY_RESPONSE",
                )
            expected_total = page_total
        if expected_total is not None:
            if accumulated > expected_total:
                raise EbayError(
                    f"eBay returned too many {field}",
                    code="INVALID_EBAY_RESPONSE",
                )
            if has_next and accumulated >= expected_total:
                raise EbayError(
                    f"eBay returned an inconsistent {field} next link",
                    code="INVALID_EBAY_RESPONSE",
                )
            if not has_next and accumulated != expected_total:
                raise EbayError(
                    f"eBay returned an incomplete {field} collection",
                    code="INVALID_EBAY_RESPONSE",
                )
        return expected_total, has_next

    def list_inventory_items(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        expected_total: Optional[int] = None
        for _page in range(MAX_PAGES):
            response = self._api_request(
                "GET",
                "/sell/inventory/v1/inventory_item",
                # offset is the number of records eBay should skip, i.e. the
                # running item count accumulated so far -- NOT the loop's
                # page index. With a 200-item page size those only coincide
                # while every page happens to return exactly one item (as
                # every test fixture to date has); on a real catalog with
                # more than 200 items, sending offset=1 instead of offset=200
                # for the second page would ask eBay to skip almost nothing,
                # re-fetching most of page one.
                query={"limit": 200, "offset": len(items)},
                safe_read=True,
            )
            items.extend(self._collection(response, "inventoryItems"))
            if len(items) > MAX_INVENTORY_RECORDS:
                raise EbayError(
                    "Inventory response exceeded its safety limit",
                    code="INVALID_EBAY_RESPONSE",
                )
            expected_total, has_next = self._check_page_progress(
                response, len(items), expected_total, "inventory items"
            )
            if not has_next:
                return items
        raise EbayError("Inventory pagination exceeded its safety limit")

    def list_offers(self, sku: str) -> List[Dict[str, Any]]:
        offers: List[Dict[str, Any]] = []
        expected_total: Optional[int] = None
        for _page in range(MAX_PAGES):
            try:
                response = self._api_request(
                    "GET",
                    "/sell/inventory/v1/offer",
                    # See the matching comment in list_inventory_items: offset
                    # must be the running item count, not the loop index.
                    query={"sku": sku, "limit": 100, "offset": len(offers)},
                    safe_read=True,
                )
            except EbayError as error:
                # eBay Sandbox returns 404 (errorId 25713, "This Offer is not
                # available") instead of a 200 with an empty ``offers: []``
                # collection when a SKU has no Offer yet. Every brand-new SKU
                # hits this, so without this, no first Offer could ever be
                # created: the pre-existing-offer check below would always
                # fail closed. Same "404 means it doesn't exist yet" shape as
                # get_inventory_location, applied here for the same reason.
                if error.status == 404:
                    return offers
                raise
            offers.extend(self._collection(response, "offers"))
            if len(offers) > MAX_OFFERS_PER_SKU:
                raise EbayError(
                    "Offer response exceeded its safety limit",
                    code="INVALID_EBAY_RESPONSE",
                )
            expected_total, has_next = self._check_page_progress(
                response, len(offers), expected_total, "offers"
            )
            if not has_next:
                return offers
        raise EbayError("Offer pagination exceeded its safety limit")

    def list_listings(self) -> List[Dict[str, Any]]:
        listings: List[Dict[str, Any]] = []
        seen_skus = set()
        seen_offer_ids = set()
        for item in self.list_inventory_items():
            sku = item.get("sku")
            if (
                not isinstance(sku, str)
                or not sku.strip()
                or len(sku) > 50
                or any(ord(character) < 0x20 or ord(character) == 0x7F for character in sku)
            ):
                raise EbayError(
                    "eBay returned an invalid inventory SKU",
                    code="INVALID_EBAY_RESPONSE",
                )
            if sku in seen_skus:
                raise EbayError(
                    "eBay returned a duplicate inventory SKU",
                    code="INVALID_EBAY_RESPONSE",
                )
            seen_skus.add(sku)
            offers = self.list_offers(sku)
            if not offers:
                listings.append({"sku": sku, "inventory": item, "offer": None})
                if len(listings) > MAX_LISTING_RECORDS:
                    raise EbayError(
                        "Listing response exceeded its safety limit",
                        code="INVALID_EBAY_RESPONSE",
                    )
            for offer in offers:
                if offer.get("sku") != sku:
                    raise EbayError(
                        "eBay returned an Offer for a different SKU",
                        code="INVALID_EBAY_RESPONSE",
                    )
                offer_id = offer.get("offerId")
                if isinstance(offer_id, str) and offer_id:
                    if offer_id in seen_offer_ids:
                        raise EbayError(
                            "eBay returned a duplicate Offer",
                            code="INVALID_EBAY_RESPONSE",
                        )
                    seen_offer_ids.add(offer_id)
                listings.append({"sku": sku, "inventory": item, "offer": offer})
                if len(listings) > MAX_LISTING_RECORDS:
                    raise EbayError(
                        "Listing response exceeded its safety limit",
                        code="INVALID_EBAY_RESPONSE",
                    )
        return listings

    @staticmethod
    def _minimize_order(order: Mapping[str, Any]) -> Dict[str, Any]:
        """Discard rich Fulfillment data before a page leaves client scope."""

        line_items = order.get("lineItems")
        if (
            not isinstance(line_items, list)
            or not line_items
            or len(line_items) > MAX_ORDER_LINE_ITEMS
            or any(not isinstance(item, Mapping) for item in line_items)
        ):
            raise EbayError(
                "eBay returned invalid order line items",
                code="INVALID_EBAY_RESPONSE",
            )
        pricing = order.get("pricingSummary")
        total = pricing.get("total") if isinstance(pricing, Mapping) else None
        cancel_status = order.get("cancelStatus")
        return {
            "order_id": order.get("orderId"),
            "creation_date": order.get("creationDate"),
            "payment_status": order.get("orderPaymentStatus"),
            "fulfillment_status": order.get("orderFulfillmentStatus"),
            "cancellation_state": (
                cancel_status.get("cancelState")
                if isinstance(cancel_status, Mapping)
                else None
            ),
            "total_value": total.get("value") if isinstance(total, Mapping) else None,
            "currency": total.get("currency") if isinstance(total, Mapping) else None,
            "line_item_count": len(line_items),
        }

    def list_orders(self) -> List[Dict[str, Any]]:
        """Fetch recent orders and retain only Phase 4's pre-validation allowlist."""

        now = datetime.fromtimestamp(self.now(), timezone.utc)
        lower = now - timedelta(days=self.config.order_lookback_days)
        iso = lambda value: value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        offset = 0
        orders: List[Dict[str, Any]] = []
        seen_order_ids = set()
        expected_total: Optional[int] = None
        for _page in range(MAX_PAGES):
            response = self._api_request(
                "GET",
                "/sell/fulfillment/v1/order",
                query={
                    # The open end avoids eBay rejecting an end timestamp when the
                    # local computer's clock is slightly ahead of eBay's clock.
                    "filter": f"creationdate:[{iso(lower)}..]",
                    "fieldGroups": "TAX_BREAKDOWN",
                    "limit": 200,
                    "offset": offset,
                },
                safe_read=True,
            )
            response_offset = response.get("offset")
            if response_offset is not None and (
                isinstance(response_offset, bool)
                or not isinstance(response_offset, int)
                or response_offset != offset
            ):
                raise EbayError(
                    "eBay returned an invalid order offset",
                    code="INVALID_EBAY_RESPONSE",
                )
            page_orders = list(self._collection(response, "orders"))
            for order in page_orders:
                order_id = order.get("orderId")
                if (
                    not isinstance(order_id, str)
                    or not order_id
                    or len(order_id) > 128
                    or any(
                        ord(character) < 0x20 or ord(character) == 0x7F
                        for character in order_id
                    )
                ):
                    raise EbayError(
                        "eBay returned an invalid order ID",
                        code="INVALID_EBAY_RESPONSE",
                    )
                if order_id in seen_order_ids:
                    raise EbayError(
                        "eBay returned a duplicate order",
                        code="INVALID_EBAY_RESPONSE",
                    )
                seen_order_ids.add(order_id)
            orders.extend(self._minimize_order(order) for order in page_orders)
            if len(orders) > MAX_ORDER_RECORDS:
                raise EbayError(
                    "Order response exceeded its safety limit",
                    code="INVALID_EBAY_RESPONSE",
                )
            expected_total, has_next = self._check_page_progress(
                response, len(orders), expected_total, "orders"
            )
            if not has_next:
                return orders
            offset += len(page_orders)
        raise EbayError("Order pagination exceeded its safety limit")

    def _assert_setup_permission(self) -> None:
        if not granted_scopes_cover(self.database.granted_scopes(), SETUP_SCOPES):
            raise EbayError(
                "Account-management permission is required for one-time "
                f"{self.config.environment.capitalize()} setup",
                status=409,
                code="SETUP_PERMISSION_REQUIRED",
            )

    @contextmanager
    def setup_mutation_guard(self):
        """Serialize the one-time Sandbox account/location/policy setup calls."""

        if self.config.feature_stage != "setup":
            raise EbayError(
                f"One-time {self.config.environment.capitalize()} setup is locked outside "
                "the setup application stage",
                status=403,
                code="MUTATION_LOCKED",
            )
        if self.config.runtime_mode != "local":
            raise EbayError(
                "Setup mutations are disabled in OAuth authorization mode",
                status=409,
                code="SETUP_LOCAL_MODE_REQUIRED",
            )
        with self._account_lock:
            self._assert_setup_permission()
            yield

    def opt_in_to_selling_policy_management(self) -> None:
        """Opt the connected Sandbox seller into Business Policies.

        A brand-new Sandbox test user is not opted in by default, and
        ``createFulfillmentPolicy``/``createPaymentPolicy``/``createReturnPolicy``
        all fail with "seller is not BP opted in" until this succeeds once.
        eBay returns 204 with no body on success, and returns a definitive,
        idempotent-safe error if the seller is already opted in.
        """

        self._api_request(
            "POST",
            "/sell/account/v1/program/opt_in",
            payload={"programType": "SELLING_POLICY_MANAGEMENT"},
            safe_read=False,
            expected_statuses=(200, 204),
        )

    def create_merchant_location(
        self,
        merchant_location_key: str,
        *,
        name: str,
        address_line_1: str,
        city: str,
        state_or_province: str,
        postal_code: str,
        country: str,
    ) -> str:
        """Create one Sandbox Inventory API merchant location.

        Returns the caller-chosen ``merchant_location_key`` back once eBay has
        confirmed the location exists, for the owner to place in ``.env``.
        """

        path = f"/sell/inventory/v1/location/{urllib.parse.quote(merchant_location_key, safe='')}"
        self._api_request(
            "POST",
            path,
            payload={
                "location": {
                    "address": {
                        "addressLine1": address_line_1,
                        "city": city,
                        "stateOrProvince": state_or_province,
                        "postalCode": postal_code,
                        "country": country,
                    }
                },
                "locationTypes": ["WAREHOUSE"],
                "name": name,
                "merchantLocationStatus": "ENABLED",
            },
            safe_read=False,
            expected_statuses=(200, 204),
        )
        return merchant_location_key

    def _create_business_policy(self, path: str, id_field: str, payload: Dict[str, Any]) -> str:
        response = self._api_request(
            "POST",
            path,
            payload=payload,
            safe_read=False,
            expected_statuses=(200, 201),
        )
        policy_id = response.get(id_field)
        if not isinstance(policy_id, str) or not policy_id:
            raise EbayError(
                "eBay did not return a policy ID for the created policy",
                code="INVALID_EBAY_RESPONSE",
            )
        return policy_id

    def create_fulfillment_policy(self, *, name: str, handling_days: int = 1) -> str:
        marketplace_id = self.config.marketplace_id
        try:
            carrier_code, service_code = _DOMESTIC_SHIPPING_SERVICE_BY_MARKETPLACE[marketplace_id]
        except KeyError:
            raise UnsupportedMarketplaceError(marketplace_id) from None
        return self._create_business_policy(
            "/sell/account/v1/fulfillment_policy",
            "fulfillmentPolicyId",
            {
                "name": name,
                "marketplaceId": marketplace_id,
                "categoryTypes": [{"name": "ALL_EXCLUDING_MOTORS_VEHICLES"}],
                "handlingTime": {"value": handling_days, "unit": "DAY"},
                "shippingOptions": [
                    {
                        "optionType": "DOMESTIC",
                        "costType": "FLAT_RATE",
                        "shippingServices": [
                            {
                                "sortOrder": 1,
                                "shippingCarrierCode": carrier_code,
                                "shippingServiceCode": service_code,
                                "shippingCost": {"value": "0.00", "currency": self.config.currency},
                                "freeShipping": True,
                            }
                        ],
                    }
                ],
            },
        )

    def create_payment_policy(self, *, name: str) -> str:
        return self._create_business_policy(
            "/sell/account/v1/payment_policy",
            "paymentPolicyId",
            {
                "name": name,
                "marketplaceId": self.config.marketplace_id,
                "categoryTypes": [{"name": "ALL_EXCLUDING_MOTORS_VEHICLES"}],
                "immediatePay": False,
            },
        )

    def create_return_policy(self, *, name: str, return_period_days: int = 30) -> str:
        return self._create_business_policy(
            "/sell/account/v1/return_policy",
            "returnPolicyId",
            {
                "name": name,
                "marketplaceId": self.config.marketplace_id,
                "categoryTypes": [{"name": "ALL_EXCLUDING_MOTORS_VEHICLES"}],
                "returnsAccepted": True,
                "returnPeriod": {"value": return_period_days, "unit": "DAY"},
                "refundMethod": "MONEY_BACK",
                "returnShippingCostPayer": "SELLER",
            },
        )

    def get_inventory_location(self, merchant_location_key: str) -> Optional[Dict[str, Any]]:
        """Read-only preflight: does this merchant location already exist?"""

        try:
            return self._api_request(
                "GET",
                f"/sell/inventory/v1/location/{urllib.parse.quote(merchant_location_key, safe='')}",
                safe_read=True,
            )
        except EbayError as error:
            if error.status == 404:
                return None
            raise

    def list_fulfillment_policies(self) -> List[Dict[str, Any]]:
        """Read-only preflight: which fulfillment policies already exist?"""

        response = self._api_request(
            "GET",
            "/sell/account/v1/fulfillment_policy",
            query={"marketplace_id": self.config.marketplace_id},
            safe_read=True,
        )
        return list(self._collection(response, "fulfillmentPolicies"))

    def list_payment_policies(self) -> List[Dict[str, Any]]:
        """Read-only preflight: which payment policies already exist?"""

        response = self._api_request(
            "GET",
            "/sell/account/v1/payment_policy",
            query={"marketplace_id": self.config.marketplace_id},
            safe_read=True,
        )
        return list(self._collection(response, "paymentPolicies"))

    def list_return_policies(self) -> List[Dict[str, Any]]:
        """Read-only preflight: which return policies already exist?"""

        response = self._api_request(
            "GET",
            "/sell/account/v1/return_policy",
            query={"marketplace_id": self.config.marketplace_id},
            safe_read=True,
        )
        return list(self._collection(response, "returnPolicies"))

    def _get_application_access_token(self) -> str:
        """An app-level token (no user consent) for public reference data only.

        Never used for anything that reads or writes seller data -- only for
        Taxonomy lookups, which eBay documents as accessible via the basic
        ``https://api.ebay.com/oauth/api_scope`` client-credentials grant.
        """

        with self._account_lock:
            if (
                self._app_access_token
                and self._app_access_token_expires_at > self.now() + 60
            ):
                return self._app_access_token
            token = self._token_request(
                {
                    "grant_type": "client_credentials",
                    "scope": "https://api.ebay.com/oauth/api_scope",
                }
            )
            self._app_access_token = str(token["access_token"])
            self._app_access_token_expires_at = self.now() + float(token["expires_in"])
            return self._app_access_token

    _NOTIFICATION_KID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

    def get_notification_public_key(self, kid: str) -> Dict[str, Any]:
        """Read-only: eBay's public key for verifying a notification signature.

        Used only to verify the ``x-ebay-signature`` header on an eBay
        Marketplace Account Deletion/Closure notification (see
        ``ebay_app/account_deletion.py`` and ``ebay_app/notification_signature.py``).
        Uses the same app-level (no user consent) token as the Taxonomy reads
        above -- this call never touches seller or buyer data, and never
        itself decides anything is verified; the caller's signature check
        does that.
        """

        if not isinstance(kid, str) or not self._NOTIFICATION_KID_PATTERN.match(kid):
            raise EbayError("Invalid notification key id", code="INVALID_KID")
        return self._api_request(
            "GET",
            f"/commerce/notification/v1/public_key/{kid}",
            safe_read=True,
            access_token=self._get_application_access_token(),
        )

    def get_default_category_tree_id(self) -> str:
        """Read-only: which Taxonomy category tree applies to this marketplace?

        Every eBay marketplace maps to one category tree (e.g. EBAY_GB does not
        share EBAY_US's tree), and a listing's categoryId is only valid within
        its marketplace's tree. This exists so a caller can look up a real,
        currently-valid category instead of guessing one.
        """

        response = self._api_request(
            "GET",
            "/commerce/taxonomy/v1/get_default_category_tree_id",
            query={"marketplace_id": self.config.marketplace_id},
            safe_read=True,
            access_token=self._get_application_access_token(),
        )
        tree_id = response.get("categoryTreeId")
        if not isinstance(tree_id, str) or not tree_id:
            raise EbayError(
                "eBay did not return a category tree ID for this marketplace",
                code="INVALID_EBAY_RESPONSE",
            )
        return tree_id

    def get_category_subtree(self, category_tree_id: str, category_id: str) -> Dict[str, Any]:
        """Read-only: the subtree rooted at one category, to check leaf status.

        eBay's suggestion endpoint does not say whether a suggested category is
        a leaf (listable) or a parent (not listable); this call, returning the
        node's own record including its children, is how to tell the two apart
        before trusting a category ID.
        """

        response = self._api_request(
            "GET",
            f"/commerce/taxonomy/v1/category_tree/{category_tree_id}/get_category_subtree",
            query={"category_id": category_id},
            safe_read=True,
            access_token=self._get_application_access_token(),
        )
        return response

    def get_category_suggestions(self, category_tree_id: str, query: str) -> List[Dict[str, Any]]:
        """Read-only: leaf categories eBay suggests for a free-text query.

        Returns eBay's own ``categorySuggestions`` list unmodified (each entry
        carries the category's id, name and ancestor path) so a human can pick
        a real, currently-valid leaf category rather than guessing an id.
        """

        response = self._api_request(
            "GET",
            f"/commerce/taxonomy/v1/category_tree/{category_tree_id}/get_category_suggestions",
            query={"q": query},
            safe_read=True,
            access_token=self._get_application_access_token(),
        )
        suggestions = response.get("categorySuggestions")
        return list(suggestions) if isinstance(suggestions, list) else []

    def get_item_aspects_for_category(
        self, category_tree_id: str, category_id: str
    ) -> List[Dict[str, Any]]:
        """Read-only: every item specific eBay defines for one leaf category.

        Each entry's ``aspectConstraint.aspectRequired`` says whether a listing
        in this category cannot be published without it -- this exists so a
        caller can learn every required item specific up front from eBay's own
        metadata, instead of discovering them one at a time from a sequence of
        failed publish attempts.
        """

        response = self._api_request(
            "GET",
            f"/commerce/taxonomy/v1/category_tree/{category_tree_id}/get_item_aspects_for_category",
            query={"category_id": category_id},
            safe_read=True,
            access_token=self._get_application_access_token(),
        )
        aspects = response.get("aspects")
        return list(aspects) if isinstance(aspects, list) else []

    @contextmanager
    def draft_mutation_guard(self):
        """Serialize one confirmed draft sequence for the connected seller."""

        if self.config.feature_stage not in {
            "drafts", "prices", "quantities", "images", "full",
        }:
            raise EbayError(
                "Unpublished offer creation is locked in this application stage",
                status=403,
                code="MUTATION_LOCKED",
            )
        if self.config.runtime_mode != "local":
            raise EbayError(
                "Draft mutations are disabled in OAuth authorization mode",
                status=409,
                code="DRAFT_LOCAL_MODE_REQUIRED",
            )
        with self._account_lock:
            yield

    @contextmanager
    def price_mutation_guard(self):
        """Serialize one confirmed price change for the connected seller."""

        if self.config.feature_stage not in {"prices", "quantities", "images"}:
            raise EbayError(
                "Editing a listing price is locked in this application stage",
                status=403,
                code="MUTATION_LOCKED",
            )
        if self.config.runtime_mode != "local":
            raise EbayError(
                "Price mutations are disabled in OAuth authorization mode",
                status=409,
                code="PRICE_LOCAL_MODE_REQUIRED",
            )
        with self._account_lock:
            yield

    @contextmanager
    def quantity_mutation_guard(self):
        """Serialize one confirmed quantity change for the connected seller."""

        if self.config.feature_stage not in {"quantities", "images"}:
            raise EbayError(
                "Editing a listing quantity is locked in this application stage",
                status=403,
                code="MUTATION_LOCKED",
            )
        if self.config.runtime_mode != "local":
            raise EbayError(
                "Quantity mutations are disabled in OAuth authorization mode",
                status=409,
                code="QUANTITY_LOCAL_MODE_REQUIRED",
            )
        with self._account_lock:
            yield

    @contextmanager
    def image_mutation_guard(self):
        """Serialize one confirmed image addition for the connected seller."""

        if self.config.feature_stage != "images":
            raise EbayError(
                "Adding a listing image is locked in this application stage",
                status=403,
                code="MUTATION_LOCKED",
            )
        if self.config.runtime_mode != "local":
            raise EbayError(
                "Image mutations are disabled in OAuth authorization mode",
                status=409,
                code="IMAGE_LOCAL_MODE_REQUIRED",
            )
        with self._account_lock:
            yield

    @contextmanager
    def migration_mutation_guard(self):
        """Serialize one confirmed legacy-listing migration for the seller.

        Deliberately its own island, not reachable from prices/quantities/
        images, the same way ``setup`` is: migration is a rare, one-off
        operation a human runs once per listing, not a repeating automated
        change, and nothing about having earned trust in price/quantity/
        image automation should by itself unlock it.
        """

        if self.config.feature_stage != "migration":
            raise EbayError(
                "Migrating a legacy listing is locked in this application stage",
                status=403,
                code="MUTATION_LOCKED",
            )
        if self.config.runtime_mode != "local":
            raise EbayError(
                "Migration mutations are disabled in OAuth authorization mode",
                status=409,
                code="MIGRATION_LOCAL_MODE_REQUIRED",
            )
        with self._account_lock:
            yield

    def verified_inventory_item_exists(
        self,
        binding: VerifiedConnectionBinding,
        sku: str,
    ) -> bool:
        """Perform the Phase 5 read-only, seller-bound SKU preflight."""

        with self._account_lock:
            self._assert_draft_permission()
            self._assert_verified_binding(binding)
            self.get_access_token()
            self._assert_draft_permission()
            self._assert_verified_binding(binding)
            exists = self.get_inventory_item(sku) is not None
            self._assert_draft_permission()
            self._assert_verified_binding(binding)
            return exists

    @staticmethod
    def _inventory_payload(draft: Mapping[str, Any]) -> Dict[str, Any]:
        product: Dict[str, Any] = {
            "title": draft["title"],
            "description": draft["description"],
        }
        if draft.get("aspects"):
            product["aspects"] = draft["aspects"]
        return {
            "availability": {
                "shipToLocationAvailability": {"quantity": draft["quantity"]}
            },
            "condition": draft["condition"],
            "product": product,
        }

    @staticmethod
    def _offer_payload(
        draft: Mapping[str, Any],
        settings: Mapping[str, Any],
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "sku": draft["sku"],
            "marketplaceId": settings["marketplace_id"],
            "format": settings["format"],
            "availableQuantity": draft["quantity"],
            "categoryId": draft["category_id"],
            "listingDuration": settings["listing_duration"],
            "pricingSummary": {
                "price": {"currency": draft["currency"], "value": draft["price"]}
            },
        }
        if settings.get("merchant_location_key"):
            payload["merchantLocationKey"] = settings["merchant_location_key"]
        policy_ids = (
            settings.get("fulfillment_policy_id"),
            settings.get("payment_policy_id"),
            settings.get("return_policy_id"),
        )
        if all(policy_ids):
            payload["listingPolicies"] = {
                "fulfillmentPolicyId": policy_ids[0],
                "paymentPolicyId": policy_ids[1],
                "returnPolicyId": policy_ids[2],
            }
        return payload

    def create_verified_inventory_item(
        self,
        binding: VerifiedConnectionBinding,
        draft: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Create one Inventory item after an immediate read-only absence check.

        eBay deliberately uses the same 204 response for a create and a replace,
        so the preceding GET is the fail-closed protection against overwriting an
        existing SKU. There is no status code that can distinguish the two cases.
        """

        with self._account_lock:
            self._assert_draft_permission()
            self._assert_verified_binding(binding)
            sku = str(draft["sku"])
            if self.get_inventory_item(sku) is not None:
                raise SkuExistsError(sku)
            self._assert_draft_permission()
            self._assert_verified_binding(binding)
            status, _response = self._api_request_with_status(
                "PUT",
                f"/sell/inventory/v1/inventory_item/{urllib.parse.quote(sku, safe='')}",
                payload=self._inventory_payload(draft),
                safe_read=False,
                expected_statuses=(204,),
            )
            if status != 204:  # Defensive: expected_statuses already enforces this.
                raise EbayError(
                    "eBay did not confirm the Inventory item write",
                    code="INVALID_EBAY_RESPONSE",
                    outcome_unknown=True,
                )
            return {"sku": sku, "inventory_created": True}

    def verify_inventory_item(
        self,
        binding: VerifiedConnectionBinding,
        draft: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Read back and verify every reviewed Inventory-item field."""

        with self._account_lock:
            self._assert_draft_permission()
            self._assert_verified_binding(binding)
            try:
                item = self.get_inventory_item(str(draft["sku"]))
            except EbayError as error:
                error.outcome_unknown = True
                raise
            product = item.get("product") if isinstance(item, Mapping) else None
            availability = (
                item.get("availability") if isinstance(item, Mapping) else None
            )
            ship_to = (
                availability.get("shipToLocationAvailability")
                if isinstance(availability, Mapping)
                else None
            )
            returned_quantity = (
                ship_to.get("quantity") if isinstance(ship_to, Mapping) else None
            )
            expected_aspects = draft.get("aspects") or {}
            returned_aspects = (
                product.get("aspects") if isinstance(product, Mapping) else None
            )
            aspects_match = (
                returned_aspects == expected_aspects
                if expected_aspects
                else returned_aspects is None
                or (isinstance(returned_aspects, Mapping) and not returned_aspects)
            )
            matches = (
                isinstance(item, Mapping)
                and item.get("sku") == draft["sku"]
                and item.get("condition") == draft["condition"]
                and isinstance(product, Mapping)
                and product.get("title") == draft["title"]
                and product.get("description") == draft["description"]
                and aspects_match
                and isinstance(returned_quantity, int)
                and not isinstance(returned_quantity, bool)
                and returned_quantity == draft["quantity"]
            )
            if not matches:
                raise EbayError(
                    "The Inventory item could not be verified against the reviewed values",
                    status=409,
                    code="DRAFT_RECONCILIATION_REQUIRED",
                    outcome_unknown=True,
                )
            self._assert_draft_permission()
            self._assert_verified_binding(binding)
            return {
                "sku": str(draft["sku"]),
                "inventory_created": True,
                "inventory_verified": True,
            }

    def create_verified_unpublished_offer(
        self,
        binding: VerifiedConnectionBinding,
        draft: Mapping[str, Any],
        settings: Mapping[str, Any],
    ) -> str:
        """Create one Offer; this method has no route or call to publish it."""

        with self._account_lock:
            self._assert_draft_permission()
            self._assert_verified_binding(binding)
            sku = str(draft["sku"])
            if self.list_offers(sku):
                raise EbayError(
                    "An Offer already exists for this SKU; no additional Offer was created",
                    status=409,
                    code="OFFER_ALREADY_EXISTS",
                    outcome_unknown=True,
                )
            self._assert_draft_permission()
            self._assert_verified_binding(binding)
            response = self._api_request(
                "POST",
                "/sell/inventory/v1/offer",
                payload=self._offer_payload(draft, settings),
                safe_read=False,
                expected_statuses=(201,),
            )
            offer_id = response.get("offerId")
            if (
                not isinstance(offer_id, str)
                or not offer_id.strip()
                or len(offer_id) > 128
                or any(ord(character) < 0x21 or ord(character) == 0x7F for character in offer_id)
            ):
                raise EbayError(
                    "eBay did not confirm a valid unpublished Offer ID",
                    code="INVALID_EBAY_RESPONSE",
                    outcome_unknown=True,
                )
            return offer_id

    @staticmethod
    def _same_decimal(left: Any, right: Any) -> bool:
        try:
            return Decimal(str(left)) == Decimal(str(right))
        except (InvalidOperation, ValueError):
            return False

    def verify_unpublished_offer(
        self,
        binding: VerifiedConnectionBinding,
        offer_id: str,
        draft: Mapping[str, Any],
        settings: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Read back and strictly prove that the new Offer is not live."""

        with self._account_lock:
            self._assert_draft_permission()
            self._assert_verified_binding(binding)
            try:
                offer = self.get_offer(offer_id)
            except EbayError as error:
                error.outcome_unknown = True
                raise
            pricing = offer.get("pricingSummary")
            price = pricing.get("price") if isinstance(pricing, Mapping) else None
            listing = offer.get("listing")
            listing_is_absent = listing is None or (
                isinstance(listing, Mapping) and not listing
            )
            returned_quantity = offer.get("availableQuantity")
            expected_location = settings.get("merchant_location_key")
            returned_location = offer.get("merchantLocationKey")
            location_matches = (
                returned_location == expected_location
                if expected_location
                else returned_location is None or returned_location == ""
            )
            expected_policy_ids = {
                "fulfillmentPolicyId": settings.get("fulfillment_policy_id"),
                "paymentPolicyId": settings.get("payment_policy_id"),
                "returnPolicyId": settings.get("return_policy_id"),
            }
            returned_policies = offer.get("listingPolicies")
            if any(expected_policy_ids.values()):
                policies_match = isinstance(returned_policies, Mapping) and all(
                    returned_policies.get(name) == value
                    for name, value in expected_policy_ids.items()
                )
            else:
                policies_match = returned_policies is None or (
                    isinstance(returned_policies, Mapping) and not returned_policies
                )
            matches = (
                offer.get("offerId") == offer_id
                and offer.get("sku") == draft["sku"]
                and offer.get("status") == "UNPUBLISHED"
                and listing_is_absent
                and offer.get("marketplaceId") == settings["marketplace_id"]
                and offer.get("format") == settings["format"]
                and offer.get("listingDuration") == settings["listing_duration"]
                and isinstance(returned_quantity, int)
                and not isinstance(returned_quantity, bool)
                and returned_quantity == draft["quantity"]
                and str(offer.get("categoryId", "")) == draft["category_id"]
                and isinstance(price, Mapping)
                and price.get("currency") == draft["currency"]
                and self._same_decimal(price.get("value"), draft["price"])
                and location_matches
                and policies_match
            )
            if not matches:
                raise EbayError(
                    "The created Offer could not be verified as the reviewed unpublished Offer",
                    status=409,
                    code="DRAFT_RECONCILIATION_REQUIRED",
                    outcome_unknown=True,
                )
            self._assert_draft_permission()
            self._assert_verified_binding(binding)
            return {
                "sku": str(draft["sku"]),
                "offer_id": offer_id,
                "status": "UNPUBLISHED",
                "marketplace": str(settings["marketplace_id"]),
            }

    def get_inventory_item(self, sku: str) -> Optional[Dict[str, Any]]:
        try:
            return self._api_request(
                "GET",
                f"/sell/inventory/v1/inventory_item/{urllib.parse.quote(sku, safe='')}",
                safe_read=True,
            )
        except EbayError as error:
            if error.status == 404:
                return None
            raise

    def get_offer(self, offer_id: str) -> Dict[str, Any]:
        return self._api_request(
            "GET",
            f"/sell/inventory/v1/offer/{urllib.parse.quote(offer_id, safe='')}",
            safe_read=True,
        )

    def _assert_grant_generation(self, expected_generation: str) -> None:
        current = self.database.connection_generation()
        if (
            not expected_generation
            or current is None
            or not secrets.compare_digest(current, expected_generation)
        ):
            raise EbayError(
                f"The {self.config.environment.capitalize()} seller connection changed "
                "after review; review again",
                status=409,
                code="CONNECTION_CHANGED",
            )

    def create_draft(
        self,
        draft: Mapping[str, Any],
        *,
        expected_generation: str,
    ) -> Dict[str, Any]:
        # This legacy helper exists only for isolated later-stage fixtures. Real
        # Phase 5 must go through DraftService so seller binding, one-use consent,
        # checkpoints and unpublished readback cannot be bypassed.
        if self.config.feature_stage != "full":
            raise EbayError(
                "Use the confirmed unpublished-offer workflow in this "
                f"{self.config.environment.capitalize()} stage",
                status=403,
                code="MUTATION_LOCKED",
            )
        with self._account_lock:
            return self._create_draft(draft, expected_generation)

    def _create_draft(
        self,
        draft: Mapping[str, Any],
        expected_generation: str,
    ) -> Dict[str, Any]:
        self._assert_grant_generation(expected_generation)
        sku = str(draft["sku"])
        if self.get_inventory_item(sku) is not None:
            raise SkuExistsError(sku)
        product: Dict[str, Any] = {
            "title": draft["title"],
            "description": draft["description"],
        }
        if draft.get("image_urls"):
            product["imageUrls"] = draft["image_urls"]
        if draft.get("aspects"):
            product["aspects"] = draft["aspects"]
        inventory_payload = {
            "availability": {"shipToLocationAvailability": {"quantity": draft["quantity"]}},
            "condition": draft["condition"],
            "product": product,
        }
        self._assert_grant_generation(expected_generation)
        inventory_result = self._api_request(
            "PUT",
            f"/sell/inventory/v1/inventory_item/{urllib.parse.quote(sku, safe='')}",
            payload=inventory_payload,
            safe_read=False,
            expected_statuses=(201,),
        )
        offer_payload: Dict[str, Any] = {
            "sku": sku,
            "marketplaceId": self.config.marketplace_id,
            "format": "FIXED_PRICE",
            "availableQuantity": draft["quantity"],
            "categoryId": draft["category_id"],
            "listingDuration": "GTC",
            "pricingSummary": {
                "price": {"currency": draft["currency"], "value": draft["price"]}
            },
        }
        if self.config.merchant_location_key:
            offer_payload["merchantLocationKey"] = self.config.merchant_location_key
        if self.config.fulfillment_policy_id:
            offer_payload["listingPolicies"] = {
                "fulfillmentPolicyId": self.config.fulfillment_policy_id,
                "paymentPolicyId": self.config.payment_policy_id,
                "returnPolicyId": self.config.return_policy_id,
            }
        try:
            self._assert_grant_generation(expected_generation)
            offer_result = self._api_request(
                "POST",
                "/sell/inventory/v1/offer",
                payload=offer_payload,
                safe_read=False,
                expected_statuses=(201,),
            )
        except EbayError as error:
            raise DraftPartialError(sku, error) from error
        offer_id = offer_result.get("offerId")
        if not isinstance(offer_id, str) or not offer_id.strip():
            raise DraftPartialError(
                sku,
                EbayError(
                    "eBay did not confirm an Offer ID",
                    code="INVALID_EBAY_RESPONSE",
                    outcome_unknown=True,
                ),
            )
        return {
            "sku": sku,
            "offer_id": offer_id,
            "status": "UNPUBLISHED",
            "inventory_response": inventory_result,
            "warnings": offer_result.get("warnings", []),
        }

    @staticmethod
    def offer_price(offer: Mapping[str, Any]) -> Dict[str, str]:
        price = offer.get("pricingSummary", {}).get("price", {})
        return {"value": str(price.get("value", "")), "currency": str(price.get("currency", ""))}

    def change_price(
        self,
        *,
        offer_id: str,
        sku: str,
        expected_price: str,
        currency: str,
        new_price: str,
        expected_generation: str,
    ) -> Dict[str, Any]:
        with self._account_lock:
            return self._change_price(
                offer_id=offer_id,
                sku=sku,
                expected_price=expected_price,
                currency=currency,
                new_price=new_price,
                expected_generation=expected_generation,
            )

    def _change_price(
        self,
        *,
        offer_id: str,
        sku: str,
        expected_price: str,
        currency: str,
        new_price: str,
        expected_generation: str,
    ) -> Dict[str, Any]:
        self._assert_grant_generation(expected_generation)
        latest = self.get_offer(offer_id)
        if latest.get("offerId") != offer_id:
            raise EbayError(
                "eBay returned a different Offer than the one reviewed",
                status=409,
                code="OFFER_MISMATCH",
            )
        if latest.get("sku") != sku:
            raise EbayError("The Offer no longer belongs to the reviewed SKU", status=409)
        if latest.get("status") != "PUBLISHED":
            raise EbayError(
                "Only published Offers can use the targeted price-change operation",
                status=409,
                code="OFFER_NOT_PUBLISHED",
            )
        current = self.offer_price(latest)
        if current != {"value": expected_price, "currency": currency}:
            raise PriceChangedError()
        payload = {
            "requests": [
                {
                    "offers": [
                        {
                            "offerId": offer_id,
                            "price": {"currency": currency, "value": new_price},
                        }
                    ]
                }
            ]
        }
        self._assert_grant_generation(expected_generation)
        response = self._api_request(
            "POST",
            "/sell/inventory/v1/bulk_update_price_quantity",
            payload=payload,
            safe_read=False,
            expected_statuses=(200,),
        )
        results = response.get("responses")
        if (
            not isinstance(results, list)
            or len(results) != 1
            or not isinstance(results[0], dict)
            or results[0].get("offerId") != offer_id
            or results[0].get("sku") != sku
            or results[0].get("statusCode") != 200
        ):
            raise EbayError(
                "eBay did not confirm the requested Offer price update",
                code="PRICE_UPDATE_UNCONFIRMED",
                details=results,
                outcome_unknown=True,
            )
        return {"offer_id": offer_id, "sku": sku, "price": new_price, "currency": currency}

    @staticmethod
    def offer_quantity(offer: Mapping[str, Any]) -> Optional[int]:
        quantity = offer.get("availableQuantity")
        return quantity if isinstance(quantity, int) else None

    def change_quantity(
        self,
        *,
        offer_id: str,
        sku: str,
        expected_quantity: int,
        new_quantity: int,
        expected_generation: str,
    ) -> Dict[str, Any]:
        with self._account_lock:
            return self._change_quantity(
                offer_id=offer_id,
                sku=sku,
                expected_quantity=expected_quantity,
                new_quantity=new_quantity,
                expected_generation=expected_generation,
            )

    def _change_quantity(
        self,
        *,
        offer_id: str,
        sku: str,
        expected_quantity: int,
        new_quantity: int,
        expected_generation: str,
    ) -> Dict[str, Any]:
        self._assert_grant_generation(expected_generation)
        latest = self.get_offer(offer_id)
        if latest.get("offerId") != offer_id:
            raise EbayError(
                "eBay returned a different Offer than the one reviewed",
                status=409,
                code="OFFER_MISMATCH",
            )
        if latest.get("sku") != sku:
            raise EbayError("The Offer no longer belongs to the reviewed SKU", status=409)
        if latest.get("status") != "PUBLISHED":
            raise EbayError(
                "Only published Offers can use the targeted quantity-change operation",
                status=409,
                code="OFFER_NOT_PUBLISHED",
            )
        current = self.offer_quantity(latest)
        if current != expected_quantity:
            raise QuantityChangedError()
        payload = {
            "requests": [
                {
                    "offers": [
                        {
                            "offerId": offer_id,
                            "availableQuantity": new_quantity,
                        }
                    ]
                }
            ]
        }
        self._assert_grant_generation(expected_generation)
        response = self._api_request(
            "POST",
            "/sell/inventory/v1/bulk_update_price_quantity",
            payload=payload,
            safe_read=False,
            expected_statuses=(200,),
        )
        results = response.get("responses")
        if (
            not isinstance(results, list)
            or len(results) != 1
            or not isinstance(results[0], dict)
            or results[0].get("offerId") != offer_id
            or results[0].get("sku") != sku
            or results[0].get("statusCode") != 200
        ):
            raise EbayError(
                "eBay did not confirm the requested Offer quantity update",
                code="QUANTITY_UPDATE_UNCONFIRMED",
                details=results,
                outcome_unknown=True,
            )
        return {"offer_id": offer_id, "sku": sku, "quantity": new_quantity}

    def migrate_listing(
        self,
        *,
        item_id: str,
        expected_generation: str,
    ) -> Dict[str, Any]:
        with self._account_lock:
            return self._migrate_listing(item_id=item_id, expected_generation=expected_generation)

    def _migrate_listing(self, item_id: str, expected_generation: str) -> Dict[str, Any]:
        """Call eBay's bulkMigrateListing for exactly one legacy listing.

        Deliberately one listing per call, never the up-to-five eBay allows:
        the whole point of a first migration is the smallest possible blast
        radius. There is no existing Offer or Inventory Item to read back and
        compare against first (that is exactly what migration creates), so
        unlike change_price/change_quantity this cannot re-verify the current
        state before writing -- the caller is responsible for having checked
        the listing's eligibility (fixed-price GTC, Business Policies in use,
        immediate payment, a SKU/Custom Label and postal code already set,
        not private, not a digital gift card) directly in Seller Hub first.

        eBay's public documentation describes the request/response shape at
        the level used below (a ``requests``/``responses`` envelope, each
        response item carrying its own ``statusCode``) but does not publish
        the full field-level schema for this specific endpoint the way it
        does for simpler ones. The raw per-item response is therefore always
        returned to the caller untouched (as ``raw_response``) rather than
        parsed into named fields this code cannot be sure of -- the caller
        must show it to the seller so they can judge the real result
        themselves, not just trust a summary of it.
        """

        self._assert_grant_generation(expected_generation)
        listing_id = item_id.strip() if item_id else ""
        if not listing_id:
            raise EbayError(
                "A real eBay listing Item ID is required to migrate it",
                code="MIGRATION_ITEM_ID_REQUIRED",
            )
        payload = {"requests": [{"listingId": listing_id}]}
        self._assert_grant_generation(expected_generation)
        response = self._api_request(
            "POST",
            "/sell/inventory/v1/bulk_migrate_listing",
            payload=payload,
            safe_read=False,
            expected_statuses=(200,),
            include_raw_payload_on_error=True,
        )
        results = response.get("responses")
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
            raise EbayError(
                "eBay returned an unexpected response shape for the migration request",
                code="MIGRATION_RESPONSE_UNRECOGNIZED",
                details=response if isinstance(response, dict) else None,
                outcome_unknown=True,
            )
        result = results[0]
        status_code = result.get("statusCode")
        if status_code != 200:
            raise EbayError(
                "eBay did not confirm the listing migration",
                code="MIGRATION_UNCONFIRMED",
                details=_error_details(result) or [result],
                outcome_unknown=status_code is None,
            )
        return {"item_id": listing_id, "raw_response": result}

    @staticmethod
    def inventory_item_image_urls(item: Mapping[str, Any]) -> List[str]:
        product = item.get("product") or {}
        urls = product.get("imageUrls")
        return [str(url) for url in urls] if isinstance(urls, list) else []

    def add_inventory_item_image(
        self,
        *,
        offer_id: str,
        sku: str,
        expected_image_urls: List[str],
        new_image_url: str,
        expected_generation: str,
    ) -> Dict[str, Any]:
        with self._account_lock:
            return self._add_inventory_item_image(
                offer_id=offer_id,
                sku=sku,
                expected_image_urls=expected_image_urls,
                new_image_url=new_image_url,
                expected_generation=expected_generation,
            )

    def _add_inventory_item_image(
        self,
        *,
        offer_id: str,
        sku: str,
        expected_image_urls: List[str],
        new_image_url: str,
        expected_generation: str,
    ) -> Dict[str, Any]:
        self._assert_grant_generation(expected_generation)
        latest_offer = self.get_offer(offer_id)
        if latest_offer.get("offerId") != offer_id:
            raise EbayError(
                "eBay returned a different Offer than the one reviewed",
                status=409,
                code="OFFER_MISMATCH",
            )
        if latest_offer.get("sku") != sku:
            raise EbayError("The Offer no longer belongs to the reviewed SKU", status=409)
        if latest_offer.get("status") != "PUBLISHED":
            raise EbayError(
                "Only published Offers can use the targeted image-add operation",
                status=409,
                code="OFFER_NOT_PUBLISHED",
            )
        item = self.get_inventory_item(sku)
        if item is None:
            raise EbayError(
                "The Inventory item no longer exists",
                status=409,
                code="INVENTORY_ITEM_MISSING",
            )
        current = self.inventory_item_image_urls(item)
        if current != expected_image_urls:
            raise ImagesChangedError()
        if new_image_url in current:
            raise EbayError(
                "This image is already attached to the listing",
                status=409,
                code="IMAGE_ALREADY_ATTACHED",
            )
        if len(current) >= 24:
            raise EbayError(
                "eBay allows at most 24 images on one listing",
                status=409,
                code="IMAGE_LIMIT_REACHED",
            )
        new_urls = current + [new_image_url]
        product = dict(item.get("product") or {})
        product["imageUrls"] = new_urls
        payload = {
            "availability": item.get("availability")
            or {"shipToLocationAvailability": {"quantity": 0}},
            "condition": item.get("condition", "NEW"),
            "product": product,
        }
        self._assert_grant_generation(expected_generation)
        self._api_request(
            "PUT",
            f"/sell/inventory/v1/inventory_item/{urllib.parse.quote(sku, safe='')}",
            payload=payload,
            safe_read=False,
            expected_statuses=(204,),
        )
        return {
            "offer_id": offer_id,
            "sku": sku,
            "image_url": new_image_url,
            "image_count": len(new_urls),
        }
