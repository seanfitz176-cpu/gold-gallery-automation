"""Phase 5: a labelled second-account copy on port 5001 (docs/second_account.md)."""

from __future__ import annotations

import io
import re
import tempfile
import unittest
import urllib.parse
from dataclasses import replace
from pathlib import Path

from ebay_app.alerts import Notifier
from ebay_app.storage import AppDatabase
from ebay_app.web import WebApplication

from helpers import make_config
from test_connection_phase import _FakeEbay, _FakeVerifier


class SecondAccountWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(make_config(Path(self.temp.name)), port=5001,
                              account_label="Second shop", feature_stage="connection")
        self.database = AppDatabase(self.config.database_path, self.config.data_encryption_key)
        self.addCleanup(self.database.close)
        self.app = WebApplication(self.config, self.database, _FakeEbay(), connection_verifier=_FakeVerifier())

    def request(self, method, path, *, host="127.0.0.1:5001", cookie="", form=None):
        body = urllib.parse.urlencode(form or {}).encode("utf-8")
        captured = {}

        def start_response(status, headers):
            captured["status"] = int(status.split(" ", 1)[0])
            captured["headers"] = dict(headers)

        environ = {
            "REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "",
            "REMOTE_ADDR": "127.0.0.1", "HTTP_HOST": host, "HTTP_COOKIE": cookie,
            "wsgi.input": io.BytesIO(body), "CONTENT_LENGTH": str(len(body)),
            "CONTENT_TYPE": "application/x-www-form-urlencoded" if method == "POST" else "",
        }
        text = b"".join(self.app(environ, start_response)).decode("utf-8")
        return captured["status"], captured["headers"], text

    def login(self):
        _s, headers, body = self.request("GET", "/login")
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)
        _s, headers, _b = self.request("POST", "/login", cookie=cookie,
                                       form={"csrf_token": csrf, "password": self.config.admin_password})
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_every_page_names_the_account(self):
        status, _h, body = self.request("GET", "/login")
        self.assertEqual(status, 200)
        self.assertIn("Account: Second shop", body)

    def test_only_its_own_port_is_accepted_in_the_host_header(self):
        status, _h, _b = self.request("GET", "/login", host="127.0.0.1:5000")
        self.assertEqual(status, 400)

    def test_ebay_sign_in_is_refused_on_the_second_port(self):
        cookie = self.login()
        _s, _h, dashboard = self.request("GET", "/", cookie=cookie)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', dashboard).group(1)
        status, _h, body = self.request("POST", "/connect", cookie=cookie, form={"csrf_token": csrf})
        self.assertEqual(status, 409)
        self.assertIn("only returns to port 5000", body)

    def test_alert_subjects_name_the_account(self):
        captured = {}

        class SMTP:
            def __init__(self, *a):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *e):
                return False

            def starttls(self, context=None):
                pass

            def login(self, u, p):
                pass

            def send_message(self, m):
                captured["subject"] = m["Subject"]

        config = replace(self.config, smtp_host="h", smtp_user="u", smtp_app_password="p", alert_email="a@b.c")
        Notifier(config, smtp_factory=SMTP).send("Engine PAUSED", "x")
        self.assertIn("Second shop", captured["subject"])
