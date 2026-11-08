from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from dataclasses import replace

from ebay_app.ebay_client import (
    DraftPartialError,
    EbayError,
    EbaySandboxClient,
    HttpResponse,
    NotConnectedError,
    PriceChangedError,
    SkuExistsError,
)
from ebay_app.config import DRAFT_SCOPES, LISTING_SCOPES
from ebay_app.storage import AppDatabase

from helpers import FakeTransport, adopt_test_access_token, json_response, make_config


class EbayClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = make_config(root)
        self.database = AppDatabase(self.config.database_path, self.config.data_encryption_key)
        self.addCleanup(self.database.close)

    def client(self, responses):
        transport = FakeTransport(responses)
        client = EbaySandboxClient(self.config, self.database, transport=transport)
        adopt_test_access_token(client)
        return client, transport

    def generation(self):
        if not self.database.get_refresh_token():
            self.database.save_refresh_token(
                "refresh-token", 9999999999, "sandbox-user", new_grant=True
            )
        generation = self.database.connection_generation()
        self.assertIsNotNone(generation)
        return generation

    def test_inventory_and_offer_offsets_are_the_running_item_count(self):
        # One item per page here, so a running item count and a page index
        # would produce the same numbers (0, 1) -- this alone does not
        # distinguish the two. See
        # test_inventory_offset_is_the_running_item_count_not_the_page_index
        # below for a fixture that actually tells them apart.
        client, transport = self.client(
            [
                json_response(200, {"inventoryItems": [{"sku": "SKU-1"}], "next": "next"}),
                json_response(200, {"inventoryItems": [{"sku": "SKU-2"}]}),
                json_response(200, {"offers": [], "total": 0}),
                json_response(200, {"offers": [{"offerId": "offer-2", "sku": "SKU-2"}]}),
            ]
        )
        listings = client.list_listings()
        self.assertEqual(len(listings), 2)
        queries = [parse_qs(urlsplit(call["url"]).query) for call in transport.calls]
        self.assertEqual(queries[0]["offset"], ["0"])
        self.assertEqual(queries[1]["offset"], ["1"])
        self.assertEqual(queries[2]["offset"], ["0"])
        self.assertEqual(queries[3]["offset"], ["0"])

    def test_inventory_offset_is_the_running_item_count_not_the_page_index(self):
        # Regression test for a real pagination bug found in the Phase 9
        # hardening pass: offset was being sent as the loop's page index
        # (0, 1, 2, ...) instead of a running item count. That was invisible
        # in every other fixture because they all return exactly one item
        # per page, where the two happen to produce identical numbers. This
        # fixture returns two items on the first page, so the second page's
        # offset must be 2 (items skipped so far), never 1 (page index).
        client, transport = self.client(
            [
                json_response(
                    200,
                    {
                        "inventoryItems": [{"sku": "SKU-1"}, {"sku": "SKU-2"}],
                        "next": "next",
                    },
                ),
                json_response(200, {"inventoryItems": [{"sku": "SKU-3"}]}),
            ]
        )
        items = client.list_inventory_items()
        self.assertEqual([item["sku"] for item in items], ["SKU-1", "SKU-2", "SKU-3"])
        queries = [parse_qs(urlsplit(call["url"]).query) for call in transport.calls]
        self.assertEqual(queries[0]["offset"], ["0"])
        self.assertEqual(queries[1]["offset"], ["2"])

    def test_malformed_collection_members_and_empty_next_pages_fail_closed(self):
        client, _transport = self.client(
            [json_response(200, {"inventoryItems": ["not-an-object"]})]
        )
        with self.assertRaisesRegex(EbayError, "invalid inventoryItems member"):
            client.list_inventory_items()

        client, _transport = self.client(
            [json_response(200, {"inventoryItems": [], "next": "next"})]
        )
        with self.assertRaisesRegex(EbayError, "empty inventoryItems page"):
            client.list_inventory_items()

    def test_inconsistent_pagination_totals_and_metadata_fail_closed(self):
        invalid_payloads = (
            {"inventoryItems": [], "total": 1},
            {"inventoryItems": [{"sku": "SKU-1"}], "total": 2},
            {"inventoryItems": [], "total": True},
            {"inventoryItems": [], "total": 0, "next": ["not-a-link"]},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                client, _transport = self.client([json_response(200, payload)])
                with self.assertRaises(EbayError) as raised:
                    client.list_inventory_items()
                self.assertEqual(raised.exception.code, "INVALID_EBAY_RESPONSE")

    def test_listing_join_rejects_missing_sku_and_offer_sku_mismatch(self):
        client, transport = self.client(
            [json_response(200, {"inventoryItems": [{"product": {}}], "total": 1})]
        )
        with self.assertRaisesRegex(EbayError, "invalid inventory SKU"):
            client.list_listings()
        self.assertEqual(len(transport.calls), 1)

        client, _transport = self.client(
            [
                json_response(200, {"inventoryItems": [{"sku": "SKU-1"}], "total": 1}),
                json_response(
                    200,
                    {"offers": [{"offerId": "offer", "sku": "SKU-OTHER"}], "total": 1},
                ),
            ]
        )
        with self.assertRaisesRegex(EbayError, "different SKU"):
            client.list_listings()

    def test_offer_pagination_uses_running_offsets_and_never_follows_next_url(self):
        # One offer per page here -- see
        # test_offer_offset_is_the_running_item_count_not_the_page_index
        # below for a fixture that actually distinguishes a running item
        # count from a page index.
        client, transport = self.client(
            [
                json_response(
                    200,
                    {
                        "offers": [{"offerId": "one", "sku": "SKU 1"}],
                        "total": 2,
                        "next": "https://attacker.invalid/never-follow",
                    },
                ),
                json_response(
                    200,
                    {"offers": [{"offerId": "two", "sku": "SKU 1"}], "total": 2},
                ),
            ]
        )
        offers = client.list_offers("SKU 1")
        self.assertEqual([offer["offerId"] for offer in offers], ["one", "two"])
        self.assertTrue(all("attacker.invalid" not in call["url"] for call in transport.calls))
        queries = [parse_qs(urlsplit(call["url"]).query) for call in transport.calls]
        self.assertEqual([query["offset"] for query in queries], [["0"], ["1"]])
        self.assertEqual([query["sku"] for query in queries], [["SKU 1"], ["SKU 1"]])

    def test_offer_offset_is_the_running_item_count_not_the_page_index(self):
        # Regression test, matching
        # test_inventory_offset_is_the_running_item_count_not_the_page_index
        # above, for the same bug in list_offers.
        client, transport = self.client(
            [
                json_response(
                    200,
                    {
                        "offers": [
                            {"offerId": "one", "sku": "SKU 1"},
                            {"offerId": "two", "sku": "SKU 1"},
                        ],
                        "total": 3,
                        "next": "next",
                    },
                ),
                json_response(
                    200,
                    {"offers": [{"offerId": "three", "sku": "SKU 1"}], "total": 3},
                ),
            ]
        )
        offers = client.list_offers("SKU 1")
        self.assertEqual([offer["offerId"] for offer in offers], ["one", "two", "three"])
        queries = [parse_qs(urlsplit(call["url"]).query) for call in transport.calls]
        self.assertEqual(queries[0]["offset"], ["0"])
        self.assertEqual(queries[1]["offset"], ["2"])

    def test_offer_lookup_treats_sandbox_404_as_no_offers_found(self):
        # eBay Sandbox returns 404 (errorId 25713, "This Offer is not
        # available") instead of an empty collection when a SKU has no Offer
        # yet -- confirmed against a real Sandbox account. list_offers must
        # treat that as "zero offers", not a failure, or a brand-new SKU could
        # never pass the pre-existing-offer check before its first Offer is
        # created.
        client, transport = self.client(
            [
                json_response(
                    404,
                    {
                        "errors": [
                            {
                                "errorId": 25713,
                                "category": "REQUEST",
                                "message": "This Offer is not available.",
                            }
                        ]
                    },
                )
            ]
        )
        self.assertEqual(client.list_offers("BRAND-NEW-SKU"), [])
        self.assertEqual(len(transport.calls), 1)

    def test_offer_lookup_still_fails_closed_on_other_errors(self):
        client, _transport = self.client([json_response(500, {"errors": []})])
        with self.assertRaises(EbayError) as failure:
            client.list_offers("SOME-SKU")
        self.assertNotEqual(failure.exception.status, 404)

    def test_order_offset_counts_rows(self):
        client, transport = self.client(
            [
                json_response(
                    200,
                    {
                        "orders": [
                            {"orderId": "1", "lineItems": [{}]},
                            {"orderId": "2", "lineItems": [{}]},
                        ],
                        "next": "next",
                    },
                ),
                json_response(
                    200, {"orders": [{"orderId": "3", "lineItems": [{}]}]}
                ),
            ]
        )
        self.assertEqual(
            [order["order_id"] for order in client.list_orders()],
            ["1", "2", "3"],
        )
        queries = [parse_qs(urlsplit(call["url"]).query) for call in transport.calls]
        self.assertEqual(queries[0]["offset"], ["0"])
        self.assertEqual(queries[1]["offset"], ["2"])

    def test_safe_read_refreshes_once_after_401(self):
        self.database.save_refresh_token("refresh-token", 9999999999)
        client, transport = self.client(
            [
                json_response(401, {"errors": [{"message": "expired"}]}),
                json_response(200, {"access_token": "fresh", "expires_in": 7200}),
                json_response(200, {"inventoryItems": [], "total": 0}),
            ]
        )
        self.assertEqual(client.list_inventory_items(), [])
        self.assertEqual([call["method"] for call in transport.calls], ["GET", "POST", "GET"])

    def test_oauth_state_is_session_bound_and_identity_precedes_storage(self):
        transport = FakeTransport(
            [
                json_response(
                    200,
                    {
                        "access_token": "new-access",
                        "expires_in": 7200,
                        "refresh_token": "new-refresh",
                        "refresh_token_expires_in": 100000,
                    },
                ),
                json_response(200, {"userId": "sandbox-seller"}),
            ]
        )
        client = EbaySandboxClient(self.config, self.database, transport=transport)
        url = client.authorization_url("session-1")
        state = parse_qs(urlsplit(url).query)["state"][0]
        with self.assertRaises(EbayError) as wrong_session:
            client.complete_authorization(state, "code", "session-2")
        self.assertEqual(wrong_session.exception.code, "INVALID_STATE")
        self.assertIsNone(self.database.get_refresh_token())
        self.assertEqual(transport.calls, [])

        url = client.authorization_url("session-1")
        state = parse_qs(urlsplit(url).query)["state"][0]
        result = client.complete_authorization(state, "code", "session-1")
        self.assertEqual(result["user_id"], "sandbox-seller")
        self.assertEqual(self.database.get_refresh_token(), "new-refresh")
        self.assertEqual(
            self.database.connection_status()["user_id"], "sandbox-seller"
        )
        self.assertEqual([call["method"] for call in transport.calls], ["POST", "GET"])

    def test_oauth_identity_failure_does_not_store_refresh_token(self):
        transport = FakeTransport(
            [
                json_response(
                    200,
                    {
                        "access_token": "new-access",
                        "expires_in": 7200,
                        "refresh_token": "new-refresh",
                        "refresh_token_expires_in": 100000,
                    },
                ),
                json_response(500, {"errors": [{"message": "identity unavailable"}]}),
            ]
        )
        client = EbaySandboxClient(self.config, self.database, transport=transport)
        state = parse_qs(urlsplit(client.authorization_url("session")).query)["state"][0]
        with self.assertRaises(EbayError):
            client.complete_authorization(state, "code", "session")
        self.assertIsNone(self.database.get_refresh_token())

    def test_connected_seller_cannot_be_silently_replaced(self):
        self.database.save_refresh_token(
            "refresh-token", 9999999999, "sandbox-user", new_grant=True
        )
        client = EbaySandboxClient(self.config, self.database, transport=FakeTransport())
        with self.assertRaises(EbayError) as raised:
            client.authorization_url("session")
        self.assertEqual(raised.exception.code, "ALREADY_CONNECTED")

    def test_quantities_images_and_migration_stages_allow_a_scope_upgrade(self):
        """Regression test for a real bug found 2026-11-06 (fourth instance
        of the same class of bug as the stage_routes/dashboard_page gaps
        above): authorization_url()'s scope-upgrade eligibility check had
        its own separate hardcoded stage set that was never updated when
        quantities/images/migration were added -- so a seller already
        connected at a lower permission level (e.g. from an earlier
        `listings` stage) hit a hard "already connected" error instead of
        being offered the upgrade, for any of these three stages. This is
        exactly what blocked the user's first live attempt to authorize
        migration's write scope."""
        self.database.save_refresh_token(
            "refresh-token",
            9999999999,
            "sandbox-user",
            new_grant=True,
            granted_scopes=LISTING_SCOPES,
        )
        self.database.mark_connection_verified(
            self.database.connection_generation(), "sandbox-user"
        )
        for stage in ("quantities", "images", "migration"):
            with self.subTest(stage=stage):
                config = replace(self.config, feature_stage=stage, scopes=DRAFT_SCOPES)
                client = EbaySandboxClient(config, self.database, transport=FakeTransport())
                # Must not raise -- DRAFT_SCOPES is not yet granted (only
                # LISTING_SCOPES is), so this must be offered as a genuine
                # scope upgrade, not rejected as "already connected".
                client.authorization_url("session")

    def test_already_sufficient_scopes_still_reject_a_redundant_authorize(self):
        """The fix above must not loosen the existing, correct behaviour:
        if the stage's required scopes are already granted, re-authorizing
        is still rejected as a no-op, same as before."""
        self.database.save_refresh_token(
            "refresh-token",
            9999999999,
            "sandbox-user",
            new_grant=True,
            granted_scopes=DRAFT_SCOPES,
        )
        self.database.mark_connection_verified(
            self.database.connection_generation(), "sandbox-user"
        )
        config = replace(self.config, feature_stage="migration", scopes=DRAFT_SCOPES)
        client = EbaySandboxClient(config, self.database, transport=FakeTransport())
        with self.assertRaises(EbayError) as raised:
            client.authorization_url("session")
        self.assertEqual(raised.exception.code, "ALREADY_CONNECTED")

    def test_quantities_images_and_migration_scope_upgrades_complete(self):
        """Regression test for a real bug found 2026-11-06, a sixth instance
        of the same underlying class of bug: complete_authorization() has
        its OWN separate hardcoded stage set -- distinct from the one in
        authorization_url() fixed just above -- governing whether a
        returning scope-upgrade callback is accepted at all. It had the
        exact same stale tuple, missing quantities/images/migration, so
        even after the authorization_url() fix let the user reach eBay's
        real consent screen and approve it, the callback that completes the
        upgrade rejected it with "Additional permission cannot be added in
        this application stage" (INVALID_OAUTH_INTENT). This is exactly
        what happened on the user's real Production account after the
        first fix went in."""
        for stage in ("quantities", "images", "migration"):
            with self.subTest(stage=stage):
                # A fresh temp database per stage avoids any state-carryover
                # between iterations -- each is an independent "already on
                # LISTING_SCOPES, now upgrading" scenario.
                temp = tempfile.TemporaryDirectory()
                self.addCleanup(temp.cleanup)
                root = Path(temp.name)
                database = AppDatabase(root / "app.sqlite", self.config.data_encryption_key)
                self.addCleanup(database.close)
                database.save_refresh_token(
                    "refresh-token",
                    9999999999,
                    "sandbox-user",
                    new_grant=True,
                    granted_scopes=LISTING_SCOPES,
                )
                database.mark_connection_verified(
                    database.connection_generation(), "sandbox-user"
                )
                config = replace(self.config, feature_stage=stage, scopes=DRAFT_SCOPES)
                transport = FakeTransport(
                    [
                        json_response(
                            200,
                            {
                                "access_token": "new-access",
                                "expires_in": 7200,
                                "refresh_token": "upgraded-refresh",
                                "refresh_token_expires_in": 100000,
                            },
                        ),
                        json_response(200, {"userId": "sandbox-user"}),
                    ]
                )
                client = EbaySandboxClient(config, database, transport=transport)
                url = client.authorization_url("session")
                state = parse_qs(urlsplit(url).query)["state"][0]
                result = client.complete_authorization(state, "code", "session")
                self.assertEqual(result["user_id"], "sandbox-user")
                self.assertTrue(database.has_granted_scopes(DRAFT_SCOPES))

    def test_revoked_refresh_token_requires_reconnection(self):
        self.database.save_refresh_token("revoked", 9999999999)
        client, transport = self.client(
            [
                json_response(401, {"errors": [{"message": "expired"}]}),
                json_response(400, {"error": "invalid_grant", "error_description": "revoked"}),
            ]
        )
        with self.assertRaises(EbayError) as raised:
            client.list_inventory_items()
        self.assertEqual(raised.exception.code, "EBAY_REAUTH_REQUIRED")
        self.assertIsNone(self.database.get_refresh_token())

    def test_draft_creation_stops_before_replacing_an_existing_sku(self):
        client, transport = self.client([json_response(200, {"sku": "SKU-1"})])
        with self.assertRaises(SkuExistsError):
            client.create_draft(self._draft(), expected_generation=self.generation())
        self.assertEqual([call["method"] for call in transport.calls], ["GET"])

    def test_draft_is_unpublished_and_reports_partial_second_step(self):
        client, transport = self.client(
            [
                json_response(404, {}),
                HttpResponse(201, {}, b""),
                json_response(400, {"errors": [{"message": "bad offer"}]}),
            ]
        )
        with self.assertRaises(DraftPartialError) as raised:
            client.create_draft(self._draft(), expected_generation=self.generation())
        self.assertFalse(raised.exception.outcome_unknown)
        self.assertEqual([call["method"] for call in transport.calls], ["GET", "PUT", "POST"])
        self.assertTrue(all("publish" not in call["url"] for call in transport.calls))

    def test_successful_draft_stops_at_unpublished_offer(self):
        client, transport = self.client(
            [json_response(404, {}), HttpResponse(201, {}, b""), json_response(201, {"offerId": "OFFER-1"})]
        )
        result = client.create_draft(self._draft(), expected_generation=self.generation())
        self.assertEqual(result["status"], "UNPUBLISHED")
        self.assertEqual(result["offer_id"], "OFFER-1")
        self.assertTrue(all("publish" not in call["url"] for call in transport.calls))

    def test_draft_rejects_replacement_success_status_as_unknown(self):
        client, transport = self.client(
            [json_response(404, {}), HttpResponse(200, {}, b"")]
        )
        with self.assertRaises(EbayError) as raised:
            client.create_draft(
                self._draft(), expected_generation=self.generation()
            )
        self.assertEqual(raised.exception.code, "INVALID_EBAY_RESPONSE")
        self.assertTrue(raised.exception.outcome_unknown)
        self.assertEqual([call["method"] for call in transport.calls], ["GET", "PUT"])

    def test_generation_mismatch_stops_before_any_ebay_request(self):
        client, transport = self.client([])
        old_generation = self.generation()
        self.database.save_refresh_token(
            "replacement", 9999999999, "sandbox-user", new_grant=True
        )
        with self.assertRaises(EbayError) as raised:
            client.create_draft(
                self._draft(), expected_generation=old_generation
            )
        self.assertEqual(raised.exception.code, "CONNECTION_CHANGED")
        self.assertEqual(transport.calls, [])

    def test_price_change_rechecks_and_never_retries_a_mutation_401(self):
        client, transport = self.client(
            [
                json_response(
                    200,
                    {
                        "offerId": "OFFER-1",
                        "sku": "SKU-1",
                        "status": "PUBLISHED",
                        "pricingSummary": {"price": {"currency": "GBP", "value": "19.99"}},
                    },
                ),
                json_response(401, {"errors": [{"message": "expired"}]}),
            ]
        )
        with self.assertRaises(EbayError) as raised:
            client.change_price(
                offer_id="OFFER-1",
                sku="SKU-1",
                expected_price="19.99",
                currency="GBP",
                new_price="24.99",
                expected_generation=self.generation(),
            )
        self.assertEqual(raised.exception.status, 401)
        self.assertEqual([call["method"] for call in transport.calls], ["GET", "POST"])

    def test_price_change_requires_exact_per_offer_confirmation(self):
        offer = {
            "offerId": "OFFER-1",
            "sku": "SKU-1",
            "status": "PUBLISHED",
            "pricingSummary": {"price": {"currency": "GBP", "value": "19.99"}},
        }
        client, transport = self.client(
            [json_response(200, offer), json_response(200, {"responses": [{"offerId": "OTHER", "statusCode": 200}]})]
        )
        with self.assertRaises(EbayError) as raised:
            client.change_price(
                offer_id="OFFER-1",
                sku="SKU-1",
                expected_price="19.99",
                currency="GBP",
                new_price="24.99",
                expected_generation=self.generation(),
            )
        self.assertTrue(raised.exception.outcome_unknown)
        payload = json.loads(transport.calls[1]["body"].decode("utf-8"))
        self.assertEqual(payload["requests"][0]["offers"][0]["price"]["value"], "24.99")

    def test_price_change_requires_get_offer_id_to_match(self):
        client, transport = self.client(
            [
                json_response(
                    200,
                    {
                        "offerId": "OTHER",
                        "sku": "SKU-1",
                        "status": "PUBLISHED",
                        "pricingSummary": {
                            "price": {"currency": "GBP", "value": "19.99"}
                        },
                    },
                )
            ]
        )
        with self.assertRaises(EbayError) as raised:
            client.change_price(
                offer_id="OFFER-1",
                sku="SKU-1",
                expected_price="19.99",
                currency="GBP",
                new_price="24.99",
                expected_generation=self.generation(),
            )
        self.assertEqual(raised.exception.code, "OFFER_MISMATCH")
        self.assertEqual([call["method"] for call in transport.calls], ["GET"])

    def test_price_change_accepts_only_matching_sku_offer_and_status(self):
        offer = {
            "offerId": "OFFER-1",
            "sku": "SKU-1",
            "status": "PUBLISHED",
            "pricingSummary": {"price": {"currency": "GBP", "value": "19.99"}},
        }
        client, _transport = self.client(
            [
                json_response(200, offer),
                json_response(
                    200,
                    {"responses": [{"offerId": "OFFER-1", "sku": "SKU-1", "statusCode": 200}]},
                ),
            ]
        )
        result = client.change_price(
            offer_id="OFFER-1",
            sku="SKU-1",
            expected_price="19.99",
            currency="GBP",
            new_price="24.99",
            expected_generation=self.generation(),
        )
        self.assertEqual(result["price"], "24.99")

    def test_request_timeout_response_marks_a_mutation_unknown(self):
        offer = {
            "offerId": "OFFER-1",
            "sku": "SKU-1",
            "status": "PUBLISHED",
            "pricingSummary": {"price": {"currency": "GBP", "value": "19.99"}},
        }
        client, _transport = self.client(
            [json_response(200, offer), json_response(408, {"errors": [{"message": "timeout"}]})]
        )
        with self.assertRaises(EbayError) as raised:
            client.change_price(
                offer_id="OFFER-1",
                sku="SKU-1",
                expected_price="19.99",
                currency="GBP",
                new_price="24.99",
                expected_generation=self.generation(),
            )
        self.assertTrue(raised.exception.outcome_unknown)

    def test_malformed_server_error_marks_a_mutation_unknown(self):
        offer = {
            "offerId": "OFFER-1",
            "sku": "SKU-1",
            "status": "PUBLISHED",
            "pricingSummary": {"price": {"currency": "GBP", "value": "19.99"}},
        }
        client, _transport = self.client(
            [json_response(200, offer), HttpResponse(500, {}, b"<html>upstream failure</html>")]
        )
        with self.assertRaises(EbayError) as raised:
            client.change_price(
                offer_id="OFFER-1",
                sku="SKU-1",
                expected_price="19.99",
                currency="GBP",
                new_price="24.99",
                expected_generation=self.generation(),
            )
        self.assertTrue(raised.exception.outcome_unknown)

    @staticmethod
    def _draft():
        return {
            "sku": "SKU-1",
            "title": "Example",
            "description": "Description",
            "condition": "NEW",
            "quantity": 2,
            "price": "19.99",
            "currency": "GBP",
            "category_id": "12345",
            "image_urls": [],
            "aspects": {},
        }


class MigrateListingTests(EbayClientTests):
    def test_migrate_listing_sends_the_requests_envelope_with_the_listing_id(self):
        client, transport = self.client(
            [json_response(200, {"responses": [{"statusCode": 200, "listingId": "ITEM-1", "sku": "SKU-1"}]})]
        )
        result = client.migrate_listing(item_id="ITEM-1", expected_generation=self.generation())
        self.assertEqual(result["item_id"], "ITEM-1")
        self.assertEqual(result["raw_response"], {"statusCode": 200, "listingId": "ITEM-1", "sku": "SKU-1"})
        self.assertEqual(transport.calls[0]["method"], "POST")
        self.assertTrue(transport.calls[0]["url"].endswith("/sell/inventory/v1/bulk_migrate_listing"))
        payload = json.loads(transport.calls[0]["body"].decode("utf-8"))
        self.assertEqual(payload, {"requests": [{"listingId": "ITEM-1"}]})

    def test_migrate_listing_strips_whitespace_from_the_item_id(self):
        client, transport = self.client(
            [json_response(200, {"responses": [{"statusCode": 200, "listingId": "ITEM-1"}]})]
        )
        client.migrate_listing(item_id="  ITEM-1  ", expected_generation=self.generation())
        payload = json.loads(transport.calls[0]["body"].decode("utf-8"))
        self.assertEqual(payload["requests"][0]["listingId"], "ITEM-1")

    def test_migrate_listing_rejects_an_empty_item_id_without_any_call(self):
        client, transport = self.client([])
        with self.assertRaises(EbayError) as raised:
            client.migrate_listing(item_id="   ", expected_generation=self.generation())
        self.assertEqual(raised.exception.code, "MIGRATION_ITEM_ID_REQUIRED")
        self.assertEqual(transport.calls, [])

    def test_migrate_listing_requires_the_current_connection_generation(self):
        client, transport = self.client([])
        old_generation = self.generation()
        self.database.save_refresh_token(
            "refresh-token-2", 9999999999, "sandbox-user", new_grant=True
        )
        with self.assertRaises(EbayError) as raised:
            client.migrate_listing(item_id="ITEM-1", expected_generation=old_generation)
        self.assertEqual(raised.exception.code, "CONNECTION_CHANGED")
        self.assertEqual(transport.calls, [])

    def test_migrate_listing_rejects_an_unrecognized_response_shape(self):
        client, _transport = self.client([json_response(200, {"notResponses": []})])
        with self.assertRaises(EbayError) as raised:
            client.migrate_listing(item_id="ITEM-1", expected_generation=self.generation())
        self.assertEqual(raised.exception.code, "MIGRATION_RESPONSE_UNRECOGNIZED")
        self.assertTrue(raised.exception.outcome_unknown)

    def test_migrate_listing_rejects_more_or_fewer_than_one_result(self):
        client, _transport = self.client(
            [json_response(200, {"responses": [{"statusCode": 200}, {"statusCode": 200}]})]
        )
        with self.assertRaises(EbayError) as raised:
            client.migrate_listing(item_id="ITEM-1", expected_generation=self.generation())
        self.assertEqual(raised.exception.code, "MIGRATION_RESPONSE_UNRECOGNIZED")

    def test_migrate_listing_surfaces_an_eligibility_error_from_ebay(self):
        client, _transport = self.client(
            [
                json_response(
                    200,
                    {
                        "responses": [
                            {
                                "statusCode": 400,
                                "errors": [
                                    {
                                        "errorId": 25500,
                                        "category": "REQUEST",
                                        "message": "The listing is not eligible for migration.",
                                        "longMessage": "The listing is not eligible for migration.",
                                    }
                                ],
                            }
                        ]
                    },
                )
            ]
        )
        with self.assertRaises(EbayError) as raised:
            client.migrate_listing(item_id="ITEM-1", expected_generation=self.generation())
        self.assertEqual(raised.exception.code, "MIGRATION_UNCONFIRMED")
        self.assertFalse(raised.exception.outcome_unknown)
        self.assertEqual(raised.exception.details[0]["errorId"], 25500)

    def test_migrate_listing_marks_outcome_unknown_when_no_status_code_is_present(self):
        client, _transport = self.client([json_response(200, {"responses": [{"listingId": "ITEM-1"}]})])
        with self.assertRaises(EbayError) as raised:
            client.migrate_listing(item_id="ITEM-1", expected_generation=self.generation())
        self.assertEqual(raised.exception.code, "MIGRATION_UNCONFIRMED")
        self.assertTrue(raised.exception.outcome_unknown)

    def test_migrate_listing_falls_back_to_the_raw_body_when_ebay_rejects_the_whole_call(self):
        """Regression test for a real bug found 2026-11-06, discovered on
        the user's actual first live migration attempt: eBay rejected the
        whole HTTP call (not a 200 envelope with a failed per-item
        statusCode -- the scenario every other test in this class covers)
        with a body that didn't use the usual {"errors": [...]} shape
        _error_details() expects. Before this fix, `_api_request()`'s
        generic non-2xx handler called `_error_details(response_payload)`
        with no fallback, so `.details` came back as `[]` (falsy) and
        migrate_listing.py's `if details:` check silently printed nothing
        at all -- leaving the user with zero information about why their
        real listing was rejected, exactly the opposite of this script's
        whole "always show the raw response" design promise. Fixed by
        adding an `include_raw_payload_on_error` flag to `_api_request()`,
        wired to True only for the migrate_listing call site (other
        endpoints are unaffected and keep today's `[]`-on-no-match
        behaviour, which other tests rely on)."""
        client, _transport = self.client(
            [json_response(400, {"message": "Unexpected body shape with no errors key"})]
        )
        with self.assertRaises(EbayError) as raised:
            client.migrate_listing(item_id="ITEM-1", expected_generation=self.generation())
        self.assertEqual(raised.exception.code, "EBAY_API_ERROR")
        self.assertFalse(raised.exception.outcome_unknown)
        self.assertEqual(
            raised.exception.details,
            [{"message": "Unexpected body shape with no errors key"}],
        )

    def test_other_endpoints_still_get_an_empty_list_with_no_raw_fallback(self):
        """The fix above is scoped to migrate_listing only -- confirms an
        ordinary endpoint's non-2xx error with no "errors" key still gets
        the original `[]`, not a surprise raw-payload dump."""
        client, _transport = self.client(
            [json_response(400, {"message": "no errors key here either"})]
        )
        with self.assertRaises(EbayError) as raised:
            client.list_inventory_items()
        self.assertEqual(raised.exception.details, [])

    def test_migrate_listing_locked_outside_the_migration_stage(self):
        client, transport = self.client([])
        client.config = replace(client.config, feature_stage="prices")
        with self.assertRaises(EbayError) as raised:
            client.migrate_listing(item_id="ITEM-1", expected_generation=self.generation())
        self.assertEqual(raised.exception.code, "MUTATION_LOCKED")
        self.assertEqual(transport.calls, [])

    def test_migration_mutation_guard_requires_the_migration_stage(self):
        client, _transport = self.client([])
        client.config = replace(client.config, feature_stage="prices")
        with self.assertRaises(EbayError) as raised:
            with client.migration_mutation_guard():
                pass
        self.assertEqual(raised.exception.code, "MUTATION_LOCKED")

    def test_migration_mutation_guard_requires_local_runtime_mode(self):
        client, _transport = self.client([])
        client.config = replace(client.config, feature_stage="migration", runtime_mode="oauth")
        with self.assertRaises(EbayError) as raised:
            with client.migration_mutation_guard():
                pass
        self.assertEqual(raised.exception.code, "MIGRATION_LOCAL_MODE_REQUIRED")

    def test_migration_mutation_guard_allows_entry_in_the_migration_stage(self):
        client, _transport = self.client([])
        client.config = replace(client.config, feature_stage="migration")
        entered = False
        with client.migration_mutation_guard():
            entered = True
        self.assertTrue(entered)


class ProductionEnvironmentErrorMessageTests(EbayClientTests):
    """2026-11-05 full labelling correction: EbaySandboxClient already
    takes the full AppConfig, so every one of its own EbayError messages
    that used to say "Sandbox" unconditionally should now reflect the
    real configured environment. Spot-checks one representative message
    from each category of fix (direct construction, a scope-gate check
    with no network call, and a mocked eBay error response), plus a
    regression proving the Sandbox default is unchanged.
    """

    def production_client(self, responses=None):
        production_config = replace(
            self.config, environment="production", feature_stage="connection"
        )
        transport = FakeTransport(responses or [])
        client = EbaySandboxClient(production_config, self.database, transport=transport)
        adopt_test_access_token(client)
        return client, transport

    def test_not_connected_error_says_production_not_sandbox(self):
        message = str(NotConnectedError("Production"))
        self.assertEqual(message, "Connect an eBay Production seller account first")
        # Default (no argument) stays exactly as every existing caller expects.
        self.assertEqual(str(NotConnectedError()), "Connect an eBay Sandbox seller account first")

    def test_draft_permission_required_message_says_production(self):
        client, _transport = self.production_client()
        with self.assertRaises(EbayError) as raised:
            client._assert_draft_permission()
        self.assertEqual(raised.exception.code, "DRAFT_PERMISSION_REQUIRED")
        self.assertIn("unpublished Production offers", str(raised.exception))
        self.assertNotIn("Sandbox", str(raised.exception))

    def test_mutation_locked_message_says_production(self):
        client, _transport = self.production_client()
        with self.assertRaises(EbayError) as raised:
            client._assert_mutation_allowed("POST", "/sell/inventory/v1/offer")
        self.assertEqual(raised.exception.code, "MUTATION_LOCKED")
        self.assertIn("current Production stage", str(raised.exception))
        self.assertNotIn("Sandbox", str(raised.exception))

    def test_invalid_client_token_error_says_production(self):
        client, _transport = self.production_client(
            [json_response(400, {"error": "invalid_client"})]
        )
        with self.assertRaises(EbayError) as raised:
            client._token_request({"grant_type": "refresh_token"})
        self.assertEqual(raised.exception.code, "EBAY_INVALID_CLIENT")
        self.assertIn("Production App ID or Cert ID", str(raised.exception))
        self.assertNotIn("Sandbox", str(raised.exception))

    def test_get_user_rejected_error_says_production(self):
        client, _transport = self.production_client([json_response(401, {})])
        with self.assertRaises(EbayError) as raised:
            client.get_user(access_token="token")
        self.assertEqual(raised.exception.code, "EBAY_IDENTITY_UNAUTHORIZED")
        self.assertIn("Production user authorization", str(raised.exception))
        self.assertNotIn("Sandbox", str(raised.exception))

    def test_setup_permission_required_message_says_production(self):
        client, _transport = self.production_client()
        with self.assertRaises(EbayError) as raised:
            client._assert_setup_permission()
        self.assertEqual(raised.exception.code, "SETUP_PERMISSION_REQUIRED")
        self.assertIn("one-time Production setup", str(raised.exception))
        self.assertNotIn("Sandbox", str(raised.exception))

    def test_sandbox_default_messages_are_unchanged(self):
        client, _transport = self.client([])
        with self.assertRaises(EbayError) as raised:
            client._assert_draft_permission()
        self.assertIn("unpublished Sandbox offers", str(raised.exception))
        locked_config = replace(self.config, feature_stage="connection")
        locked_client = EbaySandboxClient(locked_config, self.database, transport=FakeTransport([]))
        adopt_test_access_token(locked_client)
        with self.assertRaises(EbayError) as raised:
            locked_client._assert_mutation_allowed("POST", "/sell/inventory/v1/offer")
        self.assertIn("current Sandbox stage", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
