from __future__ import annotations

import io
import re
import tempfile
import unittest
import urllib.parse
from dataclasses import replace
from pathlib import Path

from ebay_app.storage import AppDatabase
from ebay_app.web import WebApplication

from helpers import make_config


class FakeEbay:
    def __init__(self):
        self.mutations = 0
        self.oauth_completions = 0
        self.after_inventory_read = None

    def get_inventory_item(self, _sku):
        if self.after_inventory_read:
            self.after_inventory_read()
        return None

    def create_draft(self, payload, *, expected_generation):
        if not expected_generation:
            raise AssertionError("Missing account generation")
        self.mutations += 1
        return {"sku": payload["sku"], "offer_id": "OFFER-1", "status": "UNPUBLISHED"}

    def get_offer(self, _offer_id):
        return {
            "offerId": "OFFER-1",
            "sku": "SKU-1",
            "status": "PUBLISHED",
            "pricingSummary": {"price": {"currency": "GBP", "value": "19.99"}},
        }

    @staticmethod
    def offer_price(offer):
        return offer["pricingSummary"]["price"]

    def change_price(self, **payload):
        if not payload.pop("expected_generation", None):
            raise AssertionError("Missing account generation")
        self.mutations += 1
        return {"offer_id": payload["offer_id"], "price": payload["new_price"]}

    def authorization_url(self, _session_id):
        return "https://auth.sandbox.ebay.com/oauth2/authorize?state=test"

    def complete_authorization(self, _state, _code, _session_id):
        self.oauth_completions += 1
        return {"user_id": "sandbox-user"}


class WebConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(make_config(Path(self.temp.name)), runtime_mode="local")
        self.database = AppDatabase(self.config.database_path, self.config.data_encryption_key)
        self.addCleanup(self.database.close)
        self.database.save_refresh_token(
            "refresh-token", 9999999999, "sandbox-user", new_grant=True
        )
        self.ebay = FakeEbay()
        self.app = WebApplication(self.config, self.database, self.ebay)

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
            "CONTENT_TYPE": "application/x-www-form-urlencoded" if method == "POST" else "",
        }
        response_body = b"".join(self.app(environ, start_response)).decode("utf-8")
        return captured["status"], captured["headers"], response_body

    @staticmethod
    def cookie(headers):
        return headers["Set-Cookie"].split(";", 1)[0]

    @staticmethod
    def hidden(body, name):
        match = re.search(rf'name="{re.escape(name)}" value="([^"]+)"', body)
        if not match:
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
        return cookie, self.hidden(body, "csrf_token")

    def test_preview_makes_no_change_and_confirmation_is_one_use(self):
        cookie, csrf = self.login()
        draft_form = {
            "csrf_token": csrf,
            "sku": "SKU-1",
            "title": "Example",
            "description": "Description",
            "condition": "NEW",
            "quantity": "2",
            "price": "19.99",
            "category_id": "12345",
            "aspects": "",
        }
        status, _headers, body = self.request(
            "POST", "/drafts/review", cookie=cookie, form=draft_form
        )
        self.assertEqual(status, 200)
        self.assertIn("No eBay change has been made yet", body)
        self.assertEqual(self.ebay.mutations, 0)
        action_token = self.hidden(body, "action_token")

        status, headers, _body = self.request(
            "POST",
            "/actions/confirm",
            cookie=cookie,
            form={"csrf_token": csrf, "action_token": action_token},
        )
        self.assertEqual(status, 303)
        self.assertEqual(self.ebay.mutations, 1)
        self.assertRegex(headers["Location"], r"^/actions/[a-f0-9]{32}$")

        status, _headers, body = self.request(
            "POST",
            "/actions/confirm",
            cookie=cookie,
            form={"csrf_token": csrf, "action_token": action_token},
        )
        self.assertEqual(status, 409)
        self.assertEqual(self.ebay.mutations, 1)
        self.assertIn("already used", body)

    def test_mutating_form_without_csrf_is_rejected(self):
        cookie, _csrf = self.login()
        status, _headers, _body = self.request(
            "POST",
            "/drafts/review",
            cookie=cookie,
            form={"csrf_token": "wrong"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.ebay.mutations, 0)

    def test_connection_change_during_review_creates_no_confirmation(self):
        cookie, csrf = self.login()
        self.ebay.after_inventory_read = lambda: self.database.save_refresh_token(
            "replacement", 9999999999, "sandbox-user", new_grant=True
        )
        status, _headers, body = self.request(
            "POST",
            "/drafts/review",
            cookie=cookie,
            form={
                "csrf_token": csrf,
                "sku": "SKU-1",
                "title": "Example",
                "description": "Description",
                "condition": "NEW",
                "quantity": "2",
                "price": "19.99",
                "category_id": "12345",
                "aspects": "",
            },
        )
        self.assertEqual(status, 409)
        self.assertIn("connection changed during review", body)
        self.assertEqual(self.database.recent_actions(), [])
        self.assertEqual(self.ebay.mutations, 0)

    def test_price_review_is_read_only_until_separate_confirmation(self):
        cookie, csrf = self.login()
        status, _headers, body = self.request(
            "POST",
            "/prices/review",
            cookie=cookie,
            form={
                "csrf_token": csrf,
                "offer_id": "OFFER-1",
                "sku": "SKU-1",
                "new_price": "24.99",
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("No price change has been made yet", body)
        self.assertEqual(self.ebay.mutations, 0)
        token = self.hidden(body, "action_token")

        status, _headers, _body = self.request(
            "POST",
            "/actions/confirm",
            cookie=cookie,
            form={"csrf_token": csrf, "action_token": token},
        )
        self.assertEqual(status, 303)
        self.assertEqual(self.ebay.mutations, 1)

    def test_malformed_query_is_a_controlled_bad_request(self):
        query = "&".join(f"field{index}=x" for index in range(31))
        status, _headers, body = self.request("GET", "/login", query=query)
        self.assertEqual(status, 400)
        self.assertIn("Invalid query string", body)

    def test_oauth_callback_requires_the_authenticated_browser_session(self):
        status, headers, _body = self.request("GET", "/login")
        self.assertEqual(status, 200)
        cookie = self.cookie(headers)
        status, headers, _body = self.request(
            "GET",
            "/oauth/callback",
            cookie=cookie,
            query="state=state&code=code",
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        self.assertEqual(self.ebay.oauth_completions, 0)

    def test_authenticated_connect_and_callback_use_the_same_session(self):
        cookie, csrf = self.login()
        status, _headers, body = self.request(
            "POST",
            "/connect",
            cookie=cookie,
            form={"csrf_token": csrf},
        )
        self.assertEqual(status, 200)
        self.assertIn("Continue to eBay Sandbox", body)
        self.assertIn("https://auth.sandbox.ebay.com/oauth2/authorize?state=test", body)

        status, headers, _body = self.request(
            "GET",
            "/oauth/callback",
            cookie=cookie,
            query="state=state&code=code",
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        self.assertEqual(self.ebay.oauth_completions, 1)


if __name__ == "__main__":
    unittest.main()
