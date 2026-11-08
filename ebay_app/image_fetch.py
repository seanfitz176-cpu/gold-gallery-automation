"""Phase 8: read-only validation of a seller-supplied external image URL.

This is a deliberately different kind of network call from the rest of the
application: every other outbound call in this app goes to eBay's own
Sandbox APIs (a fixed, trusted host). This module makes a request to a
URL the seller typed in themselves -- an untrusted destination -- purely to
confirm it looks like a reachable, reasonably-sized image before the app
ever asks eBay to store it as a listing photo. It never asks eBay anything
and never mutates anything; ``check_image_url`` is a pure read.

Because the destination is untrusted, this module is deliberately
conservative and fails closed:

- https only, no embedded username/password, no fragment
- the hostname is resolved and rejected if it points at a loopback,
  link-local, private, reserved, multicast, or otherwise non-public address
  (a minimal SSRF guard -- this app runs as a local single-user tool, so it
  is not a complete defense against DNS-rebinding, but it stops the obvious
  cases: localhost, 127.0.0.1, 169.254.x.x, 10.x.x.x, etc.)
- a HEAD request only -- the image bytes themselves are never downloaded by
  this application; if a server does not answer HEAD, this fails closed
  rather than falling back to a full GET
- the response must report one of a small set of image Content-Types
- the response must report a Content-Length, and it must not exceed eBay's
  own documented 12 MB per-picture limit -- a URL that omits Content-Length
  is rejected rather than trusted
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional

MAX_IMAGE_BYTES = 12 * 1024 * 1024  # eBay's own documented per-picture limit
ALLOWED_CONTENT_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/bmp", "image/webp"}
)
REQUEST_TIMEOUT_SECONDS = 10
MAX_URL_LENGTH = 2000

Opener = Callable[[urllib.request.Request, float], Any]


class ImageUrlError(Exception):
    """A safe, non-sensitive reason an image URL was rejected.

    ``message`` is always safe to show in the browser and to persist --
    it never includes response bodies, headers, or raw exception text from
    the remote server.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _default_opener(request: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(request, timeout=timeout)


def _reject_private_host(hostname: str) -> None:
    """Best-effort SSRF guard: resolve the hostname and reject non-public IPs."""

    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError as exc:
        raise ImageUrlError(
            "That image address could not be resolved.", code="IMAGE_URL_UNREACHABLE"
        ) from exc
    for info in infos:
        raw_address = info[4][0]
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError:
            continue
        if (
            address.is_loopback
            or address.is_link_local
            or address.is_private
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise ImageUrlError(
                "That image address is not allowed.", code="IMAGE_URL_PRIVATE_HOST"
            )


def check_image_url(
    url: str,
    *,
    opener: Opener = _default_opener,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Confirm ``url`` looks like a reachable, appropriately-sized image.

    Returns ``{"content_type": str, "content_length": int}`` on success, or
    raises ``ImageUrlError`` with a safe message and code. Never returns
    the image bytes themselves and never follows a redirect to a
    non-https destination.
    """

    if not isinstance(url, str) or len(url) > MAX_URL_LENGTH:
        raise ImageUrlError("Enter a valid image address.", code="IMAGE_URL_INVALID")

    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise ImageUrlError(
            "Enter a valid image address.", code="IMAGE_URL_INVALID"
        ) from exc

    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ImageUrlError(
            "Image addresses must be a plain https:// link.",
            code="IMAGE_URL_INVALID",
        )

    _reject_private_host(parsed.hostname)

    request = urllib.request.Request(
        url,
        method="HEAD",
        headers={
            # Some image hosts (Wikimedia among them) reject requests that
            # carry urllib's generic default User-Agent as a basic anti-bot
            # measure, which would otherwise fail every reachable, valid
            # image URL from those hosts closed with an unreachable error.
            # A descriptive, honest User-Agent avoids that false negative
            # without changing anything else about the request -- it is
            # still HEAD-only, still fails closed on any ambiguity, and
            # still never downloads the image bytes themselves.
            "User-Agent": "eBaySandboxConsole/1.0 (local seller-image URL check)"
        },
    )
    try:
        response = opener(request, timeout)
    except urllib.error.HTTPError as error:
        raise ImageUrlError(
            "That image address could not be reached.",
            code="IMAGE_URL_UNREACHABLE",
        ) from error
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise ImageUrlError(
            "That image address could not be reached.",
            code="IMAGE_URL_UNREACHABLE",
        ) from error

    try:
        status = getattr(response, "status", None)
        if status is None:
            status = response.getcode()
        headers = response.headers
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()

    if not (200 <= int(status) < 300):
        raise ImageUrlError(
            "That image address could not be reached.",
            code="IMAGE_URL_UNREACHABLE",
        )

    content_type = str(headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ImageUrlError(
            "That address is not a supported image type "
            "(JPEG, PNG, GIF, BMP, or WEBP).",
            code="IMAGE_TYPE_UNSUPPORTED",
        )

    raw_length = headers.get("Content-Length")
    if raw_length is None:
        raise ImageUrlError(
            "That image address did not report a file size.",
            code="IMAGE_URL_MISSING_LENGTH",
        )
    try:
        content_length = int(raw_length)
    except ValueError as exc:
        raise ImageUrlError(
            "That image address did not report a valid file size.",
            code="IMAGE_URL_MISSING_LENGTH",
        ) from exc
    if content_length <= 0 or content_length > MAX_IMAGE_BYTES:
        raise ImageUrlError(
            "Images must be 12 MB or smaller.", code="IMAGE_TOO_LARGE"
        )

    return {"content_type": content_type, "content_length": content_length}
