"""
Verification of eBay "Marketplace Account Deletion/Closure" push notifications.

SECURITY CONTRACT
=================
* ``verify_notification(...)`` either RETURNS a ``VerifiedNotification`` or
  RAISES ``NotificationVerificationError``. There is no third outcome: every
  internal failure (bad header, unknown alg/digest, key-fetch failure,
  malformed key, signature mismatch, or *any* unexpected exception) is
  converted into ``NotificationVerificationError``. Nothing returns ``False``
  or ``None`` that a caller could mistake for success.
* ``VerifiedNotification`` cannot be built by callers (constructor is guarded
  by a private token), so "I caught the exception and carried on" still leaves
  the caller without a verified object.
* ``extract_user_id(...)`` only accepts a ``VerifiedNotification``; a raw
  dict is refused. The payload it reads is the exact object whose bytes were
  cryptographically verified (never a second, independent parse).

WHAT IS SIGNED
==============
eBay's ``x-ebay-signature`` header is base64(JSON) with string fields
``alg`` ("ecdsa"), ``kid``, ``signature`` (base64 of an ASN.1/DER ECDSA
signature) and ``digest`` ("SHA1"). The curve is NIST P-256.

All four official eBay SDKs (Node, Java, Go, PHP) verify the signature over a
*re-serialization of the parsed JSON message*, never over the raw HTTP body
(e.g. Node ``lib/validator.js`` ``validateSignature``:
``verifier.update(JSON.stringify(message))``). This module therefore tries:

  1. the exact raw request body bytes, then
  2. the compact JSON re-serialization of the parsed body
     (``separators=(",", ":")``, key order preserved, non-ASCII emitted as
     UTF-8, i.e. the same bytes JavaScript's ``JSON.stringify`` produces for
     ordinary notification payloads).

Both candidates are representations of the *same* parsed object, and that
object is what ``VerifiedNotification`` carries, so the extra candidate does
not let an attacker get anything consumed that eBay did not sign.

PUBLIC KEY SHAPE (what ``fetch_public_key`` must return)
========================================================
``fetch_public_key(kid)`` must perform the authenticated
``GET https://api.ebay.com/commerce/notification/v1/public_key/{kid}``
(application token, scope ``https://api.ebay.com/oauth/api_scope``) and return
the parsed JSON response body as a ``dict``, unchanged, e.g.::

    {
      "key": "-----BEGIN PUBLIC KEY-----MFkwEwYH...FUg==-----END PUBLIC KEY-----",
      "algorithm": "ECDSA",
      "digest": "SHA1"
    }

(``key`` is a PEM-armoured X.509 SubjectPublicKeyInfo with NO newlines.)
It should raise on any non-200 response / transport error. Any exception it
raises is treated as "not verified".

Provenance: this module was built and self-tested (46/46 passing, including a
real eBay SDK test vector) in isolation before being added to this
repository; see ``tests/test_notification_signature.py`` for the suite that
now lives alongside it here.
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Union

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

__all__ = [
    "NotificationVerificationError",
    "InvalidUserIdError",
    "VerifiedNotification",
    "EbayPublicKey",
    "PublicKeyCache",
    "parse_public_key_response",
    "verify_notification",
    "extract_user_id",
    "ACCOUNT_DELETION_TOPIC",
]

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

EXPECTED_HEADER_ALG = "ecdsa"          # value in x-ebay-signature header
EXPECTED_HEADER_DIGEST = "SHA1"        # value in x-ebay-signature header
EXPECTED_KEY_ALGORITHM = "ECDSA"       # value in public_key response
EXPECTED_KEY_DIGEST = "SHA1"           # value in public_key response
ACCOUNT_DELETION_TOPIC = "MARKETPLACE_ACCOUNT_DELETION"

DEFAULT_MAX_BODY_BYTES = 64 * 1024     # real payloads are ~500 bytes
MAX_HEADER_CHARS = 4096
MAX_DER_SIGNATURE_BYTES = 80           # P-256 DER signatures are <= 72 bytes
MAX_USER_ID_CHARS = 256

# kid is placed into a URL path by the caller's fetcher. Real kids are UUIDs.
# Deliberately excludes '.', '/', '%' etc. so it can never form a path
# traversal or query injection.
_KID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")
_PEM_RE = re.compile(
    r"\A\s*(?:-----BEGIN PUBLIC KEY-----)?([A-Za-z0-9+/=\s]+?)(?:-----END PUBLIC KEY-----)?\s*\Z"
)

MIN_CACHE_TTL_SECONDS = 3600.0         # eBay: cache for at least one hour
DEFAULT_CACHE_TTL_SECONDS = 3600.0


# --------------------------------------------------------------------------
# Errors and result types
# --------------------------------------------------------------------------

class NotificationVerificationError(Exception):
    """The notification is NOT verified. Respond to eBay with 412 and do
    not act on the payload. ``reason`` is a short machine-friendly code
    suitable for logging (it never contains the payload or key material)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class InvalidUserIdError(Exception):
    """A verified notification did not contain a usable userId / topic."""


_CONSTRUCTION_TOKEN = object()


class VerifiedNotification:
    """Proof that a notification body passed signature verification.

    Only this module can create instances. ``payload`` returns a deep copy
    of the verified, parsed JSON object, so it cannot be mutated afterwards.
    """

    __slots__ = ("_payload", "_kid", "_matched_form")

    def __init__(self, _token: object, payload: dict, kid: str, matched_form: str):
        if _token is not _CONSTRUCTION_TOKEN:
            raise TypeError("VerifiedNotification can only be created by verify_notification()")
        self._payload = payload
        self._kid = kid
        self._matched_form = matched_form

    @property
    def payload(self) -> dict:
        return copy.deepcopy(self._payload)

    @property
    def kid(self) -> str:
        return self._kid

    @property
    def matched_form(self) -> str:
        """'raw' or 'compact' -- which byte form the signature matched."""
        return self._matched_form

    def __repr__(self) -> str:  # avoid dumping PII into logs
        return f"VerifiedNotification(kid={self._kid!r}, matched_form={self._matched_form!r})"


@dataclass(frozen=True)
class EbayPublicKey:
    """A parsed, validated eBay notification public key (P-256)."""
    kid: str
    public_key: ec.EllipticCurvePublicKey


# --------------------------------------------------------------------------
# Strict helpers
# --------------------------------------------------------------------------

def _strict_b64decode(value: Any, what: str, max_len: int) -> bytes:
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise NotificationVerificationError(f"{what}_not_a_valid_string")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        raise NotificationVerificationError(f"{what}_not_base64") from None


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


def _no_duplicate_keys(pairs: list) -> dict:
    result: dict = {}
    for k, v in pairs:
        if k in result:
            raise ValueError("duplicate JSON key")
        result[k] = v
    return result


def _strict_json_loads(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)


def _compact_serialize(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _parse_signature_header(header_value: Any) -> tuple[str, bytes]:
    raw = _strict_b64decode(header_value, "signature_header", MAX_HEADER_CHARS)
    try:
        decoded = _strict_json_loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise NotificationVerificationError("signature_header_not_json") from None
    if not isinstance(decoded, dict):
        raise NotificationVerificationError("signature_header_not_object")

    alg = decoded.get("alg")
    digest = decoded.get("digest")
    kid = decoded.get("kid")
    sig_b64 = decoded.get("signature")

    # Exact, case-sensitive matches only. Checked BEFORE any key fetch.
    if not isinstance(alg, str) or alg != EXPECTED_HEADER_ALG:
        raise NotificationVerificationError("unsupported_alg")
    if not isinstance(digest, str) or digest != EXPECTED_HEADER_DIGEST:
        raise NotificationVerificationError("unsupported_digest")
    if not isinstance(kid, str) or not _KID_RE.match(kid):
        raise NotificationVerificationError("invalid_kid")
    signature = _strict_b64decode(sig_b64, "signature", MAX_HEADER_CHARS)
    if not signature or len(signature) > MAX_DER_SIGNATURE_BYTES:
        raise NotificationVerificationError("invalid_signature_length")
    return kid, signature


def parse_public_key_response(kid: str, response: Any) -> EbayPublicKey:
    """Validate and parse the JSON returned by eBay's getPublicKey call.

    Requires ``algorithm == "ECDSA"``, ``digest == "SHA1"`` and a ``key``
    that is an X.509 SubjectPublicKeyInfo for curve P-256 (PEM armour
    optional, newlines optional). Raises NotificationVerificationError.
    """
    if not isinstance(response, Mapping):
        raise NotificationVerificationError("public_key_response_not_object")
    if response.get("algorithm") != EXPECTED_KEY_ALGORITHM:
        raise NotificationVerificationError("public_key_unexpected_algorithm")
    if response.get("digest") != EXPECTED_KEY_DIGEST:
        raise NotificationVerificationError("public_key_unexpected_digest")
    key_text = response.get("key")
    if not isinstance(key_text, str) or len(key_text) > 4096:
        raise NotificationVerificationError("public_key_missing")
    m = _PEM_RE.match(key_text)
    if not m:
        raise NotificationVerificationError("public_key_bad_format")
    b64 = re.sub(r"\s+", "", m.group(1))
    try:
        der = base64.b64decode(b64, validate=True)
        key = serialization.load_der_public_key(der)
    except Exception:
        raise NotificationVerificationError("public_key_unparseable") from None
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise NotificationVerificationError("public_key_not_p256")
    return EbayPublicKey(kid=kid, public_key=key)


# --------------------------------------------------------------------------
# Public key cache
# --------------------------------------------------------------------------

FetchFn = Callable[[str], Mapping[str, Any]]


class PublicKeyCache:
    """Thread-safe kid -> EbayPublicKey cache.

    * Positive entries live ``ttl_seconds`` (minimum 3600, per eBay).
    * Failed fetches are remembered for ``negative_ttl_seconds`` so a flood
      of forged headers with random kids cannot turn into a flood of
      authenticated calls against the app's eBay rate limit. Set to 0 to
      disable.
    * At most ``max_entries`` keys are held (LRU eviction).
    * ``clock`` is injectable for tests (defaults to ``time.monotonic``).

    The lock is held across the fetch: concurrent requests for the same
    unknown kid result in exactly one upstream call.
    """

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        *,
        negative_ttl_seconds: float = 60.0,
        max_entries: int = 64,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not isinstance(ttl_seconds, (int, float)) or ttl_seconds < MIN_CACHE_TTL_SECONDS:
            raise ValueError(f"ttl_seconds must be >= {MIN_CACHE_TTL_SECONDS} (eBay guidance)")
        if negative_ttl_seconds < 0 or max_entries < 1:
            raise ValueError("invalid cache configuration")
        self._ttl = float(ttl_seconds)
        self._neg_ttl = float(negative_ttl_seconds)
        self._max = int(max_entries)
        self._clock = clock
        self._entries: "OrderedDict[str, tuple[float, EbayPublicKey]]" = OrderedDict()
        self._failures: "OrderedDict[str, float]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key_id: str, fetch_fn: FetchFn) -> EbayPublicKey:
        """Return the parsed key for ``key_id``; fetch via ``fetch_fn`` on miss.
        Raises NotificationVerificationError on any failure."""
        with self._lock:
            now = self._clock()
            hit = self._entries.get(key_id)
            if hit is not None:
                fetched_at, key = hit
                if now - fetched_at < self._ttl:
                    self._entries.move_to_end(key_id)
                    return key
                del self._entries[key_id]

            failed_at = self._failures.get(key_id)
            if failed_at is not None:
                if now - failed_at < self._neg_ttl:
                    raise NotificationVerificationError("public_key_recently_failed")
                del self._failures[key_id]

            try:
                response = fetch_fn(key_id)
                key = parse_public_key_response(key_id, response)
            except Exception as exc:
                if self._neg_ttl > 0:
                    self._failures[key_id] = now
                    while len(self._failures) > self._max * 16:
                        self._failures.popitem(last=False)
                if isinstance(exc, NotificationVerificationError):
                    raise
                raise NotificationVerificationError("public_key_fetch_failed") from None

            self._entries[key_id] = (now, key)
            self._entries.move_to_end(key_id)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)
            return key

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._failures.clear()


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

def _ecdsa_sha1_valid(public_key: ec.EllipticCurvePublicKey, signature: bytes, data: bytes) -> bool:
    try:
        public_key.verify(signature, data, ec.ECDSA(hashes.SHA1()))
    except InvalidSignature:
        return False
    return True


def _verify_inner(
    body: Any,
    signature_header: Any,
    fetch_public_key: FetchFn,
    cache: PublicKeyCache,
    max_body_bytes: int,
) -> VerifiedNotification:
    if not isinstance(body, (bytes, bytearray)):
        raise NotificationVerificationError("body_must_be_bytes")
    body = bytes(body)
    if not body or len(body) > max_body_bytes:
        raise NotificationVerificationError("body_size_out_of_range")

    # 1. Header (alg/digest/kid checked before any network call).
    kid, signature = _parse_signature_header(signature_header)

    # 2. Body must be a strict UTF-8 JSON object.
    try:
        payload = _strict_json_loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise NotificationVerificationError("body_not_json") from None
    if not isinstance(payload, dict):
        raise NotificationVerificationError("body_not_object")

    # 3. Key.
    ebay_key = cache.get(kid, fetch_public_key)

    # 4. Signature over raw bytes, then over the compact re-serialization of
    #    the SAME parsed object we will hand back.
    if _ecdsa_sha1_valid(ebay_key.public_key, signature, body):
        return VerifiedNotification(_CONSTRUCTION_TOKEN, payload, kid, "raw")
    compact = _compact_serialize(payload)
    if compact != body and _ecdsa_sha1_valid(ebay_key.public_key, signature, compact):
        return VerifiedNotification(_CONSTRUCTION_TOKEN, payload, kid, "compact")
    raise NotificationVerificationError("signature_mismatch")


_DEFAULT_CACHE = PublicKeyCache()


def verify_notification(
    body: Union[bytes, bytearray],
    signature_header: Optional[str],
    fetch_public_key: FetchFn,
    *,
    cache: Optional[PublicKeyCache] = None,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> VerifiedNotification:
    """Verify one eBay notification.

    :param body: the RAW HTTP request body exactly as received (``bytes``;
        ``str`` is refused so no framework re-decoding can sneak in).
    :param signature_header: raw value of the ``x-ebay-signature`` header
        (``None`` if absent -> not verified).
    :param fetch_public_key: ``fetch_public_key(kid) -> dict`` returning the
        parsed JSON of eBay's getPublicKey response (see module docstring).
    :param cache: a ``PublicKeyCache``; defaults to a module-level 1-hour cache.
    :returns: ``VerifiedNotification`` -- the ONLY success outcome.
    :raises NotificationVerificationError: on every other outcome.
    """
    try:
        return _verify_inner(
            body, signature_header, fetch_public_key,
            cache if cache is not None else _DEFAULT_CACHE, max_body_bytes,
        )
    except NotificationVerificationError:
        raise
    except Exception:  # fail closed on anything unexpected
        raise NotificationVerificationError("internal_error") from None


# --------------------------------------------------------------------------
# userId extraction
# --------------------------------------------------------------------------

def extract_user_id(
    verified: VerifiedNotification,
    *,
    expected_topic: Optional[str] = ACCOUNT_DELETION_TOPIC,
) -> str:
    """Return ``notification.data.userId`` from a VERIFIED notification.

    Also requires ``metadata.topic == expected_topic`` (default
    MARKETPLACE_ACCOUNT_DELETION) so a validly-signed notification for some
    other topic can never trigger a deletion. Pass ``expected_topic=None``
    to skip that check. Raises InvalidUserIdError.
    """
    if not isinstance(verified, VerifiedNotification):
        raise InvalidUserIdError("extract_user_id requires a VerifiedNotification")
    return _user_id_from_payload(verified._payload, expected_topic)


def _user_id_from_payload(payload: Any, expected_topic: Optional[str]) -> str:
    if not isinstance(payload, dict):
        raise InvalidUserIdError("payload is not an object")
    if expected_topic is not None:
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("topic") != expected_topic:
            raise InvalidUserIdError("unexpected or missing metadata.topic")
    notification = payload.get("notification")
    if not isinstance(notification, dict):
        raise InvalidUserIdError("missing notification object")
    data = notification.get("data")
    if not isinstance(data, dict):
        raise InvalidUserIdError("missing notification.data object")
    user_id = data.get("userId")
    if not isinstance(user_id, str):
        raise InvalidUserIdError("notification.data.userId is missing or not a string")
    if not user_id or not user_id.strip() or user_id != user_id.strip():
        raise InvalidUserIdError("notification.data.userId is empty or has surrounding whitespace")
    if len(user_id) > MAX_USER_ID_CHARS:
        raise InvalidUserIdError("notification.data.userId is too long")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in user_id):
        raise InvalidUserIdError("notification.data.userId contains control characters")
    return user_id
