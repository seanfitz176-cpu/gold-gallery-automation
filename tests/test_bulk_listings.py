from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ebay_app.bulk_listings import (
    BulkListingService,
    load_and_validate_csv,
)
from ebay_app.config import DRAFT_SCOPES
from ebay_app.ebay_client import EbayError, EbaySandboxClient, HttpResponse
from ebay_app.storage import AppDatabase

from helpers import FakeTransport, adopt_test_access_token, json_response, make_config


def write_csv(tmp_dir: str, content: str) -> Path:
    path = Path(tmp_dir) / "listings.csv"
    path.write_text(content, encoding="utf-8")
    return path


HEADER = "sku,title,description,condition,quantity,price,category_id,aspects\n"


def inventory_item(sku: str, *, title: str, description: str) -> dict:
    return {
        "sku": sku,
        "condition": "NEW",
        "product": {"title": title, "description": description},
        "availability": {"shipToLocationAvailability": {"quantity": 2}},
    }


def unpublished_offer(offer_id: str, sku: str) -> dict:
    return {
        "offerId": offer_id,
        "sku": sku,
        "status": "UNPUBLISHED",
        "marketplaceId": "EBAY_GB",
        "format": "FIXED_PRICE",
        "listingDuration": "GTC",
        "availableQuantity": 2,
        "categoryId": "12345",
        "pricingSummary": {"price": {"currency": "GBP", "value": "19.99"}},
        "merchantLocationKey": "MAIN-LOCATION",
        "listingPolicies": {
            "fulfillmentPolicyId": "FULFILMENT-POLICY",
            "paymentPolicyId": "PAYMENT-POLICY",
            "returnPolicyId": "RETURN-POLICY",
        },
    }


def not_found() -> HttpResponse:
    return json_response(404, {"errors": []})


def item_creation_responses(sku: str, offer_id: str, *, title: str, description: str) -> list:
    """The exact six-call sequence one successfully created+verified item makes."""

    item = inventory_item(sku, title=title, description=description)
    offer = unpublished_offer(offer_id, sku)
    return [
        not_found(),  # create_verified_inventory_item's own existence check
        HttpResponse(204, {}, b""),  # PUT inventory_item
        json_response(200, item),  # verify_inventory_item readback
        json_response(200, {"offers": [], "total": 0}),  # list_offers precheck
        json_response(201, {"offerId": offer_id}),  # POST offer
        json_response(200, offer),  # verify_unpublished_offer readback
    ]


class BulkListingsCsvTests(unittest.TestCase):
    def test_valid_rows_parse_with_no_network_access_at_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_csv(
                tmp,
                HEADER
                + 'SKU-1,Item one,Description one,NEW,2,19.99,12345,\n'
                + 'SKU-2,Item two,Description two,USED_EXCELLENT,1,5.00,54321,"{""Brand"":[""Acme""]}"\n',
            )
            batch = load_and_validate_csv(path, "GBP")
        self.assertEqual(len(batch.drafts), 2)
        self.assertEqual(batch.row_errors, ())
        row_number, first = batch.drafts[0]
        self.assertEqual(row_number, 2)
        self.assertEqual(first["sku"], "SKU-1")
        self.assertEqual(first["currency"], "GBP")
        self.assertEqual(first["aspects"], {})
        _row_number, second = batch.drafts[1]
        self.assertEqual(second["aspects"], {"Brand": ["Acme"]})

    def test_missing_required_column_raises_before_any_row_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_csv(tmp, "sku,title\nSKU-1,Item one\n")
            with self.assertRaises(EbayError) as raised:
                load_and_validate_csv(path, "GBP")
        self.assertEqual(raised.exception.code, "BULK_LISTINGS_MISSING_COLUMNS")

    def test_one_bad_row_is_collected_and_does_not_block_the_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_csv(
                tmp,
                HEADER
                + 'SKU-1,Item one,Description one,NEW,2,19.99,12345,\n'
                + 'SKU-2,,Description two,NEW,1,5.00,54321,\n'  # missing title
                + 'SKU-3,Item three,Description three,NOT_A_REAL_CONDITION,1,5.00,54321,\n'
                + 'SKU-4,Item four,Description four,NEW,2,19.99,12345,\n',
            )
            batch = load_and_validate_csv(path, "GBP")
        self.assertEqual(len(batch.drafts), 2)
        self.assertEqual({sku for _n, draft in batch.drafts for sku in [draft["sku"]]}, {"SKU-1", "SKU-4"})
        self.assertEqual(len(batch.row_errors), 2)
        self.assertEqual(batch.row_errors[0].row_number, 3)
        self.assertIn("title", batch.row_errors[0].errors)
        self.assertEqual(batch.row_errors[1].row_number, 4)
        self.assertIn("condition", batch.row_errors[1].errors)

    def test_duplicate_sku_within_the_file_is_rejected_on_the_second_occurrence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_csv(
                tmp,
                HEADER
                + 'SKU-1,Item one,Description one,NEW,2,19.99,12345,\n'
                + 'SKU-1,Item one again,Description one again,NEW,2,19.99,12345,\n',
            )
            batch = load_and_validate_csv(path, "GBP")
        self.assertEqual(len(batch.drafts), 1)
        self.assertEqual(len(batch.row_errors), 1)
        self.assertIn("sku", batch.row_errors[0].errors)

    def test_malformed_aspects_json_is_a_row_error_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_csv(
                tmp,
                HEADER + 'SKU-1,Item one,Description one,NEW,2,19.99,12345,not-json\n',
            )
            batch = load_and_validate_csv(path, "GBP")
        self.assertEqual(batch.drafts, ())
        self.assertEqual(len(batch.row_errors), 1)
        self.assertIn("aspects", batch.row_errors[0].errors)


class BulkListingServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(
            make_config(Path(self.temp.name)),
            feature_stage="drafts",
            scopes=DRAFT_SCOPES,
            runtime_mode="local",
            merchant_location_key="MAIN-LOCATION",
            fulfillment_policy_id="FULFILMENT-POLICY",
            payment_policy_id="PAYMENT-POLICY",
            return_policy_id="RETURN-POLICY",
        )
        self.database = AppDatabase(
            self.config.database_path, self.config.data_encryption_key
        )
        self.addCleanup(self.database.close)

    def grant(self) -> None:
        self.database.save_refresh_token(
            "stored-refresh",
            9_999_999_999,
            "internal-sandbox-user",
            new_grant=True,
            granted_scopes=DRAFT_SCOPES,
        )
        generation = self.database.connection_generation()
        self.database.mark_connection_verified(generation, "internal-sandbox-user")

    def make_client(self, responses: list) -> EbaySandboxClient:
        transport = FakeTransport(responses)
        client = EbaySandboxClient(self.config, self.database, transport=transport)
        adopt_test_access_token(client)
        return client

    def draft(self, sku: str, **overrides) -> dict:
        base = {
            "sku": sku,
            "title": "Example item",
            "description": "Example description",
            "condition": "NEW",
            "quantity": 2,
            "price": "19.99",
            "currency": "GBP",
            "category_id": "12345",
            "aspects": {},
        }
        base.update(overrides)
        return base


class BulkListingGateTests(BulkListingServiceTestCase):
    def test_wrong_stage_and_missing_or_unverified_grant_are_all_rejected(self):
        client = self.make_client([])
        service = BulkListingService(client, self.database, replace(self.config, feature_stage="orders"))
        with self.assertRaises(EbayError) as wrong_stage:
            service.verified_binding()
        self.assertEqual(wrong_stage.exception.code, "DRAFT_STAGE_LOCKED")

        service = BulkListingService(client, self.database, self.config)
        with self.assertRaises(EbayError) as missing_scope:
            service.verified_binding()
        self.assertEqual(missing_scope.exception.code, "DRAFT_PERMISSION_REQUIRED")

        self.database.save_refresh_token(
            "unverified-refresh",
            9_999_999_999,
            "internal-sandbox-user",
            new_grant=True,
            granted_scopes=DRAFT_SCOPES,
        )
        with self.assertRaises(EbayError) as unverified:
            service.verified_binding()
        self.assertEqual(unverified.exception.code, "CONNECTION_NOT_VERIFIED")
        self.assertEqual(client.transport.calls, [])


class BulkListingBatchTests(BulkListingServiceTestCase):
    def test_every_row_is_created_and_independently_verified_in_order(self):
        self.grant()
        responses = item_creation_responses(
            "SKU-1", "OFFER-1", title="Example item", description="Example description"
        ) + item_creation_responses(
            "SKU-2", "OFFER-2", title="Example item", description="Example description"
        )
        client = self.make_client(responses)
        service = BulkListingService(client, self.database, self.config)
        binding = service.verified_binding()

        results = service.create_batch(
            binding, [(2, self.draft("SKU-1")), (3, self.draft("SKU-2"))]
        )

        self.assertEqual([r.status for r in results], ["created", "created"])
        self.assertEqual([r.offer_id for r in results], ["OFFER-1", "OFFER-2"])
        self.assertEqual([r.row_number for r in results], [2, 3])
        self.assertEqual(len(client.transport.calls), 12)

    def test_an_existing_sku_is_skipped_without_touching_eBay_and_the_batch_continues(self):
        self.grant()
        responses = [
            json_response(200, inventory_item("SKU-1", title="Already there", description="x")),
        ] + item_creation_responses(
            "SKU-2", "OFFER-2", title="Example item", description="Example description"
        )
        client = self.make_client(responses)
        service = BulkListingService(client, self.database, self.config)
        binding = service.verified_binding()

        results = service.create_batch(
            binding, [(2, self.draft("SKU-1")), (3, self.draft("SKU-2"))]
        )

        self.assertEqual(results[0].status, "skipped_already_exists")
        self.assertIsNone(results[0].offer_id)
        self.assertEqual(results[1].status, "created")
        self.assertEqual(results[1].offer_id, "OFFER-2")

    def test_a_partial_row_is_reported_distinctly_from_a_plain_failure(self):
        """Inventory item created, but the Offer write itself is cleanly rejected.

        This must be reported as "partial", never folded into "failed" --
        the SKU now exists on the account with no Offer, and a future run
        of this script will treat that SKU as already handled rather than
        retrying it.
        """

        self.grant()
        responses = [
            not_found(),
            HttpResponse(204, {}, b""),
            json_response(
                200,
                inventory_item("SKU-1", title="Example item", description="Example description"),
            ),
            json_response(200, {"offers": [], "total": 0}),
            json_response(
                400,
                {"errors": [{"errorId": 1, "message": "Category not valid for this site"}]},
            ),
        ] + item_creation_responses(
            "SKU-2", "OFFER-2", title="Example item", description="Example description"
        )
        client = self.make_client(responses)
        service = BulkListingService(client, self.database, self.config)
        binding = service.verified_binding()

        results = service.create_batch(
            binding, [(2, self.draft("SKU-1")), (3, self.draft("SKU-2"))]
        )

        self.assertEqual(results[0].status, "partial")
        self.assertIsNone(results[0].offer_id)
        self.assertIn("Inventory item was created but the Offer was not", results[0].detail)
        self.assertEqual(results[1].status, "created")
        self.assertEqual(results[1].offer_id, "OFFER-2")

    def test_an_unknown_row_is_reported_distinctly_from_a_plain_failure(self):
        """The Inventory item readback does not match what was just written.

        ``verify_inventory_item`` always marks this outcome_unknown (it
        cannot tell whether eBay's own data is simply inconsistent or the
        write itself went wrong), so this must be reported as "unknown",
        never "partial" or "failed" -- the caller is told to check eBay
        directly rather than assume either way.
        """

        self.grant()
        responses = [
            not_found(),
            HttpResponse(204, {}, b""),
            json_response(
                200,
                inventory_item("SKU-1", title="Some Other Title", description="Example description"),
            ),
        ] + item_creation_responses(
            "SKU-2", "OFFER-2", title="Example item", description="Example description"
        )
        client = self.make_client(responses)
        service = BulkListingService(client, self.database, self.config)
        binding = service.verified_binding()

        results = service.create_batch(
            binding, [(2, self.draft("SKU-1")), (3, self.draft("SKU-2"))]
        )

        self.assertEqual(results[0].status, "unknown")
        self.assertIsNone(results[0].offer_id)
        self.assertIn("could not be confirmed", results[0].detail)
        self.assertEqual(results[1].status, "created")
        self.assertEqual(results[1].offer_id, "OFFER-2")

    def test_a_failed_row_is_reported_with_full_detail_and_the_batch_continues(self):
        self.grant()
        responses = [
            not_found(),
            json_response(
                400,
                {"errors": [{"errorId": 12345, "message": "Category not valid for this site"}]},
            ),
        ] + item_creation_responses(
            "SKU-2", "OFFER-2", title="Example item", description="Example description"
        )
        client = self.make_client(responses)
        service = BulkListingService(client, self.database, self.config)
        binding = service.verified_binding()

        results = service.create_batch(
            binding, [(2, self.draft("SKU-1")), (3, self.draft("SKU-2"))]
        )

        self.assertEqual(results[0].status, "failed")
        self.assertIsNone(results[0].offer_id)
        self.assertIn("status=400", results[0].detail)
        self.assertIn("code=EBAY_API_ERROR", results[0].detail)
        self.assertEqual(results[1].status, "created")
        self.assertEqual(results[1].offer_id, "OFFER-2")


if __name__ == "__main__":
    unittest.main()


IMAGE_HEADER = "sku,title,description,condition,quantity,price,category_id,aspects,image_urls\n"
IMAGE_A = "https://images.example.com/a.jpg"
IMAGE_B = "https://images.example.com/b.jpg"


class BulkListingImageCsvTests(unittest.TestCase):
    def load(self, image_cell: str):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_csv(
                tmp,
                IMAGE_HEADER + f'SKU-1,Item one,Description one,NEW,2,19.99,12345,,"{image_cell}"\n',
            )
            return load_and_validate_csv(path, "GBP")

    def test_pipe_separated_images_are_parsed_in_order(self):
        batch = self.load(f"{IMAGE_A} | {IMAGE_B}")
        self.assertEqual(batch.row_errors, ())
        self.assertEqual(batch.drafts[0][1]["image_urls"], [IMAGE_A, IMAGE_B])

    def test_an_empty_image_cell_or_missing_column_means_no_images(self):
        batch = self.load("")
        self.assertNotIn("image_urls", batch.drafts[0][1])
        with tempfile.TemporaryDirectory() as tmp:
            path = write_csv(tmp, HEADER + "SKU-1,Item one,Description one,NEW,2,19.99,12345,\n")
            batch = load_and_validate_csv(path, "GBP")
        self.assertNotIn("image_urls", batch.drafts[0][1])

    def test_invalid_image_cells_are_row_errors_not_crashes(self):
        too_many = "|".join(f"https://images.example.com/{n}.jpg" for n in range(25))
        for cell in (
            "http://images.example.com/a.jpg",  # not https
            "https://user:pw@images.example.com/a.jpg",  # embedded credentials
            "https://images.example.com/a.jpg#frag",  # fragment
            "https://localhost/a.jpg",
            "https://127.0.0.1/a.jpg",
            "https://192.168.1.10/a.jpg",
            f"{IMAGE_A}|{IMAGE_A}",  # duplicate
            f"{IMAGE_A}||{IMAGE_B}",  # empty entry
            "https://images.example.com/a b.jpg",  # whitespace
            too_many,
            "not a url",
        ):
            with self.subTest(cell=cell):
                batch = self.load(cell)
                self.assertEqual(batch.drafts, ())
                self.assertIn("image_urls", batch.row_errors[0].errors)

    def test_image_and_draft_errors_on_one_row_are_reported_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_csv(
                tmp, IMAGE_HEADER + "SKU-1,Item one,Description one,NEW,0,19.99,12345,,http://x.example/a.jpg\n"
            )
            batch = load_and_validate_csv(path, "GBP")
        self.assertEqual(set(batch.row_errors[0].errors), {"quantity", "image_urls"})


class BulkListingImageBatchTests(BulkListingServiceTestCase):
    def test_images_are_sent_on_create_and_reconciled_on_readback(self):
        import json as _json

        self.grant()
        responses = item_creation_responses(
            "SKU-1", "OFFER-1", title="Example item", description="Example description"
        )
        responses[2] = json_response(
            200,
            {
                **inventory_item("SKU-1", title="Example item", description="Example description"),
                "product": {
                    "title": "Example item",
                    "description": "Example description",
                    "imageUrls": [IMAGE_A, IMAGE_B],
                },
            },
        )
        client = self.make_client(responses)
        service = BulkListingService(client, self.database, self.config)
        results = service.create_batch(
            service.verified_binding(), [(2, self.draft("SKU-1", image_urls=[IMAGE_A, IMAGE_B]))]
        )

        self.assertEqual([r.status for r in results], ["created"])
        put = client.transport.calls[1]
        self.assertEqual(put["method"], "PUT")
        self.assertEqual(_json.loads(put["body"])["product"]["imageUrls"], [IMAGE_A, IMAGE_B])

    def test_a_readback_with_missing_or_reordered_images_is_unknown_not_created(self):
        self.grant()
        for returned in ([], [IMAGE_B, IMAGE_A], None):
            with self.subTest(returned=returned):
                product = {"title": "Example item", "description": "Example description"}
                if returned is not None:
                    product["imageUrls"] = returned
                client = self.make_client(
                    [
                        not_found(),
                        HttpResponse(204, {}, b""),
                        json_response(
                            200,
                            {**inventory_item("SKU-1", title="x", description="x"), "product": product},
                        ),
                    ]
                )
                service = BulkListingService(client, self.database, self.config)
                results = service.create_batch(
                    service.verified_binding(),
                    [(2, self.draft("SKU-1", image_urls=[IMAGE_A, IMAGE_B]))],
                )
                self.assertEqual(results[0].status, "unknown")

    def test_a_draft_without_images_sends_no_imageUrls_field(self):
        import json as _json

        self.grant()
        client = self.make_client(
            item_creation_responses("SKU-1", "OFFER-1", title="Example item", description="Example description")
        )
        service = BulkListingService(client, self.database, self.config)
        service.create_batch(service.verified_binding(), [(2, self.draft("SKU-1"))])
        self.assertNotIn("imageUrls", _json.loads(client.transport.calls[1]["body"])["product"])
