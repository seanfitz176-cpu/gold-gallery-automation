"""Stdlib-only password hashing for the admin dashboard password.

``AppConfig.admin_password`` is read verbatim from an environment file
(``.env`` / ``.env.production``). Historically that value was compared
directly with ``hmac.compare_digest``, which means anyone able to read the
environment file could read the real dashboard password. This module lets
that value instead hold a salted PBKDF2 hash, verified without the plaintext
ever needing to exist on disk again -- while still accepting an existing
plaintext value so an already-deployed ``.env``/``.env.production`` keeps
working exactly as before until its owner chooses to migrate it.

No third-party dependency is introduced: this uses only ``hashlib``,
``hmac``, ``secrets``, and ``base64`` from the standard library.

To migrate an existing deployment:
    1. ``python3 -c "from ebay_app.password_hash import hash_password; \\
       print(hash_password('your-current-password'))"``
    2. Replace the value of ``APP_ADMIN_PASSWORD`` in ``.env`` /
       ``.env.production`` with the printed string.
No code change or restart-time migration is required -- ``verify_password``
recognizes the new format automatically and the old format keeps working
for anyone who hasn't migrated yet.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

HASH_PREFIX = "pbkdf2_sha256$"
ITERATIONS = 600_000
_SALT_BYTES = 16
_MAX_ITERATIONS = 10_000_000


def hash_password(password: str) -> str:
    """Return a salted PBKDF2-HMAC-SHA256 hash encoding of ``password``.

    The result is safe to store in place of the plaintext password as the
    value of ``APP_ADMIN_PASSWORD``.
    """
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return (
        f"{HASH_PREFIX}{ITERATIONS}$"
        f"{base64.urlsafe_b64encode(salt).decode('ascii')}$"
        f"{base64.urlsafe_b64encode(digest).decode('ascii')}"
    )


def is_hashed(stored_value: str) -> bool:
    """True if ``stored_value`` looks like a hash produced by ``hash_password``."""
    return isinstance(stored_value, str) and stored_value.startswith(HASH_PREFIX)


def verify_password(password: str, stored_value: str) -> bool:
    """Timing-safe check of ``password`` against ``stored_value``.

    ``stored_value`` may be a hash produced by ``hash_password`` (preferred
    going forward), or, for a not-yet-migrated deployment, the legacy
    plaintext admin password -- both are compared without ever branching on
    a secret-dependent early return beyond the format check itself.
    """
    if not isinstance(password, str) or not isinstance(stored_value, str):
        return False
    if not is_hashed(stored_value):
        # Legacy plaintext: unchanged behavior from before this module existed.
        return hmac.compare_digest(password, stored_value)
    try:
        _prefix, iterations_str, salt_b64, digest_b64 = stored_value.split("$", 3)
        iterations = int(iterations_str)
        salt = base64.urlsafe_b64decode(salt_b64)
        expected_digest = base64.urlsafe_b64decode(digest_b64)
    except (ValueError, TypeError):
        # Malformed hash: fail closed rather than raise out of a login request.
        return False
    if not (0 < iterations <= _MAX_ITERATIONS):
        return False
    candidate_digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return hmac.compare_digest(candidate_digest, expected_digest)
