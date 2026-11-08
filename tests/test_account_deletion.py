"""Tests for the eBay Marketplace Account Deletion/Closure notification
feature: EbaySandboxClient.get_notification_public_key, the AppDatabase
schema-v9 methods, ebay_app.account_deletion's polling logic, and the
blocking banner / confirm route wired into ebay_app.web.

tests/test_notification_signature.py covers the underlying signature
verification module itself; this file covers everything built on top of it.
"""

from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
import urllib.parse
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from ebay_app import account_deletion
from ebay_app.account_deletion import (
    DeletionWorkerClient,
    DeletionWorkerError,
    PollOutcome,
    maybe_poll,
    poll_once,
)
from ebay_app.ebay_client import EbayError, EbaySandboxClient
from ebay_app.storage import AppDatabase
from ebay_app.web import WebApplication

from helpers import FakeTransport, adopt_test_access_token, json_response, make_config


# ---------------------------------------------------------------------------
# EbaySandboxClient.get_notification_public_key
# ---------------------------------------------------------------------------

class PublicKeyFetchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = make_config(Path(self.temp.name))
        self.database = AppDatabase(self.config.database_path, self.config.data_encryption_key)
        self.addCleanup(self.database.close)

    def client(self, responses) -> EbaySandboxClient:
        transport = FakeTransport(responses)
        client = EbaySandboxClient(self.config, self.database, transport=transport)
        adopt_test_access_token(client)
        return client

    def test_fetches_the_public_key_using_an_application_token(self):
        kid = "9936261a-7d7b-4621-a0f1-96ccb428af49"
        client = self.client(
            [
                json_response(200, {"access_token": "app-token", "expires_in": 7200}),
                json_response(200, {"key": "PEM", "algorithm": "ECDSA", "digest": "SHA1"}),
            ]
        )
        result = client.get_notification_public_key(kid)
        self.assertEqual(result, {"key": "PEM", "algorithm": "ECDSA", "digest": "SHA1"})
        self.assertTrue(
            client.transport.calls[1]["url"].endswith(
                "/commerce/notification/v1/public_key/" + kid
            )
        )
        self.assertIn("Bearer app-token", client.transport.calls[1]["headers"]["Authorization"])
        self.assertEqual(client.transport.calls[1]["method"], "GET")

    def test_rejects_an_invalid_kid_before_any_network_call(self):
        client = self.client([])
        for bad_kid in ("", "../../oauth2/token", "a b", "x" * 200, "a/b", None, 5):
            with self.subTest(bad_kid=bad_kid):
                with self.assertRaises(EbayError) as failure:
                    client.get_notification_public_key(bad_kid)
                self.assertEqual(failure.exception.code, "INVALID_KID")
        self.assertEqual(client.transport.calls, [])


# ---------------------------------------------------------------------------
# AppDatabase schema-v9 methods
# ---------------------------------------------------------------------------

class DeletionStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = AppDatabase(Path(self.temp.name) / "app.sqlite", b"k" * 32)
        self.addCleanup(self.database.close)

    def test_default_state_is_empty(self):
        state = self.database.get_deletion_state()
        self.assertIsNone(state["last_poll_at"])
        self.assertIsNone(state["detected_at"])
        self.assertIsNone(state["notification_id"])

    def test_poll_result_is_recorded_and_overwritten(self):
        self.database.record_deletion_poll_result(success=True)
        state = self.database.get_deletion_state()
        self.assertIsNotNone(state["last_poll_at"])
        self.assertIsNone(state["last_poll_error"])

        self.database.record_deletion_poll_result(success=False, error="network error")
        state = self.database.get_deletion_state()
        self.assertEqual(state["last_poll_error"], "network error")

    def test_notification_processed_is_recorded_once(self):
        first = self.database.record_deletion_notification_processed(
            "nid-1", "some-user", matched_owner=False
        )
        second = self.database.record_deletion_notification_processed(
            "nid-1", "some-user", matched_owner=False
        )
        self.assertTrue(first)
        self.assertFalse(second)

    def test_owner_deletion_detected_sets_state_until_acknowledged(self):
        self.database.record_owner_deletion_detected("nid-owner")
        state = self.database.get_deletion_state()
        self.assertIsNotNone(state["detected_at"])
        self.assertEqual(state["notification_id"], "nid-owner")

        self.database.save_refresh_token("secret-refresh-token", 9999999999, "sandbox-user")
        self.database.acknowledge_owner_deletion()
        state = self.database.get_deletion_state()
        self.assertIsNone(state["detected_at"])
        self.assertIsNone(state["notification_id"])
        self.assertIsNone(self.database.get_refresh_token())

    def test_acknowledge_is_safe_even_with_no_prior_detection(self):
        self.database.acknowledge_owner_deletion()
        self.assertIsNone(self.database.get_deletion_state()["detected_at"])


# ---------------------------------------------------------------------------
# ebay_app.account_deletion: DeletionWorkerClient, poll_once, maybe_poll
# ---------------------------------------------------------------------------

class FakeHttpResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DeletionWorkerClientTests(unittest.TestCase):
    def test_fetch_pending_page_sends_bearer_auth_and_parses_json(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["headers"] = dict(request.header_items())
            return FakeHttpResponse({"entries": [], "cursor": None})

        client = DeletionWorkerClient("https://worker.example.workers.dev", "s" * 40)
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = client.fetch_pending_page()
        self.assertEqual(result, {"entries": [], "cursor": None})
        self.assertEqual(captured["method"], "GET")
        self.assertEqual(captured["url"], "https://worker.example.workers.dev/account-deletion/pending")
        self.assertEqual(captured["headers"].get("Authorization"), "Bearer " + "s" * 40)

    def test_fetch_pending_page_passes_the_cursor_as_a_query_parameter(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            return FakeHttpResponse({"entries": [], "cursor": None})

        client = DeletionWorkerClient("https://worker.example.workers.dev", "s" * 40)
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            client.fetch_pending_page("abc123")
        self.assertEqual(
            captured["url"],
            "https://worker.example.workers.dev/account-deletion/pending?" + urllib.parse.urlencode({"cursor": "abc123"}),
        )

    def test_acknowledge_posts_the_notification_id(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["method"] = request.get_method()
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeHttpResponse({"acknowledged": True})

        client = DeletionWorkerClient("https://worker.example.workers.dev", "s" * 40)
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            client.acknowledge("nid-1")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["body"], {"notificationId": "nid-1"})

    def test_transport_failure_raises_deletion_worker_error(self):
        import urllib.error

        def fake_urlopen(request, timeout):
            raise urllib.error.URLError("connection refused")

        client = DeletionWorkerClient("https://worker.example.workers.dev", "s" * 40)
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(DeletionWorkerError):
                client.fetch_pending_page()


class FakeWorker:
    """Stand-in for DeletionWorkerClient used by poll_once/_handle_entry
    tests, so these tests exercise the verification and detection logic
    without any HTTP involved."""

    def __init__(self, pages, *, fail_acknowledge=False):
        self._pages = list(pages)
        self.acknowledged = []
        self.fail_acknowledge = fail_acknowledge

    def fetch_pending_page(self, cursor=None):
        return self._pages.pop(0)

    def acknowledge(self, notification_id):
        if self.fail_acknowledge:
            raise DeletionWorkerError("simulated failure")
        self.acknowledged.append(notification_id)


class FakeEbayForDeletion:
    def __init__(self, public_keys):
        self.public_keys = public_keys
        self.calls = []

    def get_notification_public_key(self, kid):
        self.calls.append(kid)
        return self.public_keys[kid]


def _sign(private_key, data):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    return private_key.sign(data, ec.ECDSA(hashes.SHA1()))


def _key_response(public_key):
    from cryptography.hazmat.primitives import serialization

    pem = public_key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return {"key": pem.replace("\n", ""), "algorithm": "ECDSA", "digest": "SHA1"}


def _make_entry(private_key, kid, *, notification_id, user_id, username="buyer"):
    import base64

    message = {
        "metadata": {"topic": "MARKETPLACE_ACCOUNT_DELETION", "schemaVersion": "1.0", "deprecated": False},
        "notification": {
            "notificationId": notification_id,
            "eventDate": "2026-10-01T12:00:00.000Z",
            "publishDate": "2026-10-01T12:00:01.000Z",
            "publishAttemptCount": 1,
            "data": {"username": username, "userId": user_id, "eiasToken": "token"},
        },
    }
    raw_body = json.dumps(message, separators=(",", ":"))
    signature = _sign(private_key, raw_body.encode("utf-8"))
    header = {"alg": "ecdsa", "kid": kid, "signature": base64.b64encode(signature).decode(), "digest": "SHA1"}
    signature_header = base64.b64encode(json.dumps(header).encode()).decode()
    return {
        "notificationId": notification_id,
        "rawBody": raw_body,
        "signatureHeader": signature_header,
        "data": message["notification"]["data"],
    }


class PollOnceTests(unittest.TestCase):
    def setUp(self):
        from cryptography.hazmat.primitives.asymmetric import ec

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = AppDatabase(Path(self.temp.name) / "app.sqlite", b"k" * 32)
        self.addCleanup(self.database.close)
        self.config = replace(
            make_config(Path(self.temp.name)),
            deletion_worker_base_url="https://worker.example.workers.dev",
            deletion_worker_status_secret="s" * 40,
        )
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        # account_deletion._PUBLIC_KEY_CACHE is a process-wide singleton
        # (deliberately, so eBay's "cache >= 1 hour" guidance holds across
        # real polls). Every test below reuses the same kid string with a
        # freshly generated private key, so a stale cached entry from an
        # earlier test would otherwise make verification fail against the
        # wrong key. Clear it so each test starts from a clean cache.
        account_deletion._PUBLIC_KEY_CACHE.clear()
        self.kid = "11111111-2222-3333-4444-555555555555"
        self.ebay = FakeEbayForDeletion({self.kid: _key_response(self.private_key.public_key())})

    def test_inactive_when_not_configured(self):
        config = make_config(Path(self.temp.name))  # no deletion_worker_* set
        outcome = poll_once(config, self.ebay, self.database)
        self.assertEqual(outcome, PollOutcome(configured=False, polled=False))
        self.assertEqual(self.ebay.calls, [])

    def test_detects_a_verified_notification_matching_the_connected_owner(self):
        self.database.save_refresh_token("refresh-token", 9999999999, "owner-user-id", new_grant=True)
        entry = _make_entry(
            self.private_key, self.kid, notification_id="nid-owner", user_id="owner-user-id"
        )
        worker = FakeWorker([{"entries": [entry], "cursor": None}])
        with patch.object(account_deletion, "DeletionWorkerClient", return_value=worker):
            outcome = poll_once(self.config, self.ebay, self.database)
        self.assertTrue(outcome.configured)
        self.assertTrue(outcome.polled)
        self.assertEqual(outcome.entries_seen, 1)
        self.assertTrue(outcome.owner_deletion_detected)
        self.assertEqual(worker.acknowledged, ["nid-owner"])
        state = self.database.get_deletion_state()
        self.assertEqual(state["notification_id"], "nid-owner")

    def test_ignores_a_verified_notification_about_someone_else(self):
        self.database.save_refresh_token("refresh-token", 9999999999, "owner-user-id", new_grant=True)
        entry = _make_entry(
            self.private_key, self.kid, notification_id="nid-other", user_id="some-other-user"
        )
        worker = FakeWorker([{"entries": [entry], "cursor": None}])
        with patch.object(account_deletion, "DeletionWorkerClient", return_value=worker):
            outcome = poll_once(self.config, self.ebay, self.database)
        self.assertFalse(outcome.owner_deletion_detected)
        self.assertIsNone(self.database.get_deletion_state()["detected_at"])
        # Still acknowledged: a verified-but-irrelevant notification should
        # not pile up in the Worker's queue.
        self.assertEqual(worker.acknowledged, ["nid-other"])

    def test_detecting_the_same_notification_twice_is_a_no_op_the_second_time(self):
        self.database.save_refresh_token("refresh-token", 9999999999, "owner-user-id", new_grant=True)
        entry = _make_entry(
            self.private_key, self.kid, notification_id="nid-owner", user_id="owner-user-id"
        )
        worker = FakeWorker(
            [{"entries": [entry], "cursor": None}, {"entries": [entry], "cursor": None}]
        )
        with patch.object(account_deletion, "DeletionWorkerClient", return_value=worker):
            first = poll_once(self.config, self.ebay, self.database)
            second = poll_once(self.config, self.ebay, self.database)
        self.assertTrue(first.owner_deletion_detected)
        self.assertFalse(second.owner_deletion_detected)

    def test_a_forged_or_unverifiable_entry_is_left_queued_and_not_acknowledged(self):
        entry = _make_entry(
            self.private_key, self.kid, notification_id="nid-bad", user_id="owner-user-id"
        )
        entry["rawBody"] = entry["rawBody"].replace("owner-user-id", "tampered-user")
        worker = FakeWorker([{"entries": [entry], "cursor": None}])
        with patch.object(account_deletion, "DeletionWorkerClient", return_value=worker):
            outcome = poll_once(self.config, self.ebay, self.database)
        self.assertEqual(outcome.entries_seen, 1)
        self.assertFalse(outcome.owner_deletion_detected)
        self.assertEqual(worker.acknowledged, [])

    def test_paginates_across_multiple_pages(self):
        entry_1 = _make_entry(self.private_key, self.kid, notification_id="nid-1", user_id="x")
        entry_2 = _make_entry(self.private_key, self.kid, notification_id="nid-2", user_id="y")
        worker = FakeWorker(
            [
                {"entries": [entry_1], "cursor": "page-2"},
                {"entries": [entry_2], "cursor": None},
            ]
        )
        with patch.object(account_deletion, "DeletionWorkerClient", return_value=worker):
            outcome = poll_once(self.config, self.ebay, self.database)
        self.assertEqual(outcome.entries_seen, 2)
        self.assertEqual(sorted(worker.acknowledged), ["nid-1", "nid-2"])

    def test_worker_transport_failure_is_reported_not_raised(self):
        class BrokenWorker:
            def fetch_pending_page(self, cursor=None):
                raise DeletionWorkerError("boom")

        with patch.object(account_deletion, "DeletionWorkerClient", return_value=BrokenWorker()):
            outcome = poll_once(self.config, self.ebay, self.database)
        self.assertTrue(outcome.configured)
        self.assertFalse(outcome.polled)
        self.assertEqual(outcome.error, "boom")
        self.assertEqual(self.database.get_deletion_state()["last_poll_error"], "boom")

    def test_a_failed_acknowledge_does_not_fail_the_poll(self):
        self.database.save_refresh_token("refresh-token", 9999999999, "owner-user-id", new_grant=True)
        entry = _make_entry(
            self.private_key, self.kid, notification_id="nid-owner", user_id="owner-user-id"
        )
        worker = FakeWorker([{"entries": [entry], "cursor": None}], fail_acknowledge=True)
        with patch.object(account_deletion, "DeletionWorkerClient", return_value=worker):
            outcome = poll_once(self.config, self.ebay, self.database)
        self.assertTrue(outcome.polled)
        self.assertTrue(outcome.owner_deletion_detected)


class MaybePollTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = AppDatabase(Path(self.temp.name) / "app.sqlite", b"k" * 32)
        self.addCleanup(self.database.close)
        self.config = replace(
            make_config(Path(self.temp.name)),
            deletion_worker_base_url="https://worker.example.workers.dev",
            deletion_worker_status_secret="s" * 40,
        )

    def test_inactive_when_not_configured(self):
        outcome = maybe_poll(make_config(Path(self.temp.name)), object(), self.database)
        self.assertEqual(outcome, PollOutcome(configured=False, polled=False))

    def test_polls_once_then_skips_within_the_interval(self):
        clock = {"t": 1_000_000.0}
        with patch.object(
            account_deletion, "poll_once", return_value=PollOutcome(configured=True, polled=True)
        ) as fake_poll:
            first = maybe_poll(self.config, object(), self.database, now=lambda: clock["t"])
            self.database.record_deletion_poll_result(success=True)
            clock["t"] += 60  # well within MIN_POLL_INTERVAL_SECONDS
            second = maybe_poll(self.config, object(), self.database, now=lambda: clock["t"])
        self.assertTrue(first.polled)
        self.assertFalse(second.polled)
        self.assertEqual(fake_poll.call_count, 1)

    def test_polls_again_after_the_interval_elapses(self):
        with patch.object(
            account_deletion, "poll_once", return_value=PollOutcome(configured=True, polled=True)
        ) as fake_poll:
            self.database.record_deletion_poll_result(success=True)
            clock = {"t": time.time() + account_deletion.MIN_POLL_INTERVAL_SECONDS + 1}
            outcome = maybe_poll(self.config, object(), self.database, now=lambda: clock["t"])
        self.assertTrue(outcome.polled)
        self.assertEqual(fake_poll.call_count, 1)


# ---------------------------------------------------------------------------
# web.py: blocking banner and the /account-deletion confirm route
# ---------------------------------------------------------------------------

class FakeEbayForWeb:
    def authorization_url(self, _session_id):
        return "https://auth.sandbox.ebay.com/oauth2/authorize?state=test"

    def complete_authorization(self, _state, _code, _session_id):
        return {"user_id": "sandbox-user"}


class AccountDeletionWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(make_config(Path(self.temp.name)), runtime_mode="local")
        self.database = AppDatabase(self.config.database_path, self.config.data_encryption_key)
        self.addCleanup(self.database.close)
        self.database.save_refresh_token("refresh-token", 9999999999, "sandbox-user", new_grant=True)
        self.app = WebApplication(self.config, self.database, FakeEbayForWeb())

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
    def cookie(headers):
        return headers["Set-Cookie"].split(";", 1)[0]

    @staticmethod
    def hidden(body, name):
        import re

        match = re.search(rf'name="{re.escape(name)}" value="([^"]+)"', body)
        if not match:
            raise AssertionError(f"Missing hidden field {name}")
        return match.group(1)

    def login(self):
        status, headers, body = self.request("GET", "/login")
        cookie = self.cookie(headers)
        csrf = self.hidden(body, "csrf_token")
        status, headers, _body = self.request(
            "POST", "/login", cookie=cookie, form={"csrf_token": csrf, "password": self.config.admin_password}
        )
        self.assertEqual(status, 303)
        cookie = self.cookie(headers)
        return cookie

    def test_no_banner_when_nothing_detected(self):
        cookie = self.login()
        status, _headers, body = self.request("GET", "/", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertNotIn("Account closure reported by eBay", body)

    def test_detected_closure_blocks_every_route_except_account_deletion_and_logout(self):
        cookie = self.login()
        self.database.record_owner_deletion_detected("nid-1")

        status, _headers, body = self.request("GET", "/", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Account closure reported by eBay", body)

        status, _headers, body = self.request("GET", "/account", cookie=cookie)
        self.assertIn("Account closure reported by eBay", body)

        status, _headers, _body = self.request("POST", "/logout", cookie=cookie, form={})
        # /logout bypasses the gate; CSRF is still required so this 403s
        # rather than 200 -- the point is it is NOT replaced by the banner.
        self.assertNotEqual(status, 200)

    def test_get_account_deletion_shows_the_banner_page(self):
        cookie = self.login()
        self.database.record_owner_deletion_detected("nid-1")
        status, _headers, body = self.request("GET", "/account-deletion", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Confirm and disconnect", body)

    def test_confirm_clears_the_token_and_unblocks_the_app(self):
        cookie = self.login()
        self.database.record_owner_deletion_detected("nid-1")
        _status, _headers, body = self.request("GET", "/account-deletion", cookie=cookie)
        csrf = self.hidden(body, "csrf_token")

        status, headers, _body = self.request(
            "POST", "/account-deletion", cookie=cookie, form={"csrf_token": csrf}
        )
        self.assertEqual(status, 303)
        self.assertIsNone(self.database.get_refresh_token())

        cookie = self.cookie(headers)
        status, _headers, body = self.request("GET", "/", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertNotIn("Account closure reported by eBay", body)

    def test_confirm_requires_csrf(self):
        cookie = self.login()
        self.database.record_owner_deletion_detected("nid-1")
        status, _headers, _body = self.request(
            "POST", "/account-deletion", cookie=cookie, form={"csrf_token": "wrong"}
        )
        self.assertEqual(status, 403)
        self.assertIsNotNone(self.database.get_refresh_token())


if __name__ == "__main__":
    unittest.main()
