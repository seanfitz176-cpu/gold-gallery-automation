"""Pre-production hardening pass (Section 1, item 5 -- own API surface).

Locks in the exact set of security headers `WebApplication.__call__` sends
on every response, including the `Permissions-Policy` header added in this
pass (matching the value already used by the stable Cloudflare OAuth relay
Worker in `oauth_relay_worker/src/index.js`, so both hops of the OAuth flow
agree). Before this test existed, none of these headers had any automated
coverage at all -- a regression here would have shipped silently.
"""

from __future__ import annotations

import io
import re
import tempfile
import unittest
import urllib.parse
from dataclasses import replace
from pathlib import Path

from ebay_app.config import CONNECTION_SCOPES
from ebay_app.storage import AppDatabase
from ebay_app.web import WebApplication

from helpers import make_config

EXPECTED_PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"


class _FakeEbay:
    def authorization_url(self, _session_id: str) -> str:
        return "https://auth.sandbox.ebay.com/oauth2/authorize?state=test"


class SecurityHeaderTests(unittest.TestCase):
    def setUp(self) -> None:
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
        self.app = WebApplication(self.config, self.database, _FakeEbay())

    def request(self, method: str, path: str, *, cookie: str = "", form=None):
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
        return headers["Set-Cookie"].split(";", 1)[0]

    def assert_hardened_headers(self, header_dict):
        self.assertEqual(header_dict["Cache-Control"], "no-store")
        self.assertEqual(header_dict["X-Content-Type-Options"], "nosniff")
        self.assertEqual(header_dict["Referrer-Policy"], "no-referrer")
        self.assertEqual(header_dict["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'none'", header_dict["Content-Security-Policy"])
        self.assertEqual(header_dict["Permissions-Policy"], EXPECTED_PERMISSIONS_POLICY)

    def test_every_response_carries_the_full_hardened_header_set(self):
        # An HTML page (login) and a JSON endpoint (/health) both go through
        # the same single WSGI response chokepoint -- assert both get every
        # header, not just the happy-path HTML response.
        for path in ("/login", "/health"):
            with self.subTest(path=path):
                status, headers, _body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assert_hardened_headers(headers)

    def test_unauthenticated_redirects_also_carry_the_full_header_set(self):
        # The redirect-to-/login path is a non-200, non-HTML-body response
        # produced by a different code path than the happy-path pages.
        status, headers, _body = self.request("GET", "/listings")
        self.assertEqual(status, 303)
        self.assert_hardened_headers(headers)

    def test_authenticated_404_also_carries_the_full_header_set(self):
        cookie = self.login()
        status, headers, _body = self.request("GET", "/nonexistent-route", cookie=cookie)
        self.assertEqual(status, 404)
        self.assert_hardened_headers(headers)


if __name__ == "__main__":
    unittest.main()
