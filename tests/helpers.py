from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from ebay_app.config import AppConfig, SELLER_SCOPES
from ebay_app.ebay_client import HttpResponse


def make_config(root: Path) -> AppConfig:
    return AppConfig(
        client_id="sandbox-client",
        client_secret="sandbox-secret",
        runame="Sandbox-Runame",
        admin_password="local-password-that-is-long-enough",
        data_encryption_key=b"k" * 32,
        marketplace_id="EBAY_GB",
        content_language="en-GB",
        currency="GBP",
        merchant_location_key=None,
        fulfillment_policy_id=None,
        payment_policy_id=None,
        return_policy_id=None,
        database_path=root / "app.sqlite",
        host="127.0.0.1",
        port=5000,
        allowed_hosts=("127.0.0.1",),
        cookie_secure=False,
        request_timeout_seconds=10,
        order_lookback_days=30,
        initial_refresh_token=None,
        feature_stage="full",
        scopes=SELLER_SCOPES,
    )


def json_response(status: int, payload) -> HttpResponse:
    return HttpResponse(status, {}, json.dumps(payload).encode("utf-8"))


class FakeTransport:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(
            {"method": method, "url": url, "headers": dict(headers), "body": body, "timeout": timeout}
        )
        if not self.responses:
            raise AssertionError(f"Unexpected transport call: {method} {url}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def adopt_test_access_token(client) -> None:
    client._access_token = "access-token"
    client._access_token_expires_at = time.time() + 3600
