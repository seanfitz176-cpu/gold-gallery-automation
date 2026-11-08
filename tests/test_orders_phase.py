from __future__ import annotations

import copy
import io
import tempfile
import unittest
import urllib.parse
from dataclasses import replace
from pathlib import Path

from ebay_app.config import LISTING_SCOPES, ORDER_SCOPES
from ebay_app.ebay_client import EbayError, EbaySandboxClient
from ebay_app.orders import OrderService
from ebay_app.storage import AppDatabase
from ebay_app.views import recent_orders_page
from ebay_app.web import WebApplication

from helpers import FakeTransport, json_response, make_config


PRIVATE_VALUES = (
    "PRIVATE-BUYER-USERNAME",
    "PRIVATE-BUYER-NAME",
    "private-buyer@example.invalid",
    "+44-PRIVATE-PHONE",
    "PRIVATE-STREET-ADDRESS",
    "PRIVATE-CHECKOUT-NOTE",
    "PRIVATE-ITEM-TITLE",
    "PRIVATE-SKU",
    "PRIVATE-TRACKING-NUMBER",
    "PRIVATE-TAX-ID",
    "PRIVATE-REFUND-ID",
)


def raw_order(
    order_id: str = "12-34567-89012",
    *,
    creation_date: str = "2026-07-13T12:34:56.000Z",
) -> dict:
    """Build a realistic response containing PII canaries that must be discarded."""

    return {
        "orderId": order_id,
        "creationDate": creation_date,
        "orderPaymentStatus": "PAID",
        "orderFulfillmentStatus": "NOT_STARTED",
        "cancelStatus": {
            "cancelState": "NONE_REQUESTED",
            "cancelRequests": [{"cancelRequestId": "PRIVATE-CANCEL-ID"}],
        },
        "pricingSummary": {
            "total": {"value": "29.95", "currency": "GBP"},
            "deliveryCost": {"value": "PRIVATE-PAYMENT-DETAIL", "currency": "GBP"},
        },
        "lineItems": [
            {
                "title": "PRIVATE-ITEM-TITLE",
                "sku": "PRIVATE-SKU",
                "quantity": 1,
                "lineItemId": "PRIVATE-LINE-ITEM-ID",
                "taxes": [{"taxIdentifier": "PRIVATE-TAX-ID"}],
            },
            {"title": "Second private item", "quantity": 2},
        ],
        "buyer": {
            "username": "PRIVATE-BUYER-USERNAME",
            "buyerRegistrationAddress": {
                "fullName": "PRIVATE-BUYER-NAME",
                "email": "private-buyer@example.invalid",
                "primaryPhone": {"phoneNumber": "+44-PRIVATE-PHONE"},
                "contactAddress": {"addressLine1": "PRIVATE-STREET-ADDRESS"},
            },
        },
        "buyerCheckoutNotes": "PRIVATE-CHECKOUT-NOTE",
        "fulfillmentStartInstructions": [
            {
                "shippingStep": {
                    "shipTo": {"contactAddress": {"addressLine1": "PRIVATE-STREET-ADDRESS"}}
                }
            }
        ],
        "fulfillmentHrefs": ["PRIVATE-TRACKING-NUMBER"],
        "paymentSummary": {"refunds": [{"refundId": "PRIVATE-REFUND-ID"}]},
    }


def minimized_order(
    order_id: str = "12-34567-89012",
    *,
    creation_date: str = "2026-07-13T12:34:56.000Z",
) -> dict:
    record = EbaySandboxClient._minimize_order(
        raw_order(order_id, creation_date=creation_date)
    )
    # A fake client could add unrelated data; the service still copies only its
    # exact allowlist into the public DTO.
    record["discarded_private_test_data"] = PRIVATE_VALUES
    return record


class RecordingOrderClient:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def read_verified_order_records(self, binding):
        self.calls.append(binding)
        return self.records


class OrderServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(
            make_config(Path(self.temp.name)),
            feature_stage="orders",
            scopes=ORDER_SCOPES,
            order_lookback_days=30,
        )
        self.database = AppDatabase(
            self.config.database_path, self.config.data_encryption_key
        )
        self.addCleanup(self.database.close)

    def grant(self, scopes=ORDER_SCOPES, *, verified=True):
        user_id = "internal-sandbox-user"
        self.database.save_refresh_token(
            "stored-refresh",
            9_999_999_999,
            user_id,
            new_grant=True,
            granted_scopes=scopes,
        )
        if verified:
            generation = self.database.connection_generation()
            self.database.mark_connection_verified(generation, user_id)

    def test_permission_and_verification_gates_make_no_client_call(self):
        client = RecordingOrderClient([])
        self.grant(LISTING_SCOPES)
        service = OrderService(client, self.database, 30)

        with self.assertRaises(EbayError) as missing_scope:
            service.retrieve()
        self.assertEqual(missing_scope.exception.code, "ORDER_PERMISSION_REQUIRED")
        self.assertEqual(client.calls, [])

        self.database.save_refresh_token(
            "scoped-but-unverified",
            9_999_999_999,
            "internal-sandbox-user",
            new_grant=True,
            granted_scopes=ORDER_SCOPES,
        )
        with self.assertRaises(EbayError) as unverified:
            service.retrieve()
        self.assertEqual(unverified.exception.code, "CONNECTION_NOT_VERIFIED")
        self.assertEqual(client.calls, [])

    def test_exact_public_dto_discards_pii_and_view_has_no_controls(self):
        self.grant()
        client = RecordingOrderClient([minimized_order()])

        summary = OrderService(
            client,
            self.database,
            30,
            now=lambda: 1_700_000_000,
        ).retrieve()

        self.assertEqual(
            summary.order_dicts(),
            (
                {
                    "order_id": "12-34567-89012",
                    "created_at": "2026-07-13 12:34:56 UTC",
                    "payment_status": "Paid",
                    "fulfillment_status": "Not started",
                    "cancellation_state": "None requested",
                    "total_value": "29.95",
                    "currency": "GBP",
                    "line_item_count": 2,
                },
            ),
        )
        rendered_dto = repr(summary.order_dicts())
        html = recent_orders_page(
            summary.order_dicts(),
            summary.environment,
            summary.retrieved_at,
            summary.lookback_days,
            "csrf",
        )
        for private_value in PRIVATE_VALUES:
            self.assertNotIn(private_value, rendered_dto)
            self.assertNotIn(private_value, html)
        for forbidden in (
            "/actions/confirm",
            "/prices/review",
            "/drafts/",
            "issueRefund",
            "createShippingFulfillment",
        ):
            self.assertNotIn(forbidden, html)

    def test_newest_orders_are_sorted_first(self):
        self.grant()
        records = [
            minimized_order("OLD!1", creation_date="2026-07-10T00:00:00Z"),
            minimized_order("NEW!2", creation_date="2026-07-13T00:00:00+00:00"),
        ]

        summary = OrderService(
            RecordingOrderClient(records), self.database, 30
        ).retrieve()

        self.assertEqual(
            [order.order_id for order in summary.orders],
            ["NEW!2", "OLD!1"],
        )

    def test_malformed_public_fields_fail_closed(self):
        self.grant()
        mutations = {
            "order_id": lambda row: row.__setitem__("order_id", "bad/order"),
            "creation_date": lambda row: row.__setitem__("creation_date", "not-a-date"),
            "payment": lambda row: row.__setitem__("payment_status", "NEW_ENUM"),
            "fulfilment": lambda row: row.__setitem__("fulfillment_status", "NEW_ENUM"),
            "cancel": lambda row: row.__setitem__("cancellation_state", "NEW_ENUM"),
            "total": lambda row: row.__setitem__("total_value", "NaN"),
            "currency": lambda row: row.__setitem__("currency", "gbp"),
            "line_items": lambda row: row.__setitem__("line_item_count", True),
        }
        for field, mutate in mutations.items():
            with self.subTest(field=field):
                row = copy.deepcopy(minimized_order())
                mutate(row)
                with self.assertRaises(EbayError) as raised:
                    OrderService(
                        RecordingOrderClient([row]), self.database, 30
                    ).retrieve()
                self.assertEqual(raised.exception.code, "INVALID_EBAY_RESPONSE")


class OrderClientContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(
            make_config(Path(self.temp.name)),
            feature_stage="orders",
            scopes=ORDER_SCOPES,
            order_lookback_days=30,
        )
        self.database = AppDatabase(
            self.config.database_path, self.config.data_encryption_key
        )
        self.addCleanup(self.database.close)
        self.database.save_refresh_token(
            "order-refresh",
            9_999_999_999,
            "same-seller",
            new_grant=True,
            granted_scopes=ORDER_SCOPES,
        )
        generation = self.database.connection_generation()
        self.database.mark_connection_verified(generation, "same-seller")

    def test_verified_order_read_refreshes_exact_scopes_and_uses_row_offsets(self):
        transport = FakeTransport(
            [
                json_response(200, {"access_token": "access", "expires_in": 7200}),
                json_response(
                    200,
                    {
                        "orders": [raw_order("ORDER!1"), raw_order("ORDER!2")],
                        "total": 3,
                        "next": "https://malicious.invalid/do-not-follow",
                    },
                ),
                json_response(
                    200,
                    {"orders": [raw_order("ORDER!3")], "total": 3},
                ),
            ]
        )
        client = EbaySandboxClient(
            self.config,
            self.database,
            transport=transport,
            now=lambda: 1_752_499_200,
        )
        binding = self.database.verified_connection_binding()

        records = client.read_verified_order_records(binding)

        self.assertEqual(
            [row["order_id"] for row in records],
            ["ORDER!1", "ORDER!2", "ORDER!3"],
        )
        serialized = repr(records)
        for private_value in PRIVATE_VALUES:
            self.assertNotIn(private_value, serialized)
        self.assertEqual([call["method"] for call in transport.calls], ["POST", "GET", "GET"])
        refresh_form = urllib.parse.parse_qs(transport.calls[0]["body"].decode("utf-8"))
        self.assertEqual(
            set(refresh_form["scope"][0].split()),
            set(ORDER_SCOPES),
        )
        order_calls = transport.calls[1:]
        for call in order_calls:
            parsed = urllib.parse.urlsplit(call["url"])
            self.assertEqual(parsed.scheme, "https")
            self.assertEqual(parsed.netloc, "api.sandbox.ebay.com")
            self.assertEqual(parsed.path, "/sell/fulfillment/v1/order")
            query = urllib.parse.parse_qs(parsed.query)
            self.assertEqual(query["limit"], ["200"])
            self.assertEqual(query["fieldGroups"], ["TAX_BREAKDOWN"])
            self.assertIn("creationdate:[", query["filter"][0])
            self.assertTrue(query["filter"][0].endswith("..]"))
            self.assertNotIn("lastmodifieddate", query["filter"][0])
        offsets = [
            urllib.parse.parse_qs(urllib.parse.urlsplit(call["url"]).query)["offset"][0]
            for call in order_calls
        ]
        self.assertEqual(offsets, ["0", "2"])
        self.assertTrue(
            all("malicious.invalid" not in call["url"] for call in transport.calls)
        )

    def test_duplicate_orders_and_inconsistent_totals_fail_closed(self):
        for responses in (
            [
                json_response(
                    200,
                    {"orders": [raw_order("DUP!1")], "total": 2, "next": "next"},
                ),
                json_response(200, {"orders": [raw_order("DUP!1")], "total": 2}),
            ],
            [
                json_response(
                    200,
                    {"orders": [raw_order("A!1")], "total": 2, "next": "next"},
                ),
                json_response(200, {"orders": [raw_order("B!2")], "total": 3}),
            ],
        ):
            with self.subTest(responses=responses):
                client = EbaySandboxClient(
                    self.config,
                    self.database,
                    transport=FakeTransport(responses),
                )
                client._access_token = "access"
                client._access_token_expires_at = client.now() + 3600
                with self.assertRaises(EbayError) as raised:
                    client.list_orders()
                self.assertEqual(raised.exception.code, "INVALID_EBAY_RESPONSE")

    def test_mismatched_response_offset_fails_closed(self):
        client = EbaySandboxClient(
            self.config,
            self.database,
            transport=FakeTransport(
                [
                    json_response(
                        200,
                        {
                            "orders": [raw_order("OFFSET!1")],
                            "total": 1,
                            "offset": 1,
                        },
                    )
                ]
            ),
        )
        client._access_token = "access"
        client._access_token_expires_at = client.now() + 3600

        with self.assertRaises(EbayError) as raised:
            client.list_orders()

        self.assertEqual(raised.exception.code, "INVALID_EBAY_RESPONSE")

    def test_invalid_refresh_rotation_preserves_old_verified_grant(self):
        invalid_rotations = (
            {"refresh_token": ""},
            {"refresh_token": "x" * 16_385},
            {"refresh_token": "rotated", "refresh_token_expires_in": True},
            {"refresh_token": "rotated", "refresh_token_expires_in": -1},
            {"refresh_token": "rotated", "refresh_token_expires_in": float("nan")},
            {"refresh_token": "rotated", "refresh_token_expires_in": float("inf")},
        )
        for rotation in invalid_rotations:
            with self.subTest(rotation=repr(rotation)[:80]):
                payload = {"access_token": "access", "expires_in": 7200, **rotation}
                client = EbaySandboxClient(
                    self.config,
                    self.database,
                    transport=FakeTransport([json_response(200, payload)]),
                )

                with self.assertRaises(EbayError) as raised:
                    client.get_access_token()

                self.assertEqual(raised.exception.code, "INVALID_TOKEN_RESPONSE")
                self.assertEqual(self.database.get_refresh_token(), "order-refresh")
                self.assertTrue(self.database.connection_status()["verified"])


class PhaseFourOAuthUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(
            make_config(Path(self.temp.name)),
            feature_stage="orders",
            scopes=ORDER_SCOPES,
        )
        self.database = AppDatabase(
            self.config.database_path, self.config.data_encryption_key
        )
        self.addCleanup(self.database.close)
        self.database.save_refresh_token(
            "phase-three-refresh",
            9_999_999_999,
            "same-seller",
            new_grant=True,
            granted_scopes=LISTING_SCOPES,
        )
        generation = self.database.connection_generation()
        self.database.mark_connection_verified(generation, "same-seller")

    def test_same_seller_upgrade_requests_all_scopes_and_requires_reverification(self):
        transport = FakeTransport(
            [
                json_response(
                    200,
                    {
                        "access_token": "new-access",
                        "expires_in": 7200,
                        "refresh_token": "phase-four-refresh",
                        "refresh_token_expires_in": 100_000,
                    },
                ),
                json_response(200, {"userId": "same-seller"}),
            ]
        )
        client = EbaySandboxClient(self.config, self.database, transport=transport)

        authorization_url = client.authorization_url("browser-session")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(authorization_url).query)
        self.assertEqual(query["scope"], [" ".join(ORDER_SCOPES)])
        result = client.complete_authorization(
            query["state"][0], "authorization-code", "browser-session"
        )

        self.assertTrue(result["upgraded"])
        self.assertEqual(set(self.database.granted_scopes()), set(ORDER_SCOPES))
        self.assertEqual(self.database.get_refresh_token(), "phase-four-refresh")
        self.assertFalse(self.database.connection_status()["verified"])
        self.assertEqual(self.database.connection_status()["user_id"], "same-seller")

    def test_different_seller_preserves_phase_three_grant(self):
        transport = FakeTransport(
            [
                json_response(
                    200,
                    {
                        "access_token": "other-access",
                        "expires_in": 7200,
                        "refresh_token": "other-refresh",
                        "refresh_token_expires_in": 100_000,
                    },
                ),
                json_response(200, {"userId": "different-seller"}),
            ]
        )
        client = EbaySandboxClient(self.config, self.database, transport=transport)
        authorization_url = client.authorization_url("browser-session")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(authorization_url).query)

        with self.assertRaises(EbayError) as raised:
            client.complete_authorization(
                query["state"][0], "authorization-code", "browser-session"
            )

        self.assertEqual(raised.exception.code, "SELLER_MISMATCH")
        self.assertEqual(self.database.get_refresh_token(), "phase-three-refresh")
        self.assertEqual(set(self.database.granted_scopes()), set(LISTING_SCOPES))
        self.assertTrue(self.database.connection_status()["verified"])


class FakePhaseFourEbay:
    def __init__(self, orders):
        self.orders = orders
        self.order_reads = 0
        self.listing_reads = 0
        self.authorization_calls = 0
        self.mutations = 0

    def read_verified_order_records(self, _binding):
        self.order_reads += 1
        return self.orders

    def read_verified_listing_records(self, _binding):
        self.listing_reads += 1
        return []

    def authorization_url(self, _session_id):
        self.authorization_calls += 1
        return "https://auth.sandbox.ebay.com/oauth2/authorize?state=test"


class MustNotReadService:
    def __init__(self):
        self.calls = 0

    def retrieve(self):
        self.calls += 1
        raise AssertionError("Private seller data must not be read through the OAuth tunnel")


class OrdersStageWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(
            make_config(Path(self.temp.name)),
            feature_stage="orders",
            scopes=ORDER_SCOPES,
            runtime_mode="local",
            order_lookback_days=30,
        )
        self.database = AppDatabase(
            self.config.database_path, self.config.data_encryption_key
        )
        self.addCleanup(self.database.close)
        self.database.save_refresh_token(
            "scoped-refresh",
            9_999_999_999,
            "internal-user-id",
            new_grant=True,
            granted_scopes=ORDER_SCOPES,
        )
        generation = self.database.connection_generation()
        self.database.mark_connection_verified(generation, "internal-user-id")
        self.ebay = FakePhaseFourEbay([minimized_order()])
        self.app = WebApplication(
            self.config,
            self.database,
            self.ebay,
            order_service=OrderService(self.ebay, self.database, 30),
        )

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
            "HTTP_HOST": "127.0.0.1:5000",
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
    def hidden(body, name):
        marker = f'name="{name}" value="'
        start = body.index(marker) + len(marker)
        return body[start : body.index('"', start)]

    def login(self):
        status, headers, body = self.request("GET", "/login")
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        csrf = self.hidden(body, "csrf_token")
        status, headers, _body = self.request(
            "POST",
            "/login",
            cookie=cookie,
            form={"csrf_token": csrf, "password": self.config.admin_password},
        )
        self.assertEqual(status, 303)
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_dashboard_and_order_page_are_read_only_and_privacy_allowlisted(self):
        cookie = self.login()
        status, _headers, dashboard = self.request("GET", "/", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Phase 4 boundary", dashboard)
        self.assertIn("View recent orders", dashboard)
        self.assertIn("View active listings", dashboard)

        status, _headers, body = self.request("GET", "/orders", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(self.ebay.order_reads, 1)
        self.assertIn("Live read-only order check succeeded", body)
        self.assertIn("12-34567-89012", body)
        for private_value in PRIVATE_VALUES:
            self.assertNotIn(private_value, body)
        self.assertNotIn("/actions/confirm", body)
        self.assertNotIn("/prices/review", body)
        self.assertNotIn("/drafts/", body)

    def test_all_mutation_routes_remain_locked(self):
        cookie = self.login()
        routes = (
            ("GET", "/drafts/new"),
            ("POST", "/drafts/review"),
            ("POST", "/prices/review"),
            ("POST", "/actions/confirm"),
            ("POST", "/actions/cancel"),
            ("GET", "/actions/0123456789abcdef0123456789abcdef"),
            ("POST", "/orders/12-34567-89012/fulfil"),
            ("POST", "/orders/12-34567-89012/refund"),
        )
        for method, path in routes:
            with self.subTest(method=method, path=path):
                status, _headers, body = self.request(
                    method, path, cookie=cookie, form={}
                )
                self.assertEqual(status, 404)
                self.assertIn("locked until", body)
        self.assertEqual(self.ebay.mutations, 0)
        self.assertEqual(self.ebay.order_reads, 0)

    def test_missing_order_scope_offers_upgrade_and_preserves_earlier_links(self):
        self.database.save_refresh_token(
            "listing-only-refresh",
            9_999_999_999,
            "internal-user-id",
            new_grant=True,
            granted_scopes=LISTING_SCOPES,
        )
        generation = self.database.connection_generation()
        self.database.mark_connection_verified(generation, "internal-user-id")
        cookie = self.login()

        status, _headers, body = self.request("GET", "/", cookie=cookie)

        self.assertEqual(status, 200)
        self.assertIn("One additional permission is required", body)
        self.assertIn("Authorize read-only orders", body)
        self.assertIn("View active listings", body)
        self.assertNotIn("View recent orders", body)
        self.assertIn('action="/connect"', body)
        self.assertEqual(self.ebay.authorization_calls, 0)

    def test_unverified_earlier_grant_must_be_verified_before_scope_upgrade(self):
        self.database.save_refresh_token(
            "unverified-listing-refresh",
            9_999_999_999,
            "internal-user-id",
            new_grant=True,
            granted_scopes=LISTING_SCOPES,
        )
        cookie = self.login()

        status, _headers, body = self.request("GET", "/", cookie=cookie)

        self.assertEqual(status, 200)
        self.assertIn("Authorized, but not verified yet", body)
        self.assertIn("Verify Sandbox connection", body)
        self.assertNotIn("Authorize read-only orders", body)
        self.assertEqual(self.ebay.authorization_calls, 0)



if __name__ == "__main__":
    unittest.main()
