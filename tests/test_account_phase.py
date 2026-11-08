from __future__ import annotations

import io
import re
import tempfile
import unittest
import urllib.parse
from dataclasses import replace
from pathlib import Path

from ebay_app.account import AccountService, AccountSummary
from ebay_app.config import CONNECTION_SCOPES
from ebay_app.ebay_client import EbayError, EbaySandboxClient
from ebay_app.storage import AppDatabase
from ebay_app.views import account_page
from ebay_app.web import WebApplication

from helpers import FakeTransport, json_response, make_config


class AccountServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(
            make_config(Path(self.temp.name)),
            feature_stage="account",
            scopes=CONNECTION_SCOPES,
        )
        self.database = AppDatabase(
            self.config.database_path, self.config.data_encryption_key
        )
        self.addCleanup(self.database.close)

    def verify_grant(self, user_id="internal-sandbox-id"):
        self.database.save_refresh_token(
            "stored-refresh", 9_999_999_999, user_id, new_grant=True
        )
        generation = self.database.connection_generation()
        self.database.mark_connection_verified(generation, user_id)

    def test_returns_exact_public_allowlist_and_discards_raw_profile(self):
        self.verify_grant()
        transport = FakeTransport(
            [
                json_response(
                    200,
                    {
                        "access_token": "fresh-access",
                        "expires_in": 7200,
                        "refresh_token": "rotated-refresh",
                        "refresh_token_expires_in": 100000,
                    },
                ),
                json_response(
                    200,
                    {
                        "userId": "internal-sandbox-id",
                        "username": "PRIVATE-USERNAME-SENTINEL",
                        "accountType": "BUSINESS",
                        "registrationMarketplaceId": "EBAY_GB",
                        "status": "PRIVATE-STATUS-SENTINEL",
                        "businessAccount": {
                            "email": "PRIVATE-EMAIL-SENTINEL",
                            "primaryPhone": {"number": "PRIVATE-PHONE-SENTINEL"},
                        },
                    },
                ),
            ]
        )
        client = EbaySandboxClient(self.config, self.database, transport=transport)
        summary = AccountService(
            client, self.database, now=lambda: 1_700_000_000
        ).retrieve()

        self.assertEqual(
            summary.as_dict(),
            {
                "environment": "Sandbox",
                "account_type": "Business",
                "registration_marketplace": "EBAY_GB",
                "retrieved_at": "2023-11-14 22:13:20 UTC",
            },
        )
        rendered = repr(summary.as_dict())
        for private_value in (
            "internal-sandbox-id",
            "PRIVATE-USERNAME-SENTINEL",
            "PRIVATE-STATUS-SENTINEL",
            "PRIVATE-EMAIL-SENTINEL",
            "PRIVATE-PHONE-SENTINEL",
        ):
            self.assertNotIn(private_value, rendered)
        self.assertEqual([call["method"] for call in transport.calls], ["POST", "GET"])
        self.assertEqual(transport.calls[1]["url"], f"{self.config.identity_base_url}/user/")
        refresh_form = urllib.parse.parse_qs(
            transport.calls[0]["body"].decode("utf-8")
        )
        self.assertEqual(refresh_form["scope"], [CONNECTION_SCOPES[0]])
        self.assertTrue(self.database.connection_status()["verified"])

    def test_unknown_or_malformed_values_are_not_echoed(self):
        self.verify_grant()
        transport = FakeTransport(
            [
                json_response(200, {"access_token": "access", "expires_in": 7200}),
                json_response(
                    200,
                    {
                        "userId": "internal-sandbox-id",
                        "accountType": {"unexpected": "PRIVATE"},
                        "registrationMarketplaceId": "<PRIVATE-MARKUP>",
                    },
                ),
            ]
        )
        summary = AccountService(
            EbaySandboxClient(self.config, self.database, transport=transport),
            self.database,
        ).retrieve()
        self.assertEqual(summary.account_type, "Unavailable")
        self.assertEqual(summary.registration_marketplace, "Unavailable")

    def test_unverified_grant_makes_no_network_request(self):
        self.database.save_refresh_token(
            "stored-refresh", 9_999_999_999, "internal-id", new_grant=True
        )
        transport = FakeTransport()
        service = AccountService(
            EbaySandboxClient(self.config, self.database, transport=transport),
            self.database,
        )
        with self.assertRaises(EbayError) as raised:
            service.retrieve()
        self.assertEqual(raised.exception.code, "CONNECTION_NOT_VERIFIED")
        self.assertEqual(transport.calls, [])

    def test_identity_mismatch_fails_closed_without_returning_profile(self):
        self.verify_grant()
        transport = FakeTransport(
            [
                json_response(200, {"access_token": "access", "expires_in": 7200}),
                json_response(
                    200,
                    {
                        "userId": "different-internal-id",
                        "accountType": "INDIVIDUAL",
                        "registrationMarketplaceId": "EBAY_GB",
                    },
                ),
            ]
        )
        service = AccountService(
            EbaySandboxClient(self.config, self.database, transport=transport),
            self.database,
        )
        with self.assertRaises(EbayError) as raised:
            service.retrieve()
        self.assertEqual(raised.exception.code, "SELLER_MISMATCH")
        self.assertNotIn("different-internal-id", str(raised.exception))

    def test_account_page_title_reflects_a_production_summary_not_sandbox(self):
        rendered = account_page(
            {
                "environment": "Production",
                "account_type": "Business",
                "registration_marketplace": "EBAY_GB",
                "retrieved_at": "2026-11-05 00:00:00 UTC",
            },
            "csrf-token",
        )
        self.assertIn("Production account information", rendered)
        self.assertNotIn("Sandbox account information", rendered)

    def test_a_production_environment_is_reported_in_the_summary_not_sandbox(self):
        self.verify_grant()
        transport = FakeTransport(
            [
                json_response(200, {"access_token": "access", "expires_in": 7200}),
                json_response(
                    200,
                    {
                        "userId": "internal-sandbox-id",
                        "accountType": "BUSINESS",
                        "registrationMarketplaceId": "EBAY_GB",
                    },
                ),
            ]
        )
        summary = AccountService(
            EbaySandboxClient(self.config, self.database, transport=transport),
            self.database,
            environment="Production",
        ).retrieve()
        self.assertEqual(summary.environment, "Production")

    def test_connection_not_verified_message_says_production_not_sandbox(self):
        self.database.save_refresh_token(
            "stored-refresh", 9_999_999_999, "internal-id", new_grant=True
        )
        service = AccountService(
            EbaySandboxClient(self.config, self.database, transport=FakeTransport()),
            self.database,
            environment="Production",
        )
        with self.assertRaises(EbayError) as raised:
            service.retrieve()
        self.assertIn("Production connection", str(raised.exception))
        self.assertNotIn("Sandbox connection", str(raised.exception))


class _FakeEbay:
    def __init__(self):
        self.authorization_calls = 0
        self.mutations = 0

    def authorization_url(self, _session_id):
        self.authorization_calls += 1
        return "https://auth.sandbox.ebay.com/oauth2/authorize?state=test"


class _FakeAccountService:
    def __init__(self):
        self.calls = 0

    def retrieve(self):
        self.calls += 1
        return AccountSummary(
            "Sandbox", "Individual", "EBAY_GB", "2026-07-13 04:30:00 UTC"
        )


class AccountStageWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(
            make_config(Path(self.temp.name)),
            feature_stage="account",
            scopes=CONNECTION_SCOPES,
            runtime_mode="local",
        )
        self.database = AppDatabase(
            self.config.database_path, self.config.data_encryption_key
        )
        self.addCleanup(self.database.close)
        self.database.save_refresh_token(
            "refresh", 9_999_999_999, "internal-id", new_grant=True
        )
        generation = self.database.connection_generation()
        self.database.mark_connection_verified(generation, "internal-id")
        self.ebay = _FakeEbay()
        self.account_service = _FakeAccountService()
        self.app = WebApplication(
            self.config,
            self.database,
            self.ebay,
            account_service=self.account_service,
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
            "CONTENT_TYPE": "application/x-www-form-urlencoded" if method == "POST" else "",
        }
        response = b"".join(self.app(environ, start_response)).decode("utf-8")
        return captured["status"], captured["headers"], response

    @staticmethod
    def hidden(body, name):
        match = re.search(rf'name="{name}" value="([^"]+)"', body)
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
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _headers, body = self.request("GET", "/", cookie=cookie)
        self.assertEqual(status, 200)
        return cookie, self.hidden(body, "csrf_token"), body

    def test_dashboard_and_account_page_expose_only_phase_2(self):
        cookie, _csrf, dashboard = self.login()
        self.assertIn("Phase 2 boundary", dashboard)
        self.assertIn("View Sandbox account information", dashboard)
        self.assertNotIn("View Inventory listings", dashboard)
        self.assertNotIn("internal-id", dashboard)

        status, headers, body = self.request("GET", "/account", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(self.account_service.calls, 1)
        self.assertIn("Live read-only Identity check succeeded", body)
        self.assertIn("Individual", body)
        self.assertIn("EBAY_GB", body)
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_later_routes_remain_locked_and_no_mutation_can_run(self):
        cookie, csrf, _dashboard = self.login()
        for path in ("/listings", "/orders", "/drafts/new", "/actions/abc"):
            status, _headers, body = self.request("GET", path, cookie=cookie)
            self.assertEqual(status, 404)
            self.assertIn("locked", body)
        for path in ("/drafts/review", "/prices/review", "/actions/confirm"):
            status, _headers, body = self.request(
                "POST", path, cookie=cookie, form={"csrf_token": csrf}
            )
            self.assertEqual(status, 404)
            self.assertIn("locked", body)
        self.assertEqual(self.ebay.mutations, 0)

    def test_permanent_local_mode_starts_oauth_and_accepts_its_callback_route(self):
        cookie, csrf, _dashboard = self.login()
        status, _headers, body = self.request(
            "POST", "/connect", cookie=cookie, form={"csrf_token": csrf}
        )
        self.assertEqual(status, 200)
        self.assertIn("Continue to eBay Sandbox", body)
        self.assertEqual(self.ebay.authorization_calls, 1)

        status, headers, _body = self.request(
            "GET",
            "/oauth/callback",
            cookie=cookie,
            query="state=forged&code=forged&error=access_denied",
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")


if __name__ == "__main__":
    unittest.main()
