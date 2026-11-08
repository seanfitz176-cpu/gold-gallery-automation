"""Tests for ebay_app.password_hash and its wiring into the admin login route.

Security-audit finding (2026-10-30, Medium): the admin dashboard password was
compared in plaintext (`hmac.compare_digest(password, self.config.admin_password)`),
so anyone who could read `.env`/`.env.production` could read the real password
directly. `ebay_app.password_hash` lets `APP_ADMIN_PASSWORD` instead hold a
salted PBKDF2 hash, verified without ever needing the plaintext again, while
an existing plaintext deployment keeps working unchanged until migrated.
"""

from __future__ import annotations

import io
import tempfile
import unittest
import urllib.parse
from dataclasses import replace
from pathlib import Path

from ebay_app.config import CONNECTION_SCOPES
from ebay_app.password_hash import HASH_PREFIX, hash_password, is_hashed, verify_password
from ebay_app.storage import AppDatabase
from ebay_app.web import WebApplication

from helpers import make_config


class _FakeEbay:
    def authorization_url(self, _session_id: str) -> str:
        return "https://auth.sandbox.ebay.com/oauth2/authorize?state=test"


class HashPasswordTests(unittest.TestCase):
    def test_hash_starts_with_prefix_and_is_detected_as_hashed(self):
        encoded = hash_password("correct-horse-battery-staple")
        self.assertTrue(encoded.startswith(HASH_PREFIX))
        self.assertTrue(is_hashed(encoded))

    def test_plaintext_value_is_not_detected_as_hashed(self):
        self.assertFalse(is_hashed("local-password-that-is-long-enough"))

    def test_two_hashes_of_the_same_password_differ(self):
        first = hash_password("correct-horse-battery-staple")
        second = hash_password("correct-horse-battery-staple")
        self.assertNotEqual(first, second)
        self.assertTrue(verify_password("correct-horse-battery-staple", first))
        self.assertTrue(verify_password("correct-horse-battery-staple", second))

    def test_empty_password_is_rejected(self):
        with self.assertRaises(ValueError):
            hash_password("")

    def test_non_string_password_is_rejected(self):
        with self.assertRaises(ValueError):
            hash_password(None)  # type: ignore[arg-type]


class VerifyPasswordTests(unittest.TestCase):
    def test_correct_password_against_hash_succeeds(self):
        encoded = hash_password("correct-horse-battery-staple")
        self.assertTrue(verify_password("correct-horse-battery-staple", encoded))

    def test_wrong_password_against_hash_fails(self):
        encoded = hash_password("correct-horse-battery-staple")
        self.assertFalse(verify_password("wrong-password-entirely", encoded))

    def test_correct_legacy_plaintext_password_still_succeeds(self):
        self.assertTrue(
            verify_password(
                "local-password-that-is-long-enough",
                "local-password-that-is-long-enough",
            )
        )

    def test_wrong_legacy_plaintext_password_fails(self):
        self.assertFalse(
            verify_password("wrong-password", "local-password-that-is-long-enough")
        )

    def test_malformed_hash_like_value_fails_closed_without_raising(self):
        self.assertFalse(verify_password("anything", f"{HASH_PREFIX}not-a-real-hash"))
        self.assertFalse(verify_password("anything", f"{HASH_PREFIX}1$$"))
        self.assertFalse(
            verify_password("anything", f"{HASH_PREFIX}not-an-int$c2FsdA==$ZGlnZXN0")
        )

    def test_absurd_iteration_count_in_hash_fails_closed(self):
        salt_and_digest = hash_password("correct-horse-battery-staple").split("$", 2)[2]
        self.assertFalse(
            verify_password(
                "correct-horse-battery-staple",
                f"{HASH_PREFIX}99999999999${salt_and_digest}",
            )
        )
        self.assertFalse(
            verify_password(
                "correct-horse-battery-staple",
                f"{HASH_PREFIX}0${salt_and_digest}",
            )
        )

    def test_non_string_inputs_fail_closed(self):
        encoded = hash_password("correct-horse-battery-staple")
        self.assertFalse(verify_password(None, encoded))  # type: ignore[arg-type]
        self.assertFalse(verify_password("correct-horse-battery-staple", None))  # type: ignore[arg-type]


class AdminLoginAcceptsHashedOrPlaintextPasswordTests(unittest.TestCase):
    """Exercises the real /login route, not just the password_hash module."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def build_app(self, admin_password: str) -> WebApplication:
        config = replace(
            make_config(Path(self.temp.name)),
            admin_password=admin_password,
            feature_stage="connection",
            scopes=CONNECTION_SCOPES,
            runtime_mode="local",
        )
        database = AppDatabase(config.database_path, config.data_encryption_key)
        self.addCleanup(database.close)
        return WebApplication(config, database, _FakeEbay())

    @staticmethod
    def request(app: WebApplication, method: str, path: str, *, cookie: str = "", form=None):
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
        response_body = b"".join(app(environ, start_response)).decode("utf-8")
        return captured["status"], captured["headers"], response_body

    @staticmethod
    def csrf_from(body: str) -> str:
        import re

        match = re.search(r'name="csrf_token" value="([^"]+)"', body)
        if not match:
            raise AssertionError("Missing hidden csrf_token field")
        return match.group(1)

    def attempt_login(self, app: WebApplication, password: str):
        status, headers, body = self.request(app, "GET", "/login")
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        csrf = self.csrf_from(body)
        return self.request(
            app,
            "POST",
            "/login",
            cookie=cookie,
            form={"csrf_token": csrf, "password": password},
        )

    def test_login_succeeds_with_correct_password_when_config_stores_a_hash(self):
        app = self.build_app(hash_password("correct-horse-battery-staple"))
        status, _headers, _body = self.attempt_login(app, "correct-horse-battery-staple")
        self.assertEqual(status, 303)

    def test_login_rejected_with_wrong_password_when_config_stores_a_hash(self):
        app = self.build_app(hash_password("correct-horse-battery-staple"))
        status, _headers, body = self.attempt_login(app, "wrong-password-entirely")
        self.assertEqual(status, 401)
        self.assertIn("Incorrect password", body)

    def test_login_still_succeeds_with_legacy_plaintext_password_in_config(self):
        app = self.build_app("local-password-that-is-long-enough")
        status, _headers, _body = self.attempt_login(app, "local-password-that-is-long-enough")
        self.assertEqual(status, 303)

    def test_login_still_rejected_with_wrong_legacy_plaintext_password(self):
        app = self.build_app("local-password-that-is-long-enough")
        status, _headers, body = self.attempt_login(app, "wrong-password-entirely")
        self.assertEqual(status, 401)
        self.assertIn("Incorrect password", body)


if __name__ == "__main__":
    unittest.main()
