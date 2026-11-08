from __future__ import annotations

import io
import json
import re
import secrets
import tempfile
import unittest
import urllib.parse
from contextlib import contextmanager
from dataclasses import replace
from html import escape
from pathlib import Path
from unittest.mock import patch

from ebay_app.config import (
    DRAFT_SCOPES,
    INVENTORY_READ_SCOPE,
    INVENTORY_WRITE_SCOPE,
    ORDER_SCOPES,
)
from ebay_app.drafts import DraftService
from ebay_app.ebay_client import EbayError, EbaySandboxClient, HttpResponse
from ebay_app.storage import AppDatabase
from ebay_app.views import draft_action_page
from ebay_app.web import WebApplication

from helpers import FakeTransport, adopt_test_access_token, json_response, make_config


SELLER_CANARY = "PRIVATE-INTERNAL-SELLER-ID"
REFRESH_CANARY = "PRIVATE-REFRESH-TOKEN"
ACCESS_CANARY = "PRIVATE-ACCESS-TOKEN"
TOKEN_SUFFIX = "A1B2C3D4E5F60718293A4B5C"
GENERATED_SKU = f"SAFE-SKU-1-SBX-{TOKEN_SUFFIX}"
REAL_TOKEN_HEX = secrets.token_hex
ERROR_CANARIES = (
    "PRIVATE-EBAY-ERROR-ID",
    "PRIVATE-EBAY-ERROR-MESSAGE",
    "PRIVATE-REMOTE-RESPONSE",
    SELLER_CANARY,
    REFRESH_CANARY,
    ACCESS_CANARY,
)


def draft_form(**updates: str) -> dict[str, str]:
    values = {
        "csrf_token": "csrf",
        "sku": "SAFE-SKU-1",
        "title": "Example Sandbox item",
        "description": "A plain text Sandbox description.",
        "condition": "NEW",
        "quantity": "2",
        "price": "19.99",
        "category_id": "12345",
        "aspects": '{"Brand":["Example"]}',
    }
    values.update(updates)
    return values


def service_form(**updates: str) -> dict[str, list[str]]:
    return {name: [value] for name, value in draft_form(**updates).items()}


def unpublished_offer(
    offer_id: str,
    *,
    sku: str = GENERATED_SKU,
    status: str = "UNPUBLISHED",
) -> dict:
    return {
        "offerId": offer_id,
        "sku": sku,
        "status": status,
        "marketplaceId": "EBAY_GB",
        "format": "FIXED_PRICE",
        "listingDuration": "GTC",
        "availableQuantity": 2,
        "categoryId": "12345",
        "pricingSummary": {"price": {"currency": "GBP", "value": "19.99"}},
    }


def inventory_item(
    *,
    sku: str = GENERATED_SKU,
    title: str = "Example Sandbox item",
    description: str = "A plain text Sandbox description.",
    aspects=None,
) -> dict:
    product = {
        "title": title,
        "description": description,
    }
    if aspects is not None:
        product["aspects"] = aspects
    return {
        "sku": sku,
        "condition": "NEW",
        "product": product,
        "availability": {"shipToLocationAvailability": {"quantity": 2}},
    }


def not_found_response() -> HttpResponse:
    return json_response(404, {"errors": []})


class RecordingDraftClient:
    """Small service double; protocol tests below use the real eBay client."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.after_preflight = None
        self.inventory_exists = False

    def verified_inventory_item_exists(self, binding, sku):
        self.calls.append(("preflight", binding, sku))
        if self.after_preflight is not None:
            self.after_preflight()
        return self.inventory_exists

    @contextmanager
    def draft_mutation_guard(self):
        self.calls.append(("guard_enter",))
        try:
            yield
        finally:
            self.calls.append(("guard_exit",))

    def create_verified_inventory_item(self, binding, draft):
        self.calls.append(("create_inventory", binding, dict(draft)))
        return {"sku": draft["sku"], "inventory_created": True}

    def verify_inventory_item(self, binding, draft):
        self.calls.append(("verify_inventory", binding, dict(draft)))
        return {
            "sku": draft["sku"],
            "inventory_created": True,
            "inventory_verified": True,
        }

    def create_verified_unpublished_offer(self, binding, draft, settings):
        self.calls.append(
            ("create_offer", binding, dict(draft), dict(settings))
        )
        return "OFFER-1"

    def verify_unpublished_offer(self, binding, offer_id, draft, settings):
        self.calls.append(
            ("verify_unpublished", binding, offer_id, dict(draft), dict(settings))
        )
        return {
            "sku": draft["sku"],
            "offer_id": offer_id,
            "status": "UNPUBLISHED",
            "marketplace": settings["marketplace_id"],
        }


class DraftDatabaseTestCase(unittest.TestCase):
    def setUp(self):
        token_patch = patch(
            "ebay_app.drafts.secrets.token_hex",
            side_effect=lambda byte_count: (
                TOKEN_SUFFIX.lower()
                if byte_count == 12
                else REAL_TOKEN_HEX(byte_count)
            ),
        )
        token_patch.start()
        self.addCleanup(token_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(
            make_config(Path(self.temp.name)),
            feature_stage="drafts",
            scopes=DRAFT_SCOPES,
            runtime_mode="local",
        )
        self.database = AppDatabase(
            self.config.database_path,
            self.config.data_encryption_key,
        )
        self.addCleanup(self.database.close)

    def grant(
        self,
        scopes=DRAFT_SCOPES,
        *,
        user_id: str = SELLER_CANARY,
        verified: bool = True,
        refresh_token: str = REFRESH_CANARY,
    ) -> None:
        self.database.save_refresh_token(
            refresh_token,
            9_999_999_999,
            user_id,
            new_grant=True,
            granted_scopes=scopes,
        )
        if verified:
            generation = self.database.connection_generation()
            self.database.mark_connection_verified(generation, user_id)


class DraftServiceGateTests(DraftDatabaseTestCase):
    def test_stage_runtime_scope_and_verification_gates_precede_client_calls(self):
        client = RecordingDraftClient()

        with self.assertRaises(EbayError) as wrong_stage:
            DraftService(
                client,
                self.database,
                replace(self.config, feature_stage="orders"),
            ).prepare_review(service_form(), "session")
        self.assertEqual(wrong_stage.exception.code, "DRAFT_STAGE_LOCKED")

        with self.assertRaises(EbayError) as unsupported_runtime:
            DraftService(
                client,
                self.database,
                replace(self.config, runtime_mode="unsupported"),
            ).prepare_review(service_form(), "session")
        self.assertEqual(
            unsupported_runtime.exception.code,
            "DRAFT_LOCAL_MODE_REQUIRED",
        )

        self.grant(ORDER_SCOPES)
        with self.assertRaises(EbayError) as missing_scope:
            DraftService(client, self.database, self.config).prepare_review(
                service_form(), "session"
            )
        self.assertEqual(missing_scope.exception.code, "DRAFT_PERMISSION_REQUIRED")

        self.grant(DRAFT_SCOPES, verified=False, refresh_token="replacement")
        with self.assertRaises(EbayError) as unverified:
            DraftService(client, self.database, self.config).prepare_review(
                service_form(), "session"
            )
        self.assertEqual(unverified.exception.code, "CONNECTION_NOT_VERIFIED")
        self.assertEqual(client.calls, [])

    def test_review_is_read_only_and_fails_atomically_if_seller_changes(self):
        self.grant()
        client = RecordingDraftClient()

        def replace_seller() -> None:
            self.grant(
                user_id="DIFFERENT-SELLER",
                refresh_token="different-seller-refresh",
            )

        client.after_preflight = replace_seller
        service = DraftService(client, self.database, self.config)

        with self.assertRaises(EbayError) as changed:
            service.prepare_review(service_form(), "browser-session")

        self.assertEqual(changed.exception.code, "CONNECTION_CHANGED")
        self.assertEqual([call[0] for call in client.calls], ["preflight"])
        self.assertEqual(self.database.recent_actions(), [])

    def test_confirmation_is_bound_to_the_reviewed_grant_generation(self):
        self.grant()
        client = RecordingDraftClient()
        service = DraftService(client, self.database, self.config)
        review = service.prepare_review(service_form(), "browser-session")

        self.grant(refresh_token="new-grant-for-same-seller")
        action_id = service.confirm(review.action_token, "browser-session")

        self.assertIsNone(action_id)
        self.assertEqual([call[0] for call in client.calls], ["preflight"])

    def test_review_generates_a_unique_app_owned_sku_and_only_reads_ebay(self):
        self.grant()
        client = RecordingDraftClient()
        service = DraftService(client, self.database, self.config)
        suffixes = iter(("1" * 24, "2" * 24))

        def unique_token(byte_count):
            return next(suffixes) if byte_count == 12 else REAL_TOKEN_HEX(byte_count)

        with patch("ebay_app.drafts.secrets.token_hex", side_effect=unique_token):
            first = service.prepare_review(service_form(), "browser-session")
            second = service.prepare_review(service_form(), "browser-session")

        self.assertEqual(first.draft["sku"], f"SAFE-SKU-1-SBX-{'1' * 24}")
        self.assertEqual(second.draft["sku"], f"SAFE-SKU-1-SBX-{'2' * 24}")
        self.assertNotEqual(first.draft["sku"], second.draft["sku"])
        self.assertEqual([call[0] for call in client.calls], ["preflight", "preflight"])
        self.assertTrue(
            all(call[2].startswith("SAFE-SKU-1-SBX-") for call in client.calls)
        )

    def test_existing_generated_sku_stops_review_without_pending_action(self):
        self.grant()
        client = RecordingDraftClient()
        client.inventory_exists = True

        with self.assertRaises(EbayError) as existing:
            DraftService(client, self.database, self.config).prepare_review(
                service_form(),
                "browser-session",
            )

        self.assertEqual(existing.exception.code, "SKU_ALREADY_EXISTS")
        self.assertEqual([call[0] for call in client.calls], ["preflight"])
        self.assertEqual(self.database.recent_actions(), [])


class DraftProductionSkuMarkerTests(DraftDatabaseTestCase):
    """Regression test for a real data-correctness bug found 2026-11-05:
    the app-owned SKU-collision-avoidance suffix was hardcoded to "-SBX-"
    unconditionally, regardless of which environment the config actually
    was. DraftService is reachable from the "prices" stage (a cumulative
    capability) -- which Phase B made reachable in Production, behind its
    own opt-in -- so this was not purely cosmetic: a draft created against
    the real account would have had a literally false "-SBX-" embedded in
    its real Inventory SKU. Fixed by deriving the marker from
    ``self.config.environment``."""

    def setUp(self):
        super().setUp()
        self.config = replace(self.config, environment="production")
        self.grant()

    def test_a_production_config_marks_the_generated_sku_prd_not_sbx(self):
        client = RecordingDraftClient()
        service = DraftService(client, self.database, self.config)
        review = service.prepare_review(service_form(), "browser-session")
        self.assertEqual(review.draft["sku"], f"SAFE-SKU-1-PRD-{TOKEN_SUFFIX.upper()}")
        self.assertNotIn("-SBX-", review.draft["sku"])

    def test_connection_not_verified_message_says_production_not_sandbox(self):
        # Granted scopes but never verified -- isolates the specific
        # "Verify the eBay <environment> connection..." message from the
        # earlier permission-scope gate.
        client = RecordingDraftClient()
        empty_database = AppDatabase(
            self.config.database_path.with_name("empty.sqlite"),
            self.config.data_encryption_key,
        )
        self.addCleanup(empty_database.close)
        empty_database.save_refresh_token(
            REFRESH_CANARY,
            9_999_999_999,
            SELLER_CANARY,
            new_grant=True,
            granted_scopes=DRAFT_SCOPES,
        )
        service = DraftService(client, empty_database, self.config)
        with self.assertRaises(EbayError) as not_verified:
            service.prepare_review(service_form(), "browser-session")
        self.assertEqual(not_verified.exception.code, "CONNECTION_NOT_VERIFIED")
        self.assertIn("Production", str(not_verified.exception))
        self.assertNotIn("Sandbox", str(not_verified.exception))


class PhaseFivePermissionUpgradeTests(DraftDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.grant(
            ORDER_SCOPES,
            user_id="same-seller",
            refresh_token="phase-four-refresh",
        )

    def client_for_identity(self, user_id: str) -> tuple[EbaySandboxClient, FakeTransport]:
        transport = FakeTransport(
            [
                json_response(
                    200,
                    {
                        "access_token": "new-access",
                        "expires_in": 7200,
                        "refresh_token": "phase-five-refresh",
                        "refresh_token_expires_in": 100_000,
                    },
                ),
                json_response(200, {"userId": user_id}),
            ]
        )
        return EbaySandboxClient(self.config, self.database, transport=transport), transport

    def test_upgrade_requests_exact_manage_scope_and_requires_same_seller(self):
        client, transport = self.client_for_identity("same-seller")
        authorization_url = client.authorization_url("browser-session")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(authorization_url).query)

        self.assertEqual(query["scope"], [" ".join(DRAFT_SCOPES)])
        self.assertIn(INVENTORY_WRITE_SCOPE, query["scope"][0].split())
        self.assertNotIn(INVENTORY_READ_SCOPE, query["scope"][0].split())
        result = client.complete_authorization(
            query["state"][0],
            "authorization-code",
            "browser-session",
        )

        self.assertTrue(result["upgraded"])
        self.assertEqual(set(self.database.granted_scopes()), set(DRAFT_SCOPES))
        self.assertEqual(self.database.get_refresh_token(), "phase-five-refresh")
        self.assertFalse(self.database.connection_status()["verified"])
        self.assertEqual([call["method"] for call in transport.calls], ["POST", "GET"])

    def test_different_seller_cannot_replace_the_verified_phase_four_grant(self):
        client, _transport = self.client_for_identity("different-seller")
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(client.authorization_url("browser-session")).query
        )

        with self.assertRaises(EbayError) as mismatch:
            client.complete_authorization(
                query["state"][0],
                "authorization-code",
                "browser-session",
            )

        self.assertEqual(mismatch.exception.code, "SELLER_MISMATCH")
        self.assertEqual(self.database.get_refresh_token(), "phase-four-refresh")
        self.assertEqual(set(self.database.granted_scopes()), set(ORDER_SCOPES))
        self.assertTrue(self.database.connection_status()["verified"])


class WebTestMixin:
    app: WebApplication
    config: object
    request_host = "127.0.0.1:5000"

    def request(self, method, path, *, cookie="", form=None, query=""):
        body = urllib.parse.urlencode(form or {}).encode("utf-8")
        captured = {}

        def start_response(status, headers):
            captured["status"] = int(status.split(" ", 1)[0])
            captured["headers"] = dict(headers)

        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "REMOTE_ADDR": "127.0.0.1",
            "HTTP_HOST": self.request_host,
            "HTTP_COOKIE": cookie,
            "wsgi.input": io.BytesIO(body),
            "CONTENT_LENGTH": str(len(body)),
            "CONTENT_TYPE": (
                "application/x-www-form-urlencoded" if method == "POST" else ""
            ),
        }
        response = b"".join(self.app(environ, start_response)).decode("utf-8")
        return captured["status"], captured["headers"], response

    @staticmethod
    def cookie(headers):
        return headers["Set-Cookie"].split(";", 1)[0]

    @staticmethod
    def session_id(cookie):
        return cookie.split("=", 1)[1]

    @staticmethod
    def hidden(body, name):
        match = re.search(rf'name="{re.escape(name)}" value="([^"]+)"', body)
        if match is None:
            raise AssertionError(f"Missing hidden field {name}")
        return match.group(1)

    def login(self):
        status, headers, body = self.request("GET", "/login")
        self.assertEqual(status, 200)
        cookie = self.cookie(headers)
        csrf = self.hidden(body, "csrf_token")
        status, headers, _body = self.request(
            "POST",
            "/login",
            cookie=cookie,
            form={"csrf_token": csrf, "password": self.config.admin_password},
        )
        self.assertEqual(status, 303)
        cookie = self.cookie(headers)
        status, _headers, body = self.request("GET", "/", cookie=cookie)
        self.assertEqual(status, 200)
        return cookie, self.hidden(body, "csrf_token"), body


class DraftFullStackTests(WebTestMixin, DraftDatabaseTestCase):
    OFFER_ID = 'OFFER-<svg/onload=alert(1)>&"'

    def setUp(self):
        super().setUp()
        self.config = replace(
            self.config,
            merchant_location_key="MAIN-LOCATION",
            fulfillment_policy_id="FULFILMENT-POLICY",
            payment_policy_id="PAYMENT-POLICY",
            return_policy_id="RETURN-POLICY",
        )
        self.grant()
        verified_offer = unpublished_offer(self.OFFER_ID)
        verified_offer["merchantLocationKey"] = "MAIN-LOCATION"
        verified_offer["listingPolicies"] = {
            "fulfillmentPolicyId": "FULFILMENT-POLICY",
            "paymentPolicyId": "PAYMENT-POLICY",
            "returnPolicyId": "RETURN-POLICY",
        }
        verified_inventory = inventory_item(
            title='Rock & "Roll" record',
            description="Condition is 2 < 3 & 5 > 4.",
            aspects={"Material": ["<svg/onload=alert(1)> & suede"]},
        )
        self.transport = FakeTransport(
            [
                not_found_response(),
                not_found_response(),
                HttpResponse(204, {}, b""),
                json_response(200, verified_inventory),
                json_response(200, {"offers": [], "total": 0}),
                json_response(201, {"offerId": self.OFFER_ID}),
                json_response(200, verified_offer),
            ]
        )
        self.ebay = EbaySandboxClient(
            self.config,
            self.database,
            transport=self.transport,
        )
        adopt_test_access_token(self.ebay)
        self.ebay._access_token = ACCESS_CANARY
        self.app = WebApplication(self.config, self.database, self.ebay)

    def test_review_confirm_exact_protocol_unpublished_one_use_and_escaping(self):
        cookie, csrf, dashboard = self.login()
        self.assertIn("Phase 5 safety boundary", dashboard)
        self.assertIn("Prepare an unpublished offer", dashboard)
        self.assertNotIn(SELLER_CANARY, dashboard)

        submitted = draft_form(
            csrf_token=csrf,
            title='Rock & "Roll" record',
            description="Condition is 2 < 3 & 5 > 4.",
            aspects='{"Material":["<svg/onload=alert(1)> & suede"]}',
        )
        status, _headers, review_body = self.request(
            "POST",
            "/drafts/review",
            cookie=cookie,
            form=submitted,
        )

        self.assertEqual(status, 200)
        self.assertIn("No eBay change has been made yet", review_body)
        self.assertIn("Rock &amp; &quot;Roll&quot; record", review_body)
        self.assertIn("Condition is 2 &lt; 3 &amp; 5 &gt; 4.", review_body)
        self.assertIn("&lt;svg/onload=alert(1)&gt; &amp; suede", review_body)
        self.assertNotIn("<svg/onload", review_body)
        self.assertIn(GENERATED_SKU, review_body)
        self.assertNotEqual(submitted["sku"], GENERATED_SKU)
        for private_value in (SELLER_CANARY, REFRESH_CANARY, ACCESS_CANARY):
            self.assertNotIn(private_value, review_body)

        # Review may perform exactly one seller-bound GET preflight, never a write.
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.transport.calls[0]["method"], "GET")
        self.assertIsNone(self.transport.calls[0]["body"])
        self.assertEqual(
            urllib.parse.urlsplit(self.transport.calls[0]["url"]).path,
            f"/sell/inventory/v1/inventory_item/{GENERATED_SKU}",
        )
        token = self.hidden(review_body, "action_token")

        # A copied confirmation token is useless in a different authenticated session.
        other_cookie, other_csrf, _dashboard = self.login()
        status, _headers, body = self.request(
            "POST",
            "/actions/confirm",
            cookie=other_cookie,
            form={"csrf_token": other_csrf, "action_token": token},
        )
        self.assertEqual(status, 409)
        self.assertIn("already used", body)
        self.assertEqual(len(self.transport.calls), 1)

        status, headers, _body = self.request(
            "POST",
            "/actions/confirm",
            cookie=cookie,
            form={"csrf_token": csrf, "action_token": token},
        )
        self.assertEqual(status, 303)
        self.assertRegex(headers["Location"], r"^/actions/[a-f0-9]{32}$")
        action_id = headers["Location"].rsplit("/", 1)[1]

        calls_after_confirmation = len(self.transport.calls)
        status, _headers, replay_body = self.request(
            "POST",
            "/actions/confirm",
            cookie=cookie,
            form={"csrf_token": csrf, "action_token": token},
        )
        self.assertEqual(status, 409)
        self.assertIn("already used", replay_body)
        self.assertIn("already completed on an earlier submission", replay_body)
        self.assertEqual(len(self.transport.calls), calls_after_confirmation)

        status, _headers, other_result_body = self.request(
            "GET", headers["Location"], cookie=other_cookie
        )
        self.assertEqual(status, 200)
        self.assertIn("Unpublished Sandbox offer created and verified", other_result_body)
        status, _headers, result_body = self.request(
            "GET", headers["Location"], cookie=cookie
        )
        self.assertEqual(status, 200)
        self.assertIn("Unpublished Sandbox offer created and verified", result_body)
        self.assertIn(escape(self.OFFER_ID, quote=True), result_body)
        self.assertNotIn("<svg/onload", result_body)
        for private_value in (
            SELLER_CANARY,
            REFRESH_CANARY,
            ACCESS_CANARY,
            submitted["title"],
            submitted["description"],
            submitted["aspects"],
        ):
            self.assertNotIn(private_value, result_body)

        action = self.database.get_action(action_id, self.session_id(cookie))
        self.assertEqual(action["status"], "succeeded")
        self.assertEqual(action["checkpoint"], "verified_unpublished")
        self.assertEqual(
            action["result"],
            {
                "inventory_created": True,
                "inventory_verified": True,
                "marketplace_id": "EBAY_GB",
                "offer_created": True,
                "offer_id": self.OFFER_ID,
                "offer_status": "UNPUBLISHED",
                "reconciliation_required": False,
                "sku": GENERATED_SKU,
                "verified_unpublished": True,
            },
        )
        events = self.database.action_events(action_id, self.session_id(cookie))
        self.assertEqual(
            [event["event_type"] for event in events],
            [
                "confirmed",
                "inventory_request_started",
                "inventory_created",
                "inventory_verified",
                "offer_request_started",
                "offer_created",
                "verified_unpublished",
                "terminal_result",
            ],
        )
        self.assertEqual(events[-1]["terminal_status"], "succeeded")

        expected_offer_id = urllib.parse.quote(self.OFFER_ID, safe="")
        self.assertEqual(
            [call["method"] for call in self.transport.calls],
            ["GET", "GET", "PUT", "GET", "GET", "POST", "GET"],
        )
        self.assertEqual(
            [call["url"] for call in self.transport.calls],
            [
                f"https://api.sandbox.ebay.com/sell/inventory/v1/inventory_item/{GENERATED_SKU}",
                f"https://api.sandbox.ebay.com/sell/inventory/v1/inventory_item/{GENERATED_SKU}",
                f"https://api.sandbox.ebay.com/sell/inventory/v1/inventory_item/{GENERATED_SKU}",
                f"https://api.sandbox.ebay.com/sell/inventory/v1/inventory_item/{GENERATED_SKU}",
                f"https://api.sandbox.ebay.com/sell/inventory/v1/offer?sku={GENERATED_SKU}&limit=100&offset=0",
                "https://api.sandbox.ebay.com/sell/inventory/v1/offer",
                f"https://api.sandbox.ebay.com/sell/inventory/v1/offer/{expected_offer_id}",
            ],
        )
        self.assertEqual(
            json.loads(self.transport.calls[2]["body"]),
            {
                "availability": {"shipToLocationAvailability": {"quantity": 2}},
                "condition": "NEW",
                "product": {
                    "title": submitted["title"],
                    "description": submitted["description"],
                    "aspects": {"Material": ["<svg/onload=alert(1)> & suede"]},
                },
            },
        )
        self.assertEqual(
            json.loads(self.transport.calls[5]["body"]),
            {
                "sku": GENERATED_SKU,
                "marketplaceId": "EBAY_GB",
                "format": "FIXED_PRICE",
                "availableQuantity": 2,
                "categoryId": "12345",
                "listingDuration": "GTC",
                "pricingSummary": {
                    "price": {"currency": "GBP", "value": "19.99"}
                },
                "merchantLocationKey": "MAIN-LOCATION",
                "listingPolicies": {
                    "fulfillmentPolicyId": "FULFILMENT-POLICY",
                    "paymentPolicyId": "PAYMENT-POLICY",
                    "returnPolicyId": "RETURN-POLICY",
                },
            },
        )
        self.assertFalse(
            any("publish" in call["url"].lower() for call in self.transport.calls)
        )

        # Completed reconciliation records survive both the browser session and
        # a process/database restart; only authentication and the opaque ID gate them.
        database_path = self.config.database_path
        encryption_key = self.config.data_encryption_key
        self.database.close()
        self.database = AppDatabase(database_path, encryption_key)
        self.addCleanup(self.database.close)
        restart_transport = FakeTransport([])
        restart_ebay = EbaySandboxClient(
            self.config,
            self.database,
            transport=restart_transport,
        )
        self.app = WebApplication(self.config, self.database, restart_ebay)
        restarted_cookie, _restarted_csrf, _dashboard = self.login()
        status, _headers, restarted_body = self.request(
            "GET", headers["Location"], cookie=restarted_cookie
        )
        self.assertEqual(status, 200)
        self.assertIn("Unpublished Sandbox offer created and verified", restarted_body)
        self.assertEqual(restart_transport.calls, [])


class DraftFailureOutcomeTests(DraftDatabaseTestCase):
    session_id = "failure-browser-session"

    def setUp(self):
        super().setUp()
        self.grant()

    def run_scenario(self, responses):
        transport = FakeTransport(responses)
        client = EbaySandboxClient(self.config, self.database, transport=transport)
        adopt_test_access_token(client)
        service = DraftService(client, self.database, self.config)
        review = service.prepare_review(service_form(), self.session_id)
        action_id = service.confirm(review.action_token, self.session_id)
        self.assertIsNotNone(action_id)
        action = self.database.get_action(action_id, self.session_id)
        events = self.database.action_events(action_id, self.session_id)
        return service, review, action_id, action, events, transport

    def test_definitive_offer_rejection_is_partial_and_remote_details_are_discarded(self):
        service, review, action_id, action, events, transport = self.run_scenario(
            [
                not_found_response(),
                not_found_response(),
                HttpResponse(204, {}, b""),
                json_response(200, inventory_item(aspects={"Brand": ["Example"]})),
                json_response(200, {"offers": [], "total": 0}),
                json_response(
                    400,
                    {
                        "errors": [
                            {
                                "errorId": "PRIVATE-EBAY-ERROR-ID",
                                "message": "PRIVATE-EBAY-ERROR-MESSAGE",
                                "longMessage": "PRIVATE-REMOTE-RESPONSE",
                            }
                        ]
                    },
                ),
            ]
        )

        self.assertEqual(action["status"], "partial")
        self.assertEqual(
            action["result"],
            {
                "failure_stage": "offer_request_started",
                "inventory_created": True,
                "inventory_verified": True,
                "offer_created": False,
                "reconciliation_required": True,
                "safe_code": "EBAY_API_ERROR",
                "sku": GENERATED_SKU,
            },
        )
        self.assertEqual(
            [event["event_type"] for event in events],
            [
                "confirmed",
                "inventory_request_started",
                "inventory_created",
                "inventory_verified",
                "offer_request_started",
                "offer_request_failed",
                "terminal_result",
            ],
        )
        self.assertEqual(events[-1]["terminal_status"], "partial")
        rendered = draft_action_page(action, events, "csrf")
        self.assertIn("Do not retry this SKU yet", rendered)
        for private_value in ERROR_CANARIES:
            self.assertNotIn(private_value, repr(action))
            self.assertNotIn(private_value, repr(events))
            self.assertNotIn(private_value, rendered)
        self.assertEqual(
            [call["method"] for call in transport.calls],
            ["GET", "GET", "PUT", "GET", "GET", "POST"],
        )
        self.assertIsNone(service.confirm(review.action_token, self.session_id))
        self.assertEqual(len(transport.calls), 6)
        self.assertIsNotNone(action_id)

    def test_ambiguous_inventory_write_is_unknown_and_is_never_retried(self):
        service, review, _action_id, action, events, transport = self.run_scenario(
            [
                not_found_response(),
                not_found_response(),
                EbayError(
                    "PRIVATE-EBAY-ERROR-MESSAGE",
                    code="EBAY_CONNECTION_ERROR",
                    details={"body": "PRIVATE-REMOTE-RESPONSE"},
                ),
            ]
        )

        self.assertEqual(action["status"], "unknown")
        self.assertEqual(
            action["result"],
            {
                "failure_stage": "inventory_request_started",
                "inventory_created": False,
                "offer_created": False,
                "reconciliation_required": True,
                "safe_code": "EBAY_CONNECTION_ERROR",
                "sku": GENERATED_SKU,
            },
        )
        self.assertEqual(
            [event["event_type"] for event in events],
            [
                "confirmed",
                "inventory_request_started",
                "reconciliation_required",
                "terminal_result",
            ],
        )
        self.assertEqual([call["method"] for call in transport.calls], ["GET", "GET", "PUT"])
        rendered = draft_action_page(action, events, "csrf")
        self.assertIn("result is uncertain", rendered)
        self.assertNotIn("PRIVATE-EBAY-ERROR-MESSAGE", rendered)
        self.assertNotIn("PRIVATE-REMOTE-RESPONSE", rendered)
        self.assertIsNone(service.confirm(review.action_token, self.session_id))
        self.assertEqual(len(transport.calls), 3)

    def test_inventory_readback_mismatch_is_unknown_and_stops_before_offer(self):
        _service, _review, _action_id, action, events, transport = self.run_scenario(
            [
                not_found_response(),
                not_found_response(),
                HttpResponse(204, {}, b""),
                json_response(
                    200,
                    inventory_item(
                        title="eBay returned a different title",
                        aspects={"Brand": ["Example"]},
                    ),
                ),
            ]
        )

        self.assertEqual(action["status"], "unknown")
        self.assertEqual(action["checkpoint"], "inventory_created")
        self.assertEqual(action["result"]["safe_code"], "DRAFT_RECONCILIATION_REQUIRED")
        self.assertTrue(action["result"]["inventory_created"])
        self.assertNotIn("inventory_verified", action["result"])
        self.assertFalse(action["result"]["offer_created"])
        self.assertTrue(action["result"]["reconciliation_required"])
        self.assertEqual(
            [event["event_type"] for event in events],
            [
                "confirmed",
                "inventory_request_started",
                "inventory_created",
                "reconciliation_required",
                "terminal_result",
            ],
        )
        self.assertEqual(
            [call["method"] for call in transport.calls],
            ["GET", "GET", "PUT", "GET"],
        )
        self.assertFalse(any("/offer" in call["url"] for call in transport.calls))

    def test_published_readback_is_unknown_and_no_publish_operation_exists(self):
        offer_id = "OFFER-UNEXPECTED-LIVE"
        response = unpublished_offer(offer_id, status="PUBLISHED")
        response["listing"] = {"listingId": "PRIVATE-LIVE-LISTING-ID"}
        _service, _review, _action_id, action, events, transport = self.run_scenario(
            [
                not_found_response(),
                not_found_response(),
                HttpResponse(204, {}, b""),
                json_response(200, inventory_item(aspects={"Brand": ["Example"]})),
                json_response(200, {"offers": [], "total": 0}),
                json_response(201, {"offerId": offer_id}),
                json_response(200, response),
            ]
        )

        self.assertEqual(action["status"], "unknown")
        self.assertEqual(action["result"]["safe_code"], "DRAFT_RECONCILIATION_REQUIRED")
        self.assertTrue(action["result"]["inventory_created"])
        self.assertTrue(action["result"]["inventory_verified"])
        self.assertTrue(action["result"]["offer_created"])
        self.assertTrue(action["result"]["reconciliation_required"])
        self.assertNotIn("offer_status", action["result"])
        self.assertNotIn("verified_unpublished", action["result"])
        self.assertEqual(events[-2]["event_type"], "reconciliation_required")
        self.assertEqual(events[-1]["terminal_status"], "unknown")
        self.assertNotIn("PRIVATE-LIVE-LISTING-ID", repr(action))
        self.assertNotIn("PRIVATE-LIVE-LISTING-ID", repr(events))
        self.assertFalse(
            any("publish" in call["url"].lower() for call in transport.calls)
        )


class DraftRestartRecoveryTests(WebTestMixin, DraftDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.grant()
        self.ebay = RecordingDraftClient()
        service = DraftService(self.ebay, self.database, self.config)
        review = service.prepare_review(service_form(), "pre-restart-session")
        action = self.database.claim_pending_action(
            review.action_token,
            "pre-restart-session",
            expected_kind="create_draft",
        )
        self.action_id = action.action_id
        self.database.update_action_checkpoint(
            self.action_id,
            "inventory_request_started",
            {"sku": GENERATED_SKU},
        )

        database_path = self.config.database_path
        encryption_key = self.config.data_encryption_key
        self.database.close()
        self.database = AppDatabase(database_path, encryption_key)
        self.addCleanup(self.database.close)
        self.ebay = RecordingDraftClient()
        self.app = WebApplication(self.config, self.database, self.ebay)

    def test_interrupted_confirmation_reopens_as_unknown_for_the_new_admin_session(self):
        cookie, _csrf, dashboard = self.login()
        self.assertIn(f'/actions/{self.action_id}', dashboard)

        status, _headers, body = self.request(
            "GET",
            f"/actions/{self.action_id}",
            cookie=cookie,
        )

        self.assertEqual(status, 200)
        self.assertIn("Status:", body)
        self.assertIn("unknown", body)
        self.assertIn("result is uncertain", body)
        self.assertIn("Inventory request started", body)
        self.assertIn("Do not retry this SKU yet", body)
        self.assertNotIn(SELLER_CANARY, body)
        self.assertNotIn("Example Sandbox item", body)
        self.assertNotIn("A plain text Sandbox description", body)
        recovered = self.database.get_action(
            self.action_id,
            "pre-restart-session",
        )
        self.assertEqual(recovered["status"], "unknown")
        self.assertEqual(recovered["checkpoint"], "inventory_request_started")
        self.assertEqual(self.ebay.calls, [])


class DraftRouteAndTunnelTests(WebTestMixin, DraftDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.grant()
        self.transport = FakeTransport([])
        self.ebay = EbaySandboxClient(
            self.config,
            self.database,
            transport=self.transport,
        )
        adopt_test_access_token(self.ebay)
        self.app = WebApplication(self.config, self.database, self.ebay)

    def test_web_routes_and_client_lock_every_other_mutation(self):
        cookie, _csrf, _dashboard = self.login()
        forbidden_web_routes = (
            ("POST", "/prices/review"),
            ("POST", "/orders/12-34567-89012/fulfil"),
            ("POST", "/orders/12-34567-89012/refund"),
            ("POST", "/listings/SAFE-SKU-1/price"),
            ("POST", "/listings/SAFE-SKU-1/quantity"),
            ("POST", "/drafts/images"),
            ("POST", "/actions/0123456789abcdef0123456789abcdef"),
        )
        for method, path in forbidden_web_routes:
            with self.subTest(layer="web", method=method, path=path):
                status, _headers, body = self.request(
                    method,
                    path,
                    cookie=cookie,
                    form={},
                )
                self.assertEqual(status, 404)
                self.assertIn("locked until", body)

        forbidden_client_routes = (
            ("POST", "/sell/inventory/v1/offer/OFFER-1/publish"),
            ("DELETE", "/sell/inventory/v1/offer/OFFER-1"),
            ("DELETE", "/sell/inventory/v1/inventory_item/SAFE-SKU-1"),
            ("POST", "/sell/inventory/v1/bulk_update_price_quantity"),
            ("PUT", "/sell/inventory/v1/inventory_item"),
        )
        for method, path in forbidden_client_routes:
            with self.subTest(layer="client", method=method, path=path):
                with self.assertRaises(EbayError) as locked:
                    self.ebay._api_request(
                        method,
                        path,
                        payload={},
                        safe_read=False,
                    )
                self.assertEqual(locked.exception.code, "MUTATION_LOCKED")
        with self.assertRaises(EbayError) as legacy:
            self.ebay.create_draft({}, expected_generation="generation")
        self.assertEqual(legacy.exception.code, "MUTATION_LOCKED")
        self.assertEqual(self.transport.calls, [])

if __name__ == "__main__":
    unittest.main()
