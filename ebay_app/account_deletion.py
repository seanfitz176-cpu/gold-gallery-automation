"""eBay Marketplace Account Deletion/Closure notification handling.

Architecture (see account_deletion_worker/README.md for the Cloudflare side):

  eBay --> Cloudflare Worker (unverified queue, no eBay credentials) --> here

This module is the "here": it fetches the Worker's queue over plain HTTPS
(``DeletionWorkerClient``), cryptographically verifies each entry locally
using this application's own eBay application token
(``ebay_app.notification_signature.verify_notification``, via
``EbaySandboxClient.get_notification_public_key``), and only then decides
whether a notification is genuine and whether it is about THIS application's
own connected eBay account.

Nothing here ever clears the stored eBay token on its own. Detecting that the
connected account was closed only *records* the detection
(``AppDatabase.record_owner_deletion_detected``); clearing the token requires
an explicit confirmation from the owner on the blocking banner that
``ebay_app.web`` shows for a detected-but-not-yet-acknowledged closure (see
``AppDatabase.acknowledge_owner_deletion``). This mirrors the rest of the
application's design: nothing destructive happens without an explicit,
human confirmation.

This application has no background scheduler by design (see
docs/architecture notes) -- there is deliberately no thread polling this on a
timer. ``maybe_poll`` is instead called opportunistically (once at process
startup, and at most once per ``MIN_POLL_INTERVAL_SECONDS`` from web request
handling in ``ebay_app.web``), which keeps detection bounded to "within one
interval of the app actually being used" without adding a scheduler.

Every function here is a no-op (``PollOutcome(configured=False, ...)``)
whenever the two deletion-Worker settings are not configured in ``.env`` /
``.env.production`` -- the feature is entirely inactive until a Cloudflare
Worker has been deployed and both settings are set. See
``account_deletion_worker/README.md`` for that setup.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, TYPE_CHECKING

from .notification_signature import (
    InvalidUserIdError,
    NotificationVerificationError,
    PublicKeyCache,
    extract_user_id,
    verify_notification,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance only
    from .config import AppConfig
    from .ebay_client import EbaySandboxClient
    from .storage import AppDatabase

__all__ = [
    "DeletionWorkerError",
    "PollOutcome",
    "DeletionWorkerClient",
    "poll_once",
    "maybe_poll",
]

REQUEST_TIMEOUT_SECONDS = 15
MAX_PAGES_PER_POLL = 200  # hard ceiling; a real queue has far fewer pages

# No background scheduler exists in this app by design. This only rate-limits
# how often the lazy, request-triggered poll in ebay_app.web actually calls
# the Worker; it has no effect on the one explicit poll run at startup.
MIN_POLL_INTERVAL_SECONDS = 6 * 60 * 60

# Shared across every poll call in this process. Thread-safe on its own
# (see notification_signature.PublicKeyCache); deliberately a module-level
# singleton so eBay's "cache the key for at least an hour" guidance is
# actually honoured across repeated polls, not reset every call.
_PUBLIC_KEY_CACHE = PublicKeyCache()


class DeletionWorkerError(RuntimeError):
    """A call to the account-deletion Cloudflare Worker failed at the
    transport/HTTP level. Never raised for a notification that merely fails
    signature verification -- that is handled separately and never surfaced
    as an error, since a bad or forged entry is an expected, harmless case."""


@dataclass(frozen=True)
class PollOutcome:
    configured: bool
    polled: bool
    entries_seen: int = 0
    owner_deletion_detected: bool = False
    error: Optional[str] = None


class DeletionWorkerClient:
    """Minimal stdlib HTTP client for the Worker's two authenticated routes.

    Deliberately separate from ``EbaySandboxClient``: this talks to the
    user's own Cloudflare Worker, never to eBay, and never carries any eBay
    credential. ``bearer_secret`` is ``ACCOUNT_DELETION_STATUS_SECRET`` --
    the same value configured as the Worker's ``STATUS_API_SECRET``.
    """

    def __init__(self, base_url: str, bearer_secret: str, *, timeout: int = REQUEST_TIMEOUT_SECONDS) -> None:
        self._base_url = base_url.rstrip("/")
        self._bearer_secret = bearer_secret
        self._timeout = timeout

    def _call(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, str]] = None,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self._base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        body = (
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None
        )
        headers = {
            "Authorization": f"Bearer {self._bearer_secret}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.URLError as exc:
            raise DeletionWorkerError(
                f"Could not reach the account-deletion Worker: {exc}"
            ) from exc
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise DeletionWorkerError(
                "The account-deletion Worker returned a response that was not valid JSON"
            ) from exc
        if not isinstance(parsed, dict):
            raise DeletionWorkerError(
                "The account-deletion Worker returned an unexpected response shape"
            )
        return parsed

    def fetch_pending_page(self, cursor: Optional[str] = None) -> Dict[str, Any]:
        query = {"cursor": cursor} if cursor else None
        return self._call("GET", "/account-deletion/pending", query=query)

    def acknowledge(self, notification_id: str) -> None:
        self._call(
            "POST",
            "/account-deletion/acknowledge",
            payload={"notificationId": notification_id},
        )


def _handle_entry(
    entry: Mapping[str, Any],
    ebay: "EbaySandboxClient",
    database: "AppDatabase",
    worker: DeletionWorkerClient,
) -> bool:
    """Verify, record, and acknowledge one queued entry.

    Returns True only if this call is the FIRST time a verified notification
    confirmed that the connected account itself was closed (i.e. the caller
    should treat this as a newly detected event).
    """

    notification_id = entry.get("notificationId")
    raw_body = entry.get("rawBody")
    signature_header = entry.get("signatureHeader")
    if not isinstance(notification_id, str) or not notification_id:
        return False  # Malformed queue entry; nothing safe to acknowledge.
    if not isinstance(raw_body, str):
        return False

    try:
        verified = verify_notification(
            raw_body.encode("utf-8"),
            signature_header,
            ebay.get_notification_public_key,
            cache=_PUBLIC_KEY_CACHE,
        )
        user_id = extract_user_id(verified)
    except (NotificationVerificationError, InvalidUserIdError):
        # Leave it queued rather than acknowledging it: a transient failure
        # (e.g. a key-fetch network error right now) must not make a
        # genuine notification silently disappear. The Worker's 90-day TTL
        # is the backstop against an entry that is genuinely never valid.
        return False

    status = database.connection_status()
    owner_user_id = status.get("user_id")
    matched_owner = bool(owner_user_id) and user_id == owner_user_id
    is_new = database.record_deletion_notification_processed(
        notification_id, user_id, matched_owner=matched_owner
    )
    if matched_owner and is_new:
        database.record_owner_deletion_detected(notification_id)

    # A verified notification -- genuine or genuinely about someone else's
    # account -- has now been durably recorded locally, so the Worker's copy
    # can go. If this fails, nothing is lost: record_deletion_notification_processed
    # already makes re-processing the same id a safe no-op next time.
    try:
        worker.acknowledge(notification_id)
    except DeletionWorkerError:
        pass

    return matched_owner and is_new


def poll_once(
    config: "AppConfig", ebay: "EbaySandboxClient", database: "AppDatabase"
) -> PollOutcome:
    """One full polling pass against the account-deletion Worker's queue.

    Always safe to call: a no-op when the feature is not configured, and
    every transport failure is caught, recorded via
    ``database.record_deletion_poll_result``, and returned in the result
    rather than raised.
    """

    if not config.deletion_worker_base_url or not config.deletion_worker_status_secret:
        return PollOutcome(configured=False, polled=False)

    worker = DeletionWorkerClient(
        config.deletion_worker_base_url, config.deletion_worker_status_secret
    )
    entries_seen = 0
    owner_detected = False
    cursor: Optional[str] = None
    try:
        for _page_number in range(MAX_PAGES_PER_POLL):
            page = worker.fetch_pending_page(cursor)
            entries = page.get("entries")
            if not isinstance(entries, list):
                raise DeletionWorkerError(
                    "The account-deletion Worker returned an unexpected page shape"
                )
            for entry in entries:
                if isinstance(entry, Mapping):
                    entries_seen += 1
                    if _handle_entry(entry, ebay, database, worker):
                        owner_detected = True
            next_cursor = page.get("cursor")
            if next_cursor is None:
                break
            cursor = next_cursor
        else:
            raise DeletionWorkerError(
                "The account-deletion Worker's queue did not finish paging "
                f"within {MAX_PAGES_PER_POLL} pages"
            )
    except DeletionWorkerError as exc:
        database.record_deletion_poll_result(success=False, error=str(exc))
        return PollOutcome(
            configured=True, polled=False, entries_seen=entries_seen, error=str(exc)
        )

    database.record_deletion_poll_result(success=True)
    return PollOutcome(
        configured=True,
        polled=True,
        entries_seen=entries_seen,
        owner_deletion_detected=owner_detected,
    )


def maybe_poll(
    config: "AppConfig",
    ebay: "EbaySandboxClient",
    database: "AppDatabase",
    *,
    logger: Any = None,
    now: Any = time.time,
) -> PollOutcome:
    """Rate-limited wrapper so request handling can trigger a poll cheaply.

    No background thread polls this app's data by design, so this is instead
    called opportunistically -- once unconditionally at process startup, and
    from request handling at most once every ``MIN_POLL_INTERVAL_SECONDS``
    (tracked via ``database.get_deletion_state()['last_poll_at']``).
    """

    if not config.deletion_worker_base_url or not config.deletion_worker_status_secret:
        return PollOutcome(configured=False, polled=False)
    last_poll_at = database.get_deletion_state().get("last_poll_at")
    if isinstance(last_poll_at, (int, float)) and now() - last_poll_at < MIN_POLL_INTERVAL_SECONDS:
        return PollOutcome(configured=True, polled=False)
    outcome = poll_once(config, ebay, database)
    if outcome.error and logger is not None:
        logger.warning("Account-deletion poll failed: %s", outcome.error)
    return outcome
