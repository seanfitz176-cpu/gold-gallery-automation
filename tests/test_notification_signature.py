"""unittest suite for ebay_app.notification_signature.

Run:  python -m unittest -v tests.test_notification_signature
"""

import base64
import json
import threading
import unittest

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

import ebay_app.notification_signature as ens
from ebay_app.notification_signature import (
    InvalidUserIdError,
    NotificationVerificationError,
    PublicKeyCache,
    VerifiedNotification,
    extract_user_id,
    verify_notification,
)

# ---------------------------------------------------------------------------
# REAL eBay-produced test vector.
# Source: github.com/eBay/event-notification-nodejs-sdk, test/test.json, entry
# "VALID" (commit feaf3378ca263a81432cf5b8c8a6fd8cb3d3e2f3). Byte-identical
# copies are in event-notification-golang-sdk test/test.json, and the same
# header/key/message are in event-notification-java-sdk
# src/test/java/.../data/{DataProvider.java,message.json,public_key_response.json}.
# "SIGNATURE_MISMATCH" in the same file supplies a second real header whose
# signature does NOT match this message.
# ---------------------------------------------------------------------------
REAL_KID = "9936261a-7d7b-4621-a0f1-96ccb428af49"
REAL_HEADER = (
    "eyJhbGciOiJlY2RzYSIsImtpZCI6Ijk5MzYyNjFhLTdkN2ItNDYyMS1hMGYxLTk2Y2NiNDI4YWY0OSIsInNpZ25h"
    "dHVyZSI6Ik1FWUNJUUNmeGZJV3V4bVdjSUJRSjljNS9YN2lHREpxczJSQ0dzQkVhQWppbnlycmZBSWhBSVY2d0dj"
    "VGlCdVY1S0pVaWYyaG9reXJMK1E5c3NIa2FkK214Mm5FRTI1dyIsImRpZ2VzdCI6IlNIQTEifQ=="
)
REAL_MISMATCH_HEADER = (
    "eyJhbGciOiJlY2RzYSIsImtpZCI6Ijk5MzYyNjFhLTdkN2ItNDYyMS1hMGYxLTk2Y2NiNDI4YWY0OSIsInNpZ25h"
    "dHVyZSI6Ik1FVUNJUUNHY1NubUFrVGZyK1paMlZnMGJXRW9zOGEvdGVCcWk3UGU2OCtoR21MTUNRSWdlRnZrcnRv"
    "KzhkczhSVndJM0dnbjFtTUdDck5NRVpKM1NSbE8yZngveHFJPSIsImRpZ2VzdCI6IlNIQTEifQ=="
)
REAL_KEY_RESPONSE = {
    "key": "-----BEGIN PUBLIC KEY-----MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEZhhxXKtR+TOvtDbgTPCkSof02qgBB7IsYOyf76ilExJ/upAa/vKIKheOoCyOpcLmi4t0b4uepb7LLjmMr90FUg==-----END PUBLIC KEY-----",
    "algorithm": "ECDSA",
    "digest": "SHA1",
}
REAL_MESSAGE = {
    "metadata": {"topic": "MARKETPLACE_ACCOUNT_DELETION", "schemaVersion": "1.0", "deprecated": False},
    "notification": {
        "notificationId": "49feeaeb-4982-42d9-a377-9645b8479411_33f7e043-fed8-442b-9d44-791923bd9a6d",
        "eventDate": "2021-03-19T20:43:59.462Z",
        "publishDate": "2021-03-19T20:43:59.679Z",
        "publishAttemptCount": 1,
        "data": {
            "username": "test_user",
            "userId": "ma8vp1jySJC",
            "eiasToken": "nY+sHZ2PrBmdj6wVnY+sEZ2PrA2dj6wJnY+gAZGEpwmdj6x9nY+seQ==",
        },
    },
}
REAL_BODY_COMPACT = json.dumps(REAL_MESSAGE, separators=(",", ":")).encode()
REAL_BODY_PRETTY = json.dumps(REAL_MESSAGE, indent=2).encode()


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def make_header(kid, sig_bytes, alg="ecdsa", digest="SHA1", extra=None, drop=()):
    d = {"alg": alg, "kid": kid, "signature": b64(sig_bytes), "digest": digest}
    if extra:
        d.update(extra)
    for k in drop:
        d.pop(k, None)
    return b64(json.dumps(d).encode())


def key_response_for(public_key):
    pem = public_key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    # eBay's format: PEM armour with no newlines
    flat = pem.replace("\n", "")
    return {"key": flat, "algorithm": "ECDSA", "digest": "SHA1"}


class FakeFetcher:
    def __init__(self, responses=None, exc=None):
        self.responses = responses or {}
        self.exc = exc
        self.calls = []

    def __call__(self, kid):
        self.calls.append(kid)
        if self.exc is not None:
            raise self.exc
        return self.responses[kid]


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def fresh_cache(**kw):
    return PublicKeyCache(**kw)


# ---------------------------------------------------------------------------
# Real eBay vector
# ---------------------------------------------------------------------------
class RealEbayVectorTests(unittest.TestCase):
    def setUp(self):
        self.fetch = FakeFetcher({REAL_KID: REAL_KEY_RESPONSE})

    def test_real_vector_compact_body_verifies_as_raw(self):
        v = verify_notification(REAL_BODY_COMPACT, REAL_HEADER, self.fetch, cache=fresh_cache())
        self.assertIsInstance(v, VerifiedNotification)
        self.assertEqual(v.matched_form, "raw")
        self.assertEqual(v.kid, REAL_KID)
        self.assertEqual(extract_user_id(v), "ma8vp1jySJC")

    def test_real_vector_pretty_body_verifies_via_compact_fallback(self):
        v = verify_notification(REAL_BODY_PRETTY, REAL_HEADER, self.fetch, cache=fresh_cache())
        self.assertEqual(v.matched_form, "compact")
        self.assertEqual(extract_user_id(v), "ma8vp1jySJC")

    def test_real_vector_tampered_user_id_rejected(self):
        msg = json.loads(REAL_BODY_COMPACT)
        msg["notification"]["data"]["userId"] = "someoneElse"
        for body in (json.dumps(msg, separators=(",", ":")).encode(), json.dumps(msg, indent=2).encode()):
            with self.assertRaises(NotificationVerificationError) as cm:
                verify_notification(body, REAL_HEADER, self.fetch, cache=fresh_cache())
            self.assertEqual(cm.exception.reason, "signature_mismatch")

    def test_real_vector_reordered_keys_rejected(self):
        msg = json.loads(REAL_BODY_COMPACT)
        reordered = {"notification": msg["notification"], "metadata": msg["metadata"]}
        with self.assertRaises(NotificationVerificationError):
            verify_notification(json.dumps(reordered).encode(), REAL_HEADER, self.fetch, cache=fresh_cache())

    def test_real_sdk_signature_mismatch_fixture_rejected(self):
        with self.assertRaises(NotificationVerificationError) as cm:
            verify_notification(REAL_BODY_COMPACT, REAL_MISMATCH_HEADER, self.fetch, cache=fresh_cache())
        self.assertEqual(cm.exception.reason, "signature_mismatch")

    def test_real_signature_with_wrong_alg_label_rejected_even_though_sig_is_valid(self):
        real = json.loads(base64.b64decode(REAL_HEADER))
        for alg, digest in (("ECDSA", "SHA1"), ("es256", "SHA1"), ("ecdsa", "sha1"),
                            ("ecdsa", "SHA256"), ("", "SHA1"), ("ecdsa ", "SHA1")):
            hdr = b64(json.dumps(dict(real, alg=alg, digest=digest)).encode())
            fetch = FakeFetcher({REAL_KID: REAL_KEY_RESPONSE})
            with self.assertRaises(NotificationVerificationError):
                verify_notification(REAL_BODY_COMPACT, hdr, fetch, cache=fresh_cache())
            self.assertEqual(fetch.calls, [], "must reject before fetching a key")

    def test_real_vector_with_wrong_public_key_rejected(self):
        other = ec.generate_private_key(ec.SECP256R1()).public_key()
        fetch = FakeFetcher({REAL_KID: key_response_for(other)})
        with self.assertRaises(NotificationVerificationError):
            verify_notification(REAL_BODY_COMPACT, REAL_HEADER, fetch, cache=fresh_cache())

    def test_real_key_response_with_wrong_metadata_rejected(self):
        for bad in (dict(REAL_KEY_RESPONSE, algorithm="RSA"), dict(REAL_KEY_RESPONSE, digest="SHA256"),
                    {k: v for k, v in REAL_KEY_RESPONSE.items() if k != "algorithm"}):
            fetch = FakeFetcher({REAL_KID: bad})
            with self.assertRaises(NotificationVerificationError):
                verify_notification(REAL_BODY_COMPACT, REAL_HEADER, fetch, cache=fresh_cache())


# ---------------------------------------------------------------------------
# Synthetic key pair tests
# ---------------------------------------------------------------------------
class SyntheticTests(unittest.TestCase):
    KID = "11111111-2222-3333-4444-555555555555"

    def setUp(self):
        self.priv = ec.generate_private_key(ec.SECP256R1())
        self.fetch = FakeFetcher({self.KID: key_response_for(self.priv.public_key())})
        self.msg = {
            "metadata": {"topic": "MARKETPLACE_ACCOUNT_DELETION", "schemaVersion": "1.0", "deprecated": False},
            "notification": {"notificationId": "n1", "data": {"username": "bob", "userId": "UID123", "eiasToken": "x"}},
        }
        self.body = json.dumps(self.msg, separators=(",", ":")).encode()

    def sign(self, data, key=None):
        return (key or self.priv).sign(data, ec.ECDSA(hashes.SHA1()))

    def verify(self, body, header, fetch=None, cache=None):
        return verify_notification(body, header, fetch or self.fetch, cache=cache or fresh_cache())

    def assertRejected(self, body, header, fetch=None, reason=None):
        with self.assertRaises(NotificationVerificationError) as cm:
            self.verify(body, header, fetch)
        if reason is not None:
            self.assertEqual(cm.exception.reason, reason)
        return cm.exception

    # --- success ---
    def test_valid_signature_verifies(self):
        v = self.verify(self.body, make_header(self.KID, self.sign(self.body)))
        self.assertEqual(v.matched_form, "raw")
        self.assertEqual(extract_user_id(v), "UID123")

    def test_signature_over_compact_form_accepted_for_whitespace_body(self):
        pretty = json.dumps(self.msg, indent=4).encode()
        v = self.verify(pretty, make_header(self.KID, self.sign(self.body)))
        self.assertEqual(v.matched_form, "compact")

    def test_non_ascii_payload_compact_form(self):
        self.msg["notification"]["data"]["username"] = "jürgen☃"
        compact = json.dumps(self.msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        pretty = json.dumps(self.msg, indent=2, ensure_ascii=True).encode()
        v = self.verify(pretty, make_header(self.KID, self.sign(compact)))
        self.assertEqual(v.matched_form, "compact")

    def test_payload_property_is_a_copy(self):
        v = self.verify(self.body, make_header(self.KID, self.sign(self.body)))
        p = v.payload
        p["notification"]["data"]["userId"] = "evil"
        self.assertEqual(extract_user_id(v), "UID123")

    # --- tampering ---
    def test_tampered_body_rejected(self):
        hdr = make_header(self.KID, self.sign(self.body))
        tampered = self.body.replace(b"UID123", b"UID124")
        self.assertRejected(tampered, hdr, reason="signature_mismatch")

    def test_appended_bytes_rejected(self):
        hdr = make_header(self.KID, self.sign(self.body))
        self.assertRejected(self.body + b"x", hdr)

    def test_tampered_signature_rejected(self):
        sig = bytearray(self.sign(self.body))
        sig[-1] ^= 0x01
        self.assertRejected(self.body, make_header(self.KID, bytes(sig)))

    def test_truncated_and_garbage_signature_rejected(self):
        sig = self.sign(self.body)
        for bad in (sig[:-1], sig[:8], b"\x00", b"\x30\x00", b"A" * 64):
            self.assertRejected(self.body, make_header(self.KID, bad))

    def test_raw_r_s_signature_rejected(self):
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        r, s = decode_dss_signature(self.sign(self.body))
        raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        self.assertRejected(self.body, make_header(self.KID, raw))

    def test_wrong_public_key_rejected(self):
        other = ec.generate_private_key(ec.SECP256R1())
        self.assertRejected(self.body, make_header(self.KID, self.sign(self.body, key=other)))

    def test_sha256_signature_rejected(self):
        sig = self.priv.sign(self.body, ec.ECDSA(hashes.SHA256()))
        self.assertRejected(self.body, make_header(self.KID, sig))

    def test_non_p256_key_rejected(self):
        p384 = ec.generate_private_key(ec.SECP384R1())
        fetch = FakeFetcher({self.KID: key_response_for(p384.public_key())})
        # A P-384 DER signature exceeds the P-256 size cap and is rejected even
        # earlier; use a P-256-sized signature so the key-curve check is reached.
        self.assertRejected(self.body, make_header(self.KID, p384.sign(self.body, ec.ECDSA(hashes.SHA1()))), fetch=fetch)
        self.assertRejected(self.body, make_header(self.KID, self.sign(self.body)), fetch=fetch,
                            reason="public_key_not_p256")

    # --- alg / digest ---
    def test_wrong_alg_or_digest_rejected_even_with_valid_signature(self):
        sig = self.sign(self.body)
        for alg, digest in (("rsa", "SHA1"), ("ECDSA", "SHA1"), ("ecdsa", "SHA256"),
                            ("ecdsa", "sha1"), ("none", "SHA1"), ("ecdsa", "")):
            fetch = FakeFetcher({self.KID: key_response_for(self.priv.public_key())})
            self.assertRejected(self.body, make_header(self.KID, sig, alg=alg, digest=digest), fetch=fetch)
            self.assertEqual(fetch.calls, [])

    def test_non_string_alg_digest_rejected(self):
        sig = self.sign(self.body)
        for extra in ({"alg": ["ecdsa"]}, {"digest": None}, {"alg": 1}):
            self.assertRejected(self.body, make_header(self.KID, sig, extra=extra))

    # --- malformed headers ---
    def test_missing_header_rejected(self):
        self.assertRejected(self.body, None)
        self.assertRejected(self.body, "")

    def test_malformed_headers_rejected(self):
        sig = self.sign(self.body)
        bad_headers = [
            "not base64!!!",
            b64(b"not json"),
            b64(b"[1,2,3]"),
            b64(b"\xff\xfe\x00"),
            b64(json.dumps({"alg": "ecdsa"}).encode()),
            make_header(self.KID, sig, drop=("kid",)),
            make_header(self.KID, sig, drop=("signature",)),
            make_header(self.KID, sig, drop=("alg",)),
            make_header(self.KID, sig, drop=("digest",)),
            make_header(self.KID, sig, extra={"signature": "@@@"}),
            make_header(self.KID, sig, extra={"signature": ""}),
            make_header(self.KID, sig, extra={"signature": 123}),
            make_header("../../oauth2/token", sig),
            make_header("a.b", sig),
            make_header("", sig),
            make_header("x" * 200, sig),
            make_header(self.KID + "\n", sig),
            b64(b'{"alg":"ecdsa","alg":"ecdsa","kid":"k","signature":"AA==","digest":"SHA1"}'),
            "A" * 10000,
            12345,
            b"bytes-header",
        ]
        for hdr in bad_headers:
            with self.subTest(hdr=str(hdr)[:60]):
                self.assertRejected(self.body, hdr)
        self.assertEqual(self.fetch.calls, [], "no malformed header may trigger a key fetch")

    # --- malformed bodies ---
    def test_malformed_bodies_rejected(self):
        hdr = make_header(self.KID, self.sign(self.body))
        for body in (self.body.decode(), b"", b"not json", b"[1]", b"\xff\xfe",
                     b'{"a":NaN}', b'{"a":1,"a":2}', b"x" * (ens.DEFAULT_MAX_BODY_BYTES + 1), None, 42):
            with self.subTest(body=repr(body)[:40]):
                self.assertRejected(body, hdr)

    def test_signed_but_non_json_body_rejected(self):
        body = b"hello"
        self.assertRejected(body, make_header(self.KID, self.sign(body)), reason="body_not_json")

    # --- key fetch failure ---
    def test_fetch_failure_is_not_verified(self):
        hdr = make_header(self.KID, self.sign(self.body))
        for exc in (RuntimeError("boom"), KeyError("x"), TimeoutError(), ValueError()):
            fetch = FakeFetcher(exc=exc)
            self.assertRejected(self.body, hdr, fetch=fetch, reason="public_key_fetch_failed")

    def test_fetch_returning_garbage_is_not_verified(self):
        hdr = make_header(self.KID, self.sign(self.body))
        for resp in (None, "string", [], {}, {"key": "", "algorithm": "ECDSA", "digest": "SHA1"},
                     {"key": "-----BEGIN PUBLIC KEY-----!!!-----END PUBLIC KEY-----", "algorithm": "ECDSA", "digest": "SHA1"},
                     {"key": 5, "algorithm": "ECDSA", "digest": "SHA1"}):
            with self.subTest(resp=resp):
                self.assertRejected(self.body, hdr, fetch=FakeFetcher({self.KID: resp}))

    def test_unexpected_internal_exception_fails_closed(self):
        class Exploding(PublicKeyCache):
            def get(self, key_id, fetch_fn):
                raise ZeroDivisionError()
        hdr = make_header(self.KID, self.sign(self.body))
        with self.assertRaises(NotificationVerificationError) as cm:
            verify_notification(self.body, hdr, self.fetch, cache=Exploding())
        self.assertEqual(cm.exception.reason, "internal_error")

    def test_newline_formatted_pem_accepted(self):
        pem = self.priv.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        fetch = FakeFetcher({self.KID: {"key": pem, "algorithm": "ECDSA", "digest": "SHA1"}})
        self.verify(self.body, make_header(self.KID, self.sign(self.body)), fetch=fetch)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
class CacheTests(unittest.TestCase):
    KID = "kid-1"

    def setUp(self):
        self.priv = ec.generate_private_key(ec.SECP256R1())
        self.fetch = FakeFetcher({self.KID: key_response_for(self.priv.public_key()),
                                  "kid-2": key_response_for(self.priv.public_key())})
        self.clock = FakeClock()

    def test_ttl_minimum_enforced(self):
        with self.assertRaises(ValueError):
            PublicKeyCache(ttl_seconds=3599)
        PublicKeyCache(ttl_seconds=3600)

    def test_hit_within_window_does_not_refetch(self):
        c = PublicKeyCache(clock=self.clock)
        k1 = c.get(self.KID, self.fetch)
        self.clock.t += 3599
        k2 = c.get(self.KID, self.fetch)
        self.assertIs(k1, k2)
        self.assertEqual(self.fetch.calls, [self.KID])

    def test_refetch_after_expiry(self):
        c = PublicKeyCache(clock=self.clock)
        c.get(self.KID, self.fetch)
        self.clock.t += 3600
        c.get(self.KID, self.fetch)
        self.assertEqual(self.fetch.calls, [self.KID, self.KID])

    def test_custom_longer_ttl(self):
        c = PublicKeyCache(ttl_seconds=7200, clock=self.clock)
        c.get(self.KID, self.fetch)
        self.clock.t += 7199
        c.get(self.KID, self.fetch)
        self.assertEqual(len(self.fetch.calls), 1)
        self.clock.t += 1
        c.get(self.KID, self.fetch)
        self.assertEqual(len(self.fetch.calls), 2)

    def test_failures_not_cached_as_success_and_negative_ttl(self):
        bad = FakeFetcher(exc=RuntimeError())
        c = PublicKeyCache(clock=self.clock, negative_ttl_seconds=60)
        with self.assertRaises(NotificationVerificationError):
            c.get(self.KID, bad)
        with self.assertRaises(NotificationVerificationError) as cm:
            c.get(self.KID, self.fetch)   # within negative window: no call
        self.assertEqual(cm.exception.reason, "public_key_recently_failed")
        self.assertEqual(self.fetch.calls, [])
        self.clock.t += 60
        c.get(self.KID, self.fetch)       # window over: real fetch succeeds
        self.assertEqual(self.fetch.calls, [self.KID])

    def test_negative_cache_can_be_disabled(self):
        c = PublicKeyCache(clock=self.clock, negative_ttl_seconds=0)
        with self.assertRaises(NotificationVerificationError):
            c.get(self.KID, FakeFetcher(exc=RuntimeError()))
        c.get(self.KID, self.fetch)
        self.assertEqual(self.fetch.calls, [self.KID])

    def test_lru_bound(self):
        c = PublicKeyCache(clock=self.clock, max_entries=1)
        c.get(self.KID, self.fetch)
        c.get("kid-2", self.fetch)
        c.get(self.KID, self.fetch)
        self.assertEqual(self.fetch.calls, [self.KID, "kid-2", self.KID])

    def test_cache_used_by_verify(self):
        c = PublicKeyCache(clock=self.clock)
        body = b'{"a":1}'
        hdr = make_header(self.KID, self.priv.sign(body, ec.ECDSA(hashes.SHA1())))
        verify_notification(body, hdr, self.fetch, cache=c)
        verify_notification(body, hdr, self.fetch, cache=c)
        self.assertEqual(self.fetch.calls, [self.KID])

    def test_concurrent_miss_fetches_once(self):
        c = PublicKeyCache()
        barrier = threading.Barrier(8)
        def worker():
            barrier.wait()
            c.get(self.KID, self.fetch)
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(self.fetch.calls, [self.KID])


# ---------------------------------------------------------------------------
# userId extraction
# ---------------------------------------------------------------------------
def _verified(payload):
    return VerifiedNotification(ens._CONSTRUCTION_TOKEN, payload, "kid", "raw")


def _payload_with(user_id=..., topic="MARKETPLACE_ACCOUNT_DELETION"):
    data = {"username": "bob"}
    if user_id is not ...:
        data["userId"] = user_id
    return {"metadata": {"topic": topic}, "notification": {"data": data}}


class ExtractUserIdTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(extract_user_id(_verified(_payload_with("ma8vp1jySJC"))), "ma8vp1jySJC")

    def test_rejects_bad_values(self):
        for bad in (None, 123, 1.5, True, [], {}, ["a"], "", "   ", " abc", "abc\n", "a\x00b",
                    "x" * 257):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidUserIdError):
                    extract_user_id(_verified(_payload_with(bad)))

    def test_rejects_missing(self):
        with self.assertRaises(InvalidUserIdError):
            extract_user_id(_verified(_payload_with()))
        for payload in ({}, {"metadata": {"topic": "MARKETPLACE_ACCOUNT_DELETION"}},
                        {"metadata": {"topic": "MARKETPLACE_ACCOUNT_DELETION"}, "notification": None},
                        {"metadata": {"topic": "MARKETPLACE_ACCOUNT_DELETION"}, "notification": {"data": "x"}}):
            with self.assertRaises(InvalidUserIdError):
                extract_user_id(_verified(payload))

    def test_does_not_fall_back_to_username(self):
        p = {"metadata": {"topic": "MARKETPLACE_ACCOUNT_DELETION"},
             "notification": {"data": {"username": "bob"}}}
        with self.assertRaises(InvalidUserIdError):
            extract_user_id(_verified(p))

    def test_rejects_other_topic(self):
        with self.assertRaises(InvalidUserIdError):
            extract_user_id(_verified(_payload_with("u1", topic="PRIORITY_LISTING_REVISION")))
        self.assertEqual(extract_user_id(_verified(_payload_with("u1", topic="OTHER")), expected_topic=None), "u1")

    def test_rejects_unverified_dict(self):
        with self.assertRaises(InvalidUserIdError):
            extract_user_id(_payload_with("u1"))

    def test_cannot_construct_verified_notification_externally(self):
        with self.assertRaises(TypeError):
            VerifiedNotification(object(), {}, "kid", "raw")


if __name__ == "__main__":
    unittest.main()
