from __future__ import annotations

import socket
import unittest
import urllib.error
from typing import Dict, Optional
from unittest.mock import patch

from ebay_app.image_fetch import (
    ALLOWED_CONTENT_TYPES,
    MAX_IMAGE_BYTES,
    ImageUrlError,
    check_image_url,
)

_REAL_GETADDRINFO = socket.getaddrinfo


def _fake_getaddrinfo(host, *args, **kwargs):
    # The sandboxed test environment has no outbound DNS at all, so a real
    # lookup of a non-local test domain fails closed with "Temporary failure
    # in name resolution" -- nothing to do with this module's own logic.
    # Stand in a fixed public address for the one fictitious hostname these
    # tests use, and fall back to the real resolver for everything else
    # (loopback/private/link-local literals resolve locally with no DNS
    # query at all, so they are unaffected either way).
    if host == "images.example.com":
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    return _REAL_GETADDRINFO(host, *args, **kwargs)


class FakeHeaders:
    def __init__(self, values: Dict[str, str]) -> None:
        self._values = values

    def get(self, name, default=None):
        return self._values.get(name, default)


class FakeResponse:
    def __init__(self, status: int, headers: Dict[str, str]) -> None:
        self.status = status
        self.headers = FakeHeaders(headers)
        self.closed = False

    def getcode(self):
        return self.status

    def close(self):
        self.closed = True


def make_opener(response=None, error: Optional[Exception] = None, calls: Optional[list] = None):
    def opener(request, timeout):
        if calls is not None:
            calls.append((request.full_url, request.get_method(), timeout))
        if error is not None:
            raise error
        return response

    return opener


class CheckImageUrlTests(unittest.TestCase):
    VALID_URL = "https://images.example.com/photo.jpg"

    def setUp(self):
        patcher = patch(
            "ebay_app.image_fetch.socket.getaddrinfo", side_effect=_fake_getaddrinfo
        )
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_valid_image_passes_and_returns_type_and_length(self):
        response = FakeResponse(
            200, {"Content-Type": "image/jpeg; charset=binary", "Content-Length": "2048"}
        )
        calls: list = []
        result = check_image_url(
            self.VALID_URL, opener=make_opener(response=response, calls=calls)
        )
        self.assertEqual(result, {"content_type": "image/jpeg", "content_length": 2048})
        self.assertEqual(calls, [(self.VALID_URL, "HEAD", 10)])
        self.assertTrue(response.closed)  # the app never keeps the connection open

    def test_never_makes_more_than_the_one_head_request(self):
        response = FakeResponse(200, {"Content-Type": "image/png", "Content-Length": "10"})
        calls: list = []
        check_image_url(self.VALID_URL, opener=make_opener(response=response, calls=calls))
        self.assertEqual(len(calls), 1)

    def test_rejects_non_https_scheme(self):
        with self.assertRaises(ImageUrlError) as failure:
            check_image_url("http://images.example.com/photo.jpg", opener=make_opener())
        self.assertEqual(failure.exception.code, "IMAGE_URL_INVALID")

    def test_rejects_embedded_userinfo(self):
        with self.assertRaises(ImageUrlError) as failure:
            check_image_url(
                "https://user:pass@images.example.com/photo.jpg", opener=make_opener()
            )
        self.assertEqual(failure.exception.code, "IMAGE_URL_INVALID")

    def test_rejects_fragment(self):
        with self.assertRaises(ImageUrlError) as failure:
            check_image_url("https://images.example.com/photo.jpg#frag", opener=make_opener())
        self.assertEqual(failure.exception.code, "IMAGE_URL_INVALID")

    def test_rejects_missing_hostname(self):
        with self.assertRaises(ImageUrlError) as failure:
            check_image_url("https:///photo.jpg", opener=make_opener())
        self.assertEqual(failure.exception.code, "IMAGE_URL_INVALID")

    def test_rejects_url_over_max_length(self):
        long_url = "https://images.example.com/" + ("a" * 2000) + ".jpg"
        with self.assertRaises(ImageUrlError) as failure:
            check_image_url(long_url, opener=make_opener())
        self.assertEqual(failure.exception.code, "IMAGE_URL_INVALID")

    def test_rejects_non_string_url(self):
        with self.assertRaises(ImageUrlError) as failure:
            check_image_url(12345, opener=make_opener())  # type: ignore[arg-type]
        self.assertEqual(failure.exception.code, "IMAGE_URL_INVALID")

    def test_rejects_loopback_host(self):
        with self.assertRaises(ImageUrlError) as failure:
            check_image_url("https://localhost/photo.jpg", opener=make_opener())
        self.assertEqual(failure.exception.code, "IMAGE_URL_PRIVATE_HOST")

    def test_rejects_private_ipv4_host(self):
        with self.assertRaises(ImageUrlError) as failure:
            check_image_url("https://10.0.0.5/photo.jpg", opener=make_opener())
        self.assertEqual(failure.exception.code, "IMAGE_URL_PRIVATE_HOST")

    def test_rejects_link_local_host(self):
        with self.assertRaises(ImageUrlError) as failure:
            check_image_url("https://169.254.169.254/photo.jpg", opener=make_opener())
        self.assertEqual(failure.exception.code, "IMAGE_URL_PRIVATE_HOST")

    def test_rejects_unresolvable_host(self):
        def raising_getaddrinfo(*args, **kwargs):
            raise OSError("name resolution failed")

        with patch("ebay_app.image_fetch.socket.getaddrinfo", side_effect=raising_getaddrinfo):
            with self.assertRaises(ImageUrlError) as failure:
                check_image_url(self.VALID_URL, opener=make_opener())
        self.assertEqual(failure.exception.code, "IMAGE_URL_UNREACHABLE")

    def test_head_http_error_fails_closed(self):
        error = urllib.error.HTTPError(self.VALID_URL, 404, "Not Found", {}, None)
        with self.assertRaises(ImageUrlError) as failure:
            check_image_url(self.VALID_URL, opener=make_opener(error=error))
        self.assertEqual(failure.exception.code, "IMAGE_URL_UNREACHABLE")

    def test_head_connection_error_fails_closed(self):
        error = urllib.error.URLError("connection refused")
        with self.assertRaises(ImageUrlError) as failure:
            check_image_url(self.VALID_URL, opener=make_opener(error=error))
        self.assertEqual(failure.exception.code, "IMAGE_URL_UNREACHABLE")

    def test_head_timeout_fails_closed(self):
        with self.assertRaises(ImageUrlError) as failure:
            check_image_url(self.VALID_URL, opener=make_opener(error=TimeoutError("slow")))
        self.assertEqual(failure.exception.code, "IMAGE_URL_UNREACHABLE")

    def test_non_2xx_status_fails_closed(self):
        response = FakeResponse(500, {"Content-Type": "image/jpeg", "Content-Length": "10"})
        with self.assertRaises(ImageUrlError) as failure:
            check_image_url(self.VALID_URL, opener=make_opener(response=response))
        self.assertEqual(failure.exception.code, "IMAGE_URL_UNREACHABLE")
        self.assertTrue(response.closed)

    def test_unsupported_content_type_is_rejected(self):
        response = FakeResponse(200, {"Content-Type": "text/html", "Content-Length": "10"})
        with self.assertRaises(ImageUrlError) as failure:
            check_image_url(self.VALID_URL, opener=make_opener(response=response))
        self.assertEqual(failure.exception.code, "IMAGE_TYPE_UNSUPPORTED")

    def test_every_allowed_content_type_is_accepted(self):
        for content_type in sorted(ALLOWED_CONTENT_TYPES):
            with self.subTest(content_type=content_type):
                response = FakeResponse(200, {"Content-Type": content_type, "Content-Length": "10"})
                result = check_image_url(self.VALID_URL, opener=make_opener(response=response))
                self.assertEqual(result["content_type"], content_type)

    def test_missing_content_length_is_rejected(self):
        response = FakeResponse(200, {"Content-Type": "image/png"})
        with self.assertRaises(ImageUrlError) as failure:
            check_image_url(self.VALID_URL, opener=make_opener(response=response))
        self.assertEqual(failure.exception.code, "IMAGE_URL_MISSING_LENGTH")

    def test_non_numeric_content_length_is_rejected(self):
        response = FakeResponse(200, {"Content-Type": "image/png", "Content-Length": "not-a-number"})
        with self.assertRaises(ImageUrlError) as failure:
            check_image_url(self.VALID_URL, opener=make_opener(response=response))
        self.assertEqual(failure.exception.code, "IMAGE_URL_MISSING_LENGTH")

    def test_zero_content_length_is_rejected(self):
        response = FakeResponse(200, {"Content-Type": "image/png", "Content-Length": "0"})
        with self.assertRaises(ImageUrlError) as failure:
            check_image_url(self.VALID_URL, opener=make_opener(response=response))
        self.assertEqual(failure.exception.code, "IMAGE_TOO_LARGE")

    def test_oversized_image_is_rejected(self):
        response = FakeResponse(
            200, {"Content-Type": "image/png", "Content-Length": str(MAX_IMAGE_BYTES + 1)}
        )
        with self.assertRaises(ImageUrlError) as failure:
            check_image_url(self.VALID_URL, opener=make_opener(response=response))
        self.assertEqual(failure.exception.code, "IMAGE_TOO_LARGE")

    def test_exact_max_size_is_accepted(self):
        response = FakeResponse(
            200, {"Content-Type": "image/png", "Content-Length": str(MAX_IMAGE_BYTES)}
        )
        result = check_image_url(self.VALID_URL, opener=make_opener(response=response))
        self.assertEqual(result["content_length"], MAX_IMAGE_BYTES)

    def test_error_message_never_includes_raw_response_headers(self):
        response = FakeResponse(
            500,
            {
                "Content-Type": "image/jpeg",
                "Content-Length": "10",
                "X-Secret": "PRIVATE-LEAK",
            },
        )
        with self.assertRaises(ImageUrlError) as failure:
            check_image_url(self.VALID_URL, opener=make_opener(response=response))
        self.assertNotIn("PRIVATE-LEAK", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
