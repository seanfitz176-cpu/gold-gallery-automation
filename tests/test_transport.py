from __future__ import annotations

import io
import unittest
import urllib.error
from email.message import Message
from unittest import mock

from ebay_app.ebay_client import (
    MAX_RESPONSE_BYTES,
    EbayError,
    UrllibTransport,
)


class _Response:
    def __init__(self, body=b"{}", status=200, headers=None):
        self.body = body
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size):
        return self.body


class UrllibTransportTests(unittest.TestCase):
    def setUp(self):
        self.transport = UrllibTransport()
        self.transport._opener = mock.Mock()

    def test_success_uses_the_exact_request_and_bounded_read(self):
        self.transport._opener.open.return_value = _Response(
            body=b'{"ok":true}',
            status=201,
            headers={"X-Result": "safe"},
        )

        result = self.transport(
            "POST",
            "https://api.sandbox.ebay.com/example",
            {"Content-Type": "application/json", "X-Test": "safe"},
            b"{}",
            9,
        )

        request, = self.transport._opener.open.call_args.args
        self.assertEqual(request.full_url, "https://api.sandbox.ebay.com/example")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, b"{}")
        self.assertEqual(self.transport._opener.open.call_args.kwargs, {"timeout": 9})
        self.assertEqual(result.status, 201)
        self.assertEqual(result.headers, {"x-result": "safe"})
        self.assertEqual(result.body, b'{"ok":true}')

    def test_success_response_over_the_limit_fails_closed(self):
        self.transport._opener.open.return_value = _Response(
            body=b"x" * (MAX_RESPONSE_BYTES + 1)
        )

        with self.assertRaises(EbayError) as captured:
            self.transport(
                "GET",
                "https://api.sandbox.ebay.com/example",
                {},
                None,
                5,
            )

        self.assertEqual(captured.exception.code, "RESPONSE_TOO_LARGE")

    def test_http_error_is_returned_as_a_bounded_response(self):
        headers = Message()
        headers["Content-Type"] = "application/json"
        self.transport._opener.open.side_effect = urllib.error.HTTPError(
            "https://api.sandbox.ebay.com/example",
            400,
            "Bad Request",
            headers,
            io.BytesIO(b'{"errors":[]}'),
        )

        result = self.transport(
            "GET",
            "https://api.sandbox.ebay.com/example",
            {},
            None,
            5,
        )

        self.assertEqual(result.status, 400)
        self.assertEqual(result.headers["content-type"], "application/json")
        self.assertEqual(result.body, b'{"errors":[]}')

    def test_network_error_is_sanitized(self):
        self.transport._opener.open.side_effect = urllib.error.URLError(
            "test transport unavailable"
        )

        with self.assertRaises(EbayError) as captured:
            self.transport(
                "GET",
                "https://api.sandbox.ebay.com/example",
                {},
                None,
                5,
            )

        self.assertEqual(captured.exception.code, "EBAY_CONNECTION_ERROR")
        self.assertNotIn("test transport unavailable", str(captured.exception))

    def test_redirect_handler_never_follows_an_ebay_redirect(self):
        handler = UrllibTransport._NoRedirect()
        headers = Message()

        with self.assertRaises(urllib.error.HTTPError) as captured:
            handler.http_error_302(
                mock.Mock(full_url="https://api.sandbox.ebay.com/example"),
                io.BytesIO(b""),
                302,
                "Found",
                headers,
            )

        self.assertEqual(captured.exception.code, 302)


if __name__ == "__main__":
    unittest.main()
