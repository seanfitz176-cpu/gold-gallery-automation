from __future__ import annotations

import io
import re
import tempfile
import unittest
import urllib.parse
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from ebay_app.config import DRAFT_SCOPES, ORDER_SCOPES
from ebay_app.ebay_client import EbayError, EbaySandboxClient, HttpResponse
from ebay_app.image_fetch import ImageUrlError
from ebay_app.images import IMAGE_ACTION_KIND, ImageService
from ebay_app.storage import AppDatabase
from ebay_app.validation import ValidationError
from ebay_app.views import listings_page
from ebay_app.web import WebApplication

from helpers import FakeTransport, adopt_test_access_token, json_response, make_config


SELLER_CANARY = "PRIVATE-INTERNAL-SELLER-ID"
REFRESH_CANARY = "PRIVATE-REFRESH-TOKEN"
OFFER_ID = "OFFER-1"
SKU = "SKU-1"
IMAGE_URL = "https://images.example.com/photo.jpg"


def fake_url_checker(content_type: str = "image/jpeg", content_length: int = 2048):
    def checker(url: str):
        return {"content_type": content_type, "content_length": content_length}

    return checker


def failing_url_checker(code: str = "IMAGE_URL_UNREACHABLE", message: str = "That image address could not be reached."):
    def checker(url: str):
        raise ImageUrlError(message, code=code)

    return checker


def image_form(**updates: str) -> dict[str, str]:
    values = {
        "csrf_token": "csrf",
        "offer_id": OFFER_ID,
        "sku": SKU,
        "image_url": IMAGE_URL,
    }
    values.update(updates)
    return values


def service_form(**updates: str) -> dict[str, list[str]]:
    return {name: [value] for name, value in image_form(**updates).items()}


def published_offer(
    offer_id: str,
    *,
    sku: str = SKU,
    status: str = "PUBLISHED",
    available_quantity: int = 3,
) -> dict:
    return {
        "offerId": offer_id,
        "sku": sku,
        "status": status,
        "marketplaceId": "EBAY_GB",
        "availableQuantity": available_quantity,
        "pricingSummary": {"price": {"currency": "GBP", "value": "19.99"}},
    }


def inventory_item(sku: str, *, image_urls=()) -> dict:
    return {
        "sku": sku,
        "condition": "NEW",
        "availability": {"shipToLocationAvailability": {"quantity": 5}},
        "product": {
            "title": "Test item",
            "description": "Test description",
            "imageUrls": list(image_urls),
        },
    }


class RecordingImageClient:
    """Small service double; the full-stack tests below use the real eBay client."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.offer_response: dict = {}
        self.inventory_item_response = None
        self.after_get_offer = None
        self.after_get_inventory_item = None
        self.add_image_result: dict = {}
        self.add_image_error: Exception | None = None

    def get_offer(self, offer_id):
        self.calls.append(("get_offer", offer_id))
        if self.after_get_offer is not None:
            self.after_get_offer()
        return self.offer_response

    def get_inventory_item(self, sku):
        self.calls.append(("get_inventory_item", sku))
        if self.after_get_inventory_item is not None:
            self.after_get_inventory_item()
        return self.inventory_item_response

    @staticmethod
    def inventory_item_image_urls(item):
        product = item.get("product") or {}
        urls = product.get("imageUrls")
        return [str(url) for url in urls] if isinstance(urls, list) else []

    @contextmanager
    def image_mutation_guard(self):
        self.calls.append(("guard_enter",))
        try:
            yield
        finally:
            self.calls.append(("guard_exit",))

    def add_inventory_item_image(self, **kwargs):
        self.calls.append(("add_inventory_item_image", dict(kwargs)))
        if self.add_image_error is not None:
            raise self.add_image_error
        return self.add_image_result


class ImageDatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(
            make_config(Path(self.temp.name)),
            feature_stage="images",
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


class ImageServiceGateTests(ImageDatabaseTestCase):
    def test_stage_runtime_and_scope_gates_precede_any_client_call(self):
        client = RecordingImageClient()

        with self.assertRaises(EbayError) as wrong_stage:
            ImageService(
                client, self.database, replace(self.config, feature_stage="drafts")
            ).prepare_review(service_form(), "session")
        self.assertEqual(wrong_stage.exception.code, "IMAGE_STAGE_LOCKED")

        # ``images`` is the newest stage: it is not reachable a stage early
        # either, unlike PriceService/QuantityService which are cumulative
        # over their own predecessor.
        with self.assertRaises(EbayError) as earlier_stage:
            ImageService(
                client, self.database, replace(self.config, feature_stage="quantities")
            ).prepare_review(service_form(), "session")
        self.assertEqual(earlier_stage.exception.code, "IMAGE_STAGE_LOCKED")

        with self.assertRaises(EbayError) as unsupported_runtime:
            ImageService(
                client, self.database, replace(self.config, runtime_mode="unsupported")
            ).prepare_review(service_form(), "session")
        self.assertEqual(unsupported_runtime.exception.code, "IMAGE_LOCAL_MODE_REQUIRED")

        self.grant(ORDER_SCOPES)
        with self.assertRaises(EbayError) as missing_scope:
            ImageService(client, self.database, self.config).prepare_review(
                service_form(), "session"
            )
        self.assertEqual(missing_scope.exception.code, "IMAGE_PERMISSION_REQUIRED")

        # None of the locked-out attempts above ever reached the eBay client.
        self.assertEqual(client.calls, [])

    def test_not_connected_gate_is_checked_before_any_client_call(self):
        self.grant()
        client = RecordingImageClient()
        service = ImageService(client, self.database, self.config)
        with patch.object(self.database, "connection_generation", return_value=None):
            with self.assertRaises(EbayError) as not_connected:
                service.prepare_review(service_form(), "session")
        self.assertEqual(not_connected.exception.code, "NOT_CONNECTED")
        self.assertEqual(client.calls, [])

    def test_confirm_and_cancel_also_enforce_the_stage_gate(self):
        self.grant()
        client = RecordingImageClient()
        other_stage = replace(self.config, feature_stage="quantities")
        with self.assertRaises(EbayError) as confirm_locked:
            ImageService(client, self.database, other_stage).confirm("token", "session")
        self.assertEqual(confirm_locked.exception.code, "IMAGE_STAGE_LOCKED")
        with self.assertRaises(EbayError) as cancel_locked:
            ImageService(client, self.database, other_stage).cancel("token", "session")
        self.assertEqual(cancel_locked.exception.code, "IMAGE_STAGE_LOCKED")
        self.assertEqual(client.calls, [])


class ImageServiceReviewTests(ImageDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.grant()
        self.client = RecordingImageClient()
        self.client.offer_response = published_offer(OFFER_ID)
        self.client.inventory_item_response = inventory_item(SKU, image_urls=[])
        self.service = ImageService(
            self.client, self.database, self.config, url_checker=fake_url_checker()
        )

    def test_successful_review_performs_exactly_two_reads_and_makes_no_change(self):
        review = self.service.prepare_review(service_form(), "browser-session")
        self.assertEqual(review.offer_id, OFFER_ID)
        self.assertEqual(review.sku, SKU)
        self.assertEqual(review.expected_image_urls, [])
        self.assertEqual(review.new_image_url, IMAGE_URL)
        self.assertEqual(review.content_type, "image/jpeg")
        self.assertEqual(review.content_length, 2048)
        self.assertEqual(
            [call[0] for call in self.client.calls], ["get_offer", "get_inventory_item"]
        )
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

    def test_missing_inventory_item_stops_review(self):
        self.client.inventory_item_response = None
        with self.assertRaises(EbayError) as missing_item:
            self.service.prepare_review(service_form(), "browser-session")
        self.assertEqual(missing_item.exception.code, "INVENTORY_ITEM_MISSING")

    def test_duplicate_image_url_is_a_validation_error_not_an_ebay_call_result(self):
        self.client.inventory_item_response = inventory_item(SKU, image_urls=[IMAGE_URL])
        with self.assertRaises(ValidationError) as duplicate:
            self.service.prepare_review(service_form(), "browser-session")
        self.assertIn("image_url", duplicate.exception.errors)
        self.assertEqual(self.database.recent_actions(), [])

    def test_image_limit_reached_stops_review(self):
        self.client.inventory_item_response = inventory_item(
            SKU, image_urls=[f"https://images.example.com/{i}.jpg" for i in range(24)]
        )
        with self.assertRaises(EbayError) as limit_reached:
            self.service.prepare_review(service_form(), "browser-session")
        self.assertEqual(limit_reached.exception.code, "IMAGE_LIMIT_REACHED")

    def test_unreachable_image_url_is_a_validation_error_not_an_ebay_call_result(self):
        self.service = ImageService(
            self.client,
            self.database,
            self.config,
            url_checker=failing_url_checker(
                code="IMAGE_TYPE_UNSUPPORTED", message="Not a supported image type."
            ),
        )
        with self.assertRaises(ValidationError) as bad_url:
            self.service.prepare_review(service_form(), "browser-session")
        self.assertIn("image_url", bad_url.exception.errors)
        self.assertEqual(bad_url.exception.errors["image_url"], "Not a supported image type.")
        self.assertEqual(self.database.recent_actions(), [])

    def test_connection_change_during_review_creates_no_pending_action(self):
        def replace_seller() -> None:
            self.grant(user_id="DIFFERENT-SELLER", refresh_token="different-seller-refresh")

        self.client.after_get_inventory_item = replace_seller
        with self.assertRaises(EbayError) as changed:
            self.service.prepare_review(service_form(), "browser-session")
        self.assertEqual(changed.exception.code, "CONNECTION_CHANGED")
        self.assertEqual(self.database.recent_actions(), [])


class ImageServiceConfirmTests(ImageDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.grant()
        self.client = RecordingImageClient()
        self.client.offer_response = published_offer(OFFER_ID)
        self.client.inventory_item_response = inventory_item(SKU, image_urls=[])
        self.service = ImageService(
            self.client, self.database, self.config, url_checker=fake_url_checker()
        )

    def review(self):
        return self.service.prepare_review(service_form(), "browser-session")

    def test_confirm_wraps_the_write_in_the_mutation_guard_and_records_success(self):
        review = self.review()
        self.client.add_image_result = {
            "offer_id": OFFER_ID,
            "sku": SKU,
            "image_url": IMAGE_URL,
            "image_count": 1,
        }
        action_id = self.service.confirm(review.action_token, "browser-session")
        self.assertIsNotNone(action_id)
        self.assertEqual(
            [call[0] for call in self.client.calls],
            ["get_offer", "get_inventory_item", "guard_enter", "add_inventory_item_image", "guard_exit"],
        )
        action = self.database.get_action_for_admin(action_id)
        self.assertEqual(action["status"], "succeeded")
        self.assertEqual(action["result"]["image_count"], 1)
        self.assertEqual(action["result"]["image_url"], IMAGE_URL)

    def test_confirm_is_one_use(self):
        review = self.review()
        self.client.add_image_result = {
            "offer_id": OFFER_ID,
            "sku": SKU,
            "image_url": IMAGE_URL,
            "image_count": 1,
        }
        first = self.service.confirm(review.action_token, "browser-session")
        self.assertIsNotNone(first)
        second = self.service.confirm(review.action_token, "browser-session")
        self.assertIsNone(second)
        self.assertEqual(
            [call[0] for call in self.client.calls].count("add_inventory_item_image"), 1
        )

    def test_definitive_ebay_error_is_recorded_failed_and_remote_details_are_discarded(self):
        review = self.review()
        self.client.add_image_error = EbayError(
            "The listing's images changed after review; review the latest images before "
            "trying again",
            status=409,
            code="IMAGES_CHANGED",
            details={"leaked": "PRIVATE-REMOTE-DETAIL"},
        )
        action_id = self.service.confirm(review.action_token, "browser-session")
        action = self.database.get_action_for_admin(action_id)
        self.assertEqual(action["status"], "failed")
        self.assertEqual(
            action["result"],
            {"safe_code": "IMAGES_CHANGED", "reconciliation_required": False},
        )
        self.assertNotIn("PRIVATE-REMOTE-DETAIL", str(action))

    def test_ambiguous_ebay_error_is_recorded_unknown_and_flags_reconciliation(self):
        review = self.review()
        self.client.add_image_error = EbayError(
            "eBay did not confirm the requested image addition",
            code="IMAGE_UPDATE_UNCONFIRMED",
            details={"leaked": "PRIVATE-REMOTE-DETAIL"},
            outcome_unknown=True,
        )
        action_id = self.service.confirm(review.action_token, "browser-session")
        action = self.database.get_action_for_admin(action_id)
        self.assertEqual(action["status"], "unknown")
        self.assertEqual(
            action["result"],
            {"safe_code": "IMAGE_UPDATE_UNCONFIRMED", "reconciliation_required": True},
        )
        self.assertNotIn("PRIVATE-REMOTE-DETAIL", str(action))

    def test_invalid_saved_payload_fails_closed_without_calling_ebay(self):
        generation = self.database.connection_generation()
        token = self.database.create_pending_action(
            "browser-session",
            IMAGE_ACTION_KIND,
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
        self.client.add_image_error = RuntimeError("boom")
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
            [call[0] for call in self.client.calls], ["get_offer", "get_inventory_item"]
        )  # only the read-only review calls; no guard, no add_inventory_item_image


class ImageDryRunTests(ImageDatabaseTestCase):
    """The write-phase kill switch's other half (AppConfig.dry_run, see
    ImageService.confirm), mirroring PriceDryRunTests exactly: a dry run
    must claim and journal the action exactly as normal, but must never call
    eBay at all."""

    def setUp(self):
        super().setUp()
        self.config = replace(self.config, dry_run=True)
        self.grant()
        self.client = RecordingImageClient()
        self.client.offer_response = published_offer(OFFER_ID)
        self.client.inventory_item_response = inventory_item(SKU, image_urls=[])
        self.service = ImageService(
            self.client, self.database, self.config, url_checker=fake_url_checker()
        )

    def review(self):
        return self.service.prepare_review(service_form(), "browser-session")

    def test_confirm_never_calls_ebay_and_records_a_dry_run_result(self):
        review = self.review()
        self.assertEqual(
            [call[0] for call in self.client.calls], ["get_offer", "get_inventory_item"]
        )

        action_id = self.service.confirm(review.action_token, "browser-session")

        self.assertIsNotNone(action_id)
        # No guard_enter/add_inventory_item_image/guard_exit at all -- confirm
        # never touched the eBay client beyond the review's own reads.
        self.assertEqual(
            [call[0] for call in self.client.calls], ["get_offer", "get_inventory_item"]
        )
        action = self.database.get_action_for_admin(action_id)
        self.assertEqual(action["status"], "succeeded")
        self.assertEqual(
            action["result"],
            {"dry_run": True, "offer_id": OFFER_ID, "sku": SKU, "image_url": IMAGE_URL},
        )

    def test_dry_run_still_advances_the_actions_budget(self):
        self.config = replace(self.config, max_actions_per_run=1)
        self.service = ImageService(
            self.client, self.database, self.config, url_checker=fake_url_checker()
        )
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


class ImageActionsBudgetTests(ImageDatabaseTestCase):
    """Section 6 safety control extended to ImageService this pass,
    mirroring PriceSafetyLimitTests' own budget coverage: ImageService
    previously had no actions-per-run ceiling at all."""

    def setUp(self):
        super().setUp()
        self.config = replace(self.config, max_actions_per_run=2)
        self.grant()
        self.client = RecordingImageClient()
        self.client.offer_response = published_offer(OFFER_ID)
        self.client.inventory_item_response = inventory_item(SKU, image_urls=[])
        self.client.add_image_result = {"ok": True}
        self.service = ImageService(
            self.client, self.database, self.config, url_checker=fake_url_checker()
        )

    def test_max_actions_per_run_blocks_review_once_the_budget_is_exhausted(self):
        for _ in range(self.config.max_actions_per_run):
            review = self.service.prepare_review(service_form(), "browser-session")
            self.service.confirm(review.action_token, "browser-session")
        with self.assertRaises(EbayError) as refused:
            self.service.prepare_review(service_form(), "browser-session")
        self.assertEqual(refused.exception.code, "MAX_ACTIONS_PER_RUN_REACHED")

    def test_max_actions_per_run_also_blocks_confirm_directly(self):
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
        self.client.add_image_error = EbayError("boom", code="IMAGES_CHANGED", status=409)
        review = self.service.prepare_review(service_form(), "browser-session")
        self.service.confirm(review.action_token, "browser-session")
        self.assertEqual(self.service._actions_this_run, 0)


class ImageMutationGuardTests(ImageDatabaseTestCase):
    def client(self, responses=()) -> EbaySandboxClient:
        transport = FakeTransport(list(responses))
        client = EbaySandboxClient(self.config, self.database, transport=transport)
        adopt_test_access_token(client)
        return client

    def test_guard_rejects_wrong_feature_stage(self):
        config = replace(self.config, feature_stage="drafts", scopes=DRAFT_SCOPES)
        client = EbaySandboxClient(config, self.database, transport=FakeTransport([]))
        with self.assertRaises(EbayError) as failure:
            with client.image_mutation_guard():
                pass
        self.assertEqual(failure.exception.code, "MUTATION_LOCKED")

    def test_guard_rejects_the_quantities_stage_too(self):
        # Unlike PriceService/QuantityService, ImageService is the newest
        # capability and is not reachable a stage early -- only ``images``
        # unlocks it.
        config = replace(self.config, feature_stage="quantities", scopes=DRAFT_SCOPES)
        client = EbaySandboxClient(config, self.database, transport=FakeTransport([]))
        with self.assertRaises(EbayError) as failure:
            with client.image_mutation_guard():
                pass
        self.assertEqual(failure.exception.code, "MUTATION_LOCKED")

    def test_guard_rejects_oauth_authorization_runtime_mode(self):
        config = replace(self.config, runtime_mode="oauth_authorization")
        client = EbaySandboxClient(config, self.database, transport=FakeTransport([]))
        with self.assertRaises(EbayError) as failure:
            with client.image_mutation_guard():
                pass
        self.assertEqual(failure.exception.code, "IMAGE_LOCAL_MODE_REQUIRED")

    def test_guard_allows_images_stage_in_local_mode(self):
        client = self.client()
        with client.image_mutation_guard():
            pass  # no error


class ImageMutationAllowlistTests(ImageDatabaseTestCase):
    def client(self, responses=()) -> EbaySandboxClient:
        transport = FakeTransport(list(responses))
        client = EbaySandboxClient(self.config, self.database, transport=transport)
        adopt_test_access_token(client)
        return client

    def test_images_stage_allows_the_targeted_inventory_item_put_call(self):
        client = self.client()
        client._assert_mutation_allowed(
            "PUT", "/sell/inventory/v1/inventory_item/SAFE-SKU-1"
        )  # must not raise

    def test_images_stage_remains_cumulative_over_drafts_prices_and_quantities_calls(self):
        client = self.client()
        client._assert_mutation_allowed("POST", "/sell/inventory/v1/offer")
        client._assert_mutation_allowed(
            "POST", "/sell/inventory/v1/bulk_update_price_quantity"
        )

    def test_images_stage_still_locks_every_other_mutation(self):
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

    def test_setup_stage_cannot_reach_the_inventory_item_put_call(self):
        config = replace(self.config, feature_stage="setup", scopes=DRAFT_SCOPES)
        client = EbaySandboxClient(config, self.database, transport=FakeTransport([]))
        with self.assertRaises(EbayError) as failure:
            client._assert_mutation_allowed(
                "PUT", "/sell/inventory/v1/inventory_item/SAFE-SKU-1"
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


class ImageFullStackTests(WebTestMixin, ImageDatabaseTestCase):
    OFFER_ID = 'OFFER-<svg/onload=alert(1)>&"'

    def setUp(self):
        super().setUp()
        self.grant()
        offer = published_offer(self.OFFER_ID, available_quantity=3)
        item = inventory_item(SKU, image_urls=[])
        self.transport = FakeTransport(
            [
                json_response(200, offer),  # review: ImageService.prepare_review -> get_offer
                json_response(200, item),  # review: get_inventory_item
                json_response(200, offer),  # confirm: add_inventory_item_image's re-verify get_offer
                json_response(200, item),  # confirm: re-verify get_inventory_item
                HttpResponse(204, {}, b""),  # confirm: inventory_item PUT
            ]
        )
        self.ebay = EbaySandboxClient(self.config, self.database, transport=self.transport)
        adopt_test_access_token(self.ebay)
        self.image_service = ImageService(
            self.ebay, self.database, self.config, url_checker=fake_url_checker()
        )
        self.app = WebApplication(
            self.config, self.database, self.ebay, image_service=self.image_service
        )

    def test_review_confirm_exact_protocol_one_use_and_escaping(self):
        cookie, csrf, dashboard = self.login()
        self.assertIn("Phase 8 safety boundary", dashboard)
        self.assertNotIn(SELLER_CANARY, dashboard)

        status, _headers, review_body = self.request(
            "POST",
            "/images/review",
            cookie=cookie,
            form=image_form(csrf_token=csrf, offer_id=self.OFFER_ID),
        )
        self.assertEqual(status, 200)
        self.assertIn("No image has been added yet", review_body)
        self.assertIn(IMAGE_URL, review_body)
        self.assertIn("&lt;svg/onload=alert(1)&gt;", review_body)
        self.assertNotIn("<svg/onload=alert(1)>", review_body)
        self.assertEqual(len(self.transport.calls), 2)
        action_token = self.hidden(review_body, "action_token")

        status, headers, _body = self.request(
            "POST",
            "/actions/confirm",
            cookie=cookie,
            form={"csrf_token": csrf, "action_token": action_token},
        )
        self.assertEqual(status, 303)
        self.assertEqual(len(self.transport.calls), 5)
        location = headers["Location"]
        self.assertRegex(location, r"^/actions/[a-f0-9]{32}$")

        status, _headers, result_body = self.request("GET", location, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("The Sandbox listing image was added and confirmed.", result_body)
        self.assertIn(IMAGE_URL, result_body)
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
        # A repeat submission of a token that genuinely already completed
        # gets an enriched message pointing at the dashboard, not just the
        # generic "expired, cancelled, or already used" text -- this is the
        # double-submit UX fix, and it must never turn into a second success.
        self.assertIn("already completed on an earlier submission", repeat_body)
        self.assertEqual(len(self.transport.calls), 5)

    def test_cancel_makes_no_ebay_call_and_the_token_becomes_unusable(self):
        cookie, csrf, _dashboard = self.login()
        status, _headers, review_body = self.request(
            "POST",
            "/images/review",
            cookie=cookie,
            form=image_form(csrf_token=csrf, offer_id=self.OFFER_ID),
        )
        self.assertEqual(status, 200)
        action_token = self.hidden(review_body, "action_token")
        self.assertEqual(len(self.transport.calls), 2)

        status, headers, _body = self.request(
            "POST",
            "/actions/cancel",
            cookie=cookie,
            form={"csrf_token": csrf, "action_token": action_token},
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        self.assertEqual(len(self.transport.calls), 2)  # still just the review reads

        status, _headers, _body = self.request(
            "POST",
            "/actions/confirm",
            cookie=cookie,
            form={"csrf_token": csrf, "action_token": action_token},
        )
        self.assertEqual(status, 409)
        self.assertEqual(len(self.transport.calls), 2)

    def test_the_price_and_quantity_change_capabilities_remain_reachable_at_the_images_stage(self):
        # ``images`` is cumulative over ``quantities`` (and ``prices`` in
        # turn): the same "Review price"/"Review quantity" controls from
        # Phases 6 and 7 must still work once the stage moves on.
        cookie, csrf, _dashboard = self.login()
        offer = published_offer(self.OFFER_ID, available_quantity=3)
        self.transport.responses = [json_response(200, offer)]
        status, _headers, review_body = self.request(
            "POST",
            "/prices/review",
            cookie=cookie,
            form={
                "csrf_token": csrf,
                "offer_id": self.OFFER_ID,
                "sku": SKU,
                "new_price": "24.99",
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("No price change has been made yet", review_body)

        self.transport.responses = [json_response(200, offer)]
        status, _headers, review_body = self.request(
            "POST",
            "/quantities/review",
            cookie=cookie,
            form={
                "csrf_token": csrf,
                "offer_id": self.OFFER_ID,
                "sku": SKU,
                "new_quantity": "9",
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("No quantity change has been made yet", review_body)


class ImageDryRunWebTests(WebTestMixin, ImageDatabaseTestCase):
    """Full-stack proof that a dry run never reaches eBay and is shown
    unmistakably on the result page, mirroring PriceDryRunWebTests exactly:
    review still makes its own real, read-only reads either way -- dry run
    only changes what confirm does."""

    def setUp(self):
        super().setUp()
        self.config = replace(self.config, dry_run=True)
        self.grant()
        offer = published_offer(OFFER_ID, available_quantity=3)
        item = inventory_item(SKU, image_urls=[])
        self.transport = FakeTransport(
            [
                json_response(200, offer),  # review's get_offer only --
                json_response(200, item),  # review's get_inventory_item only --
                # confirm must never add a third call in dry-run mode.
            ]
        )
        self.ebay = EbaySandboxClient(self.config, self.database, transport=self.transport)
        adopt_test_access_token(self.ebay)
        self.image_service = ImageService(
            self.ebay, self.database, self.config, url_checker=fake_url_checker()
        )
        self.app = WebApplication(
            self.config, self.database, self.ebay, image_service=self.image_service
        )

    def test_confirm_makes_no_further_transport_call_and_the_result_page_says_dry_run(self):
        cookie, csrf, _dashboard = self.login()
        status, _headers, review_body = self.request(
            "POST", "/images/review", cookie=cookie, form=image_form(csrf_token=csrf)
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(self.transport.calls), 2)  # the two review reads only
        action_token = self.hidden(review_body, "action_token")

        status, headers, _body = self.request(
            "POST",
            "/actions/confirm",
            cookie=cookie,
            form={"csrf_token": csrf, "action_token": action_token},
        )
        self.assertEqual(status, 303)
        self.assertEqual(len(self.transport.calls), 2)  # still just the two reads -- the
        # mutating call that a real confirm would make never happened.

        status, _headers, result_body = self.request(
            "GET", headers["Location"], cookie=cookie
        )
        self.assertEqual(status, 200)
        self.assertIn("DRY RUN", result_body)
        self.assertIn(IMAGE_URL, result_body)


class ImageDashboardAndRouteTests(WebTestMixin, ImageDatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.grant()
        self.transport = FakeTransport([])
        self.ebay = EbaySandboxClient(self.config, self.database, transport=self.transport)
        adopt_test_access_token(self.ebay)
        self.app = WebApplication(self.config, self.database, self.ebay)

    def test_dashboard_combines_all_four_confirmation_kinds_in_one_table(self):
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
        quantity_token = self.database.create_pending_action(
            "any-session",
            "change_quantity",
            {"offer_id": OFFER_ID},
            expected_generation=generation,
        )
        quantity_action = self.database.claim_pending_action(quantity_token, "any-session")
        self.database.complete_action(
            quantity_action.action_id, "succeeded", result={"offer_id": OFFER_ID}
        )
        image_token = self.database.create_pending_action(
            "any-session",
            "add_image",
            {"offer_id": OFFER_ID},
            expected_generation=generation,
        )
        image_action = self.database.claim_pending_action(image_token, "any-session")
        self.database.complete_action(
            image_action.action_id, "succeeded", result={"offer_id": OFFER_ID, "image_url": IMAGE_URL}
        )

        cookie, _csrf, _body = self.login()
        status, _headers, dashboard = self.request("GET", "/", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Unpublished offer", dashboard)
        self.assertIn("Price change", dashboard)
        self.assertIn("Quantity change", dashboard)
        self.assertIn("Image added", dashboard)
        self.assertIn(f"/actions/{draft_action.action_id}", dashboard)
        self.assertIn(f"/actions/{price_action.action_id}", dashboard)
        self.assertIn(f"/actions/{quantity_action.action_id}", dashboard)
        self.assertIn(f"/actions/{image_action.action_id}", dashboard)

    def test_web_routes_and_client_lock_every_other_mutation(self):
        cookie, _csrf, _dashboard = self.login()
        forbidden_web_routes = (
            ("POST", "/orders/12-34567-89012/fulfil"),
            ("POST", "/orders/12-34567-89012/refund"),
            ("POST", "/listings/SAFE-SKU-1/price"),
            ("POST", "/listings/SAFE-SKU-1/quantity"),
            ("POST", "/drafts/new-image-upload"),
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
        quantity_token = self.database.create_pending_action(
            "any-session",
            "change_quantity",
            {"offer_id": OFFER_ID},
            expected_generation=generation,
        )
        quantity_action = self.database.claim_pending_action(quantity_token, "any-session")
        self.database.complete_action(
            quantity_action.action_id,
            "succeeded",
            result={"offer_id": OFFER_ID, "quantity": 7},
        )
        image_token = self.database.create_pending_action(
            "any-session",
            "add_image",
            {"offer_id": OFFER_ID},
            expected_generation=generation,
        )
        image_action = self.database.claim_pending_action(image_token, "any-session")
        self.database.complete_action(
            image_action.action_id,
            "succeeded",
            result={"offer_id": OFFER_ID, "image_url": IMAGE_URL, "image_count": 1},
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

        status, _headers, quantity_body = self.request(
            "GET", f"/actions/{quantity_action.action_id}", cookie=cookie
        )
        self.assertEqual(status, 200)
        self.assertIn("The Sandbox Offer quantity was changed and confirmed.", quantity_body)

        status, _headers, image_body = self.request(
            "GET", f"/actions/{image_action.action_id}", cookie=cookie
        )
        self.assertEqual(status, 200)
        self.assertIn("The Sandbox listing image was added and confirmed.", image_body)
        self.assertIn(IMAGE_URL, image_body)

        status, _headers, missing_body = self.request(
            "GET", "/actions/0123456789abcdef0123456789abcdef", cookie=cookie
        )
        self.assertEqual(status, 404)
        self.assertIn("Action not found", missing_body)

    def test_listings_page_shows_the_image_form_for_published_offers_at_the_images_stage(self):
        entry = {
            "sku": SKU,
            "inventory": {"product": {"title": "Widget"}},
            "offer": {
                "offerId": OFFER_ID,
                "status": "PUBLISHED",
                "pricingSummary": {"price": {"currency": "GBP", "value": "9.99"}},
                "availableQuantity": 3,
            },
        }
        body = listings_page([entry], "csrf", "images")
        self.assertIn('action="/images/review"', body)
        self.assertIn('name="image_url"', body)
        self.assertIn('action="/quantities/review"', body)
        self.assertIn('action="/prices/review"', body)

    def test_listings_page_omits_the_image_form_outside_the_images_stage(self):
        entry = {
            "sku": SKU,
            "inventory": {"product": {"title": "Widget"}},
            "offer": {
                "offerId": OFFER_ID,
                "status": "PUBLISHED",
                "pricingSummary": {"price": {"currency": "GBP", "value": "9.99"}},
                "availableQuantity": 3,
            },
        }
        body = listings_page([entry], "csrf", "prices")
        self.assertNotIn('action="/images/review"', body)
        self.assertNotIn('action="/quantities/review"', body)
        self.assertIn('action="/prices/review"', body)


if __name__ == "__main__":
    unittest.main()
