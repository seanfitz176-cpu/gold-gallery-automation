from __future__ import annotations

import io
import re
import tempfile
import unittest
import urllib.parse
from dataclasses import replace
from pathlib import Path

from ebay_app.config import CONNECTION_SCOPES, DRAFT_SCOPES
from ebay_app.connection import ConnectionCheckResult, ConnectionVerifier
from ebay_app.ebay_client import EbayError, EbaySandboxClient, NotConnectedError
from ebay_app.storage import AppDatabase
from ebay_app.web import WebApplication

from helpers import FakeTransport, json_response, make_config


class ConnectionVerifierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = replace(
            make_config(root),
            feature_stage="connection",
            scopes=CONNECTION_SCOPES,
        )
        self.database = AppDatabase(self.config.database_path, self.config.data_encryption_key)
        self.addCleanup(self.database.close)

    def test_authorization_url_requests_only_identity_and_contains_no_secret(self):
        client = EbaySandboxClient(self.config, self.database, transport=FakeTransport())
        url = client.authorization_url("browser-session")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(query["scope"], [CONNECTION_SCOPES[0]])
        self.assertNotIn(self.config.client_secret, url)

    def test_verify_forces_refresh_then_identity_and_returns_allowlisted_result(self):
        self.database.save_refresh_token(
            "stored-refresh", 9999999999, "sandbox-user", new_grant=True
        )
        transport = FakeTransport(
            [
                json_response(200, {"access_token": "fresh-access", "expires_in": 7200}),
                json_response(200, {"userId": "sandbox-user", "email": "not-returned"}),
            ]
        )
        client = EbaySandboxClient(self.config, self.database, transport=transport)
        result = ConnectionVerifier(client, self.database).verify().as_dict()

        self.assertEqual(
            set(result),
            {"environment", "oauth", "identity_read", "checked_at"},
        )
        self.assertEqual(result["environment"], "sandbox")
        self.assertNotIn("sandbox-user", str(result))
        self.assertEqual([call["method"] for call in transport.calls], ["POST", "GET"])
        self.assertEqual(transport.calls[0]["url"], self.config.token_url)
        self.assertEqual(
            transport.calls[1]["url"], f"{self.config.identity_base_url}/user/"
        )
        self.assertTrue(self.database.connection_status()["verified"])

    def test_verify_without_grant_makes_no_network_request(self):
        transport = FakeTransport()
        verifier = ConnectionVerifier(
            EbaySandboxClient(self.config, self.database, transport=transport),
            self.database,
        )
        with self.assertRaises(NotConnectedError):
            verifier.verify()
        self.assertEqual(transport.calls, [])

    def test_invalid_grant_clears_authorization_and_never_marks_verified(self):
        self.database.save_refresh_token(
            "revoked-refresh", 9999999999, "sandbox-user", new_grant=True
        )
        transport = FakeTransport(
            [json_response(400, {"error": "invalid_grant", "error_description": "revoked"})]
        )
        verifier = ConnectionVerifier(
            EbaySandboxClient(self.config, self.database, transport=transport),
            self.database,
        )
        with self.assertRaises(EbayError) as raised:
            verifier.verify()
        self.assertEqual(raised.exception.code, "EBAY_REAUTH_REQUIRED")
        self.assertFalse(self.database.connection_status()["connected"])

    def test_invalid_client_diagnostic_does_not_echo_credentials(self):
        self.database.save_refresh_token(
            "stored-refresh", 9999999999, "sandbox-user", new_grant=True
        )
        transport = FakeTransport([json_response(401, {"error": "invalid_client"})])
        verifier = ConnectionVerifier(
            EbaySandboxClient(self.config, self.database, transport=transport),
            self.database,
        )
        with self.assertRaises(EbayError) as raised:
            verifier.verify()
        self.assertEqual(raised.exception.code, "EBAY_INVALID_CLIENT")
        self.assertNotIn(self.config.client_secret, str(raised.exception))
        self.assertFalse(self.database.connection_status()["verified"])

    def test_invalid_oauth_request_identifies_runame_keyset_check_safely(self):
        transport = FakeTransport([json_response(400, {"error": "invalid_request"})])
        client = EbaySandboxClient(self.config, self.database, transport=transport)
        with self.assertRaises(EbayError) as raised:
            client._token_request(
                {
                    "grant_type": "authorization_code",
                    "code": "short-lived-code",
                    "redirect_uri": self.config.runame,
                }
            )
        self.assertEqual(raised.exception.code, "EBAY_INVALID_OAUTH_REQUEST")
        self.assertIn("RuName", str(raised.exception))
        self.assertNotIn(self.config.client_secret, str(raised.exception))

    def test_missing_identity_user_never_marks_connection_verified(self):
        self.database.save_refresh_token(
            "stored-refresh", 9999999999, "sandbox-user", new_grant=True
        )
        transport = FakeTransport(
            [
                json_response(200, {"access_token": "fresh-access", "expires_in": 7200}),
                json_response(200, {"registrationMarketplaceId": "EBAY_GB"}),
            ]
        )
        verifier = ConnectionVerifier(
            EbaySandboxClient(self.config, self.database, transport=transport),
            self.database,
        )
        with self.assertRaises(EbayError) as raised:
            verifier.verify()
        self.assertEqual(raised.exception.code, "INVALID_IDENTITY_RESPONSE")
        self.assertFalse(self.database.connection_status()["verified"])


class _FakeEbay:
    def __init__(self) -> None:
        self.mutations = 0
        self.authorization_calls = 0

    def authorization_url(self, _session_id: str) -> str:
        self.authorization_calls += 1
        return "https://auth.sandbox.ebay.com/oauth2/authorize?state=test"


class _FakeVerifier:
    def __init__(self) -> None:
        self.calls = 0

    def verify(self) -> ConnectionCheckResult:
        self.calls += 1
        return ConnectionCheckResult("sandbox", "valid", "successful", "2026-01-01T00:00:00Z")


class ConnectionStageWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(
            make_config(Path(self.temp.name)),
            feature_stage="connection",
            scopes=CONNECTION_SCOPES,
            runtime_mode="local",
        )
        self.database = AppDatabase(self.config.database_path, self.config.data_encryption_key)
        self.addCleanup(self.database.close)
        self.ebay = _FakeEbay()
        self.verifier = _FakeVerifier()
        self.app = WebApplication(
            self.config,
            self.database,
            self.ebay,
            connection_verifier=self.verifier,
        )

    def request(self, method, path, *, cookie="", form=None):
        body = urllib.parse.urlencode(form or {}).encode("utf-8")
        captured = {}

        def start_response(status, headers):
            captured["status"] = int(status.split(" ", 1)[0])
            captured["headers"] = dict(headers)

        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": "",
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
        return cookie, self.hidden(body, "csrf_token"), body

    def test_dashboard_and_direct_routes_keep_later_features_locked(self):
        cookie, csrf, dashboard = self.login()
        self.assertIn("Phase 1 boundary", dashboard)
        self.assertNotIn("View Inventory listings", dashboard)
        self.assertNotIn("New unpublished draft", dashboard)

        status, _headers, body = self.request("GET", "/listings", cookie=cookie)
        self.assertEqual(status, 404)
        self.assertIn("locked until", body)
        status, _headers, _body = self.request(
            "POST",
            "/actions/confirm",
            cookie=cookie,
            form={"csrf_token": csrf, "action_token": "anything"},
        )
        self.assertEqual(status, 404)
        self.assertEqual(self.ebay.mutations, 0)

    def test_connect_uses_an_explicit_sandbox_handoff_page(self):
        cookie, csrf, _dashboard = self.login()
        status, headers, body = self.request(
            "POST",
            "/connect",
            cookie=cookie,
            form={"csrf_token": csrf},
        )
        self.assertEqual(status, 200)
        self.assertNotIn("Location", headers)
        self.assertIn("Continue to eBay Sandbox", body)
        self.assertIn("https://auth.sandbox.ebay.com/oauth2/authorize?state=test", body)
        self.assertNotIn(self.config.client_secret, body)
        self.assertNotIn("<script", body.lower())
        self.assertNotIn("http-equiv", body.lower())
        self.assertEqual(self.ebay.authorization_calls, 1)

    def test_connect_rejects_missing_session_bad_csrf_and_non_ebay_destination(self):
        status, headers, _body = self.request("POST", "/connect", form={})
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/login")
        self.assertEqual(self.ebay.authorization_calls, 0)

        cookie, _csrf, _dashboard = self.login()
        status, _headers, _body = self.request(
            "POST",
            "/connect",
            cookie=cookie,
            form={"csrf_token": "wrong"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.ebay.authorization_calls, 0)

        self.ebay.authorization_url = lambda _session_id: "https://example.com/oauth2/authorize"
        status, _headers, body = self.request(
            "POST",
            "/connect",
            cookie=cookie,
            form={"csrf_token": self.hidden(_dashboard, "csrf_token")},
        )
        self.assertEqual(status, 502)
        self.assertNotIn("https://example.com", body)

    def test_public_privacy_page_and_health_do_not_claim_api_readiness(self):
        status, _headers, body = self.request("GET", "/privacy")
        self.assertEqual(status, 200)
        self.assertIn("Privacy notice", body)
        self.assertNotIn(self.config.client_secret, body)

        status, _headers, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertIn('"status":"alive"', body)
        self.assertNotIn("verified", body)

    def test_verify_requires_login_and_csrf(self):
        status, _headers, _body = self.request("POST", "/connection/verify", form={})
        self.assertEqual(status, 303)
        self.assertEqual(self.verifier.calls, 0)

        cookie, csrf, _dashboard = self.login()
        status, _headers, _body = self.request(
            "POST",
            "/connection/verify",
            cookie=cookie,
            form={"csrf_token": "wrong"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.verifier.calls, 0)

        status, headers, _body = self.request(
            "POST",
            "/connection/verify",
            cookie=cookie,
            form={"csrf_token": csrf},
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        self.assertEqual(self.verifier.calls, 1)


class MigrationStageWebTests(unittest.TestCase):
    """Regression test for a real bug found 2026-11-06: ``stage_routes`` in
    web.py had no entry at all for the new ``migration`` stage, so every
    route -- including the dashboard root and ``/connect`` -- was locked,
    with no way to even sign in or reconnect OAuth for the write scope
    migration needs. Fixed by giving ``migration`` the same minimal route
    set ``setup`` already has: migration's one real write happens entirely
    through the standalone migrate_listing.py script, never through a
    dashboard route, so nothing more than sign-in/connect/verify/logout is
    ever needed here."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(
            make_config(Path(self.temp.name)),
            feature_stage="migration",
            scopes=DRAFT_SCOPES,
            runtime_mode="local",
        )
        self.database = AppDatabase(self.config.database_path, self.config.data_encryption_key)
        self.addCleanup(self.database.close)
        self.ebay = _FakeEbay()
        self.verifier = _FakeVerifier()
        self.app = WebApplication(
            self.config,
            self.database,
            self.ebay,
            connection_verifier=self.verifier,
        )

    def request(self, method, path, *, cookie="", form=None):
        body = urllib.parse.urlencode(form or {}).encode("utf-8")
        captured = {}

        def start_response(status, headers):
            captured["status"] = int(status.split(" ", 1)[0])
            captured["headers"] = dict(headers)

        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": "",
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
        return cookie

    def test_dashboard_and_connect_are_reachable_in_the_migration_stage(self):
        cookie = self.login()
        status, _headers, dashboard = self.request("GET", "/", cookie=cookie)
        self.assertEqual(status, 200)
        csrf = self.hidden(dashboard, "csrf_token")

        # /connect is a POST-only route (CSRF-protected form submission),
        # same as every other stage -- see
        # test_connect_uses_an_explicit_sandbox_handoff_page above.
        status, _headers, body = self.request(
            "POST", "/connect", cookie=cookie, form={"csrf_token": csrf}
        )
        self.assertEqual(status, 200)
        self.assertNotIn("locked until", body)
        self.assertIn("Continue to eBay Sandbox", body)

    def test_dashboard_shows_the_migration_boundary_not_the_generic_fallback(self):
        """Regression test for a real bug found 2026-11-06: ``dashboard_page()``
        in views.py did not recognise ``migration`` in its own top-level
        stage set (separate from the ``stage_routes`` bug above), so it fell
        through to the generic full-access fallback card -- showing links to
        ``/listings``, ``/orders`` and ``/drafts/new`` that migration's own
        ``stage_routes`` entry locks, and giving no path at all to the
        permission-upgrade button migration actually needs."""
        cookie = self.login()
        status, _headers, dashboard = self.request("GET", "/", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Listing migration", dashboard)
        self.assertIn("migrate_listing.py", dashboard)
        self.assertNotIn("View Inventory listings", dashboard)
        self.assertNotIn("Create an unpublished draft", dashboard)
        self.assertNotIn("Phase 1 boundary", dashboard)

    def test_every_other_route_stays_locked_in_the_migration_stage(self):
        cookie = self.login()
        for method, path in (
            ("GET", "/listings"),
            ("GET", "/orders"),
            ("GET", "/account"),
            ("GET", "/drafts/new"),
        ):
            with self.subTest(method=method, path=path):
                status, _headers, body = self.request(method, path, cookie=cookie)
                self.assertEqual(status, 404)
                self.assertIn("locked until", body)
        self.assertEqual(self.ebay.mutations, 0)


if __name__ == "__main__":
    unittest.main()
