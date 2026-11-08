from __future__ import annotations

import copy
import io
import re
import tempfile
import unittest
import urllib.parse
from dataclasses import replace
from pathlib import Path

from ebay_app.config import CONNECTION_SCOPES, LISTING_SCOPES
from ebay_app.ebay_client import EbayError, EbaySandboxClient
from ebay_app.listings import ListingService
from ebay_app.storage import AppDatabase
from ebay_app.views import active_listings_page
from ebay_app.web import WebApplication

from helpers import FakeTransport, json_response, make_config


def active_record(
    sku: str = "SKU-1",
    *,
    title: str = "Sandbox lamp",
    offer_status: str = "PUBLISHED",
    listing_status: str = "ACTIVE",
    listing_on_hold: bool = False,
) -> dict:
    """Build a realistic Inventory item/Offer join without using eBay."""

    return {
        "sku": sku,
        "inventory": {
            "sku": sku,
            "product": {
                "title": title,
                "description": "PRIVATE-DESCRIPTION-SENTINEL",
                "aspects": {"Private": ["PRIVATE-ASPECT-SENTINEL"]},
                "imageUrls": ["https://private.invalid/image.jpg"],
            },
            "condition": "NEW",
            "availability": {"PRIVATE-LOCATION-SENTINEL": "hidden"},
        },
        "offer": {
            "offerId": f"PRIVATE-OFFER-{sku}",
            "sku": sku,
            "status": offer_status,
            "marketplaceId": "EBAY_GB",
            "availableQuantity": 4,
            "listing": {
                "listingId": f"LISTING-{sku}",
                "listingStatus": listing_status,
                "listingOnHold": listing_on_hold,
            },
            "pricingSummary": {"price": {"value": "19.90", "currency": "GBP"}},
            "listingPolicies": {"PRIVATE-POLICY-SENTINEL": "hidden"},
        },
    }


class RecordingListingClient:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def read_verified_listing_records(self, binding):
        self.calls.append(binding)
        return self.records


class ListingServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(
            make_config(Path(self.temp.name)),
            feature_stage="listings",
            scopes=LISTING_SCOPES,
        )
        self.database = AppDatabase(
            self.config.database_path, self.config.data_encryption_key
        )
        self.addCleanup(self.database.close)

    def grant(self, scopes=LISTING_SCOPES, *, verified=True):
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

    def test_permission_and_verification_gates_make_no_client_or_network_call(self):
        self.grant(CONNECTION_SCOPES)
        transport = FakeTransport()
        service = ListingService(
            EbaySandboxClient(self.config, self.database, transport=transport),
            self.database,
        )

        with self.assertRaises(EbayError) as missing_scope:
            service.retrieve()
        self.assertEqual(missing_scope.exception.code, "LISTING_PERMISSION_REQUIRED")
        self.assertEqual(transport.calls, [])

        self.database.save_refresh_token(
            "scoped-but-unverified",
            9_999_999_999,
            "internal-sandbox-user",
            new_grant=True,
            granted_scopes=LISTING_SCOPES,
        )
        with self.assertRaises(EbayError) as unverified:
            service.retrieve()
        self.assertEqual(unverified.exception.code, "CONNECTION_NOT_VERIFIED")
        self.assertEqual(transport.calls, [])

    def test_returns_exact_public_dto_and_escapes_title_in_the_view(self):
        self.grant()
        record = active_record(title="<script>alert(1)</script> Lamp")
        client = RecordingListingClient([record])

        summary = ListingService(
            client, self.database, now=lambda: 1_700_000_000
        ).retrieve()

        self.assertEqual(
            summary.listing_dicts(),
            (
                {
                    "title": "<script>alert(1)</script> Lamp",
                    "sku": "SKU-1",
                    "listing_id": "LISTING-SKU-1",
                    "listing_state": "Active",
                    "marketplace": "EBAY_GB",
                    "price_value": "19.90",
                    "currency": "GBP",
                    "available_quantity": 4,
                },
            ),
        )
        self.assertEqual(summary.environment, "Sandbox")
        self.assertEqual(summary.retrieved_at, "2023-11-14 22:13:20 UTC")
        rendered_dto = repr(summary.listing_dicts())
        for private_value in (
            "PRIVATE-DESCRIPTION-SENTINEL",
            "PRIVATE-ASPECT-SENTINEL",
            "PRIVATE-LOCATION-SENTINEL",
            "PRIVATE-POLICY-SENTINEL",
            "PRIVATE-OFFER-SKU-1",
            "https://private.invalid/image.jpg",
        ):
            self.assertNotIn(private_value, rendered_dto)

        html = active_listings_page(
            summary.listing_dicts(),
            summary.environment,
            summary.retrieved_at,
            "csrf",
        )
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; Lamp", html)
        self.assertNotIn("PRIVATE-DESCRIPTION-SENTINEL", html)
        self.assertNotIn("/prices/review", html)
        self.assertNotIn("new_price", html)

    def test_only_published_active_out_of_stock_and_on_hold_rows_are_returned(self):
        self.grant()
        records = [
            active_record("ACTIVE", title="A", listing_status="ACTIVE"),
            active_record(
                "OUT", title="B", listing_status="OUT_OF_STOCK"
            ),
            active_record(
                "HOLD",
                title="C",
                listing_status="ACTIVE",
                listing_on_hold=True,
            ),
            active_record("DRAFT", offer_status="UNPUBLISHED"),
        ]
        records.extend(
            active_record(status, listing_status=status)
            for status in (
                "INACTIVE",
                "ENDED",
                "EBAY_ENDED",
                "NOT_LISTED",
            )
        )

        summary = ListingService(
            RecordingListingClient(records), self.database
        ).retrieve()

        self.assertEqual(
            [(item.sku, item.listing_state) for item in summary.listings],
            [
                ("ACTIVE", "Active"),
                ("OUT", "Out of stock"),
                ("HOLD", "On hold"),
            ],
        )

    def test_malformed_fields_on_an_active_row_fail_closed(self):
        self.grant()
        mutations = {
            "sku": lambda row: row.__setitem__("sku", "bad\nSKU"),
            "title": lambda row: row["inventory"]["product"].__setitem__(
                "title", "bad\x00title"
            ),
            "listing_id": lambda row: row["offer"]["listing"].__setitem__(
                "listingId", "<invalid>"
            ),
            "hold": lambda row: row["offer"]["listing"].__setitem__(
                "listingOnHold", "yes"
            ),
            "marketplace": lambda row: row["offer"].__setitem__(
                "marketplaceId", "<script>"
            ),
            "price": lambda row: row["offer"]["pricingSummary"]["price"].__setitem__(
                "value", "NaN"
            ),
            "currency": lambda row: row["offer"]["pricingSummary"]["price"].__setitem__(
                "currency", "gbp"
            ),
            "quantity": lambda row: row["offer"].__setitem__(
                "availableQuantity", True
            ),
        }
        for field, mutate in mutations.items():
            with self.subTest(field=field):
                row = copy.deepcopy(active_record())
                mutate(row)
                with self.assertRaises(EbayError) as raised:
                    ListingService(
                        RecordingListingClient([row]), self.database
                    ).retrieve()
                self.assertEqual(raised.exception.code, "INVALID_EBAY_RESPONSE")

    def test_falls_back_to_inventory_availability_when_the_offer_omits_quantity(self):
        # A migrated listing's Offer (created by eBay's own bulkMigrateListing,
        # not this app's create_offer()) has no availableQuantity field at
        # all -- confirmed live on 2026-11-07 against item 336629764656 /
        # SKU GG-336629764656. eBay's documented behaviour is that the
        # Inventory Item's own availability then governs, so the service
        # must still succeed and report that quantity, not fail closed.
        self.grant()
        row = copy.deepcopy(active_record())
        del row["offer"]["availableQuantity"]
        row["inventory"]["availability"] = {
            "shipToLocationAvailability": {"quantity": 1}
        }

        summary = ListingService(
            RecordingListingClient([row]), self.database, now=lambda: 1_700_000_000
        ).retrieve()

        self.assertEqual(summary.listing_dicts()[0]["available_quantity"], 1)

    def test_offer_quantity_takes_precedence_over_inventory_availability(self):
        self.grant()
        row = copy.deepcopy(active_record())
        row["offer"]["availableQuantity"] = 4
        row["inventory"]["availability"] = {
            "shipToLocationAvailability": {"quantity": 1}
        }

        summary = ListingService(
            RecordingListingClient([row]), self.database, now=lambda: 1_700_000_000
        ).retrieve()

        self.assertEqual(summary.listing_dicts()[0]["available_quantity"], 4)

    def test_fails_closed_when_quantity_is_missing_from_both_offer_and_inventory(self):
        self.grant()
        mutations = {
            "no_availability_key_at_all": lambda row: row["inventory"].pop(
                "availability", None
            ),
            "availability_not_a_mapping": lambda row: row["inventory"].__setitem__(
                "availability", "nope"
            ),
            "ship_to_not_a_mapping": lambda row: row["inventory"].__setitem__(
                "availability", {"shipToLocationAvailability": "nope"}
            ),
            "quantity_missing_on_ship_to": lambda row: row["inventory"].__setitem__(
                "availability", {"shipToLocationAvailability": {}}
            ),
            "quantity_is_a_bool": lambda row: row["inventory"].__setitem__(
                "availability", {"shipToLocationAvailability": {"quantity": True}}
            ),
        }
        for field, mutate in mutations.items():
            with self.subTest(field=field):
                row = copy.deepcopy(active_record())
                del row["offer"]["availableQuantity"]
                mutate(row)
                with self.assertRaises(EbayError) as raised:
                    ListingService(
                        RecordingListingClient([row]), self.database
                    ).retrieve()
                self.assertEqual(raised.exception.code, "INVALID_EBAY_RESPONSE")

    def test_verified_listing_read_uses_only_token_post_and_inventory_gets(self):
        self.grant()
        item = active_record()["inventory"]
        offer = active_record()["offer"]
        transport = FakeTransport(
            [
                json_response(200, {"access_token": "fresh-access", "expires_in": 7200}),
                json_response(200, {"inventoryItems": [item], "total": 1}),
                json_response(200, {"offers": [offer], "total": 1}),
            ]
        )
        client = EbaySandboxClient(self.config, self.database, transport=transport)

        records = client.read_verified_listing_records(
            self.database.verified_connection_binding()
        )

        self.assertEqual(len(records), 1)
        self.assertEqual([call["method"] for call in transport.calls], ["POST", "GET", "GET"])
        self.assertEqual(transport.calls[0]["url"], self.config.token_url)
        self.assertIn("/sell/inventory/v1/inventory_item", transport.calls[1]["url"])
        self.assertIn("/sell/inventory/v1/offer", transport.calls[2]["url"])
        self.assertNotIn("publish", " ".join(call["url"] for call in transport.calls))
        self.assertTrue(
            all(
                call["method"] == "GET"
                for call in transport.calls
                if "/sell/" in call["url"]
            )
        )
        refresh_form = urllib.parse.parse_qs(
            transport.calls[0]["body"].decode("utf-8")
        )
        self.assertEqual(set(refresh_form["scope"][0].split()), set(LISTING_SCOPES))


class OAuthScopeUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(
            make_config(Path(self.temp.name)),
            feature_stage="listings",
            scopes=LISTING_SCOPES,
        )
        self.database = AppDatabase(
            self.config.database_path, self.config.data_encryption_key
        )
        self.addCleanup(self.database.close)
        self.database.save_refresh_token(
            "old-refresh",
            9_999_999_999,
            "same-sandbox-seller",
            new_grant=True,
            granted_scopes=CONNECTION_SCOPES,
        )
        generation = self.database.connection_generation()
        self.database.mark_connection_verified(generation, "same-sandbox-seller")

    def start_upgrade(self, client, session_id="browser-session"):
        url = client.authorization_url(session_id)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(set(query["scope"][0].split()), set(LISTING_SCOPES))
        self.assertEqual(query["redirect_uri"], [self.config.runame])
        return query["state"][0]

    @staticmethod
    def success_responses(user_id="same-sandbox-seller"):
        return [
            json_response(
                200,
                {
                    "access_token": "new-access",
                    "expires_in": 7200,
                    "refresh_token": "new-refresh",
                    "refresh_token_expires_in": 100_000,
                },
            ),
            json_response(200, {"userId": user_id}),
        ]

    def snapshot(self):
        return {
            "token": self.database.get_refresh_token(),
            "generation": self.database.connection_generation(),
            "scopes": self.database.granted_scopes(),
            "status": self.database.connection_status(),
        }

    def test_same_seller_upgrade_records_scopes_and_requires_reverification(self):
        before = self.snapshot()
        transport = FakeTransport(self.success_responses())
        client = EbaySandboxClient(self.config, self.database, transport=transport)
        state = self.start_upgrade(client)

        result = client.complete_authorization(
            state, "authorization-code", "browser-session"
        )

        self.assertEqual(
            result,
            {"user_id": "same-sandbox-seller", "upgraded": True},
        )
        self.assertEqual(self.database.get_refresh_token(), "new-refresh")
        self.assertEqual(set(self.database.granted_scopes()), set(LISTING_SCOPES))
        self.assertNotEqual(
            self.database.connection_generation(), before["generation"]
        )
        self.assertTrue(self.database.connection_status()["connected"])
        self.assertFalse(self.database.connection_status()["verified"])
        self.assertIsNone(self.database.verified_connection_binding())
        self.assertEqual([call["method"] for call in transport.calls], ["POST", "GET"])

    def test_different_seller_preserves_the_old_verified_grant(self):
        before = self.snapshot()
        transport = FakeTransport(self.success_responses("different-sandbox-seller"))
        client = EbaySandboxClient(self.config, self.database, transport=transport)
        state = self.start_upgrade(client)

        with self.assertRaises(EbayError) as raised:
            client.complete_authorization(
                state, "authorization-code", "browser-session"
            )

        self.assertEqual(raised.exception.code, "SELLER_MISMATCH")
        self.assertNotIn("different-sandbox-seller", str(raised.exception))
        self.assertEqual(self.snapshot(), before)

    def test_remote_failure_preserves_the_old_verified_grant(self):
        before = self.snapshot()
        transport = FakeTransport(
            [
                self.success_responses()[0],
                json_response(500, {"errors": [{"message": "unavailable"}]}),
            ]
        )
        client = EbaySandboxClient(self.config, self.database, transport=transport)
        state = self.start_upgrade(client)

        with self.assertRaises(EbayError) as raised:
            client.complete_authorization(
                state, "authorization-code", "browser-session"
            )

        self.assertEqual(raised.exception.code, "EBAY_IDENTITY_UNAVAILABLE")
        self.assertEqual(self.snapshot(), before)

    def test_wrong_browser_session_and_stale_generation_stop_before_network(self):
        wrong_session_transport = FakeTransport()
        wrong_session_client = EbaySandboxClient(
            self.config, self.database, transport=wrong_session_transport
        )
        state = self.start_upgrade(wrong_session_client)
        with self.assertRaises(EbayError) as wrong_session:
            wrong_session_client.complete_authorization(
                state, "authorization-code", "another-browser"
            )
        self.assertEqual(wrong_session.exception.code, "INVALID_STATE")
        self.assertEqual(wrong_session_transport.calls, [])
        self.assertEqual(self.database.get_refresh_token(), "old-refresh")

        stale_transport = FakeTransport()
        stale_client = EbaySandboxClient(
            self.config, self.database, transport=stale_transport
        )
        stale_state = self.start_upgrade(stale_client, "stale-browser")
        self.database.save_refresh_token(
            "replacement-refresh",
            9_999_999_999,
            "same-sandbox-seller",
            new_grant=True,
            granted_scopes=CONNECTION_SCOPES,
        )
        replacement_generation = self.database.connection_generation()
        with self.assertRaises(EbayError) as stale:
            stale_client.complete_authorization(
                stale_state, "authorization-code", "stale-browser"
            )
        self.assertEqual(stale.exception.code, "INVALID_STATE")
        self.assertEqual(stale_transport.calls, [])
        self.assertEqual(self.database.get_refresh_token(), "replacement-refresh")
        self.assertEqual(
            self.database.connection_generation(), replacement_generation
        )

    def test_connected_grant_with_all_scopes_cannot_be_replaced(self):
        self.database.save_refresh_token(
            "already-scoped",
            9_999_999_999,
            "same-sandbox-seller",
            new_grant=True,
            granted_scopes=LISTING_SCOPES,
        )
        transport = FakeTransport()
        client = EbaySandboxClient(self.config, self.database, transport=transport)

        with self.assertRaises(EbayError) as raised:
            client.authorization_url("browser-session")

        self.assertEqual(raised.exception.code, "ALREADY_CONNECTED")
        self.assertEqual(transport.calls, [])


class FakePhaseThreeEbay:
    def __init__(self, records):
        self.records = records
        self.listing_reads = 0
        self.authorization_calls = 0
        self.mutations = 0

    def read_verified_listing_records(self, _binding):
        self.listing_reads += 1
        return self.records

    def authorization_url(self, _session_id):
        self.authorization_calls += 1
        return "https://auth.sandbox.ebay.com/oauth2/authorize?state=test"


class CountingAccountService:
    def __init__(self):
        self.calls = 0

    def retrieve(self):
        self.calls += 1
        raise AssertionError("Account data must not be read through the OAuth tunnel")


class ListingsStageWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(
            make_config(Path(self.temp.name)),
            feature_stage="listings",
            scopes=LISTING_SCOPES,
            runtime_mode="local",
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
            granted_scopes=LISTING_SCOPES,
        )
        generation = self.database.connection_generation()
        self.database.mark_connection_verified(generation, "internal-user-id")
        self.ebay = FakePhaseThreeEbay(
            [active_record(title="<script>alert(1)</script> Visible title")]
        )
        self.app = WebApplication(
            self.config,
            self.database,
            self.ebay,
            listing_service=ListingService(self.ebay, self.database),
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
        match = re.search(rf'name="{re.escape(name)}" value="([^"]+)"', body)
        if not match:
            raise AssertionError(f"Missing hidden field {name}")
        return match.group(1)

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

    def test_active_listing_page_is_escaped_read_only_and_privacy_allowlisted(self):
        cookie = self.login()
        status, _headers, dashboard = self.request("GET", "/", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Phase 3 boundary", dashboard)
        self.assertIn("View active listings", dashboard)

        status, _headers, body = self.request("GET", "/listings", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(self.ebay.listing_reads, 1)
        self.assertIn("Active Sandbox listings", body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; Visible title", body)
        self.assertNotIn("<script>alert(1)</script>", body)
        for private_value in (
            "PRIVATE-DESCRIPTION-SENTINEL",
            "PRIVATE-ASPECT-SENTINEL",
            "PRIVATE-LOCATION-SENTINEL",
            "PRIVATE-POLICY-SENTINEL",
            "PRIVATE-OFFER-SKU-1",
        ):
            self.assertNotIn(private_value, body)
        self.assertNotIn("/prices/review", body)
        self.assertNotIn("/drafts/", body)
        self.assertNotIn("/actions/confirm", body)
        self.assertNotIn("new_price", body)

    def test_orders_drafts_prices_and_all_action_routes_remain_locked(self):
        cookie = self.login()
        routes = (
            ("GET", "/orders"),
            ("GET", "/drafts/new"),
            ("POST", "/drafts/review"),
            ("POST", "/prices/review"),
            ("POST", "/actions/confirm"),
            ("POST", "/actions/cancel"),
            ("GET", "/actions/0123456789abcdef0123456789abcdef"),
        )
        for method, path in routes:
            with self.subTest(method=method, path=path):
                status, _headers, body = self.request(
                    method, path, cookie=cookie, form={}
                )
                self.assertEqual(status, 404)
                self.assertIn("locked until", body)
        self.assertEqual(self.ebay.mutations, 0)
        self.assertEqual(self.ebay.listing_reads, 0)

    def test_missing_scope_offers_controlled_upgrade_without_starting_it(self):
        self.database.save_refresh_token(
            "identity-only-refresh",
            9_999_999_999,
            "internal-user-id",
            new_grant=True,
            granted_scopes=CONNECTION_SCOPES,
        )
        generation = self.database.connection_generation()
        self.database.mark_connection_verified(generation, "internal-user-id")
        cookie = self.login()

        status, _headers, body = self.request("GET", "/", cookie=cookie)

        self.assertEqual(status, 200)
        self.assertIn("One additional permission is required", body)
        self.assertIn("Authorize read-only listings", body)
        self.assertIn('action="/connect"', body)
        self.assertNotIn("View active listings", body)
        self.assertEqual(self.ebay.authorization_calls, 0)


if __name__ == "__main__":
    unittest.main()
