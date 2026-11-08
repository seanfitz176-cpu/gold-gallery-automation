from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX path
    msvcrt = None


class StorageError(RuntimeError):
    pass


class ConnectionChangedStorageError(StorageError):
    pass


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_scopes(
    scopes: Optional[Iterable[str]],
    *,
    allow_none: bool = True,
) -> Optional[Tuple[str, ...]]:
    """Return a deterministic, validated OAuth scope set.

    OAuth scope names are case-sensitive. They may contain URI punctuation but may
    not contain whitespace or control characters. Limits keep untrusted token
    responses from creating unbounded local records.
    """
    if scopes is None:
        if allow_none:
            return None
        raise StorageError("OAuth scopes are required")
    if isinstance(scopes, (str, bytes)):
        raise StorageError("OAuth scopes must be supplied as separate values")

    normalized = set()
    try:
        iterator = iter(scopes)
    except TypeError as exc:
        raise StorageError("OAuth scopes must be an iterable of strings") from exc
    value_count = 0
    for value in iterator:
        value_count += 1
        if value_count > 64:
            raise StorageError("Too many OAuth scopes were supplied")
        if not isinstance(value, str):
            raise StorageError("Every OAuth scope must be a string")
        scope = value.strip()
        if (
            not scope
            or len(scope) > 1024
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in scope)
        ):
            raise StorageError("OAuth scope contains unsupported characters")
        normalized.add(scope)
    result = tuple(sorted(normalized))
    if sum(len(scope) for scope in result) > 16 * 1024:
        raise StorageError("OAuth scope data is unexpectedly large")
    return result


def _decode_scopes(value: Optional[str]) -> Tuple[str, ...]:
    # NULL deliberately means an older or otherwise unrecorded grant. Never infer
    # permissions for it from the application's current configuration.
    if value is None:
        return ()
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StorageError("Stored OAuth scopes are invalid") from exc
    if not isinstance(payload, list):
        raise StorageError("Stored OAuth scopes are invalid")
    normalized = _normalize_scopes(payload, allow_none=False)
    assert normalized is not None
    if payload != list(normalized):
        raise StorageError("Stored OAuth scopes are not normalized")
    return normalized


class EnvelopeCipher:
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise StorageError("The data encryption key must be 32 bytes")
        self._cipher = AESGCM(key)

    def encrypt(self, value: str, context: str) -> str:
        nonce = secrets.token_bytes(12)
        ciphertext = self._cipher.encrypt(nonce, value.encode("utf-8"), context.encode("utf-8"))
        return json.dumps(
            {
                "v": 1,
                "n": base64.b64encode(nonce).decode("ascii"),
                "d": base64.b64encode(ciphertext).decode("ascii"),
            },
            separators=(",", ":"),
        )

    def decrypt(self, envelope: str, context: str) -> str:
        try:
            payload = json.loads(envelope)
            if payload.get("v") != 1:
                raise ValueError("unsupported envelope")
            return self._cipher.decrypt(
                base64.b64decode(payload["n"], validate=True),
                base64.b64decode(payload["d"], validate=True),
                context.encode("utf-8"),
            ).decode("utf-8")
        except Exception as exc:
            raise StorageError("Stored encrypted data could not be decrypted") from exc


@dataclass(frozen=True)
class ClaimedAction:
    action_id: str
    kind: str
    payload: Dict[str, Any]
    grant_generation: str


@dataclass(frozen=True)
class VerifiedConnectionBinding:
    """Internal grant/identity snapshot. Never render or log this object."""

    grant_generation: str
    user_id: str


@dataclass(frozen=True)
class OAuthStateRecord:
    """One-use OAuth callback intent bound to a browser session."""

    intent: str
    expected_grant_generation: Optional[str]


class AppDatabase:
    SCHEMA_VERSION = 11
    OAUTH_STATE_INTENTS = {"connect", "scope_upgrade"}
    ACTION_KINDS = {"create_draft", "change_price", "change_quantity", "add_image"}
    ACTION_STATUSES = {
        "pending",
        "executing",
        "succeeded",
        "failed",
        "partial",
        "unknown",
        "cancelled",
        "expired",
    }
    ACTION_TERMINAL_STATUSES = ACTION_STATUSES - {"pending", "executing"}
    # The subset of terminal statuses that only result from an actual claim
    # having gone through (not from a cancellation or an untouched expiry).
    # Used only to make a duplicate /actions/confirm submission idempotent:
    # replaying to the SAME already-confirmed result is safe, but a
    # cancelled or expired token must keep failing closed, never replay.
    ACTION_CONFIRMED_TERMINAL_STATUSES = {"succeeded", "failed", "partial", "unknown"}
    ACTION_RESERVATION_STATUSES = {
        "pending",
        "executing",
        "succeeded",
        "partial",
        "unknown",
    }
    ACTION_CHECKPOINTS = (
        "confirmed",
        "inventory_request_started",
        "inventory_created",
        "inventory_verified",
        "offer_request_started",
        "offer_created",
        "verified_unpublished",
    )
    ACTION_EVENT_TYPES = set(ACTION_CHECKPOINTS) | {
        "inventory_request_failed",
        "offer_request_failed",
        "verification_failed",
        "reconciliation_required",
        "terminal_result",
    }
    SAFE_ACTION_RESULT_FIELDS = {
        "category_id",
        "code",
        "currency",
        "dry_run",
        "error_category",
        "error_code",
        "error_id",
        "existing_inventory",
        "failure_stage",
        "image_count",
        "image_url",
        "inventory_created",
        "inventory_verified",
        "inventory_status",
        "listing_id",
        "marketplace_id",
        "offer_created",
        "offer_id",
        "offer_status",
        "ok",
        "outcome",
        "price",
        "quantity",
        "reconciliation_required",
        "safe_code",
        "sku",
        "status",
        "verified_unpublished",
    }

    def __init__(
        self,
        path: Path,
        encryption_key: bytes,
        now=time.time,
        environment: str = "Sandbox",
    ) -> None:
        self.path = path.absolute()
        self._now = now
        self.environment = environment
        self._cipher = EnvelopeCipher(encryption_key)
        self._lock = threading.RLock()
        self._connection: Optional[sqlite3.Connection] = None
        self._process_lock_descriptor: Optional[int] = None
        self._closed = False
        path = self.path
        if path.parent.is_symlink() or path.is_symlink():
            raise StorageError("APP_DATABASE_PATH and its parent must not be symbolic links")
        if path.parent.exists():
            if not path.parent.is_dir() or stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
                raise StorageError(
                    f"Database directory must already be private; run: chmod 700 {path.parent}"
                )
        else:
            path.parent.mkdir(parents=True, mode=0o700)
            os.chmod(path.parent, 0o700)
        for candidate in self._database_files():
            self._validate_existing_file(candidate)
        self._acquire_process_lock()

        previous_umask = os.umask(0o077)
        try:
            flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            os.close(descriptor)
            os.chmod(path, 0o600)
            self._connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._migrate()
            self._verify_key()
            self._restrict_files()
        except Exception as exc:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self._release_process_lock()
            if isinstance(exc, sqlite3.Error):
                raise StorageError("Could not initialize the private SQLite database") from exc
            raise
        finally:
            os.umask(previous_umask)

    def _database_files(self) -> tuple[Path, ...]:
        return (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
            Path(f"{self.path}.lock"),
        )

    @staticmethod
    def _validate_existing_file(path: Path) -> None:
        try:
            details = os.lstat(path)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(details.st_mode):
            raise StorageError(f"SQLite storage path must be a regular file: {path}")
        if details.st_nlink != 1:
            raise StorageError(f"SQLite storage file must not have hard links: {path}")
        if stat.S_IMODE(details.st_mode) & 0o077:
            raise StorageError(f"Database file must be private; run: chmod 600 {path}")

    def _acquire_process_lock(self) -> None:
        lock_path = Path(f"{self.path}.lock")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            os.chmod(lock_path, 0o600)
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:  # pragma: no cover - Windows fallback
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - unsupported Python platform
                raise StorageError("This platform does not provide an exclusive file lock")
        except (BlockingIOError, OSError) as exc:
            os.close(descriptor)
            raise StorageError(
                f"Another eBay {self.environment} app process is already using this database"
            ) from exc
        except StorageError:
            os.close(descriptor)
            raise
        self._process_lock_descriptor = descriptor

    def _release_process_lock(self) -> None:
        descriptor = self._process_lock_descriptor
        if descriptor is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows fallback
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(descriptor)
            self._process_lock_descriptor = None

    def _migrate(self) -> None:
        assert self._connection is not None
        with self._connection:
            version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
            if version > self.SCHEMA_VERSION:
                raise StorageError("The SQLite database was created by a newer application version")
            self._connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                PRAGMA busy_timeout = 5000;
                PRAGMA secure_delete = ON;
                PRAGMA journal_mode = WAL;

                CREATE TABLE IF NOT EXISTS encryption_metadata (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    check_envelope TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS oauth_token (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    refresh_token_envelope TEXT NOT NULL,
                    refresh_token_expires_at INTEGER,
                    user_id TEXT,
                    grant_generation TEXT NOT NULL,
                    granted_scopes TEXT,
                    verified_at INTEGER,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS oauth_states (
                    state_hash TEXT PRIMARY KEY,
                    session_hash TEXT NOT NULL,
                    intent TEXT NOT NULL CHECK (intent IN ('connect', 'scope_upgrade')),
                    expected_grant_generation TEXT,
                    expires_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS actions (
                    action_id TEXT PRIMARY KEY,
                    token_hash TEXT UNIQUE NOT NULL,
                    session_hash TEXT NOT NULL,
                    grant_generation TEXT NOT NULL,
                    seller_hash TEXT,
                    resource_hash TEXT,
                    kind TEXT NOT NULL CHECK (kind IN ('create_draft', 'change_price', 'change_quantity', 'add_image')),
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'pending', 'executing', 'succeeded', 'failed',
                            'partial', 'unknown', 'cancelled', 'expired'
                        )
                    ),
                    checkpoint TEXT CHECK (
                        checkpoint IS NULL OR checkpoint IN (
                            'confirmed', 'inventory_request_started', 'inventory_created',
                            'inventory_verified', 'offer_request_started', 'offer_created',
                            'verified_unpublished'
                        )
                    ),
                    payload_envelope TEXT NOT NULL,
                    result_envelope TEXT,
                    error_message TEXT,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS action_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_id TEXT NOT NULL REFERENCES actions(action_id),
                    event_type TEXT NOT NULL CHECK (
                        event_type IN (
                            'confirmed', 'inventory_request_started', 'inventory_created',
                            'inventory_verified', 'offer_request_started', 'offer_created',
                            'verified_unpublished',
                            'inventory_request_failed', 'offer_request_failed',
                            'verification_failed', 'reconciliation_required', 'terminal_result'
                        )
                    ),
                    stage TEXT CHECK (
                        stage IS NULL OR stage IN (
                            'confirmed', 'inventory_request_started', 'inventory_created',
                            'inventory_verified', 'offer_request_started', 'offer_created',
                            'verified_unpublished'
                        )
                    ),
                    terminal_status TEXT CHECK (
                        terminal_status IS NULL OR terminal_status IN (
                            'succeeded', 'failed', 'partial', 'unknown', 'cancelled', 'expired'
                        )
                    ),
                    ebay_error_id TEXT,
                    ebay_error_category TEXT,
                    result_envelope TEXT,
                    created_at INTEGER NOT NULL
                );

                -- Schema v9: eBay Marketplace Account Deletion/Closure notification
                -- handling (see ebay_app/account_deletion.py). Both tables are a
                -- single-process local audit/state trail -- no buyer or seller
                -- personal data from the notification itself is retained here
                -- beyond the eBay user id needed to recognise a repeat.
                CREATE TABLE IF NOT EXISTS account_deletion_notifications (
                    notification_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    processed_at INTEGER NOT NULL,
                    matched_owner INTEGER NOT NULL CHECK (matched_owner IN (0, 1))
                );

                CREATE TABLE IF NOT EXISTS account_deletion_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_poll_at INTEGER,
                    last_poll_error TEXT,
                    detected_at INTEGER,
                    notification_id TEXT,
                    acknowledged_at INTEGER
                );

                -- Schema v10: Section 6 production price-safety controls. One row
                -- per SKU the owner has explicitly set a floor for. This is the
                -- owner's own business input (never eBay response data or a
                -- credential), so it is stored in plain columns like the other
                -- local configuration tables above -- consistent with this
                -- database's existing practice of reserving envelope encryption
                -- for credentials and eBay transaction content.
                CREATE TABLE IF NOT EXISTS price_limits (
                    sku TEXT PRIMARY KEY,
                    floor_price TEXT NOT NULL,
                    floor_currency TEXT NOT NULL,
                    max_change_percent TEXT,
                    updated_at INTEGER NOT NULL
                );

                -- Schema v11: autonomy engine (docs/autonomy_engine.md). All
                -- three tables hold the owner's own business inputs or the
                -- engine's own proposals -- never credentials or raw eBay
                -- responses -- so, like price_limits, they are plain columns.
                -- Money and percentages are stored as validated decimal strings.
                CREATE TABLE IF NOT EXISTS sku_costs (
                    sku TEXT PRIMARY KEY,
                    item_cost TEXT NOT NULL,
                    postage_cost TEXT NOT NULL,
                    fee_percent TEXT NOT NULL,
                    margin_percent TEXT,
                    currency TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS engine_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS engine_proposals (
                    proposal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sku TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('price', 'quantity', 'republish')),
                    current_value TEXT NOT NULL,
                    proposed_value TEXT NOT NULL,
                    currency TEXT,
                    reason TEXT NOT NULL,
                    decision TEXT NOT NULL CHECK (decision IN ('auto', 'queue')),
                    status TEXT NOT NULL CHECK (status IN (
                        'pending', 'approved', 'rejected', 'executed',
                        'expired', 'failed', 'unknown', 'superseded'
                    )),
                    outcome TEXT,
                    action_id TEXT,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS engine_proposals_pending_idx
                    ON engine_proposals (status, sku, kind);

                CREATE INDEX IF NOT EXISTS actions_created_idx ON actions (created_at DESC);
                CREATE INDEX IF NOT EXISTS action_events_action_idx
                    ON action_events (action_id, event_id);

                CREATE TRIGGER IF NOT EXISTS action_events_no_update
                BEFORE UPDATE ON action_events
                BEGIN
                    SELECT RAISE(ABORT, 'action events are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS action_events_no_delete
                BEFORE DELETE ON action_events
                BEGIN
                    SELECT RAISE(ABORT, 'action events are append-only');
                END;
                """
            )
            now = int(self._now())
            oauth_columns = self._table_columns("oauth_token")
            state_columns = self._table_columns("oauth_states")
            action_columns = self._table_columns("actions")
            if "grant_generation" not in oauth_columns:
                self._connection.execute(
                    "ALTER TABLE oauth_token ADD COLUMN grant_generation TEXT"
                )
            if "verified_at" not in oauth_columns:
                # Existing grants predate the verified-connection workflow. They must
                # prove that they still work before the UI treats them as verified.
                self._connection.execute(
                    "ALTER TABLE oauth_token ADD COLUMN verified_at INTEGER"
                )
            if "granted_scopes" not in oauth_columns:
                # Existing grants did not record the permission set. NULL is kept as
                # "unknown" so every Phase 3 scope check fails until fresh consent.
                self._connection.execute(
                    "ALTER TABLE oauth_token ADD COLUMN granted_scopes TEXT"
                )
            self._connection.execute(
                """
                UPDATE oauth_token SET grant_generation = ?
                WHERE grant_generation IS NULL OR grant_generation = ''
                """,
                (secrets.token_hex(16),),
            )
            if "session_hash" not in state_columns:
                self._connection.execute(
                    "ALTER TABLE oauth_states ADD COLUMN session_hash TEXT"
                )
            if "intent" not in state_columns:
                self._connection.execute(
                    "ALTER TABLE oauth_states ADD COLUMN intent TEXT"
                )
            if "expected_grant_generation" not in state_columns:
                self._connection.execute(
                    "ALTER TABLE oauth_states ADD COLUMN expected_grant_generation TEXT"
                )
            # Browser sessions do not survive a restart, so no saved OAuth state can remain valid.
            self._connection.execute("DELETE FROM oauth_states")
            if "grant_generation" not in action_columns:
                self._connection.execute(
                    "ALTER TABLE actions ADD COLUMN grant_generation TEXT"
                )
            if "seller_hash" not in action_columns:
                self._connection.execute("ALTER TABLE actions ADD COLUMN seller_hash TEXT")
            if "resource_hash" not in action_columns:
                self._connection.execute("ALTER TABLE actions ADD COLUMN resource_hash TEXT")
            if "checkpoint" not in action_columns:
                self._connection.execute(
                    """
                    ALTER TABLE actions ADD COLUMN checkpoint TEXT CHECK (
                        checkpoint IS NULL OR checkpoint IN (
                            'confirmed', 'inventory_request_started', 'inventory_created',
                            'inventory_verified', 'offer_request_started', 'offer_created',
                            'verified_unpublished'
                        )
                    )
                    """
                )
            # These three statements must run before the CHECK-constraint
            # rebuild below: it recreates ``actions`` with grant_generation
            # TEXT NOT NULL, and a genuinely old row (from before that column
            # existed) is still NULL here, immediately after the ALTER TABLE
            # above added it. Backfilling first means the rebuild's copy
            # always satisfies the new table's NOT NULL constraint.
            self._connection.execute(
                """
                INSERT INTO action_events (
                    action_id, event_type, stage, terminal_status, created_at
                )
                SELECT action_id, 'terminal_result', checkpoint, 'cancelled', ?
                FROM actions
                WHERE status = 'pending'
                  AND (grant_generation IS NULL OR grant_generation = '')
                """,
                (now,),
            )
            self._connection.execute(
                """
                UPDATE actions SET status = 'cancelled',
                    error_message = 'Application security upgrade requires a fresh review',
                    updated_at = ?
                WHERE status = 'pending'
                  AND (grant_generation IS NULL OR grant_generation = '')
                """,
                (now,),
            )
            self._connection.execute(
                "UPDATE actions SET grant_generation = '' WHERE grant_generation IS NULL"
            )
            self._upgrade_action_checkpoint_schema(version)
            # Older draft code could save raw eBay exception text. Scrub those
            # messages by state without decrypting either payload or result data.
            self._connection.execute(
                """
                UPDATE actions SET error_message = CASE status
                    WHEN 'failed' THEN
                        'Draft creation failed before any eBay mutation was confirmed'
                    WHEN 'partial' THEN
                        'Draft creation stopped after inventory creation'
                    WHEN 'unknown' THEN
                        'Draft creation outcome requires reconciliation'
                    WHEN 'cancelled' THEN
                        'Draft confirmation was cancelled before execution'
                    WHEN 'expired' THEN
                        'Draft confirmation expired before execution'
                    ELSE NULL
                END
                WHERE kind = 'create_draft' AND error_message IS NOT NULL
                """
            )
            # Legacy v4 actions are deliberately not decrypted to infer a seller or
            # resource. Only newly reviewed, explicitly bound draft actions can hold
            # a Phase 5 reservation.
            self._connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS actions_active_draft_reservation_idx
                ON actions (seller_hash, resource_hash)
                WHERE kind = 'create_draft'
                  AND seller_hash IS NOT NULL
                  AND resource_hash IS NOT NULL
                  AND status IN ('pending', 'executing', 'succeeded', 'partial', 'unknown')
                """
            )
            self._validate_schema()
            self._connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
            interrupted_at = int(self._now())
            self._connection.execute(
                """
                INSERT INTO action_events (
                    action_id, event_type, stage, terminal_status, created_at
                )
                SELECT action_id, 'terminal_result', checkpoint, 'unknown', ?
                FROM actions WHERE status = 'executing'
                """,
                (interrupted_at,),
            )
            self._connection.execute(
                """
                UPDATE actions SET status = 'unknown',
                    error_message = 'Application stopped while this eBay mutation was executing',
                    updated_at = ?
                WHERE status = 'executing'
                """,
                (interrupted_at,),
            )
            self._connection.execute(
                """
                INSERT INTO action_events (
                    action_id, event_type, stage, terminal_status, created_at
                )
                SELECT action_id, 'terminal_result', checkpoint, 'expired', ?
                FROM actions WHERE status = 'pending' AND expires_at < ?
                """,
                (interrupted_at, interrupted_at),
            )
            self._connection.execute(
                "UPDATE actions SET status = 'expired', updated_at = ? "
                "WHERE status = 'pending' AND expires_at < ?",
                (interrupted_at, interrupted_at),
            )
            # Browser sessions live only in memory. A restart must invalidate every
            # remaining confirmation capability, even if its wall-clock TTL has not
            # elapsed; the separately encrypted reviewed payload remains journaled.
            self._connection.execute(
                """
                INSERT INTO action_events (
                    action_id, event_type, stage, terminal_status, created_at
                )
                SELECT action_id, 'terminal_result', checkpoint, 'cancelled', ?
                FROM actions WHERE status = 'pending'
                """,
                (interrupted_at,),
            )
            self._connection.execute(
                """
                UPDATE actions SET status = 'cancelled',
                    error_message = CASE kind
                        WHEN 'create_draft' THEN
                            'Draft confirmation was cancelled before execution'
                        ELSE 'Application restarted before confirmation'
                    END,
                    updated_at = ?
                WHERE status = 'pending'
                """,
                (interrupted_at,),
            )

    def _table_columns(self, table: str) -> set[str]:
        assert self._connection is not None
        return {
            str(row["name"])
            for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()
        }

    def _upgrade_action_checkpoint_schema(self, source_version: int) -> None:
        """Rebuild the actions/action_events tables to the current CHECK constraints
        without decrypting rows.

        Originally written for the Phase 5 Inventory readback checkpoint; reused
        as-is for the Phase 7 ``change_quantity`` kind so an existing local
        database (with real journaled actions) is upgraded losslessly instead
        of being deleted and recreated.
        """

        assert self._connection is not None
        definitions = {
            row["name"]: row["sql"] or ""
            for row in self._connection.execute(
                """
                SELECT name, sql FROM sqlite_master
                WHERE type = 'table' AND name IN ('actions', 'action_events')
                """
            ).fetchall()
        }
        # The value must be allowed by one CHECK on actions and by both the
        # event_type and stage CHECKs on action_events.  A substring-only check
        # could mistake a partly upgraded database for a complete one and make
        # the first post-write verification checkpoint fail.  Version 5 is the
        # one known deployed schema without the checkpoint, so rebuild it
        # deterministically even if its SQL was modified locally. The same
        # substring approach also detects a ``kind`` CHECK that predates any
        # later action kind (Phase 7's ``change_quantity``, Phase 8's
        # ``add_image``, and so on) -- checked generically against every
        # kind in ACTION_KINDS so a future phase does not need to hand-edit
        # this method again.
        checkpoint_marker = "'inventory_verified'"
        kind_markers_present = all(
            definitions.get("actions", "").count(f"'{kind}'") >= 1
            for kind in self.ACTION_KINDS
        )
        has_complete_constraints = (
            definitions.get("actions", "").count(checkpoint_marker) >= 1
            and definitions.get("action_events", "").count(checkpoint_marker) >= 2
            and kind_markers_present
        )
        if source_version != 5 and has_complete_constraints:
            return

        for statement in (
            "DROP TRIGGER IF EXISTS action_events_no_update",
            "DROP TRIGGER IF EXISTS action_events_no_delete",
            "DROP INDEX IF EXISTS actions_created_idx",
            "DROP INDEX IF EXISTS action_events_action_idx",
            "DROP INDEX IF EXISTS actions_active_draft_reservation_idx",
            "ALTER TABLE action_events RENAME TO action_events_checkpoint_legacy",
            "ALTER TABLE actions RENAME TO actions_checkpoint_legacy",
        ):
            self._connection.execute(statement)
        self._connection.execute(
            """
            CREATE TABLE actions (
                action_id TEXT PRIMARY KEY,
                token_hash TEXT UNIQUE NOT NULL,
                session_hash TEXT NOT NULL,
                grant_generation TEXT NOT NULL,
                seller_hash TEXT,
                resource_hash TEXT,
                kind TEXT NOT NULL CHECK (kind IN ('create_draft', 'change_price', 'change_quantity', 'add_image')),
                status TEXT NOT NULL CHECK (
                    status IN (
                        'pending', 'executing', 'succeeded', 'failed',
                        'partial', 'unknown', 'cancelled', 'expired'
                    )
                ),
                checkpoint TEXT CHECK (
                    checkpoint IS NULL OR checkpoint IN (
                        'confirmed', 'inventory_request_started', 'inventory_created',
                        'inventory_verified', 'offer_request_started', 'offer_created',
                        'verified_unpublished'
                    )
                ),
                payload_envelope TEXT NOT NULL,
                result_envelope TEXT,
                error_message TEXT,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE action_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_id TEXT NOT NULL REFERENCES actions(action_id),
                event_type TEXT NOT NULL CHECK (
                    event_type IN (
                        'confirmed', 'inventory_request_started', 'inventory_created',
                        'inventory_verified', 'offer_request_started', 'offer_created',
                        'verified_unpublished', 'inventory_request_failed',
                        'offer_request_failed', 'verification_failed',
                        'reconciliation_required', 'terminal_result'
                    )
                ),
                stage TEXT CHECK (
                    stage IS NULL OR stage IN (
                        'confirmed', 'inventory_request_started', 'inventory_created',
                        'inventory_verified', 'offer_request_started', 'offer_created',
                        'verified_unpublished'
                    )
                ),
                terminal_status TEXT CHECK (
                    terminal_status IS NULL OR terminal_status IN (
                        'succeeded', 'failed', 'partial', 'unknown', 'cancelled', 'expired'
                    )
                ),
                ebay_error_id TEXT,
                ebay_error_category TEXT,
                result_envelope TEXT,
                created_at INTEGER NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            INSERT INTO actions (
                action_id, token_hash, session_hash, grant_generation,
                seller_hash, resource_hash, kind, status, checkpoint,
                payload_envelope, result_envelope, error_message,
                expires_at, created_at, updated_at
            )
            SELECT action_id, token_hash, session_hash, grant_generation,
                   seller_hash, resource_hash, kind, status, checkpoint,
                   payload_envelope, result_envelope, error_message,
                   expires_at, created_at, updated_at
            FROM actions_checkpoint_legacy
            """
        )
        self._connection.execute(
            """
            INSERT INTO action_events (
                event_id, action_id, event_type, stage, terminal_status,
                ebay_error_id, ebay_error_category, result_envelope, created_at
            )
            SELECT event_id, action_id, event_type, stage, terminal_status,
                   ebay_error_id, ebay_error_category, result_envelope, created_at
            FROM action_events_checkpoint_legacy
            ORDER BY event_id
            """
        )
        for statement in (
            "DROP TABLE action_events_checkpoint_legacy",
            "DROP TABLE actions_checkpoint_legacy",
            "CREATE INDEX actions_created_idx ON actions (created_at DESC)",
            "CREATE INDEX action_events_action_idx ON action_events (action_id, event_id)",
            """
            CREATE TRIGGER action_events_no_update
            BEFORE UPDATE ON action_events
            BEGIN
                SELECT RAISE(ABORT, 'action events are append-only');
            END
            """,
            """
            CREATE TRIGGER action_events_no_delete
            BEFORE DELETE ON action_events
            BEGIN
                SELECT RAISE(ABORT, 'action events are append-only');
            END
            """,
        ):
            self._connection.execute(statement)

    def _validate_schema(self) -> None:
        expected = {
            "oauth_token": {
                "id",
                "refresh_token_envelope",
                "refresh_token_expires_at",
                "user_id",
                "grant_generation",
                "granted_scopes",
                "verified_at",
                "updated_at",
            },
            "oauth_states": {
                "state_hash",
                "session_hash",
                "intent",
                "expected_grant_generation",
                "expires_at",
            },
            "actions": {
                "action_id",
                "token_hash",
                "session_hash",
                "grant_generation",
                "seller_hash",
                "resource_hash",
                "kind",
                "status",
                "checkpoint",
                "payload_envelope",
                "result_envelope",
                "error_message",
                "expires_at",
                "created_at",
                "updated_at",
            },
            "action_events": {
                "event_id",
                "action_id",
                "event_type",
                "stage",
                "terminal_status",
                "ebay_error_id",
                "ebay_error_category",
                "result_envelope",
                "created_at",
            },
        }
        for table, columns in expected.items():
            if not columns.issubset(self._table_columns(table)):
                raise StorageError(f"SQLite table {table} has an unsupported schema")

        definitions = {
            row["name"]: row["sql"] or ""
            for row in self._connection.execute(
                """
                SELECT name, sql FROM sqlite_master
                WHERE type = 'table' AND name IN ('actions', 'action_events')
                """
            ).fetchall()
        }
        checkpoint_marker = "'inventory_verified'"
        if (
            definitions.get("actions", "").count(checkpoint_marker) < 1
            or definitions.get("action_events", "").count(checkpoint_marker) < 2
        ):
            raise StorageError("SQLite action checkpoint constraints are incomplete")

        required_indexes = {
            "actions_created_idx",
            "action_events_action_idx",
            "actions_active_draft_reservation_idx",
        }
        indexes = {
            row["name"]
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        if not required_indexes.issubset(indexes):
            raise StorageError("SQLite action indexes are incomplete")

        required_triggers = {
            "action_events_no_update",
            "action_events_no_delete",
        }
        triggers = {
            row["name"]
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        if not required_triggers.issubset(triggers):
            raise StorageError("SQLite action event protections are incomplete")

        foreign_keys = self._connection.execute(
            "PRAGMA foreign_key_list(action_events)"
        ).fetchall()
        if not any(
            row["table"] == "actions"
            and row["from"] == "action_id"
            and row["to"] == "action_id"
            for row in foreign_keys
        ):
            raise StorageError("SQLite action event relationship is missing")
        if self._connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise StorageError("SQLite action event relationship is inconsistent")

    def _verify_key(self) -> None:
        context = "metadata:key-check"
        expected = "python-ebay-sandbox-key-check"
        row = self._connection.execute(
            "SELECT check_envelope FROM encryption_metadata WHERE id = 1"
        ).fetchone()
        if row is None:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO encryption_metadata (id, check_envelope) VALUES (1, ?)",
                    (self._cipher.encrypt(expected, context),),
                )
            return
        if self._cipher.decrypt(row["check_envelope"], context) != expected:
            raise StorageError("The configured data encryption key does not match this database")

    def _restrict_files(self) -> None:
        for candidate in self._database_files():
            if candidate.exists():
                os.chmod(candidate, 0o600)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self._release_process_lock()
            self._closed = True

    def save_refresh_token(
        self,
        refresh_token: str,
        expires_at: Optional[int] = None,
        user_id: Optional[str] = None,
        new_grant: bool = False,
        granted_scopes: Optional[Iterable[str]] = None,
    ) -> None:
        normalized_scopes = _normalize_scopes(granted_scopes)
        scopes_json = (
            json.dumps(normalized_scopes, separators=(",", ":"))
            if normalized_scopes is not None
            else None
        )
        envelope = self._cipher.encrypt(refresh_token, "oauth-token:refresh")
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT grant_generation FROM oauth_token WHERE id = 1"
            ).fetchone()
            generation = (
                secrets.token_hex(16)
                if new_grant or existing is None or not existing["grant_generation"]
                else existing["grant_generation"]
            )
            now = int(self._now())
            if new_grant:
                self._connection.execute(
                    """
                    INSERT INTO action_events (
                        action_id, event_type, stage, terminal_status, created_at
                    )
                    SELECT action_id, 'terminal_result', checkpoint, 'cancelled', ?
                    FROM actions WHERE status = 'pending'
                    """,
                    (now,),
                )
                self._connection.execute(
                    """
                    UPDATE actions SET status = 'cancelled',
                        error_message = 'Seller connection changed before confirmation',
                        updated_at = ? WHERE status = 'pending'
                    """,
                    (now,),
                )
                self._connection.execute(
                    """
                    INSERT INTO oauth_token (
                        id, refresh_token_envelope, refresh_token_expires_at,
                        user_id, grant_generation, granted_scopes, verified_at, updated_at
                    ) VALUES (1, ?, ?, ?, ?, ?, NULL, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        refresh_token_envelope = excluded.refresh_token_envelope,
                        refresh_token_expires_at = excluded.refresh_token_expires_at,
                        user_id = excluded.user_id,
                        grant_generation = excluded.grant_generation,
                        granted_scopes = excluded.granted_scopes,
                        verified_at = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (envelope, expires_at, user_id, generation, scopes_json, now),
                )
            else:
                self._connection.execute(
                    """
                    INSERT INTO oauth_token (
                        id, refresh_token_envelope, refresh_token_expires_at,
                        user_id, grant_generation, granted_scopes, verified_at, updated_at
                    ) VALUES (1, ?, ?, ?, ?, ?, NULL, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        refresh_token_envelope = excluded.refresh_token_envelope,
                        refresh_token_expires_at = COALESCE(
                            excluded.refresh_token_expires_at,
                            oauth_token.refresh_token_expires_at
                        ),
                        user_id = COALESCE(oauth_token.user_id, excluded.user_id),
                        updated_at = excluded.updated_at
                    """,
                    (envelope, expires_at, user_id, generation, scopes_json, now),
                )

    def replace_refresh_token_for_scope_upgrade(
        self,
        refresh_token: str,
        expires_at: int,
        user_id: str,
        granted_scopes: Iterable[str],
        expected_generation: str,
        expected_user_id: str,
    ) -> None:
        """Atomically replace one known seller grant after broader OAuth consent.

        The old row and its pending actions are preserved unless both pieces of the
        caller's snapshot still match. This prevents a delayed OAuth callback from
        replacing a grant that was reconnected while consent was in progress.
        """
        if (
            not isinstance(refresh_token, str)
            or not refresh_token
            or len(refresh_token) > 16_384
        ):
            raise StorageError("A replacement refresh token is required")
        if isinstance(expires_at, bool) or not isinstance(expires_at, int):
            raise StorageError("Refresh token expiry must be an integer")
        if (
            not isinstance(user_id, str)
            or not user_id
            or user_id != user_id.strip()
            or len(user_id) > 512
        ):
            raise StorageError(f"A {self.environment} seller user ID is required")
        if (
            not isinstance(expected_user_id, str)
            or not expected_user_id
            or expected_user_id != expected_user_id.strip()
            or len(expected_user_id) > 512
        ):
            raise StorageError(f"An expected {self.environment} seller user ID is required")
        if (
            not isinstance(expected_generation, str)
            or not expected_generation
            or len(expected_generation) > 256
        ):
            raise StorageError("An expected grant generation is required")

        if not hmac.compare_digest(user_id, expected_user_id):
            raise ConnectionChangedStorageError(
                f"The scope upgrade belongs to a different {self.environment} seller"
            )
        normalized_scopes = _normalize_scopes(granted_scopes, allow_none=False)
        assert normalized_scopes is not None
        if not normalized_scopes:
            raise StorageError("A scope upgrade must record at least one OAuth scope")
        scopes_json = json.dumps(normalized_scopes, separators=(",", ":"))
        envelope = self._cipher.encrypt(refresh_token, "oauth-token:refresh")
        now = int(self._now())
        if expires_at <= now:
            raise StorageError("Replacement refresh token is already expired")
        new_generation = secrets.token_hex(16)
        while hmac.compare_digest(new_generation, expected_generation):
            new_generation = secrets.token_hex(16)

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._connection.execute(
                    """
                    SELECT refresh_token_envelope, refresh_token_expires_at,
                           user_id, grant_generation
                    FROM oauth_token WHERE id = 1
                    """
                ).fetchone()
                current_generation = current["grant_generation"] if current else None
                current_user_id = current["user_id"] if current else None
                if (
                    current is None
                    or not isinstance(current_generation, str)
                    or not hmac.compare_digest(current_generation, expected_generation)
                    or not isinstance(current_user_id, str)
                    or not hmac.compare_digest(current_user_id, expected_user_id)
                    or (
                        current["refresh_token_expires_at"] is not None
                        and current["refresh_token_expires_at"] <= now
                    )
                ):
                    raise ConnectionChangedStorageError(
                        f"The {self.environment} seller grant changed during the scope upgrade"
                    )
                self._cipher.decrypt(
                    current["refresh_token_envelope"], "oauth-token:refresh"
                )
                self._connection.execute(
                    """
                    INSERT INTO action_events (
                        action_id, event_type, stage, terminal_status, created_at
                    )
                    SELECT action_id, 'terminal_result', checkpoint, 'cancelled', ?
                    FROM actions WHERE status = 'pending'
                    """,
                    (now,),
                )
                self._connection.execute(
                    """
                    UPDATE actions SET status = 'cancelled',
                        error_message = 'Seller permissions changed before confirmation',
                        updated_at = ? WHERE status = 'pending'
                    """,
                    (now,),
                )
                cursor = self._connection.execute(
                    """
                    UPDATE oauth_token SET
                        refresh_token_envelope = ?,
                        refresh_token_expires_at = ?,
                        user_id = ?,
                        grant_generation = ?,
                        granted_scopes = ?,
                        verified_at = NULL,
                        updated_at = ?
                    WHERE id = 1 AND grant_generation = ? AND user_id = ?
                    """,
                    (
                        envelope,
                        expires_at,
                        user_id,
                        new_generation,
                        scopes_json,
                        now,
                        expected_generation,
                        expected_user_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConnectionChangedStorageError(
                        f"The {self.environment} seller grant changed during the scope upgrade"
                    )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def get_refresh_token(self) -> Optional[str]:
        with self._lock:
            row = self._connection.execute(
                "SELECT refresh_token_envelope, refresh_token_expires_at FROM oauth_token WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        if (
            row["refresh_token_expires_at"] is not None
            and row["refresh_token_expires_at"] <= int(self._now())
        ):
            return None
        return self._cipher.decrypt(row["refresh_token_envelope"], "oauth-token:refresh")

    def granted_scopes(self) -> Tuple[str, ...]:
        """Return the normalized permissions recorded for the current grant."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT refresh_token_envelope, refresh_token_expires_at, granted_scopes
                FROM oauth_token WHERE id = 1
                """
            ).fetchone()
            if row is None or (
                row["refresh_token_expires_at"] is not None
                and row["refresh_token_expires_at"] <= int(self._now())
            ):
                return ()
            self._cipher.decrypt(row["refresh_token_envelope"], "oauth-token:refresh")
            return _decode_scopes(row["granted_scopes"])

    def has_granted_scopes(self, required: Iterable[str]) -> bool:
        """Fail closed unless every requested permission belongs to this grant."""
        normalized_required = _normalize_scopes(required, allow_none=False)
        assert normalized_required is not None
        if not normalized_required:
            return False
        return set(normalized_required).issubset(self.granted_scopes())

    def connection_status(self) -> Dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT refresh_token_envelope, user_id, refresh_token_expires_at,
                       verified_at, updated_at
                FROM oauth_token WHERE id = 1
                """
            ).fetchone()
            connected = bool(
                row
                and (
                    row["refresh_token_expires_at"] is None
                    or row["refresh_token_expires_at"] > int(self._now())
                )
            )
            if connected:
                self._cipher.decrypt(
                    row["refresh_token_envelope"], "oauth-token:refresh"
                )
            return {
                "connected": connected,
                "verified": connected and row["verified_at"] is not None,
                "verified_at": row["verified_at"] if row else None,
                "user_id": row["user_id"] if row else None,
                "refresh_token_expires_at": row["refresh_token_expires_at"] if row else None,
                "updated_at": row["updated_at"] if row else None,
            }

    def verified_connection_binding(self) -> Optional[VerifiedConnectionBinding]:
        """Return an atomic, decrypted snapshot only for the current verified grant."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT refresh_token_envelope, refresh_token_expires_at,
                       verified_at, grant_generation, user_id
                FROM oauth_token WHERE id = 1
                """
            ).fetchone()
            if (
                row is None
                or row["verified_at"] is None
                or not isinstance(row["grant_generation"], str)
                or not row["grant_generation"]
                or not isinstance(row["user_id"], str)
                or not row["user_id"].strip()
                or (
                    row["refresh_token_expires_at"] is not None
                    and row["refresh_token_expires_at"] <= int(self._now())
                )
            ):
                return None
            self._cipher.decrypt(
                row["refresh_token_envelope"], "oauth-token:refresh"
            )
            return VerifiedConnectionBinding(
                grant_generation=row["grant_generation"],
                user_id=row["user_id"].strip(),
            )

    def mark_connection_verified(self, expected_generation: str, user_id: str) -> int:
        """Atomically bind a successful API identity check to the current grant.

        The caller must pass the grant generation it used for the API request. If a
        reconnect happened while that request was in flight, this method fails rather
        than accidentally marking the replacement grant as verified.
        """
        if not isinstance(expected_generation, str) or not expected_generation:
            raise StorageError("A grant generation is required to verify the connection")
        if not isinstance(user_id, str) or not user_id.strip():
            raise StorageError(f"A {self.environment} seller user ID is required to verify the connection")
        user_id = user_id.strip()
        now = int(self._now())
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    """
                    SELECT refresh_token_envelope, refresh_token_expires_at,
                           user_id, grant_generation
                    FROM oauth_token WHERE id = 1
                    """
                ).fetchone()
                current_generation = row["grant_generation"] if row else None
                if (
                    row is None
                    or not isinstance(current_generation, str)
                    or not hmac.compare_digest(current_generation, expected_generation)
                    or (
                        row["refresh_token_expires_at"] is not None
                        and row["refresh_token_expires_at"] <= now
                    )
                    or (row["user_id"] is not None and row["user_id"] != user_id)
                ):
                    raise ConnectionChangedStorageError(
                        f"The {self.environment} seller connection changed before it was verified"
                    )
                # Decryption is part of verification: a corrupt token must never be
                # represented as a healthy connection.
                self._cipher.decrypt(
                    row["refresh_token_envelope"], "oauth-token:refresh"
                )
                cursor = self._connection.execute(
                    """
                    UPDATE oauth_token SET user_id = ?, verified_at = ?, updated_at = ?
                    WHERE id = 1 AND grant_generation = ?
                    """,
                    (user_id, now, now, expected_generation),
                )
                if cursor.rowcount != 1:
                    raise ConnectionChangedStorageError(
                        f"The {self.environment} seller connection changed before it was verified"
                    )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return now

    def connection_generation(self) -> Optional[str]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT refresh_token_envelope, refresh_token_expires_at, grant_generation
                FROM oauth_token WHERE id = 1
                """
            ).fetchone()
            if row is None:
                return None
            if (
                row["refresh_token_expires_at"] is not None
                and row["refresh_token_expires_at"] <= int(self._now())
            ):
                return None
            self._cipher.decrypt(row["refresh_token_envelope"], "oauth-token:refresh")
            generation = row["grant_generation"]
            return generation if isinstance(generation, str) and generation else None

    def clear_refresh_token(self) -> None:
        with self._lock, self._connection:
            now = int(self._now())
            self._connection.execute(
                """
                INSERT INTO action_events (
                    action_id, event_type, stage, terminal_status, created_at
                )
                SELECT action_id, 'terminal_result', checkpoint, 'cancelled', ?
                FROM actions WHERE status = 'pending'
                """,
                (now,),
            )
            self._connection.execute(
                """
                UPDATE actions SET status = 'cancelled',
                    error_message = 'Seller connection was removed before confirmation',
                    updated_at = ? WHERE status = 'pending'
                """,
                (now,),
            )
            self._connection.execute("DELETE FROM oauth_token")

    # -- Account Deletion/Closure notification handling (schema v9) ---------

    def get_deletion_state(self) -> Dict[str, Any]:
        """Current poll/detection state for the account-deletion banner.

        ``detected_at`` is set only when eBay confirmed (verified signature,
        matching eBay user id) that THIS application's own connected account
        was closed, and clears again once ``acknowledge_owner_deletion`` runs.
        """
        with self._lock:
            row = self._connection.execute(
                """
                SELECT last_poll_at, last_poll_error, detected_at, notification_id, acknowledged_at
                FROM account_deletion_state WHERE id = 1
                """
            ).fetchone()
            if row is None:
                return {
                    "last_poll_at": None,
                    "last_poll_error": None,
                    "detected_at": None,
                    "notification_id": None,
                    "acknowledged_at": None,
                }
            state = dict(row)
            if state["acknowledged_at"] is not None:
                state["detected_at"] = None
                state["notification_id"] = None
            return state

    def record_deletion_poll_result(self, *, success: bool, error: Optional[str] = None) -> None:
        """Best-effort bookkeeping only; never raises on a poll failure itself."""
        if error is not None and (not isinstance(error, str) or len(error) > 500):
            error = "Unavailable"
        now = int(self._now())
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO account_deletion_state (id, last_poll_at, last_poll_error)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    last_poll_at = excluded.last_poll_at,
                    last_poll_error = excluded.last_poll_error
                """,
                (now, None if success else (error or "Unknown error")),
            )

    def set_price_limit(
        self,
        sku: str,
        floor_price: str,
        floor_currency: str,
        max_change_percent: Optional[str] = None,
    ) -> None:
        """Create or replace the price-safety limit for one SKU. ``floor_price``
        and ``max_change_percent`` are pre-validated decimal strings (see
        ``validate_price_limit_form``); ``max_change_percent`` of ``None`` means
        "use the global default", not "no limit"."""
        now = int(self._now())
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO price_limits
                    (sku, floor_price, floor_currency, max_change_percent, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(sku) DO UPDATE SET
                    floor_price = excluded.floor_price,
                    floor_currency = excluded.floor_currency,
                    max_change_percent = excluded.max_change_percent,
                    updated_at = excluded.updated_at
                """,
                (sku, floor_price, floor_currency, max_change_percent, now),
            )

    def get_price_limit(self, sku: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT sku, floor_price, floor_currency, max_change_percent, updated_at
                FROM price_limits WHERE sku = ?
                """,
                (sku,),
            ).fetchone()
            return dict(row) if row is not None else None

    def list_price_limits(self) -> Tuple[Dict[str, Any], ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT sku, floor_price, floor_currency, max_change_percent, updated_at
                FROM price_limits ORDER BY sku ASC
                """
            ).fetchall()
            return tuple(dict(row) for row in rows)

    def delete_price_limit(self, sku: str) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM price_limits WHERE sku = ?", (sku,)
            )
            return cursor.rowcount > 0

    # -- Autonomy engine: per-SKU cost table (schema v11) ------------------

    def set_sku_cost(
        self,
        sku: str,
        *,
        item_cost: str,
        postage_cost: str,
        fee_percent: str,
        currency: str,
        margin_percent: Optional[str] = None,
    ) -> None:
        """Create or replace one SKU's cost row. All values are pre-validated
        decimal strings (see ``engine_pricing.parse_cost_row``);
        ``margin_percent`` of ``None`` means "use the global default, if any"."""
        now = int(self._now())
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO sku_costs
                    (sku, item_cost, postage_cost, fee_percent, margin_percent, currency, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sku) DO UPDATE SET
                    item_cost = excluded.item_cost,
                    postage_cost = excluded.postage_cost,
                    fee_percent = excluded.fee_percent,
                    margin_percent = excluded.margin_percent,
                    currency = excluded.currency,
                    updated_at = excluded.updated_at
                """,
                (sku, item_cost, postage_cost, fee_percent, margin_percent, currency, now),
            )

    def get_sku_cost(self, sku: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT sku, item_cost, postage_cost, fee_percent, margin_percent, currency, updated_at
                FROM sku_costs WHERE sku = ?
                """,
                (sku,),
            ).fetchone()
            return dict(row) if row is not None else None

    def list_sku_costs(self) -> Tuple[Dict[str, Any], ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT sku, item_cost, postage_cost, fee_percent, margin_percent, currency, updated_at
                FROM sku_costs ORDER BY sku ASC
                """
            ).fetchall()
            return tuple(dict(row) for row in rows)

    def delete_sku_cost(self, sku: str) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute("DELETE FROM sku_costs WHERE sku = ?", (sku,))
            return cursor.rowcount > 0

    # -- Autonomy engine: state and proposals (schema v11) -----------------

    def get_engine_state(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM engine_state WHERE key = ?", (key,)
            ).fetchone()
            return str(row["value"]) if row is not None else None

    def set_engine_state(self, key: str, value: str) -> None:
        now = int(self._now())
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO engine_state (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now),
            )

    def delete_engine_state(self, key: str) -> None:
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM engine_state WHERE key = ?", (key,))

    def record_engine_proposal(
        self,
        *,
        sku: str,
        kind: str,
        current_value: str,
        proposed_value: str,
        currency: Optional[str],
        reason: str,
        decision: str,
        ttl_seconds: int,
    ) -> int:
        """Store one proposal. Any older still-pending proposal for the same
        SKU and kind is marked ``superseded`` in the same transaction, so at
        most one pending proposal per SKU/kind ever exists."""
        now = int(self._now())
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE engine_proposals SET status = 'superseded', updated_at = ?
                WHERE sku = ? AND kind = ? AND status = 'pending'
                """,
                (now, sku, kind),
            )
            cursor = self._connection.execute(
                """
                INSERT INTO engine_proposals
                    (sku, kind, current_value, proposed_value, currency, reason, decision,
                     status, created_at, expires_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (sku, kind, current_value, proposed_value, currency, reason, decision,
                 now, now + int(ttl_seconds), now),
            )
            return int(cursor.lastrowid)

    def list_engine_proposals(
        self, statuses: Sequence[str] = ("pending",), limit: int = 500
    ) -> Tuple[Dict[str, Any], ...]:
        placeholders = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT proposal_id, sku, kind, current_value, proposed_value, currency, reason,
                       decision, status, outcome, action_id, created_at, expires_at, updated_at
                FROM engine_proposals WHERE status IN ({placeholders})
                ORDER BY created_at DESC, proposal_id DESC LIMIT ?
                """,
                (*statuses, int(limit)),
            ).fetchall()
            return tuple(dict(row) for row in rows)

    def get_engine_proposal(self, proposal_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT proposal_id, sku, kind, current_value, proposed_value, currency, reason,
                       decision, status, outcome, action_id, created_at, expires_at, updated_at
                FROM engine_proposals WHERE proposal_id = ?
                """,
                (int(proposal_id),),
            ).fetchone()
            return dict(row) if row is not None else None

    def set_engine_proposal_status(
        self,
        proposal_id: int,
        status: str,
        *,
        expected: str = "pending",
        outcome: Optional[str] = None,
        action_id: Optional[str] = None,
    ) -> bool:
        """Compare-and-set, so a proposal can never be acted on twice."""
        now = int(self._now())
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE engine_proposals
                SET status = ?, updated_at = ?,
                    outcome = COALESCE(?, outcome), action_id = COALESCE(?, action_id)
                WHERE proposal_id = ? AND status = ?
                """,
                (status, now, outcome[:500] if outcome else None, action_id, int(proposal_id), expected),
            )
            return cursor.rowcount == 1

    def expire_engine_proposals(self) -> int:
        now = int(self._now())
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE engine_proposals SET status = 'expired', updated_at = ? "
                "WHERE status = 'pending' AND expires_at <= ?",
                (now, now),
            )
            return cursor.rowcount

    def record_deletion_notification_processed(
        self, notification_id: str, user_id: str, *, matched_owner: bool
    ) -> bool:
        """Idempotent replay guard for one VERIFIED notification.

        Returns True the first time this ``notification_id`` is recorded,
        False on every later call for the same id (so a caller can skip
        re-running detection logic for a notification already handled).
        """
        if not isinstance(notification_id, str) or not notification_id or len(notification_id) > 256:
            raise StorageError("Invalid notification id")
        if not isinstance(user_id, str) or not user_id or len(user_id) > 256:
            raise StorageError("Invalid eBay user id")
        now = int(self._now())
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO account_deletion_notifications
                    (notification_id, user_id, processed_at, matched_owner)
                VALUES (?, ?, ?, ?)
                """,
                (notification_id, user_id, now, 1 if matched_owner else 0),
            )
            return cursor.rowcount == 1

    def record_owner_deletion_detected(self, notification_id: str) -> None:
        """Record that eBay reported THIS CONNECTED account closed.

        Deliberately does not itself clear the stored token: this app's
        existing "nothing happens without an explicit confirmation" design
        means that step only happens via ``acknowledge_owner_deletion``, once
        the owner has seen the blocking banner and confirmed it.
        """
        if not isinstance(notification_id, str) or not notification_id:
            raise StorageError("Invalid notification id")
        now = int(self._now())
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO account_deletion_state
                    (id, detected_at, notification_id, acknowledged_at)
                VALUES (1, ?, ?, NULL)
                ON CONFLICT(id) DO UPDATE SET
                    detected_at = excluded.detected_at,
                    notification_id = excluded.notification_id,
                    acknowledged_at = NULL
                """,
                (now, notification_id),
            )

    def acknowledge_owner_deletion(self) -> None:
        """The owner confirmed the detected closure on the blocking banner.

        Clears the locally stored eBay token and cancels any pending actions
        (the existing, already-idempotent ``clear_refresh_token``), then
        marks the banner acknowledged so the application becomes usable
        again (reconnecting, if the owner believes this was a false alarm).
        """
        now = int(self._now())
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO account_deletion_state (id, acknowledged_at)
                VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET acknowledged_at = excluded.acknowledged_at
                """,
                (now,),
            )
        self.clear_refresh_token()

    def save_oauth_state(
        self,
        state: str,
        session_id: str,
        ttl_seconds: int = 600,
        *,
        intent: str = "connect",
        expected_grant_generation: Optional[str] = None,
    ) -> None:
        if not isinstance(state, str) or not state or len(state) > 4096:
            raise StorageError("OAuth state is invalid")
        if not isinstance(session_id, str) or not session_id or len(session_id) > 4096:
            raise StorageError("OAuth browser session is invalid")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise StorageError("OAuth state lifetime must be an integer")
        if not 1 <= ttl_seconds <= 3600:
            raise StorageError("OAuth state lifetime must be between 1 and 3600 seconds")
        if not isinstance(intent, str) or intent not in self.OAUTH_STATE_INTENTS:
            raise StorageError("Unsupported OAuth state intent")
        if expected_grant_generation is not None and (
            not isinstance(expected_grant_generation, str)
            or not expected_grant_generation
            or len(expected_grant_generation) > 256
        ):
            raise StorageError("Expected grant generation is invalid")
        if intent == "connect" and expected_grant_generation is not None:
            raise StorageError("A connect OAuth state must not target an existing grant")
        if intent == "scope_upgrade" and expected_grant_generation is None:
            raise StorageError("A scope upgrade must target the current grant")

        now = int(self._now())
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM oauth_states WHERE expires_at < ?", (now,))
            if intent == "scope_upgrade":
                row = self._connection.execute(
                    """
                    SELECT refresh_token_envelope, refresh_token_expires_at, grant_generation
                    FROM oauth_token WHERE id = 1
                    """
                ).fetchone()
                current_generation = row["grant_generation"] if row else None
                if (
                    row is None
                    or not isinstance(current_generation, str)
                    or not hmac.compare_digest(
                        current_generation, expected_grant_generation or ""
                    )
                    or (
                        row["refresh_token_expires_at"] is not None
                        and row["refresh_token_expires_at"] <= now
                    )
                ):
                    raise ConnectionChangedStorageError(
                        f"The {self.environment} grant changed before the scope upgrade began"
                    )
                self._cipher.decrypt(
                    row["refresh_token_envelope"], "oauth-token:refresh"
                )
            self._connection.execute(
                """
                INSERT INTO oauth_states (
                    state_hash, session_hash, intent,
                    expected_grant_generation, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    _digest(state),
                    _digest(session_id),
                    intent,
                    expected_grant_generation,
                    now + ttl_seconds,
                ),
            )

    def consume_oauth_state_record(
        self,
        state: str,
        session_id: str,
    ) -> Optional[OAuthStateRecord]:
        if (
            not isinstance(state, str)
            or not state
            or len(state) > 4096
            or not isinstance(session_id, str)
            or not session_id
            or len(session_id) > 4096
        ):
            return None
        now = int(self._now())
        with self._lock, self._connection:
            state_hash = _digest(state)
            row = self._connection.execute(
                """
                SELECT session_hash, intent, expected_grant_generation, expires_at
                FROM oauth_states WHERE state_hash = ?
                """,
                (state_hash,),
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] < now:
                self._connection.execute(
                    "DELETE FROM oauth_states WHERE state_hash = ?", (state_hash,)
                )
                return None
            # A callback from a different browser session must not be able to
            # burn the legitimate session's one-use state. Delete only after
            # the session binding has matched.
            if (
                not isinstance(row["session_hash"], str)
                or not hmac.compare_digest(row["session_hash"], _digest(session_id))
            ):
                return None
            self._connection.execute(
                "DELETE FROM oauth_states WHERE state_hash = ?", (state_hash,)
            )
            if row["intent"] not in self.OAUTH_STATE_INTENTS:
                return None
            expected_generation = row["expected_grant_generation"]
            if row["intent"] == "connect":
                if expected_generation is not None:
                    return None
            else:
                if not isinstance(expected_generation, str) or not expected_generation:
                    return None
                grant = self._connection.execute(
                    """
                    SELECT refresh_token_envelope, refresh_token_expires_at, grant_generation
                    FROM oauth_token WHERE id = 1
                    """
                ).fetchone()
                current_generation = grant["grant_generation"] if grant else None
                if (
                    grant is None
                    or not isinstance(current_generation, str)
                    or not hmac.compare_digest(current_generation, expected_generation)
                    or (
                        grant["refresh_token_expires_at"] is not None
                        and grant["refresh_token_expires_at"] <= now
                    )
                ):
                    return None
                self._cipher.decrypt(
                    grant["refresh_token_envelope"], "oauth-token:refresh"
                )
            return OAuthStateRecord(row["intent"], expected_generation)

    def consume_oauth_state(self, state: str, session_id: str) -> bool:
        return self.consume_oauth_state_record(state, session_id) is not None

    @classmethod
    def _validate_expected_kind(cls, expected_kind: Optional[str]) -> None:
        if expected_kind is not None and (
            not isinstance(expected_kind, str) or expected_kind not in cls.ACTION_KINDS
        ):
            raise StorageError("Unsupported expected action kind")

    @staticmethod
    def _valid_opaque_value(value: Any, *, maximum: int = 4096) -> bool:
        return isinstance(value, str) and bool(value) and len(value) <= maximum

    @staticmethod
    def _binding_hash(value: str, label: str, *, maximum: int) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > maximum
            or value != value.strip()
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
        ):
            raise StorageError(f"The expected {label} is invalid")
        return _digest(f"{label}:{value}")

    @classmethod
    def _safe_action_result(cls, result: Mapping[str, Any]) -> Dict[str, Any]:
        """Copy a small allowlisted result; reject body-shaped or nested data.

        eBay request/response objects, descriptions, aspects and warnings can never
        pass this boundary. Checkpoints contain only the few identifiers and state
        flags required for safe reconciliation.
        """
        if not isinstance(result, Mapping) or len(result) > len(cls.SAFE_ACTION_RESULT_FIELDS):
            raise StorageError("Action result checkpoint is invalid")
        safe: Dict[str, Any] = {}
        for key, value in result.items():
            if not isinstance(key, str) or key not in cls.SAFE_ACTION_RESULT_FIELDS:
                raise StorageError("Action result checkpoint contains an unsafe field")
            if value is None or isinstance(value, bool):
                safe[key] = value
            elif isinstance(value, int) and not isinstance(value, bool):
                if abs(value) > 9_007_199_254_740_991:
                    raise StorageError("Action result checkpoint integer is out of range")
                safe[key] = value
            elif isinstance(value, str):
                if (
                    not value
                    or len(value) > 256
                    or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
                ):
                    raise StorageError("Action result checkpoint text is invalid")
                safe[key] = value
            else:
                raise StorageError("Action result checkpoint values must be scalar")
        return safe

    @staticmethod
    def _safe_event_identifier(value: Optional[str], label: str) -> Optional[str]:
        if value is None:
            return None
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 128
            or any(
                not (character.isascii() and (character.isalnum() or character in "._:-"))
                for character in value
            )
        ):
            raise StorageError(f"Action event {label} is invalid")
        return value

    @staticmethod
    def _safe_error_message(value: Optional[str]) -> Optional[str]:
        if value is None or value == "":
            return None
        if (
            not isinstance(value, str)
            or len(value) > 500
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        ):
            raise StorageError("Action error message is invalid")
        return value

    @staticmethod
    def _draft_error_message(status: str) -> Optional[str]:
        return {
            "failed": "Draft creation failed before any eBay mutation was confirmed",
            "partial": "Draft creation stopped after inventory creation",
            "unknown": "Draft creation outcome requires reconciliation",
            "cancelled": "Draft confirmation was cancelled before execution",
            "expired": "Draft confirmation expired before execution",
        }.get(status)

    def _decode_action_result(self, action_id: str, envelope: str) -> Dict[str, Any]:
        try:
            decoded = json.loads(
                self._cipher.decrypt(envelope, f"action:{action_id}:result")
            )
        except json.JSONDecodeError as exc:
            raise StorageError("Stored action result checkpoint is invalid") from exc
        return self._safe_action_result(decoded)

    def _insert_action_event_locked(
        self,
        action_id: str,
        event_type: str,
        *,
        stage: Optional[str] = None,
        terminal_status: Optional[str] = None,
        ebay_error_id: Optional[str] = None,
        ebay_error_category: Optional[str] = None,
        result: Optional[Mapping[str, Any]] = None,
        created_at: Optional[int] = None,
    ) -> None:
        if not isinstance(event_type, str) or event_type not in self.ACTION_EVENT_TYPES:
            raise StorageError("Unsupported action event type")
        if stage is not None and stage not in self.ACTION_CHECKPOINTS:
            raise StorageError("Unsupported action event stage")
        if event_type == "terminal_result":
            if terminal_status not in self.ACTION_TERMINAL_STATUSES:
                raise StorageError("Terminal action event requires a terminal status")
        elif terminal_status is not None:
            raise StorageError("A non-terminal action event cannot have a terminal status")
        safe_result = self._safe_action_result(result) if result is not None else None
        result_envelope = (
            self._cipher.encrypt(
                json.dumps(safe_result, separators=(",", ":"), sort_keys=True),
                f"action-event:{action_id}:result",
            )
            if safe_result is not None
            else None
        )
        self._connection.execute(
            """
            INSERT INTO action_events (
                action_id, event_type, stage, terminal_status,
                ebay_error_id, ebay_error_category, result_envelope, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action_id,
                event_type,
                stage,
                terminal_status,
                self._safe_event_identifier(ebay_error_id, "eBay error ID"),
                self._safe_event_identifier(ebay_error_category, "eBay error category"),
                result_envelope,
                int(self._now()) if created_at is None else created_at,
            ),
        )

    def create_pending_action(
        self,
        session_id: str,
        kind: str,
        payload: Dict[str, Any],
        expected_generation: str,
        ttl_seconds: int = 300,
        *,
        expected_user_id: Optional[str] = None,
        resource_key: Optional[str] = None,
    ) -> str:
        if not isinstance(kind, str) or kind not in self.ACTION_KINDS:
            raise StorageError("Unsupported pending action")
        if not isinstance(session_id, str) or not session_id or len(session_id) > 4096:
            raise StorageError("Action browser session is invalid")
        if (
            not isinstance(expected_generation, str)
            or not expected_generation
            or len(expected_generation) > 256
        ):
            raise StorageError("An expected grant generation is required")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise StorageError("Action lifetime must be an integer")
        if not 1 <= ttl_seconds <= 3600:
            raise StorageError("Action lifetime must be between 1 and 3600 seconds")
        if (expected_user_id is None) != (resource_key is None):
            raise StorageError(
                "Seller-bound actions require both an expected user ID and resource key"
            )
        seller_hash = (
            self._binding_hash(expected_user_id, "seller", maximum=256)
            if expected_user_id is not None
            else None
        )
        resource_hash = (
            self._binding_hash(resource_key, "resource", maximum=512)
            if resource_key is not None
            else None
        )
        if expected_user_id is not None and ttl_seconds > 300:
            raise StorageError("Seller-bound action lifetime cannot exceed five minutes")
        raw_token = secrets.token_urlsafe(32)
        action_id = secrets.token_hex(16)
        now = int(self._now())
        if not isinstance(payload, Mapping):
            raise StorageError("Pending action payload must be an object")
        try:
            serialized_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise StorageError("Pending action payload is not serializable") from exc
        if len(serialized_payload.encode("utf-8")) > 256 * 1024:
            raise StorageError("Pending action payload is unexpectedly large")
        envelope = self._cipher.encrypt(serialized_payload, f"action:{action_id}:payload")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                connection = self._connection.execute(
                    """
                    SELECT refresh_token_envelope, refresh_token_expires_at,
                           grant_generation, user_id, verified_at
                    FROM oauth_token WHERE id = 1
                    """
                ).fetchone()
                if (
                    connection is None
                    or (
                        connection["refresh_token_expires_at"] is not None
                        and connection["refresh_token_expires_at"] <= now
                    )
                    or not connection["grant_generation"]
                    or not expected_generation
                    or not hmac.compare_digest(
                        connection["grant_generation"], expected_generation
                    )
                    or (
                        expected_user_id is not None
                        and (
                            connection["verified_at"] is None
                            or not isinstance(connection["user_id"], str)
                            or not hmac.compare_digest(
                                connection["user_id"], expected_user_id
                            )
                        )
                    )
                ):
                    raise ConnectionChangedStorageError(
                        f"The {self.environment} seller connection changed during review"
                    )
                self._cipher.decrypt(
                    connection["refresh_token_envelope"], "oauth-token:refresh"
                )
                self._connection.execute(
                    """
                    INSERT INTO action_events (
                        action_id, event_type, stage, terminal_status, created_at
                    )
                    SELECT action_id, 'terminal_result', checkpoint, 'expired', ?
                    FROM actions WHERE status = 'pending' AND expires_at < ?
                    """,
                    (now, now),
                )
                self._connection.execute(
                    """
                    UPDATE actions SET status = 'expired', updated_at = ?
                    WHERE status = 'pending' AND expires_at < ?
                    """,
                    (now, now),
                )
                if kind == "create_draft" and seller_hash and resource_hash:
                    reservation = self._connection.execute(
                        """
                        SELECT 1 FROM actions
                        WHERE kind = 'create_draft'
                          AND seller_hash = ? AND resource_hash = ?
                          AND status IN (
                              'pending', 'executing', 'succeeded', 'partial', 'unknown'
                          )
                        """,
                        (seller_hash, resource_hash),
                    ).fetchone()
                    if reservation is not None:
                        raise StorageError(
                            f"This {self.environment} seller resource already has an active draft reservation"
                        )
                self._connection.execute(
                    """
                    INSERT INTO actions (
                        action_id, token_hash, session_hash, grant_generation,
                        seller_hash, resource_hash, kind, status, payload_envelope,
                        expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                    """,
                    (
                        action_id,
                        _digest(raw_token),
                        _digest(session_id),
                        connection["grant_generation"],
                        seller_hash,
                        resource_hash,
                        kind,
                        envelope,
                        now + ttl_seconds,
                        now,
                        now,
                    ),
                )
                self._connection.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                self._connection.execute("ROLLBACK")
                if kind == "create_draft" and seller_hash and resource_hash:
                    raise StorageError(
                        f"This {self.environment} seller resource already has an active draft reservation"
                    ) from exc
                raise StorageError("Could not create the pending action") from exc
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return raw_token

    def get_pending_action(
        self,
        token: str,
        session_id: str,
        expected_kind: Optional[str] = None,
    ) -> Optional[ClaimedAction]:
        self._validate_expected_kind(expected_kind)
        if not self._valid_opaque_value(token) or not self._valid_opaque_value(session_id):
            return None
        now = int(self._now())
        with self._lock:
            row = self._connection.execute(
                """
                SELECT action_id, kind, payload_envelope, grant_generation, seller_hash,
                       (SELECT user_id FROM oauth_token WHERE id = 1) AS current_user_id,
                       (SELECT verified_at FROM oauth_token WHERE id = 1) AS current_verified_at
                FROM actions
                WHERE token_hash = ? AND session_hash = ? AND status = 'pending'
                  AND (? IS NULL OR kind = ?)
                  AND expires_at >= ?
                  AND EXISTS (
                      SELECT 1 FROM oauth_token
                      WHERE id = 1
                        AND oauth_token.grant_generation = actions.grant_generation
                        AND (refresh_token_expires_at IS NULL OR refresh_token_expires_at > ?)
                  )
                """,
                (
                    _digest(token),
                    _digest(session_id),
                    expected_kind,
                    expected_kind,
                    now,
                    now,
                ),
            ).fetchone()
        if row is not None and row["seller_hash"] is not None:
            current_user_id = row["current_user_id"]
            if (
                row["current_verified_at"] is None
                or not isinstance(current_user_id, str)
                or not hmac.compare_digest(
                    row["seller_hash"], _digest(f"seller:{current_user_id}")
                )
            ):
                return None
        return self._row_to_action(row) if row else None

    def find_confirmed_action_id_by_token(
        self,
        token: str,
        session_id: str,
        expected_kind: Optional[str] = None,
    ) -> Optional[str]:
        """Look up an action by its own confirmation token, win-or-lose.

        Used only to make a duplicate ``/actions/confirm`` submission (the
        classic double-click/double-submit) clearer, never to let it
        succeed a second time: ``claim_pending_action`` is a one-use atomic
        claim, so a losing duplicate call always returns ``None`` even when
        the winning call it raced against genuinely succeeded moments
        earlier. The web layer still fails that duplicate submission closed
        with its usual 409 -- the one-use invariant never bends -- but when
        this lookup shows the same token already reached a real terminal
        result, the error message it raises can say so plainly instead of
        leaving it ambiguous whether the original click did anything at
        all. It intentionally excludes ``cancelled`` and ``expired`` --
        those must keep failing closed with the original, unenriched
        message, never described as "already completed".
        """

        self._validate_expected_kind(expected_kind)
        if not self._valid_opaque_value(token) or not self._valid_opaque_value(session_id):
            return None
        with self._lock:
            row = self._connection.execute(
                """
                SELECT action_id, status
                FROM actions
                WHERE token_hash = ? AND session_hash = ?
                  AND (? IS NULL OR kind = ?)
                """,
                (_digest(token), _digest(session_id), expected_kind, expected_kind),
            ).fetchone()
        if row is None or row["status"] not in self.ACTION_CONFIRMED_TERMINAL_STATUSES:
            return None
        return str(row["action_id"])

    def claim_pending_action(
        self,
        token: str,
        session_id: str,
        expected_kind: Optional[str] = None,
    ) -> Optional[ClaimedAction]:
        self._validate_expected_kind(expected_kind)
        if not self._valid_opaque_value(token) or not self._valid_opaque_value(session_id):
            return None
        now = int(self._now())
        token_hash = _digest(token)
        session_hash = _digest(session_id)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                binding = self._connection.execute(
                    """
                    SELECT actions.seller_hash, oauth_token.user_id,
                           oauth_token.verified_at, oauth_token.refresh_token_envelope
                    FROM actions
                    JOIN oauth_token
                      ON oauth_token.id = 1
                     AND oauth_token.grant_generation = actions.grant_generation
                    WHERE actions.token_hash = ? AND actions.session_hash = ?
                      AND actions.status = 'pending'
                      AND (? IS NULL OR actions.kind = ?)
                      AND actions.expires_at >= ?
                      AND (
                          oauth_token.refresh_token_expires_at IS NULL
                          OR oauth_token.refresh_token_expires_at > ?
                      )
                    """,
                    (
                        token_hash,
                        session_hash,
                        expected_kind,
                        expected_kind,
                        now,
                        now,
                    ),
                ).fetchone()
                if binding is None:
                    self._connection.execute("COMMIT")
                    return None
                if binding["seller_hash"] is not None:
                    current_user_id = binding["user_id"]
                    if (
                        binding["verified_at"] is None
                        or not isinstance(current_user_id, str)
                        or not hmac.compare_digest(
                            binding["seller_hash"], _digest(f"seller:{current_user_id}")
                        )
                    ):
                        self._connection.execute("COMMIT")
                        return None
                self._cipher.decrypt(
                    binding["refresh_token_envelope"], "oauth-token:refresh"
                )
                cursor = self._connection.execute(
                    """
                    UPDATE actions SET status = 'executing', checkpoint = 'confirmed',
                        updated_at = ?
                    WHERE token_hash = ? AND session_hash = ? AND status = 'pending'
                      AND (? IS NULL OR kind = ?)
                      AND expires_at >= ?
                      AND EXISTS (
                          SELECT 1 FROM oauth_token
                          WHERE id = 1
                            AND oauth_token.grant_generation = actions.grant_generation
                            AND (refresh_token_expires_at IS NULL OR refresh_token_expires_at > ?)
                      )
                    """,
                    (
                        now,
                        token_hash,
                        session_hash,
                        expected_kind,
                        expected_kind,
                        now,
                        now,
                    ),
                )
                row = None
                if cursor.rowcount == 1:
                    row = self._connection.execute(
                        """
                        SELECT action_id, kind, payload_envelope, grant_generation
                        FROM actions WHERE token_hash = ? AND session_hash = ?
                        """,
                        (token_hash, session_hash),
                    ).fetchone()
                    self._insert_action_event_locked(
                        row["action_id"], "confirmed", stage="confirmed", created_at=now
                    )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return self._row_to_action(row) if row else None

    def cancel_pending_action(
        self,
        token: str,
        session_id: str,
        expected_kind: Optional[str] = None,
    ) -> bool:
        self._validate_expected_kind(expected_kind)
        if not self._valid_opaque_value(token) or not self._valid_opaque_value(session_id):
            return False
        now = int(self._now())
        token_hash = _digest(token)
        session_hash = _digest(session_id)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    INSERT INTO action_events (
                        action_id, event_type, stage, terminal_status, created_at
                    )
                    SELECT action_id, 'terminal_result', checkpoint, 'expired', ?
                    FROM actions WHERE status = 'pending' AND expires_at < ?
                    """,
                    (now, now),
                )
                self._connection.execute(
                    """
                    UPDATE actions SET status = 'expired', updated_at = ?
                    WHERE status = 'pending' AND expires_at < ?
                    """,
                    (now, now),
                )
                cursor = self._connection.execute(
                    """
                    UPDATE actions SET status = 'cancelled', updated_at = ?
                    WHERE token_hash = ? AND session_hash = ? AND status = 'pending'
                      AND (? IS NULL OR kind = ?)
                      AND expires_at >= ?
                    """,
                    (
                        now,
                        token_hash,
                        session_hash,
                        expected_kind,
                        expected_kind,
                        now,
                    ),
                )
                if cursor.rowcount == 1:
                    row = self._connection.execute(
                        """
                        SELECT action_id, checkpoint FROM actions
                        WHERE token_hash = ? AND session_hash = ?
                        """,
                        (token_hash, session_hash),
                    ).fetchone()
                    self._insert_action_event_locked(
                        row["action_id"],
                        "terminal_result",
                        stage=row["checkpoint"],
                        terminal_status="cancelled",
                        created_at=now,
                    )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return cursor.rowcount == 1

    def update_action_checkpoint(
        self,
        action_id: str,
        checkpoint: str,
        result: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if not isinstance(checkpoint, str) or checkpoint not in self.ACTION_CHECKPOINTS:
            raise StorageError("Unsupported action checkpoint")
        safe_update = self._safe_action_result(result) if result is not None else None
        now = int(self._now())
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    """
                    SELECT checkpoint, result_envelope FROM actions
                    WHERE action_id = ? AND status = 'executing'
                    """,
                    (action_id,),
                ).fetchone()
                if row is None:
                    raise StorageError("The action journal was not in the executing state")
                previous_checkpoint = row["checkpoint"]
                if (
                    previous_checkpoint is not None
                    and self.ACTION_CHECKPOINTS.index(checkpoint)
                    < self.ACTION_CHECKPOINTS.index(previous_checkpoint)
                ):
                    raise StorageError("Action checkpoints cannot move backwards")
                merged_result: Optional[Dict[str, Any]] = None
                result_envelope = row["result_envelope"]
                if result_envelope is not None:
                    merged_result = self._decode_action_result(action_id, result_envelope)
                if safe_update is not None:
                    merged_result = {**(merged_result or {}), **safe_update}
                    result_envelope = self._cipher.encrypt(
                        json.dumps(merged_result, separators=(",", ":"), sort_keys=True),
                        f"action:{action_id}:result",
                    )
                self._connection.execute(
                    """
                    UPDATE actions SET checkpoint = ?, result_envelope = ?, updated_at = ?
                    WHERE action_id = ? AND status = 'executing'
                    """,
                    (checkpoint, result_envelope, now, action_id),
                )
                self._insert_action_event_locked(
                    action_id,
                    checkpoint,
                    stage=checkpoint,
                    result=merged_result,
                    created_at=now,
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def append_action_event(
        self,
        action_id: str,
        event_type: str,
        *,
        stage: Optional[str] = None,
        terminal_status: Optional[str] = None,
        ebay_error_id: Optional[str] = None,
        ebay_error_category: Optional[str] = None,
        result: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if not isinstance(event_type, str):
            raise StorageError("Unsupported action event type")
        if event_type == "terminal_result" or terminal_status is not None:
            raise StorageError("Terminal action events are written by complete_action")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    """
                    SELECT checkpoint FROM actions
                    WHERE action_id = ? AND status = 'executing'
                    """,
                    (action_id,),
                ).fetchone()
                if row is None:
                    raise StorageError("The action journal was not in the executing state")
                self._insert_action_event_locked(
                    action_id,
                    event_type,
                    stage=row["checkpoint"] if stage is None else stage,
                    ebay_error_id=ebay_error_id,
                    ebay_error_category=ebay_error_category,
                    result=result,
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def complete_action(
        self,
        action_id: str,
        status: str,
        result: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
        *,
        ebay_error_id: Optional[str] = None,
        ebay_error_category: Optional[str] = None,
    ) -> None:
        if not isinstance(status, str) or status not in self.ACTION_TERMINAL_STATUSES:
            raise StorageError("Invalid completed action status")
        safe_update = self._safe_action_result(result) if result is not None else None
        safe_message = self._safe_error_message(error_message)
        now = int(self._now())
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    """
                    SELECT kind, seller_hash, checkpoint, result_envelope FROM actions
                    WHERE action_id = ? AND status = 'executing'
                    """,
                    (action_id,),
                ).fetchone()
                if row is None:
                    raise StorageError("The action journal was not in the executing state")
                merged_result: Optional[Dict[str, Any]] = None
                result_envelope = row["result_envelope"]
                if result_envelope is not None and safe_update is not None:
                    merged_result = self._decode_action_result(action_id, result_envelope)
                if safe_update is not None:
                    merged_result = {**(merged_result or {}), **safe_update}
                    result_envelope = self._cipher.encrypt(
                        json.dumps(merged_result, separators=(",", ":"), sort_keys=True),
                        f"action:{action_id}:result",
                    )
                stored_message = (
                    self._draft_error_message(status)
                    if row["kind"] == "create_draft" and row["seller_hash"] is not None
                    else safe_message
                )
                cursor = self._connection.execute(
                    """
                    UPDATE actions SET status = ?, result_envelope = ?,
                        error_message = ?, updated_at = ?
                    WHERE action_id = ? AND status = 'executing'
                    """,
                    (status, result_envelope, stored_message, now, action_id),
                )
                if cursor.rowcount != 1:
                    raise StorageError("The action journal was not in the executing state")
                self._insert_action_event_locked(
                    action_id,
                    "terminal_result",
                    stage=row["checkpoint"],
                    terminal_status=status,
                    ebay_error_id=ebay_error_id,
                    ebay_error_category=ebay_error_category,
                    result=merged_result if safe_update is not None else None,
                    created_at=now,
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def recent_actions(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT action_id, kind, status, checkpoint, error_message,
                       created_at, updated_at
                FROM actions WHERE status NOT IN ('pending', 'executing')
                ORDER BY created_at DESC LIMIT ?
                """,
                (max(1, min(limit, 50)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def _get_completed_action(
        self,
        action_id: str,
        session_hash: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT action_id, kind, status, checkpoint, result_envelope, error_message,
                       created_at, updated_at
                FROM actions WHERE action_id = ?
                  AND (? IS NULL OR session_hash = ?)
                  AND status NOT IN ('pending', 'executing')
                """,
                (action_id, session_hash, session_hash),
            ).fetchone()
        if row is None:
            return None
        result = None
        if row["result_envelope"]:
            result = self._decode_action_result(action_id, row["result_envelope"])
        action = dict(row)
        action.pop("result_envelope", None)
        return {**action, "result": result}

    def get_action(self, action_id: str, session_id: str) -> Optional[Dict[str, Any]]:
        if not self._valid_opaque_value(action_id, maximum=128) or not self._valid_opaque_value(
            session_id
        ):
            return None
        return self._get_completed_action(action_id, _digest(session_id))

    def get_action_for_admin(self, action_id: str) -> Optional[Dict[str, Any]]:
        """Return a completed safe result after the web layer authenticates the admin.

        Completed journals must survive logout and process restart so uncertain
        writes can still be reconciled. Pending capabilities remain session-bound.
        """

        if not self._valid_opaque_value(action_id, maximum=128):
            return None
        return self._get_completed_action(action_id, None)

    def _action_events(
        self,
        action_id: str,
        session_hash: Optional[str],
    ) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT events.event_id, events.event_type, events.stage,
                       events.terminal_status, events.ebay_error_id,
                       events.ebay_error_category, events.result_envelope,
                       events.created_at
                FROM action_events AS events
                JOIN actions ON actions.action_id = events.action_id
                WHERE events.action_id = ?
                  AND (? IS NULL OR actions.session_hash = ?)
                ORDER BY events.event_id
                """,
                (action_id, session_hash, session_hash),
            ).fetchall()
        events: List[Dict[str, Any]] = []
        for row in rows:
            event = dict(row)
            envelope = event.pop("result_envelope")
            result = None
            if envelope is not None:
                try:
                    decoded = json.loads(
                        self._cipher.decrypt(envelope, f"action-event:{action_id}:result")
                    )
                except json.JSONDecodeError as exc:
                    raise StorageError("Stored action event result is invalid") from exc
                result = self._safe_action_result(decoded)
            events.append({**event, "result": result})
        return events

    def action_events(self, action_id: str, session_id: str) -> List[Dict[str, Any]]:
        if not self._valid_opaque_value(action_id, maximum=128) or not self._valid_opaque_value(
            session_id
        ):
            return []
        return self._action_events(action_id, _digest(session_id))

    def action_events_for_admin(self, action_id: str) -> List[Dict[str, Any]]:
        """Return safe journal events after web-layer admin authentication."""

        if not self._valid_opaque_value(action_id, maximum=128):
            return []
        return self._action_events(action_id, None)

    def _row_to_action(self, row: sqlite3.Row) -> ClaimedAction:
        payload = json.loads(
            self._cipher.decrypt(row["payload_envelope"], f"action:{row['action_id']}:payload")
        )
        return ClaimedAction(
            row["action_id"], row["kind"], payload, row["grant_generation"]
        )
