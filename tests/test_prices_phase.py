from __future__ import annotations

import io
import re
import tempfile
import unittest
import urllib.parse
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from ebay_app.config import DRAFT_SCOPES, ORDER_SCOPES
from ebay_app.ebay_client import EbayError, EbaySandboxClient, HttpResponse
from ebay_app.prices import PRICE_ACTION_KIND, PriceService
from ebay_app.storage import AppDatabase
from ebay_app.validation import ValidationError
from ebay_app.web import WebApplication

from helpers import FakeTransport, adopt_test_access_token, json_response, make_config


SELLER_CANARY = "PRIVATE-INTERNAL-SELLER-ID"
REFRESH_CANARY = "PRIVATE-REFRESH-TOKEN"
OFFER_ID = "OFFER-1"
SKU = "SKU-1"


def price_form(**updates: str) -> dict[str, str]:
    values = {
        "csrf_token": "csrf",
        "offer_id": OFFER_ID,
        "sku": SKU,
        "new_price": "24.99",
    }
    values.update(updates)
    return values


def service_form(**updates: str) -> dict[str, list[str]]:
    return {name: [value] for name, value in price_form(**updates).items()}


def published_offer(
    offer_id: str,
    *,
    sku: str = SKU,
    status: str = "PUBLISHED",
    price_value: str = "19.99",
    price_currency: str = "GBP",
) -> dict:
    return {
        "offerId": offer_id,
        "sku": sku,
        "status": status,
        "marketplaceId": "EBAY_GB",
        "pricingSummary": {"price": {"currency": price_currency, "value": price_value}},
    }


class RecordingPriceClient:
    """Small service double; the full-stack tests below use the real eBay client."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.offer_response: dict = {}
        self.after_get_offer = None
        self.change_price_result: dict = {}
        self.change_price_error: Exception | None = None

    def get_offer(self, offer_id):
        self.calls.append(("get_offer", offer_id))
        if self.after_get_offer is not None:
            self.after_get_offer()
        return self.offer_response

    @staticmethod
    def offer_price(offer):
        price = offer.get("pricingSummary", {}).get("price", {})
        return {"value": str(price.get("value", "")), "currency": str(price.get("currency", ""))}

    @contextmanager
    def price_mutation_guard(self):
        self.calls.append(("guard_enter",))
        try:
            yield
        finally:
            self.calls.append(("guard_exit",))

    def change_price(self, **kwargs):
        self.calls.append(("change_price", dict(kwargs)))
        if self.change_price_error is not None:
            raise self.change_price_error
        return self.change_price_result


class PriceDatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(
            make_config(Path(self.temp.name)),
            feature_stage="prices",
            scopes=DRAFT_SCOPES,
            runtime_mode="local",
            # These tests exercise the pre-existing review/confirm protocol,
            # not the Section 6 safety-limit feature -- neutralize both limits
            # here so they don't need unrelated price-limit setup. The limits
            # themselves get their own dedicated test class with real defaults.
            require_price_floor=False,
            max_price_change_percent=Decimal("100"),
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


class PriceServiceGateTests(PriceDatabaseTestCase):
    def test_stage_runtime_and_scope_gates_precede_any_client_call(self):
        client = RecordingPriceClient()

        with self.assertRaises(EbayError) as wrong_stage:
            PriceService(
                client, self.database, replace(self.config, feature_stage="drafts")
            ).prepare_review(service_form(), "session")
        self.assertEqual(wrong_stage.exception.code, "PRICE_STAGE_LOCKED")

        with self.assertRaises(EbayError) as unsupported_runtime:
            PriceService(
                client, self.database, replace(self.config, runtime_mode="unsupported")
            ).prepare_review(service_form(), "session")
        self.assertEqual(unsupported_runtime.exception.code, "PRICE_LOCAL_MODE_REQUIRED")

        self.grant(ORDER_SCOPES)
        with self.assertRaises(EbayError) as missing_scope:
            PriceService(client, self.database, self.config).prepare_review(
                service_form(), "session"
            )
        self.assertEqual(missing_scope.exception.code, "PRICE_PERMISSION_REQUIRED")

        # None of the locked-out attempts above ever reached the eBay client.
        self.assertEqual(client.calls, [])

    def test_not_connected_gate_is_checked_before_any_client_call(self):
        self.grant()
        client = RecordingPriceClient()
        service = PriceService(client, self.database, self.config)
        with patch.object(self.database, "connection_generation", return_value=None):
            with self.assertRaises(EbayError) as not_connected:
                service.prepare_review(service_form(), "session")
        self.assertEqual(not_connected.exception.code, "NOT_CONNECTED")
        self.assertEqual(client.calls, [])

    def test_confirm_and_cancel_also_enforce_the_stage_gate(self):
        self.grant()
        client = RecordingPriceClient()
        service = PriceService(client, self.database, self.config)
        other_stage = replace(self.config, feature_stage="drafts")
        with self.assertRaises(EbayError) as confirm_locked:
            PriceService(client, self.database, other_stage).confirm("token", "session")
        self.assertEqual(confirm_locked.exception.code, "PRICE_STAGE_LOCKED")
        with self.assertRaises(EbayError) as cancel_locked:
            PriceService(client, self.database, other_stage).cancel("token", "session")
        self.assertEqual(cancel_locked.exception.code, "PRICE_STAGE_LOCKED")
        self.assertEqual(client.calls, [])


class PriceServiceReviewTests(PriceDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.grant()
        self.client = RecordingPriceClient()
        self.client.offer_response = published_offer(OFFER_ID)
        self.service = PriceService(self.client, self.database, self.config)

    def test_successful_review_performs_exactly_one_read_and_makes_no_change(self):
        review = self.service.prepare_review(service_form(), "browser-session")
        self.assertEqual(review.offer_id, OFFER_ID)
        self.assertEqual(review.sku, SKU)
        self.assertEqual(review.expected_price, "19.99")
        self.assertEqual(review.currency, "GBP")
        self.assertEqual(review.new_price, "24.99")
        self.assertEqual([call[0] for call in self.client.calls], ["get_offer"])
        self.assertEqual(self.database.recent_actions(), [])

    def test_offer_mismatch_stops_review(self):
        self.client.offer_response = published_offer("DIFFERENT-OFFER")
        with self.assertRaises(EbayError) as mismatch:
            self.service.prepare_review(service_form(), "browser-session")
        self.assertEqual(mismatch.exception.code, "OFFER_MISMATCH")
        self.assertEqual(self.database.recent_actions(), [])

    def test_sku_mismatch_stops_review(self):
        self.client.offer_response = published_offer(OFFER_ID, sku="OTHER-SKU")
        with self.assertRaises(EbayError) as mismatch:
            self.service.prepare_review(service_form(), "browser-session")
        self.assertEqual(mismatch.exception.code, "OFFER_SKU_MISMATCH")

    def test_unpublished_offer_stops_review(self):
        self.client.offer_response = published_offer(OFFER_ID, status="UNPUBLISHED")
        with self.assertRaises(EbayError) as not_published:
            self.service.prepare_review(service_form(), "browser-session")
        self.assertEqual(not_published.exception.code, "OFFER_NOT_PUBLISHED")

    def test_missing_current_price_stops_review(self):
        self.client.offer_response = published_offer(
            OFFER_ID, price_value="", price_currency=""
        )
        with self.assertRaises(EbayError) as missing_price:
            self.service.prepare_review(service_form(), "browser-session")
        self.assertEqual(missing_price.exception.code, "INVALID_EBAY_RESPONSE")

    def test_unchanged_price_is_a_form_validation_error_not_an_ebay_call_result(self):
        with self.assertRaises(ValidationError) as unchanged:
            self.service.prepare_review(service_form(new_price="19.99"), "browser-session")
        self.assertIn("new_price", unchanged.exception.errors)

    def test_connection_change_during_review_creates_no_pending_action(self):
        def replace_seller() -> None:
            self.grant(user_id="DIFFERENT-SELLER", refresh_token="different-seller-refresh")

        self.client.after_get_offer = replace_seller
        with self.assertRaises(EbayError) as changed:
            self.service.prepare_review(service_form(), "browser-session")
        self.assertEqual(changed.exception.code, "CONNECTION_CHANGED")
        self.assertEqual(self.database.recent_actions(), [])


class PriceServiceConfirmTests(PriceDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.grant()
        self.client = RecordingPriceClient()
        self.client.offer_response = published_offer(OFFER_ID)
        self.service = PriceService(self.client, self.database, self.config)

    def review(self):
        return self.service.prepare_review(service_form(), "browser-session")

    def test_confirm_wraps_the_write_in_the_mutation_guard_and_records_success(self):
        review = self.review()
        self.client.change_price_result = {
            "offer_id": OFFER_ID,
            "sku": SKU,
            "price": "24.99",
            "currency": "GBP",
        }
        action_id = self.service.confirm(review.action_token, "browser-session")
        self.assertIsNotNone(action_id)
        self.assertEqual(
            [call[0] for call in self.client.calls],
            ["get_offer", "guard_enter", "change_price", "guard_exit"],
        )
        action = self.database.get_action_for_admin(action_id)
        self.assertEqual(action["status"], "succeeded")
        self.assertEqual(action["result"]["price"], "24.99")

    def test_confirm_is_one_use(self):
        review = self.review()
        self.client.change_price_result = {
            "offer_id": OFFER_ID,
            "sku": SKU,
            "price": "24.99",
            "currency": "GBP",
        }
        first = self.service.confirm(review.action_token, "browser-session")
        self.assertIsNotNone(first)
        second = self.service.confirm(review.action_token, "browser-session")
        self.assertIsNone(second)
        self.assertEqual(
            [call[0] for call in self.client.calls].count("change_price"), 1
        )

    def test_definitive_ebay_error_is_recorded_failed_and_remote_details_are_discarded(self):
        review = self.review()
        self.client.change_price_error = EbayError(
            "The Offer price changed after review; review the latest price before trying again",
            status=409,
            code="PRICE_CHANGED",
            details={"leaked": "PRIVATE-REMOTE-DETAIL"},
        )
        action_id = self.service.confirm(review.action_token, "browser-session")
        action = self.database.get_action_for_admin(action_id)
        self.assertEqual(action["status"], "failed")
        self.assertEqual(
            action["result"],
            {"safe_code": "PRICE_CHANGED", "reconciliation_required": False},
        )
        self.assertNotIn("PRIVATE-REMOTE-DETAIL", str(action))

    def test_ambiguous_ebay_error_is_recorded_unknown_and_flags_reconciliation(self):
        review = self.review()
        self.client.change_price_error = EbayError(
            "eBay did not confirm the requested Offer price update",
            code="PRICE_UPDATE_UNCONFIRMED",
            details={"leaked": "PRIVATE-REMOTE-DETAIL"},
            outcome_unknown=True,
        )
        action_id = self.service.confirm(review.action_token, "browser-session")
        action = self.database.get_action_for_admin(action_id)
        self.assertEqual(action["status"], "unknown")
        self.assertEqual(
            action["result"],
            {"safe_code": "PRICE_UPDATE_UNCONFIRMED", "reconciliation_required": True},
        )
        self.assertNotIn("PRIVATE-REMOTE-DETAIL", str(action))

    def test_invalid_saved_payload_fails_closed_without_calling_ebay(self):
        generation = self.database.connection_generation()
        token = self.database.create_pending_action(
            "browser-session",
            PRICE_ACTION_KIND,
            {"version": 999},
            expected_generation=generation,
        )
        action_id = self.service.confirm(token, "browser-session")
        self.assertIsNotNone(action_id)
        action = self.database.get_action_for_admin(action_id)
        self.assertEqual(action["status"], "failed")
        self.assertIn("invalid", action["error_message"].lower())
        self.assertEqual(self.client.calls, [])

    def test_generic_failure_after_confirmation_is_recorded_unknown_and_reraised(self):
        review = self.review()
        self.client.change_price_error = RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            self.service.confirm(review.action_token, "browser-session")
        actions = self.database.recent_actions()
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["status"], "unknown")
        self.assertIn("reconcile", actions[0]["error_message"].lower())

    def test_cancel_removes_the_pending_action_without_calling_ebay(self):
        review = self.review()
        self.assertTrue(self.service.cancel(review.action_token, "browser-session"))
        self.assertIsNone(self.service.confirm(review.action_token, "browser-session"))
        self.assertEqual(
            [call[0] for call in self.client.calls], ["get_offer"]
        )  # only the read-only review call; no guard, no change_price


class PriceDryRunTests(PriceDatabaseTestCase):
    """The write-phase kill switch's other half (AppConfig.dry_run, see
    PriceService.confirm): a dry run must claim and journal the action
    exactly as normal, but must never call eBay at all."""

    def setUp(self):
        super().setUp()
        self.config = replace(self.config, dry_run=True)
        self.grant()
        self.client = RecordingPriceClient()
        self.client.offer_response = published_offer(OFFER_ID, price_value="19.99")
        self.service = PriceService(self.client, self.database, self.config)

    def review(self):
        return self.service.prepare_review(service_form(), "browser-session")

    def test_confirm_never_calls_ebay_and_records_a_dry_run_result(self):
        review = self.review()
        self.assertEqual([call[0] for call in self.client.calls], ["get_offer"])

        action_id = self.service.confirm(review.action_token, "browser-session")

        self.assertIsNotNone(action_id)
        # No guard_enter/change_price/guard_exit at all -- confirm never
        # touched the eBay client beyond the review's own read.
        self.assertEqual([call[0] for call in self.client.calls], ["get_offer"])
        action = self.database.get_action_for_admin(action_id)
        self.assertEqual(action["status"], "succeeded")
        self.assertEqual(
            action["result"],
            {
                "dry_run": True,
                "offer_id": OFFER_ID,
                "sku": SKU,
                "price": "24.99",
                "currency": "GBP",
            },
        )

    def test_dry_run_still_advances_the_actions_budget(self):
        self.config = replace(self.config, max_actions_per_run=1)
        self.service = PriceService(self.client, self.database, self.config)
        review = self.review()
        self.service.confirm(review.action_token, "browser-session")
        self.assertEqual(self.service._actions_this_run, 1)
        with self.assertRaises(EbayError) as refused:
            self.service.prepare_review(service_form(), "browser-session")
        self.assertEqual(refused.exception.code, "MAX_ACTIONS_PER_RUN_REACHED")

    def test_dry_run_confirmation_is_still_one_use(self):
        review = self.review()
        first = self.service.confirm(review.action_token, "browser-session")
        self.assertIsNotNone(first)
        second = self.service.confirm(review.action_token, "browser-session")
        self.assertIsNone(second)

    def test_dry_run_still_applies_the_section_6_safety_limits(self):
        # The kill switch is an additional, independent layer -- it must
        # never bypass the floor/cap checks that already ran at review time.
        self.config = replace(self.config, require_price_floor=True)
        self.service = PriceService(self.client, self.database, self.config)
        with self.assertRaises(EbayError) as refused:
            self.review()
        self.assertEqual(refused.exception.code, "PRICE_FLOOR_REQUIRED")


class PriceMutationGuardTests(PriceDatabaseTestCase):
    def client(self, responses=()) -> EbaySandboxClient:
        transport = FakeTransport(list(responses))
        client = EbaySandboxClient(self.config, self.database, transport=transport)
        adopt_test_access_token(client)
        return client

    def test_guard_rejects_wrong_feature_stage(self):
        config = replace(self.config, feature_stage="drafts", scopes=DRAFT_SCOPES)
        client = EbaySandboxClient(config, self.database, transport=FakeTransport([]))
        with self.assertRaises(EbayError) as failure:
            with client.price_mutation_guard():
                pass
        self.assertEqual(failure.exception.code, "MUTATION_LOCKED")

    def test_guard_rejects_oauth_authorization_runtime_mode(self):
        config = replace(self.config, runtime_mode="oauth_authorization")
        client = EbaySandboxClient(config, self.database, transport=FakeTransport([]))
        with self.assertRaises(EbayError) as failure:
            with client.price_mutation_guard():
                pass
        self.assertEqual(failure.exception.code, "PRICE_LOCAL_MODE_REQUIRED")

    def test_guard_allows_prices_stage_in_local_mode(self):
        client = self.client()
        with client.price_mutation_guard():
            pass  # no error


class PriceMutationAllowlistTests(PriceDatabaseTestCase):
    def client(self, responses=()) -> EbaySandboxClient:
        transport = FakeTransport(list(responses))
        client = EbaySandboxClient(self.config, self.database, transport=transport)
        adopt_test_access_token(client)
        return client

    def test_prices_stage_allows_the_targeted_price_update_call(self):
        client = self.client()
        client._assert_mutation_allowed(
            "POST", "/sell/inventory/v1/bulk_update_price_quantity"
        )  # must not raise

    def test_prices_stage_remains_cumulative_over_drafts_creation_calls(self):
        client = self.client()
        client._assert_mutation_allowed("POST", "/sell/inventory/v1/offer")
        client._assert_mutation_allowed(
            "PUT", "/sell/inventory/v1/inventory_item/SAFE-SKU-1"
        )

    def test_prices_stage_still_locks_every_other_mutation(self):
        client = self.client()
        forbidden = (
            ("POST", "/sell/inventory/v1/offer/OFFER-1/publish"),
            ("DELETE", "/sell/inventory/v1/offer/OFFER-1"),
            ("DELETE", "/sell/inventory/v1/inventory_item/SAFE-SKU-1"),
            ("POST", "/sell/account/v1/program/opt_in"),
            ("POST", "/sell/account/v1/fulfillment_policy"),
        )
        for method, path in forbidden:
            with self.subTest(method=method, path=path):
                with self.assertRaises(EbayError) as failure:
                    client._assert_mutation_allowed(method, path)
                self.assertEqual(failure.exception.code, "MUTATION_LOCKED")

    def test_drafts_stage_cannot_reach_the_price_update_call(self):
        config = replace(self.config, feature_stage="drafts", scopes=DRAFT_SCOPES)
        client = EbaySandboxClient(config, self.database, transport=FakeTransport([]))
        with self.assertRaises(EbayError) as failure:
            client._assert_mutation_allowed(
                "POST", "/sell/inventory/v1/bulk_update_price_quantity"
            )
        self.assertEqual(failure.exception.code, "MUTATION_LOCKED")

    def test_setup_stage_cannot_reach_the_price_update_call(self):
        config = replace(self.config, feature_stage="setup", scopes=DRAFT_SCOPES)
        client = EbaySandboxClient(config, self.database, transport=FakeTransport([]))
        with self.assertRaises(EbayError) as failure:
            client._assert_mutation_allowed(
                "POST", "/sell/inventory/v1/bulk_update_price_quantity"
            )
        self.assertEqual(failure.exception.code, "MUTATION_LOCKED")


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


class PriceFullStackTests(WebTestMixin, PriceDatabaseTestCase):
    OFFER_ID = 'OFFER-<svg/onload=alert(1)>&"'

    def setUp(self):
        super().setUp()
        self.grant()
        offer = published_offer(self.OFFER_ID, price_value="19.99", price_currency="GBP")
        self.transport = FakeTransport(
            [
                json_response(200, offer),  # review: PriceService.prepare_review -> get_offer
                json_response(200, offer),  # confirm: change_price's own re-verify get_offer
                json_response(
                    200,
                    {
                        "responses": [
                            {"offerId": self.OFFER_ID, "sku": SKU, "statusCode": 200}
                        ]
                    },
                ),  # confirm: bulk_update_price_quantity
            ]
        )
        self.ebay = EbaySandboxClient(self.config, self.database, transport=self.transport)
        adopt_test_access_token(self.ebay)
        self.app = WebApplication(self.config, self.database, self.ebay)

    def test_review_confirm_exact_protocol_one_use_and_escaping(self):
        cookie, csrf, dashboard = self.login()
        self.assertIn("Phase 6 safety boundary", dashboard)
        self.assertNotIn(SELLER_CANARY, dashboard)

        status, _headers, review_body = self.request(
            "POST",
            "/prices/review",
            cookie=cookie,
            form=price_form(csrf_token=csrf, offer_id=self.OFFER_ID),
        )
        self.assertEqual(status, 200)
        self.assertIn("No price change has been made yet", review_body)
        self.assertIn("GBP", review_body)
        self.assertIn("19.99", review_body)
        self.assertIn("24.99", review_body)
        self.assertIn("&lt;svg/onload=alert(1)&gt;", review_body)
        self.assertNotIn("<svg/onload=alert(1)>", review_body)
        self.assertEqual(len(self.transport.calls), 1)
        action_token = self.hidden(review_body, "action_token")

        status, headers, _body = self.request(
            "POST",
            "/actions/confirm",
            cookie=cookie,
            form={"csrf_token": csrf, "action_token": action_token},
        )
        self.assertEqual(status, 303)
        self.assertEqual(len(self.transport.calls), 3)
        location = headers["Location"]
        self.assertRegex(location, r"^/actions/[a-f0-9]{32}$")

        status, _headers, result_body = self.request("GET", location, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("The Sandbox Offer price was changed and confirmed.", result_body)
        self.assertIn("24.99", result_body)
        self.assertIn("&lt;svg/onload=alert(1)&gt;", result_body)
        self.assertNotIn("<svg/onload=alert(1)>", result_body)

        status, _headers, repeat_body = self.request(
            "POST",
            "/actions/confirm",
            cookie=cookie,
            form={"csrf_token": csrf, "action_token": action_token},
        )
        self.assertEqual(status, 409)
        self.assertIn("already used", repeat_body)
        self.assertIn("already completed on an earlier submission", repeat_body)
        self.assertEqual(len(self.transport.calls), 3)

    def test_cancel_makes_no_ebay_call_and_the_token_becomes_unusable(self):
        cookie, csrf, _dashboard = self.login()
        status, _headers, review_body = self.request(
            "POST",
            "/prices/review",
            cookie=cookie,
            form=price_form(csrf_token=csrf, offer_id=self.OFFER_ID),
        )
        self.assertEqual(status, 200)
        action_token = self.hidden(review_body, "action_token")
        self.assertEqual(len(self.transport.calls), 1)

        status, headers, _body = self.request(
            "POST",
            "/actions/cancel",
            cookie=cookie,
            form={"csrf_token": csrf, "action_token": action_token},
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        self.assertEqual(len(self.transport.calls), 1)  # still just the review read

        status, _headers, _body = self.request(
            "POST",
            "/actions/confirm",
            cookie=cookie,
            form={"csrf_token": csrf, "action_token": action_token},
        )
        self.assertEqual(status, 409)
        self.assertEqual(len(self.transport.calls), 1)


class PriceDryRunWebTests(WebTestMixin, PriceDatabaseTestCase):
    """Full-stack proof that a dry run never reaches eBay and is shown
    unmistakably on the result page (see PriceDryRunTests for the service
    layer; this exercises the same thing through the real WSGI review ->
    confirm -> result routes). Review still makes its own real, read-only
    get_offer call either way -- dry run only changes what confirm does."""

    def setUp(self):
        super().setUp()
        self.config = replace(self.config, dry_run=True)
        self.grant()
        offer = published_offer(OFFER_ID, price_value="19.99", price_currency="GBP")
        self.transport = FakeTransport([json_response(200, offer)])  # review's get_offer
        # only -- confirm must never add a second call in dry-run mode.
        self.ebay = EbaySandboxClient(self.config, self.database, transport=self.transport)
        adopt_test_access_token(self.ebay)
        self.app = WebApplication(self.config, self.database, self.ebay)

    def test_confirm_makes_no_further_transport_call_and_the_result_page_says_dry_run(self):
        cookie, csrf, _dashboard = self.login()
        status, _headers, review_body = self.request(
            "POST", "/prices/review", cookie=cookie, form=price_form(csrf_token=csrf)
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(self.transport.calls), 1)  # the review read only
        action_token = self.hidden(review_body, "action_token")

        status, headers, _body = self.request(
            "POST",
            "/actions/confirm",
            cookie=cookie,
            form={"csrf_token": csrf, "action_token": action_token},
        )
        self.assertEqual(status, 303)
        self.assertEqual(len(self.transport.calls), 1)  # still just the one read -- the
        # mutating call that a real confirm would make never happened.

        status, _headers, result_body = self.request(
            "GET", headers["Location"], cookie=cookie
        )
        self.assertEqual(status, 200)
        self.assertIn("DRY RUN", result_body)
        self.assertIn("24.99", result_body)


class PriceDashboardAndRouteTests(WebTestMixin, PriceDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.grant()
        self.transport = FakeTransport([])
        self.ebay = EbaySandboxClient(self.config, self.database, transport=self.transport)
        adopt_test_access_token(self.ebay)
        self.app = WebApplication(self.config, self.database, self.ebay)

    def test_dashboard_combines_draft_and_price_confirmations_in_one_table(self):
        generation = self.database.connection_generation()
        draft_token = self.database.create_pending_action(
            "any-session", "create_draft", {"sku": "SAFE-SKU-1"}, expected_generation=generation
        )
        draft_action = self.database.claim_pending_action(draft_token, "any-session")
        self.database.complete_action(
            draft_action.action_id, "succeeded", result={"sku": "SAFE-SKU-1"}
        )
        price_token = self.database.create_pending_action(
            "any-session", "change_price", {"offer_id": OFFER_ID}, expected_generation=generation
        )
        price_action = self.database.claim_pending_action(price_token, "any-session")
        self.database.complete_action(
            price_action.action_id, "succeeded", result={"offer_id": OFFER_ID}
        )

        cookie, _csrf, _body = self.login()
        status, _headers, dashboard = self.request("GET", "/", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Unpublished offer", dashboard)
        self.assertIn("Price change", dashboard)
        self.assertIn(f"/actions/{draft_action.action_id}", dashboard)
        self.assertIn(f"/actions/{price_action.action_id}", dashboard)

    def test_web_routes_and_client_lock_every_other_mutation(self):
        cookie, _csrf, _dashboard = self.login()
        forbidden_web_routes = (
            ("POST", "/orders/12-34567-89012/fulfil"),
            ("POST", "/orders/12-34567-89012/refund"),
            ("POST", "/listings/SAFE-SKU-1/price"),
            ("POST", "/listings/SAFE-SKU-1/quantity"),
            ("POST", "/drafts/images"),
            ("GET", "/account/setup"),
        )
        for method, path in forbidden_web_routes:
            with self.subTest(layer="web", method=method, path=path):
                status, _headers, body = self.request(method, path, cookie=cookie, form={})
                self.assertEqual(status, 404)
                self.assertIn("locked until", body)

        forbidden_client_routes = (
            ("POST", "/sell/inventory/v1/offer/OFFER-1/publish"),
            ("DELETE", "/sell/inventory/v1/offer/OFFER-1"),
            ("DELETE", "/sell/inventory/v1/inventory_item/SAFE-SKU-1"),
            ("POST", "/sell/account/v1/program/opt_in"),
        )
        for method, path in forbidden_client_routes:
            with self.subTest(layer="client", method=method, path=path):
                with self.assertRaises(EbayError) as locked:
                    self.ebay._api_request(method, path, payload={}, safe_read=False)
                self.assertEqual(locked.exception.code, "MUTATION_LOCKED")
        self.assertEqual(self.transport.calls, [])

    def test_actions_page_dispatches_by_kind_and_rejects_unrecognised_kinds(self):
        generation = self.database.connection_generation()
        draft_token = self.database.create_pending_action(
            "any-session", "create_draft", {"sku": "SAFE-SKU-1"}, expected_generation=generation
        )
        draft_action = self.database.claim_pending_action(draft_token, "any-session")
        self.database.complete_action(
            draft_action.action_id, "succeeded", result={"sku": "SAFE-SKU-1"}
        )
        price_token = self.database.create_pending_action(
            "any-session", "change_price", {"offer_id": OFFER_ID}, expected_generation=generation
        )
        price_action = self.database.claim_pending_action(price_token, "any-session")
        self.database.complete_action(
            price_action.action_id, "succeeded", result={"offer_id": OFFER_ID, "price": "24.99"}
        )

        cookie, _csrf, _body = self.login()
        status, _headers, draft_body = self.request(
            "GET", f"/actions/{draft_action.action_id}", cookie=cookie
        )
        self.assertEqual(status, 200)
        self.assertIn("SAFE-SKU-1", draft_body)

        status, _headers, price_body = self.request(
            "GET", f"/actions/{price_action.action_id}", cookie=cookie
        )
        self.assertEqual(status, 200)
        self.assertIn("The Sandbox Offer price was changed and confirmed.", price_body)

        status, _headers, missing_body = self.request(
            "GET", "/actions/0123456789abcdef0123456789abcdef", cookie=cookie
        )
        self.assertEqual(status, 404)
        self.assertIn("Action not found", missing_body)


class PriceSafetyLimitTests(PriceDatabaseTestCase):
    """Section 6 production price-safety controls: per-SKU floor, per-SKU
    change-cap override, global change-cap default, and the max-actions-per-run
    budget. Unlike PriceDatabaseTestCase's own default, these use the REAL
    production defaults (floor required, 10% cap) so the actual shipped
    behaviour is directly exercised somewhere, not just the neutralized
    config the other test classes use."""

    def setUp(self):
        super().setUp()
        self.config = replace(
            self.config,
            require_price_floor=True,
            max_price_change_percent=Decimal("10"),
            max_actions_per_run=2,
        )
        self.grant()
        self.client = RecordingPriceClient()
        self.client.offer_response = published_offer(OFFER_ID, price_value="19.99")
        self.service = PriceService(self.client, self.database, self.config)

    def test_review_is_refused_without_a_floor_when_floor_is_required(self):
        with self.assertRaises(EbayError) as refused:
            self.service.prepare_review(
                service_form(new_price="20.50"), "browser-session"
            )
        self.assertEqual(refused.exception.code, "PRICE_FLOOR_REQUIRED")
        self.assertEqual(self.database.recent_actions(), [])

    def test_review_is_refused_below_the_configured_floor(self):
        self.database.set_price_limit(SKU, "18.00", "GBP")
        with self.assertRaises(EbayError) as refused:
            self.service.prepare_review(
                service_form(new_price="17.99"), "browser-session"
            )
        self.assertEqual(refused.exception.code, "PRICE_BELOW_FLOOR")

    def test_review_at_exactly_the_floor_is_allowed(self):
        self.database.set_price_limit(SKU, "18.00", "GBP")
        review = self.service.prepare_review(
            service_form(new_price="18.00"), "browser-session"
        )
        self.assertEqual(review.new_price, "18.00")

    def test_review_is_refused_above_the_default_change_cap(self):
        # 19.99 -> 24.99 is about a 25% move; default cap is 10%.
        self.database.set_price_limit(SKU, "1.00", "GBP")
        with self.assertRaises(EbayError) as refused:
            self.service.prepare_review(service_form(), "browser-session")
        self.assertEqual(refused.exception.code, "PRICE_CHANGE_EXCEEDS_LIMIT")

    def test_review_within_the_default_change_cap_is_allowed(self):
        self.database.set_price_limit(SKU, "1.00", "GBP")
        review = self.service.prepare_review(
            service_form(new_price="21.50"), "browser-session"  # ~7.6% move
        )
        self.assertEqual(review.new_price, "21.50")

    def test_per_sku_change_cap_override_is_tighter_than_the_global_default(self):
        self.database.set_price_limit(SKU, "1.00", "GBP", max_change_percent="5")
        with self.assertRaises(EbayError) as refused:
            # ~7.6% move: allowed by the 10% global default, refused by the 5% override.
            self.service.prepare_review(
                service_form(new_price="21.50"), "browser-session"
            )
        self.assertEqual(refused.exception.code, "PRICE_CHANGE_EXCEEDS_LIMIT")

    def test_per_sku_change_cap_override_can_also_be_looser_than_the_default(self):
        self.database.set_price_limit(SKU, "1.00", "GBP", max_change_percent="30")
        review = self.service.prepare_review(service_form(), "browser-session")  # ~25% move
        self.assertEqual(review.new_price, "24.99")

    def test_max_actions_per_run_blocks_review_once_the_budget_is_exhausted(self):
        self.database.set_price_limit(SKU, "1.00", "GBP", max_change_percent="100")
        self.client.change_price_result = {"ok": True}
        for _ in range(self.config.max_actions_per_run):
            review = self.service.prepare_review(service_form(), "browser-session")
            self.service.confirm(review.action_token, "browser-session")
        with self.assertRaises(EbayError) as refused:
            self.service.prepare_review(service_form(), "browser-session")
        self.assertEqual(refused.exception.code, "MAX_ACTIONS_PER_RUN_REACHED")

    def test_max_actions_per_run_also_blocks_confirm_directly(self):
        # Defence in depth: even if a review were somehow already prepared
        # (e.g. a race), confirm() itself must refuse once the budget is spent.
        self.database.set_price_limit(SKU, "1.00", "GBP", max_change_percent="100")
        self.client.change_price_result = {"ok": True}
        tokens = []
        for _ in range(self.config.max_actions_per_run + 1):
            tokens.append(
                self.service.prepare_review(service_form(), "browser-session").action_token
            )
        for token in tokens[:-1]:
            self.service.confirm(token, "browser-session")
        with self.assertRaises(EbayError) as refused:
            self.service.confirm(tokens[-1], "browser-session")
        self.assertEqual(refused.exception.code, "MAX_ACTIONS_PER_RUN_REACHED")

    def test_a_failed_confirmation_does_not_consume_the_actions_budget(self):
        self.database.set_price_limit(SKU, "1.00", "GBP", max_change_percent="100")
        self.client.change_price_error = EbayError("boom", code="PRICE_CHANGED", status=409)
        review = self.service.prepare_review(service_form(), "browser-session")
        self.service.confirm(review.action_token, "browser-session")
        self.assertEqual(self.service._actions_this_run, 0)


class PriceLimitStorageTests(PriceDatabaseTestCase):
    def test_set_then_get_round_trips_every_field(self):
        self.database.set_price_limit(SKU, "18.00", "GBP", max_change_percent="7.5")
        limit = self.database.get_price_limit(SKU)
        self.assertEqual(limit["sku"], SKU)
        self.assertEqual(limit["floor_price"], "18.00")
        self.assertEqual(limit["floor_currency"], "GBP")
        self.assertEqual(limit["max_change_percent"], "7.5")

    def test_get_returns_none_for_an_unset_sku(self):
        self.assertIsNone(self.database.get_price_limit("NEVER-SET"))

    def test_set_again_replaces_rather_than_duplicates(self):
        self.database.set_price_limit(SKU, "18.00", "GBP")
        self.database.set_price_limit(SKU, "19.00", "GBP", max_change_percent="5")
        self.assertEqual(len(self.database.list_price_limits()), 1)
        limit = self.database.get_price_limit(SKU)
        self.assertEqual(limit["floor_price"], "19.00")
        self.assertEqual(limit["max_change_percent"], "5")

    def test_list_is_sorted_by_sku(self):
        self.database.set_price_limit("SKU-B", "1.00", "GBP")
        self.database.set_price_limit("SKU-A", "1.00", "GBP")
        skus = [row["sku"] for row in self.database.list_price_limits()]
        self.assertEqual(skus, ["SKU-A", "SKU-B"])

    def test_delete_removes_the_row_and_reports_whether_one_existed(self):
        self.database.set_price_limit(SKU, "18.00", "GBP")
        self.assertTrue(self.database.delete_price_limit(SKU))
        self.assertIsNone(self.database.get_price_limit(SKU))
        self.assertFalse(self.database.delete_price_limit(SKU))


class PriceLimitsWebTests(WebTestMixin, PriceDatabaseTestCase):
    """GET/POST /price-limits and POST /price-limits/delete (Section 6 UI)."""

    def setUp(self):
        super().setUp()
        self.grant()
        self.transport = FakeTransport([])
        self.ebay = EbaySandboxClient(self.config, self.database, transport=self.transport)
        adopt_test_access_token(self.ebay)
        self.app = WebApplication(self.config, self.database, self.ebay)

    def test_dashboard_links_to_the_page_only_once_connected(self):
        cookie, _csrf, dashboard = self.login()
        self.assertIn("/price-limits", dashboard)
        self.assertIn("Manage price safety limits", dashboard)

    def test_empty_state_and_the_configured_default_cap_are_shown(self):
        cookie, _csrf, _dashboard = self.login()
        status, _headers, body = self.request("GET", "/price-limits", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("No SKUs have a floor price set yet.", body)
        self.assertIn("100%", body)  # this test class neutralizes the global cap to 100

    def test_saving_a_limit_persists_it_and_shows_it_in_the_table(self):
        cookie, csrf, _dashboard = self.login()
        status, headers, _body = self.request(
            "POST",
            "/price-limits",
            cookie=cookie,
            form={
                "csrf_token": csrf,
                "sku": SKU,
                "floor_price": "15.00",
                "max_change_percent": "5",
            },
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/price-limits")

        stored = self.database.get_price_limit(SKU)
        self.assertEqual(stored["floor_price"], "15.00")
        self.assertEqual(stored["floor_currency"], self.config.currency)
        self.assertEqual(stored["max_change_percent"], "5")

        status, _headers, body = self.request("GET", "/price-limits", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn(SKU, body)
        self.assertIn("15.00", body)
        self.assertIn("5%", body)
        self.assertIn("Saved the price limit", body)

    def test_an_optional_cap_can_be_left_blank_to_use_the_global_default(self):
        cookie, csrf, _dashboard = self.login()
        status, _headers, _body = self.request(
            "POST",
            "/price-limits",
            cookie=cookie,
            form={"csrf_token": csrf, "sku": SKU, "floor_price": "15.00"},
        )
        self.assertEqual(status, 303)
        stored = self.database.get_price_limit(SKU)
        self.assertIsNone(stored["max_change_percent"])

        status, _headers, body = self.request("GET", "/price-limits", cookie=cookie)
        self.assertIn("Uses global default", body)

    def test_saving_the_same_sku_again_updates_rather_than_duplicates(self):
        cookie, csrf, _dashboard = self.login()
        for floor in ("15.00", "20.00"):
            status, _headers, _body = self.request(
                "POST",
                "/price-limits",
                cookie=cookie,
                form={"csrf_token": csrf, "sku": SKU, "floor_price": floor},
            )
            self.assertEqual(status, 303)
        self.assertEqual(len(self.database.list_price_limits()), 1)
        self.assertEqual(self.database.get_price_limit(SKU)["floor_price"], "20.00")

    def test_an_invalid_submission_is_rejected_and_redisplayed_with_the_error(self):
        cookie, csrf, _dashboard = self.login()
        status, _headers, body = self.request(
            "POST",
            "/price-limits",
            cookie=cookie,
            form={"csrf_token": csrf, "sku": "bad sku!", "floor_price": "15.00"},
        )
        self.assertEqual(status, 400)
        self.assertIn("was not saved", body)
        self.assertIsNone(self.database.get_price_limit("bad sku!"))

    def test_an_out_of_range_floor_is_rejected(self):
        cookie, csrf, _dashboard = self.login()
        status, _headers, body = self.request(
            "POST",
            "/price-limits",
            cookie=cookie,
            form={"csrf_token": csrf, "sku": SKU, "floor_price": "0.00"},
        )
        self.assertEqual(status, 400)
        self.assertIsNone(self.database.get_price_limit(SKU))

    def test_deleting_a_limit_removes_it_and_flashes_confirmation(self):
        self.database.set_price_limit(SKU, "15.00", "GBP")
        cookie, csrf, _dashboard = self.login()
        status, headers, _body = self.request(
            "POST",
            "/price-limits/delete",
            cookie=cookie,
            form={"csrf_token": csrf, "sku": SKU},
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/price-limits")
        self.assertIsNone(self.database.get_price_limit(SKU))

        status, _headers, body = self.request("GET", "/price-limits", cookie=cookie)
        self.assertIn("Removed the price limit", body)

    def test_deleting_an_unknown_sku_is_a_no_op_with_an_informational_flash(self):
        cookie, csrf, _dashboard = self.login()
        status, headers, _body = self.request(
            "POST",
            "/price-limits/delete",
            cookie=cookie,
            form={"csrf_token": csrf, "sku": "NO-SUCH-SKU"},
        )
        self.assertEqual(status, 303)
        status, _headers, body = self.request("GET", "/price-limits", cookie=cookie)
        self.assertIn("No price limit was set", body)

    def test_both_routes_require_a_valid_csrf_token(self):
        cookie, _csrf, _dashboard = self.login()
        status, _headers, body = self.request(
            "POST",
            "/price-limits",
            cookie=cookie,
            form={"csrf_token": "wrong", "sku": SKU, "floor_price": "15.00"},
        )
        self.assertEqual(status, 403)
        status, _headers, body = self.request(
            "POST",
            "/price-limits/delete",
            cookie=cookie,
            form={"csrf_token": "wrong", "sku": SKU},
        )
        self.assertEqual(status, 403)

    def test_route_is_locked_outside_the_prices_quantities_and_images_stages(self):
        cookie, _csrf, _dashboard = self.login()
        for stage in ("connection", "account", "listings", "orders", "drafts"):
            self.app.config = replace(self.config, feature_stage=stage)
            status, _headers, body = self.request("GET", "/price-limits", cookie=cookie)
            self.assertEqual(status, 404)
            self.assertIn("locked until", body)


if __name__ == "__main__":
    unittest.main()
