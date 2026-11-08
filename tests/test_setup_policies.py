from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ebay_app.config import (
    ACCOUNT_SCOPE,
    DRAFT_SCOPES,
    SETUP_SCOPES,
    scopes_for_stage,
)
from ebay_app.ebay_client import EbayError, EbaySandboxClient, HttpResponse, UnsupportedMarketplaceError
from ebay_app.storage import AppDatabase

from helpers import FakeTransport, adopt_test_access_token, json_response, make_config


SELLER_CANARY = "PRIVATE-INTERNAL-SELLER-ID"
REFRESH_CANARY = "PRIVATE-REFRESH-TOKEN"


class ScopesForStageTests(unittest.TestCase):
    def test_setup_stage_returns_setup_scopes(self):
        self.assertEqual(scopes_for_stage("setup"), SETUP_SCOPES)

    def test_setup_scopes_add_account_scope_on_top_of_draft_scopes(self):
        self.assertIn(ACCOUNT_SCOPE, SETUP_SCOPES)
        for scope in DRAFT_SCOPES:
            self.assertIn(scope, SETUP_SCOPES)


class SetupPoliciesClientTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(
            make_config(Path(self.temp.name)),
            feature_stage="setup",
            scopes=SETUP_SCOPES,
            runtime_mode="local",
        )
        self.database = AppDatabase(
            self.config.database_path,
            self.config.data_encryption_key,
        )
        self.addCleanup(self.database.close)

    def grant(self, scopes=SETUP_SCOPES) -> None:
        self.database.save_refresh_token(
            REFRESH_CANARY,
            9_999_999_999,
            SELLER_CANARY,
            new_grant=True,
            granted_scopes=scopes,
        )

    def client(self, responses) -> EbaySandboxClient:
        transport = FakeTransport(responses)
        client = EbaySandboxClient(self.config, self.database, transport=transport)
        adopt_test_access_token(client)
        return client


class SetupMutationGuardTests(SetupPoliciesClientTestCase):
    def test_guard_rejects_wrong_feature_stage(self):
        config = replace(self.config, feature_stage="drafts", scopes=DRAFT_SCOPES)
        client = EbaySandboxClient(config, self.database, transport=FakeTransport([]))
        with self.assertRaises(EbayError) as failure:
            with client.setup_mutation_guard():
                pass
        self.assertEqual(failure.exception.code, "MUTATION_LOCKED")

    def test_guard_rejects_missing_account_scope(self):
        self.grant(scopes=DRAFT_SCOPES)
        client = self.client([])
        with self.assertRaises(EbayError) as failure:
            with client.setup_mutation_guard():
                pass
        self.assertEqual(failure.exception.code, "SETUP_PERMISSION_REQUIRED")

    def test_guard_allows_setup_stage_with_account_scope(self):
        self.grant()
        client = self.client([])
        with client.setup_mutation_guard():
            pass  # no error


class MutationAllowlistTests(SetupPoliciesClientTestCase):
    def test_setup_stage_allows_only_its_five_calls(self):
        self.grant()
        client = self.client([])
        allowed = [
            ("POST", "/sell/account/v1/program/opt_in"),
            ("POST", "/sell/inventory/v1/location/SANDBOX-WAREHOUSE-1"),
            ("POST", "/sell/account/v1/fulfillment_policy"),
            ("POST", "/sell/account/v1/payment_policy"),
            ("POST", "/sell/account/v1/return_policy"),
        ]
        for method, path in allowed:
            client._assert_mutation_allowed(method, path)  # must not raise

    def test_setup_stage_still_locks_phase_five_offer_creation(self):
        self.grant()
        client = self.client([])
        with self.assertRaises(EbayError) as failure:
            client._assert_mutation_allowed("POST", "/sell/inventory/v1/offer")
        self.assertEqual(failure.exception.code, "MUTATION_LOCKED")

    def test_drafts_stage_cannot_reach_setup_mutations(self):
        config = replace(self.config, feature_stage="drafts", scopes=DRAFT_SCOPES)
        client = EbaySandboxClient(config, self.database, transport=FakeTransport([]))
        with self.assertRaises(EbayError) as failure:
            client._assert_mutation_allowed("POST", "/sell/account/v1/fulfillment_policy")
        self.assertEqual(failure.exception.code, "MUTATION_LOCKED")


class SetupApiCallTests(SetupPoliciesClientTestCase):
    def test_opt_in_success(self):
        self.grant()
        client = self.client([HttpResponse(204, {}, b"")])
        client.opt_in_to_selling_policy_management()  # must not raise
        self.assertEqual(client.transport.calls[0]["method"], "POST")
        self.assertTrue(
            client.transport.calls[0]["url"].endswith("/sell/account/v1/program/opt_in")
        )

    def test_create_merchant_location_returns_the_chosen_key(self):
        self.grant()
        client = self.client([HttpResponse(204, {}, b"")])
        key = client.create_merchant_location(
            "SANDBOX-WAREHOUSE-1",
            name="Sandbox Warehouse",
            address_line_1="1 Sandbox Way",
            city="London",
            state_or_province="London",
            postal_code="SW1A 1AA",
            country="GB",
        )
        self.assertEqual(key, "SANDBOX-WAREHOUSE-1")

    def test_get_inventory_location_returns_none_on_404(self):
        self.grant()
        client = self.client(
            [json_response(404, {"errors": [{"errorId": 11800, "message": "Not found"}]})]
        )
        self.assertIsNone(client.get_inventory_location("MISSING-KEY"))

    def test_create_fulfillment_policy_returns_id(self):
        self.grant()
        client = self.client([json_response(201, {"fulfillmentPolicyId": "FP-1"})])
        self.assertEqual(
            client.create_fulfillment_policy(name="Sandbox Standard Shipping"), "FP-1"
        )

    def test_create_fulfillment_policy_uses_a_verified_gb_shipping_service(self):
        self.grant()
        client = self.client([json_response(201, {"fulfillmentPolicyId": "FP-1"})])
        client.create_fulfillment_policy(name="Sandbox Standard Shipping")
        import json as _json

        sent = _json.loads(client.transport.calls[0]["body"])
        service = sent["shippingOptions"][0]["shippingServices"][0]
        self.assertEqual(service["shippingCarrierCode"], "RoyalMail")
        self.assertEqual(service["shippingServiceCode"], "UK_RoyalMailFirstClassStandard")

    def test_create_fulfillment_policy_fails_closed_for_unmapped_marketplace(self):
        self.grant()
        config = replace(self.config, marketplace_id="EBAY_DE")
        client = EbaySandboxClient(config, self.database, transport=FakeTransport([]))
        adopt_test_access_token(client)
        with self.assertRaises(UnsupportedMarketplaceError) as failure:
            client.create_fulfillment_policy(name="Sandbox Standard Shipping")
        self.assertEqual(failure.exception.code, "UNSUPPORTED_MARKETPLACE")
        self.assertEqual(client.transport.calls, [])

    def test_create_payment_policy_returns_id(self):
        self.grant()
        client = self.client([json_response(201, {"paymentPolicyId": "PP-1"})])
        self.assertEqual(
            client.create_payment_policy(name="Sandbox Standard Payment"), "PP-1"
        )

    def test_create_return_policy_returns_id(self):
        self.grant()
        client = self.client([json_response(201, {"returnPolicyId": "RP-1"})])
        self.assertEqual(
            client.create_return_policy(name="Sandbox 30 Day Returns"), "RP-1"
        )

    def test_create_policy_without_id_field_raises(self):
        self.grant()
        client = self.client([json_response(201, {})])
        with self.assertRaises(EbayError) as failure:
            client.create_fulfillment_policy(name="Sandbox Standard Shipping")
        self.assertEqual(failure.exception.code, "INVALID_EBAY_RESPONSE")

    def test_list_fulfillment_policies_reads_the_collection(self):
        self.grant()
        client = self.client(
            [json_response(200, {"fulfillmentPolicies": [{"name": "X"}]})]
        )
        self.assertEqual(client.list_fulfillment_policies(), [{"name": "X"}])


if __name__ == "__main__":
    unittest.main()
