from __future__ import annotations

import io
import logging
import re
import tempfile
import unittest
import urllib.parse
from dataclasses import replace
from pathlib import Path

from ebay_app.config import DRAFT_SCOPES
from ebay_app.ebay_client import EbayError
from ebay_app.storage import AppDatabase
from ebay_app.web import WebApplication

from helpers import make_config


class _StatefulFakeEbay:
    STATE = "A" * 43

    def __init__(self, database: AppDatabase) -> None:
        self.database = database
        self.authorization_calls = 0
        self.completion_calls = []

    def authorization_url(self, session_id: str) -> str:
        self.authorization_calls += 1
        self.database.save_oauth_state(self.STATE, session_id)
        return (
            "https://auth.sandbox.ebay.com/oauth2/authorize?"
            + urllib.parse.urlencode(
                {
                    "client_id": "sandbox-client",
                    "redirect_uri": "Sandbox-Runame",
                    "response_type": "code",
                    "state": self.STATE,
                }
            )
        )

    def complete_authorization(self, state: str, code: str, session_id: str):
        if self.database.consume_oauth_state_record(state, session_id) is None:
            raise EbayError(
                "OAuth state is invalid, expired, or already used",
                code="INVALID_STATE",
            )
        self.completion_calls.append((state, code, session_id))
        return {"upgraded": True}


class PermanentLocalOAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(
            make_config(Path(self.temp.name)),
            feature_stage="drafts",
            scopes=DRAFT_SCOPES,
            runtime_mode="local",
            host="127.0.0.1",
            port=5000,
            allowed_hosts=("127.0.0.1",),
            cookie_secure=False,
        )
        self.database = AppDatabase(
            self.config.database_path,
            self.config.data_encryption_key,
        )
        self.addCleanup(self.database.close)
        self.ebay = _StatefulFakeEbay(self.database)
        self.log_stream = io.StringIO()
        self.logger = logging.getLogger(f"oauth-test-{id(self)}")
        self.logger.handlers = [logging.StreamHandler(self.log_stream)]
        self.logger.propagate = False
        self.app = WebApplication(
            self.config,
            self.database,
            self.ebay,
            logger=self.logger,
        )

    def request(
        self,
        method,
        path,
        *,
        cookie="",
        form=None,
        query="",
        host="127.0.0.1:5000",
        remote_addr="127.0.0.1",
    ):
        body = urllib.parse.urlencode(form or {}).encode("utf-8")
        captured = {}

        def start_response(status, headers):
            captured["status"] = int(status.split(" ", 1)[0])
            captured["headers"] = dict(headers)

        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "REMOTE_ADDR": remote_addr,
            "HTTP_HOST": host,
            "HTTP_COOKIE": cookie,
            "wsgi.input": io.BytesIO(body),
            "CONTENT_LENGTH": str(len(body)),
            "CONTENT_TYPE": (
                "application/x-www-form-urlencoded" if method == "POST" else ""
            ),
        }
        result = b"".join(self.app(environ, start_response)).decode("utf-8")
        return captured["status"], captured["headers"], result

    @staticmethod
    def hidden(body, name):
        match = re.search(rf'name="{name}" value="([^"]+)"', body)
        if not match:
            raise AssertionError(f"Missing hidden field {name}")
        return match.group(1)

    @staticmethod
    def cookie(headers):
        return headers["Set-Cookie"].split(";", 1)[0]

    def login(self):
        status, headers, body = self.request("GET", "/login")
        self.assertEqual(status, 200)
        first_cookie = self.cookie(headers)
        csrf = self.hidden(body, "csrf_token")
        status, headers, _body = self.request(
            "POST",
            "/login",
            cookie=first_cookie,
            form={"csrf_token": csrf, "password": self.config.admin_password},
        )
        self.assertEqual(status, 303)
        authenticated_cookie = self.cookie(headers)
        status, _headers, dashboard = self.request(
            "GET", "/", cookie=authenticated_cookie
        )
        self.assertEqual(status, 200)
        return authenticated_cookie, self.hidden(dashboard, "csrf_token")

    def start_oauth(self):
        cookie, csrf = self.login()
        status, _headers, handoff = self.request(
            "POST",
            "/connect",
            cookie=cookie,
            form={"csrf_token": csrf},
        )
        self.assertEqual(status, 200)
        self.assertIn("Continue to eBay Sandbox", handoff)
        return cookie

    def test_cookie_and_request_origin_are_exactly_loopback(self):
        status, headers, _body = self.request("GET", "/login")
        self.assertEqual(status, 200)
        set_cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Lax", set_cookie)
        self.assertNotIn("Secure", set_cookie)

        invalid_hosts = (
            "127.0.0.1",
            "127.0.0.1:5001",
            "localhost:5000",
            "user@127.0.0.1:5000",
            "example.test:5000",
        )
        for host in invalid_hosts:
            with self.subTest(host=host):
                status, _headers, _body = self.request("GET", "/login", host=host)
                self.assertEqual(status, 400)
        status, _headers, _body = self.request(
            "GET", "/login", remote_addr="203.0.113.4"
        )
        self.assertEqual(status, 403)

    def test_same_authenticated_session_completes_one_use_callback(self):
        cookie = self.start_oauth()
        status, headers, _body = self.request(
            "GET",
            "/oauth/callback",
            cookie=cookie,
            query=urllib.parse.urlencode(
                {"state": self.ebay.STATE, "code": "short-lived-code"}
            ),
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        self.assertEqual(len(self.ebay.completion_calls), 1)

        status, _headers, _body = self.request(
            "GET",
            "/oauth/callback",
            cookie=cookie,
            query=urllib.parse.urlencode(
                {"state": self.ebay.STATE, "code": "short-lived-code"}
            ),
        )
        self.assertEqual(status, 303)
        self.assertEqual(len(self.ebay.completion_calls), 1)
        self.assertIn("reason=INVALID_STATE", self.log_stream.getvalue())

    def test_callback_rejects_malformed_results_without_exposing_values(self):
        cookie, _csrf = self.login()
        secret_code = "code-value-that-must-not-be-logged"
        secret_state = "state-value-that-must-not-be-logged"
        malformed = (
            "",
            f"state={secret_state}",
            f"state={secret_state}&code={secret_code}&error=access_denied",
            f"state={secret_state}&code={secret_code}&extra=value",
            f"state={secret_state}&state=other&code={secret_code}",
            f"state={secret_state}&code={secret_code}&code=other",
        )
        for query in malformed:
            with self.subTest(query=query):
                status, headers, _body = self.request(
                    "GET",
                    "/oauth/callback",
                    cookie=cookie,
                    query=query,
                )
                self.assertEqual(status, 303)
                self.assertEqual(headers["Location"], "/")
        self.assertEqual(self.ebay.completion_calls, [])
        rendered_logs = self.log_stream.getvalue()
        self.assertIn("reason=INVALID_CALLBACK_REQUEST", rendered_logs)
        self.assertNotIn(secret_code, rendered_logs)
        self.assertNotIn(secret_state, rendered_logs)

    def test_callback_requires_authenticated_browser_and_consumes_decline_state(self):
        cookie = self.start_oauth()
        query = urllib.parse.urlencode(
            {"state": self.ebay.STATE, "error": "access_denied"}
        )
        status, _headers, _body = self.request("GET", "/oauth/callback", query=query)
        self.assertEqual(status, 303)
        self.assertNotIn("INVALID_STATE", self.log_stream.getvalue())

        status, headers, _body = self.request(
            "GET",
            "/oauth/callback",
            cookie=cookie,
            query=query,
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        self.assertFalse(
            self.database.consume_oauth_state(self.ebay.STATE, "unrelated-session")
        )


if __name__ == "__main__":
    unittest.main()
