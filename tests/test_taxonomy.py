from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ebay_app.ebay_client import EbayError, EbaySandboxClient, HttpResponse
from ebay_app.storage import AppDatabase

from helpers import FakeTransport, adopt_test_access_token, json_response, make_config


class TaxonomyLookupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = make_config(Path(self.temp.name))
        self.database = AppDatabase(
            self.config.database_path,
            self.config.data_encryption_key,
        )
        self.addCleanup(self.database.close)

    def client(self, responses) -> EbaySandboxClient:
        transport = FakeTransport(responses)
        client = EbaySandboxClient(self.config, self.database, transport=transport)
        adopt_test_access_token(client)
        return client

    def test_get_default_category_tree_id_returns_the_tree_id(self):
        client = self.client([json_response(200, {"access_token": "app-token", "expires_in": 7200}), json_response(200, {"categoryTreeId": "3"})])
        self.assertEqual(client.get_default_category_tree_id(), "3")
        self.assertTrue(
            client.transport.calls[1]["url"].endswith(
                "/commerce/taxonomy/v1/get_default_category_tree_id?marketplace_id=EBAY_GB"
            )
        )

    def test_get_default_category_tree_id_uses_an_application_token_not_the_user_token(self):
        client = self.client([json_response(200, {"access_token": "app-token", "expires_in": 7200}), json_response(200, {"categoryTreeId": "3"})])
        client.get_default_category_tree_id()
        self.assertEqual(client.transport.calls[0]["method"], "POST")
        self.assertTrue(client.transport.calls[0]["url"].endswith("/identity/v1/oauth2/token"))
        self.assertIn(
            "Bearer app-token",
            client.transport.calls[1]["headers"]["Authorization"],
        )

    def test_get_default_category_tree_id_fails_closed_on_missing_field(self):
        client = self.client([json_response(200, {"access_token": "app-token", "expires_in": 7200}), json_response(200, {})])
        with self.assertRaises(EbayError) as failure:
            client.get_default_category_tree_id()
        self.assertEqual(failure.exception.code, "INVALID_EBAY_RESPONSE")

    def test_get_category_suggestions_returns_the_list(self):
        payload = {
            "categorySuggestions": [
                {"category": {"categoryId": "9355", "categoryName": "Mobile Phones"}}
            ]
        }
        client = self.client([json_response(200, {"access_token": "app-token", "expires_in": 7200}), json_response(200, payload)])
        suggestions = client.get_category_suggestions("3", "mobile phone")
        self.assertEqual(suggestions, payload["categorySuggestions"])
        self.assertIn("get_category_suggestions?q=mobile", client.transport.calls[1]["url"].replace("%20", "+"))

    def test_get_category_suggestions_returns_empty_list_when_absent(self):
        client = self.client([json_response(200, {"access_token": "app-token", "expires_in": 7200}), json_response(200, {})])
        self.assertEqual(client.get_category_suggestions("3", "mobile phone"), [])

    def test_get_category_subtree_returns_the_raw_node(self):
        app_token = json_response(200, {"access_token": "app-token", "expires_in": 7200})
        payload = {"categorySubtreeNode": {"category": {"categoryId": "9355"}, "leafCategoryTreeNode": False}}
        client = self.client([app_token, json_response(200, payload)])
        result = client.get_category_subtree("3", "9355")
        self.assertEqual(result, payload)
        self.assertIn("category_id=9355", client.transport.calls[1]["url"])

    def test_get_category_suggestions_is_read_only_and_never_locked(self):
        # Even outside a mutation-permitting stage, a GET must never be gated
        # by the mutation allowlist -- confirms the taxonomy lookup works from
        # any feature stage, since it never writes anything.
        config = replace(self.config, feature_stage="account", scopes=())
        client = EbaySandboxClient(config, self.database, transport=FakeTransport(
            [json_response(200, {"access_token": "app-token", "expires_in": 7200}), json_response(200, {"categoryTreeId": "3"})]
        ))
        adopt_test_access_token(client)
        self.assertEqual(client.get_default_category_tree_id(), "3")

    def test_get_item_aspects_for_category_returns_the_list(self):
        payload = {
            "aspects": [
                {
                    "localizedAspectName": "Storage Capacity",
                    "aspectConstraint": {"aspectRequired": True},
                    "aspectValues": [{"localizedValue": "128 GB"}],
                },
                {
                    "localizedAspectName": "Colour",
                    "aspectConstraint": {"aspectRequired": True},
                    "aspectValues": [{"localizedValue": "Black"}],
                },
            ]
        }
        client = self.client(
            [json_response(200, {"access_token": "app-token", "expires_in": 7200}), json_response(200, payload)]
        )
        aspects = client.get_item_aspects_for_category("3", "9355")
        self.assertEqual(aspects, payload["aspects"])
        self.assertIn(
            "get_item_aspects_for_category?category_id=9355",
            client.transport.calls[1]["url"],
        )

    def test_get_item_aspects_for_category_returns_empty_list_when_absent(self):
        client = self.client(
            [json_response(200, {"access_token": "app-token", "expires_in": 7200}), json_response(200, {})]
        )
        self.assertEqual(client.get_item_aspects_for_category("3", "9355"), [])

    def test_get_item_aspects_for_category_is_read_only_and_never_locked(self):
        config = replace(self.config, feature_stage="account", scopes=())
        client = EbaySandboxClient(config, self.database, transport=FakeTransport(
            [json_response(200, {"access_token": "app-token", "expires_in": 7200}), json_response(200, {"aspects": []})]
        ))
        adopt_test_access_token(client)
        self.assertEqual(client.get_item_aspects_for_category("3", "9355"), [])


if __name__ == "__main__":
    unittest.main()
