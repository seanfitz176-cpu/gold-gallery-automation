from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import threading
import time
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from ebay_app.storage import (
    AppDatabase,
    ConnectionChangedStorageError,
    EnvelopeCipher,
    OAuthStateRecord,
    StorageError,
    VerifiedConnectionBinding,
)


class StorageTests(unittest.TestCase):
    @staticmethod
    def _create_version_5_action_database(
        path: Path,
        key: bytes,
        *,
        partial_inventory_verified_marker: bool = False,
    ) -> dict[str, object]:
        """Create the deployed v5 action schema without using current migrations."""

        cipher = EnvelopeCipher(key)
        now = 1_700_000_000
        action_id = "5" * 32
        session_id = "version-5-session"
        payload_envelope = cipher.encrypt(
            '{"sku":"LEGACY-V5"}', f"action:{action_id}:payload"
        )
        result_envelope = cipher.encrypt(
            '{"inventory_created":true,"sku":"LEGACY-V5"}',
            f"action:{action_id}:result",
        )
        event_result_envelope = cipher.encrypt(
            '{"inventory_created":true,"sku":"LEGACY-V5"}',
            f"action-event:{action_id}:result",
        )
        action_inventory_verified = (
            ", 'inventory_verified'" if partial_inventory_verified_marker else ""
        )
        event_inventory_verified = (
            ", 'inventory_verified'" if partial_inventory_verified_marker else ""
        )

        connection = sqlite3.connect(path)
        connection.executescript(
            f"""
            PRAGMA user_version = 5;
            PRAGMA foreign_keys = ON;

            CREATE TABLE encryption_metadata (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                check_envelope TEXT NOT NULL
            );
            CREATE TABLE oauth_token (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                refresh_token_envelope TEXT NOT NULL,
                refresh_token_expires_at INTEGER,
                user_id TEXT,
                grant_generation TEXT NOT NULL,
                granted_scopes TEXT,
                verified_at INTEGER,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE oauth_states (
                state_hash TEXT PRIMARY KEY,
                session_hash TEXT NOT NULL,
                intent TEXT NOT NULL CHECK (intent IN ('connect', 'scope_upgrade')),
                expected_grant_generation TEXT,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE actions (
                action_id TEXT PRIMARY KEY,
                token_hash TEXT UNIQUE NOT NULL,
                session_hash TEXT NOT NULL,
                grant_generation TEXT NOT NULL,
                seller_hash TEXT,
                resource_hash TEXT,
                kind TEXT NOT NULL CHECK (kind IN ('create_draft', 'change_price')),
                status TEXT NOT NULL CHECK (
                    status IN (
                        'pending', 'executing', 'succeeded', 'failed',
                        'partial', 'unknown', 'cancelled', 'expired'
                    )
                ),
                checkpoint TEXT CHECK (
                    checkpoint IS NULL OR checkpoint IN (
                        'confirmed', 'inventory_request_started', 'inventory_created'
                        {action_inventory_verified}, 'offer_request_started',
                        'offer_created', 'verified_unpublished'
                    )
                ),
                payload_envelope TEXT NOT NULL,
                result_envelope TEXT,
                error_message TEXT,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE action_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_id TEXT NOT NULL REFERENCES actions(action_id),
                event_type TEXT NOT NULL CHECK (
                    event_type IN (
                        'confirmed', 'inventory_request_started', 'inventory_created'
                        {event_inventory_verified}, 'offer_request_started', 'offer_created',
                        'verified_unpublished', 'inventory_request_failed',
                        'offer_request_failed', 'verification_failed',
                        'reconciliation_required', 'terminal_result'
                    )
                ),
                stage TEXT CHECK (
                    stage IS NULL OR stage IN (
                        'confirmed', 'inventory_request_started', 'inventory_created',
                        'offer_request_started', 'offer_created', 'verified_unpublished'
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
            CREATE INDEX actions_created_idx ON actions (created_at DESC);
            CREATE INDEX action_events_action_idx
                ON action_events (action_id, event_id);
            CREATE UNIQUE INDEX actions_active_draft_reservation_idx
                ON actions (seller_hash, resource_hash)
                WHERE kind = 'create_draft'
                  AND seller_hash IS NOT NULL
                  AND resource_hash IS NOT NULL
                  AND status IN ('pending', 'executing', 'succeeded', 'partial', 'unknown');
            CREATE TRIGGER action_events_no_update
            BEFORE UPDATE ON action_events
            BEGIN
                SELECT RAISE(ABORT, 'action events are append-only');
            END;
            CREATE TRIGGER action_events_no_delete
            BEFORE DELETE ON action_events
            BEGIN
                SELECT RAISE(ABORT, 'action events are append-only');
            END;
            """
        )
        connection.execute(
            "INSERT INTO encryption_metadata VALUES (1, ?)",
            (cipher.encrypt("python-ebay-sandbox-key-check", "metadata:key-check"),),
        )
        connection.execute(
            "INSERT INTO oauth_token VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
            (
                cipher.encrypt("version-5-refresh", "oauth-token:refresh"),
                9_999_999_999,
                "seller",
                "version-5-generation",
                "[]",
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO actions (
                action_id, token_hash, session_hash, grant_generation,
                seller_hash, resource_hash, kind, status, checkpoint,
                payload_envelope, result_envelope, error_message,
                expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                action_id,
                "version-5-token-hash",
                hashlib.sha256(session_id.encode("utf-8")).hexdigest(),
                "version-5-generation",
                hashlib.sha256(b"seller:seller").hexdigest(),
                hashlib.sha256(b"resource:LEGACY-V5").hexdigest(),
                "create_draft",
                "succeeded",
                "inventory_created",
                payload_envelope,
                result_envelope,
                9_999_999_999,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO action_events (
                event_id, action_id, event_type, stage, terminal_status,
                ebay_error_id, ebay_error_category, result_envelope, created_at
            ) VALUES (7, ?, 'inventory_created', 'inventory_created', NULL,
                      NULL, NULL, ?, ?)
            """,
            (action_id, event_result_envelope, now),
        )
        connection.execute(
            """
            INSERT INTO action_events (
                event_id, action_id, event_type, stage, terminal_status,
                ebay_error_id, ebay_error_category, result_envelope, created_at
            ) VALUES (11, ?, 'terminal_result', 'inventory_created', 'succeeded',
                      NULL, NULL, ?, ?)
            """,
            (action_id, event_result_envelope, now + 1),
        )
        connection.commit()
        connection.close()
        path.chmod(0o600)
        return {
            "action_id": action_id,
            "session_id": session_id,
            "payload_envelope": payload_envelope,
            "result_envelope": result_envelope,
            "event_result_envelope": event_result_envelope,
        }

    def test_scope_upgrade_compare_and_swap_replaces_grant_atomically(self):
        identity_scope = "https://api.ebay.com/oauth/api_scope/commerce.identity.readonly"
        inventory_scope = "https://api.ebay.com/oauth/api_scope/sell.inventory"
        clock = [1_700_000_000]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.sqlite"
            database = AppDatabase(path, b"w" * 32, now=lambda: clock[0])
            self.addCleanup(database.close)
            database.save_refresh_token(
                "original-token",
                1_800_000_000,
                "seller",
                new_grant=True,
                granted_scopes=[identity_scope],
            )
            original_generation = database.connection_generation()
            database.mark_connection_verified(original_generation, "seller")
            action_token = database.create_pending_action(
                "session",
                "change_price",
                {"value": "20.00"},
                expected_generation=original_generation,
            )

            clock[0] += 60
            database.replace_refresh_token_for_scope_upgrade(
                "upgraded-token",
                1_900_000_000,
                "seller",
                [inventory_scope, identity_scope, inventory_scope],
                original_generation,
                "seller",
            )

            self.assertEqual(database.get_refresh_token(), "upgraded-token")
            self.assertNotEqual(database.connection_generation(), original_generation)
            self.assertEqual(
                database.granted_scopes(),
                (identity_scope, inventory_scope),
            )
            status = database.connection_status()
            self.assertFalse(status["verified"])
            self.assertIsNone(status["verified_at"])
            self.assertIsNone(database.claim_pending_action(action_token, "session"))
            self.assertEqual(database.recent_actions()[0]["status"], "cancelled")
            self.assertIn("permissions changed", database.recent_actions()[0]["error_message"])

            database._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            self.assertNotIn(b"upgraded-token", path.read_bytes())

    def test_scope_upgrade_compare_and_swap_mismatch_preserves_grant_and_pending_actions(self):
        identity_scope = "https://api.ebay.com/oauth/api_scope/commerce.identity.readonly"
        with tempfile.TemporaryDirectory() as directory:
            database = AppDatabase(Path(directory) / "app.sqlite", b"x" * 32)
            self.addCleanup(database.close)
            database.save_refresh_token(
                "original-token",
                9999999999,
                "seller",
                new_grant=True,
                granted_scopes=[identity_scope],
            )
            generation = database.connection_generation()
            database.mark_connection_verified(generation, "seller")
            action_token = database.create_pending_action(
                "session",
                "change_price",
                {"value": "20.00"},
                expected_generation=generation,
            )
            original_status = database.connection_status()

            with self.assertRaises(ConnectionChangedStorageError):
                database.replace_refresh_token_for_scope_upgrade(
                    "must-not-be-stored",
                    9999999999,
                    "seller",
                    [identity_scope, "scope:inventory"],
                    "stale-generation",
                    "seller",
                )
            with self.assertRaises(ConnectionChangedStorageError):
                database.replace_refresh_token_for_scope_upgrade(
                    "must-not-be-stored",
                    9999999999,
                    "different-seller",
                    [identity_scope, "scope:inventory"],
                    generation,
                    "different-seller",
                )
            with self.assertRaises(ConnectionChangedStorageError):
                database.replace_refresh_token_for_scope_upgrade(
                    "must-not-be-stored",
                    9999999999,
                    "different-seller",
                    [identity_scope, "scope:inventory"],
                    generation,
                    "seller",
                )

            self.assertEqual(database.get_refresh_token(), "original-token")
            self.assertEqual(database.connection_generation(), generation)
            self.assertEqual(database.granted_scopes(), (identity_scope,))
            self.assertEqual(database.connection_status(), original_status)
            self.assertEqual(database.recent_actions(), [])
            self.assertTrue(database.cancel_pending_action(action_token, "session"))

    def test_granted_scopes_are_normalized_preserved_on_rotation_and_replaced_with_grant(self):
        identity_scope = "https://api.ebay.com/oauth/api_scope/commerce.identity.readonly"
        inventory_scope = "https://api.ebay.com/oauth/api_scope/sell.inventory"
        with tempfile.TemporaryDirectory() as directory:
            database = AppDatabase(Path(directory) / "app.sqlite", b"o" * 32)
            self.addCleanup(database.close)
            database.save_refresh_token(
                "first-token",
                9999999999,
                "seller",
                new_grant=True,
                granted_scopes=[inventory_scope, identity_scope, f" {identity_scope} "],
            )
            first_generation = database.connection_generation()
            database.mark_connection_verified(first_generation, "seller")

            self.assertEqual(
                database.granted_scopes(),
                (identity_scope, inventory_scope),
            )
            self.assertTrue(database.has_granted_scopes([identity_scope]))
            self.assertTrue(
                database.has_granted_scopes([inventory_scope, identity_scope])
            )
            self.assertFalse(database.has_granted_scopes(["scope:not-granted"]))
            self.assertFalse(database.has_granted_scopes([]))

            # A token rotation belongs to the same grant. Even if a token response
            # supplies confusing scope data, it cannot silently expand or narrow it.
            database.save_refresh_token(
                "rotated-token",
                9999999999,
                granted_scopes=["scope:not-granted"],
            )
            self.assertEqual(database.connection_generation(), first_generation)
            self.assertEqual(database.granted_scopes(), (identity_scope, inventory_scope))
            self.assertTrue(database.connection_status()["verified"])

            database.save_refresh_token(
                "replacement-token",
                9999999999,
                "seller",
                new_grant=True,
                granted_scopes=[identity_scope],
            )
            self.assertNotEqual(database.connection_generation(), first_generation)
            self.assertEqual(database.granted_scopes(), (identity_scope,))
            self.assertFalse(database.has_granted_scopes([inventory_scope]))
            self.assertFalse(database.connection_status()["verified"])

            database.save_refresh_token(
                "replacement-with-unknown-scopes",
                9999999999,
                "seller",
                new_grant=True,
            )
            self.assertEqual(database.granted_scopes(), ())
            self.assertFalse(database.has_granted_scopes([identity_scope]))

            database.clear_refresh_token()
            self.assertEqual(database.granted_scopes(), ())
            self.assertFalse(database.has_granted_scopes([identity_scope]))

    def test_scope_inputs_and_corrupt_stored_scopes_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = AppDatabase(Path(directory) / "app.sqlite", b"p" * 32)
            self.addCleanup(database.close)

            with self.assertRaises(StorageError):
                database.save_refresh_token(
                    "token", 9999999999, new_grant=True, granted_scopes="scope:one"
                )
            with self.assertRaises(StorageError):
                database.save_refresh_token(
                    "token", 9999999999, new_grant=True, granted_scopes=["two scopes"]
                )
            with self.assertRaises(StorageError):
                database.save_refresh_token(
                    "token", 9999999999, new_grant=True, granted_scopes=[1]
                )
            with self.assertRaises(StorageError):
                database.has_granted_scopes(None)

            database.save_refresh_token(
                "token", 9999999999, new_grant=True, granted_scopes=["scope:one"]
            )
            with database._connection:
                database._connection.execute(
                    "UPDATE oauth_token SET granted_scopes = ? WHERE id = 1",
                    ('["scope:one","scope:one"]',),
                )
            with self.assertRaises(StorageError):
                database.granted_scopes()
            with self.assertRaises(StorageError):
                database.has_granted_scopes(["scope:one"])

    def test_oauth_state_records_are_session_bound_typed_immutable_and_one_use(self):
        with tempfile.TemporaryDirectory() as directory:
            database = AppDatabase(Path(directory) / "app.sqlite", b"i" * 32)
            self.addCleanup(database.close)

            database.save_oauth_state("connect-state", "session-1")
            record = database.consume_oauth_state_record("connect-state", "session-1")
            self.assertEqual(record, OAuthStateRecord("connect", None))
            with self.assertRaises(FrozenInstanceError):
                record.intent = "scope_upgrade"
            self.assertIsNone(
                database.consume_oauth_state_record("connect-state", "session-1")
            )

            database.save_oauth_state("wrong-session", "session-1")
            self.assertIsNone(
                database.consume_oauth_state_record("wrong-session", "session-2")
            )
            self.assertEqual(
                database.consume_oauth_state_record("wrong-session", "session-1"),
                OAuthStateRecord("connect", None),
            )

            database.save_refresh_token(
                "token",
                9999999999,
                "seller",
                new_grant=True,
                granted_scopes=["scope:identity"],
            )
            generation = database.connection_generation()
            database.save_oauth_state(
                "upgrade-state",
                "session-1",
                intent="scope_upgrade",
                expected_grant_generation=generation,
            )
            self.assertEqual(
                database.consume_oauth_state_record("upgrade-state", "session-1"),
                OAuthStateRecord("scope_upgrade", generation),
            )

    def test_scope_upgrade_state_validation_and_grant_change_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = AppDatabase(Path(directory) / "app.sqlite", b"u" * 32)
            self.addCleanup(database.close)
            database.save_refresh_token(
                "first", 9999999999, "seller", new_grant=True
            )
            generation = database.connection_generation()

            with self.assertRaises(StorageError):
                database.save_oauth_state("state", "session", intent="unexpected")
            with self.assertRaises(StorageError):
                database.save_oauth_state("state", "session", intent=[])
            with self.assertRaises(StorageError):
                database.save_oauth_state("state", "session", intent="scope_upgrade")
            with self.assertRaises(StorageError):
                database.save_oauth_state(
                    "state",
                    "session",
                    intent="connect",
                    expected_grant_generation=generation,
                )
            with self.assertRaises(ConnectionChangedStorageError):
                database.save_oauth_state(
                    "state",
                    "session",
                    intent="scope_upgrade",
                    expected_grant_generation="stale-generation",
                )

            database.save_oauth_state(
                "upgrade-state",
                "session",
                intent="scope_upgrade",
                expected_grant_generation=generation,
            )
            database.save_refresh_token(
                "replacement", 9999999999, "seller", new_grant=True
            )
            self.assertIsNone(
                database.consume_oauth_state_record("upgrade-state", "session")
            )
            self.assertIsNone(
                database.consume_oauth_state_record("upgrade-state", "session")
            )

    def test_verified_binding_is_atomic_and_requires_a_current_verified_grant(self):
        clock = [1_700_000_000]
        with tempfile.TemporaryDirectory() as directory:
            database = AppDatabase(
                Path(directory) / "app.sqlite", b"q" * 32, now=lambda: clock[0]
            )
            self.addCleanup(database.close)
            self.assertIsNone(database.verified_connection_binding())

            database.save_refresh_token(
                "refresh-token", 1_800_000_000, "sandbox-seller", new_grant=True
            )
            self.assertIsNone(database.verified_connection_binding())

            generation = database.connection_generation()
            database.mark_connection_verified(generation, "sandbox-seller")
            self.assertEqual(
                database.verified_connection_binding(),
                VerifiedConnectionBinding(generation, "sandbox-seller"),
            )

            clock[0] = 1_800_000_000
            self.assertIsNone(database.verified_connection_binding())

    def test_connection_is_unverified_until_current_grant_and_identity_are_marked(self):
        with tempfile.TemporaryDirectory() as directory:
            database = AppDatabase(
                Path(directory) / "app.sqlite", b"v" * 32, now=lambda: 1_700_000_000
            )
            self.addCleanup(database.close)
            database.save_refresh_token(
                "refresh-token", 1_800_000_000, None, new_grant=True
            )
            generation = database.connection_generation()

            self.assertEqual(
                database.connection_status(),
                {
                    "connected": True,
                    "verified": False,
                    "verified_at": None,
                    "user_id": None,
                    "refresh_token_expires_at": 1_800_000_000,
                    "updated_at": 1_700_000_000,
                },
            )

            verified_at = database.mark_connection_verified(generation, "sandbox-seller")

            self.assertEqual(verified_at, 1_700_000_000)
            status = database.connection_status()
            self.assertTrue(status["verified"])
            self.assertEqual(status["verified_at"], 1_700_000_000)
            self.assertEqual(status["user_id"], "sandbox-seller")

    def test_refresh_rotation_preserves_verification_but_new_grant_and_clear_do_not(self):
        clock = [1_700_000_000]
        with tempfile.TemporaryDirectory() as directory:
            database = AppDatabase(
                Path(directory) / "app.sqlite", b"r" * 32, now=lambda: clock[0]
            )
            self.addCleanup(database.close)
            database.save_refresh_token(
                "first-token", 1_800_000_000, "seller", new_grant=True
            )
            first_generation = database.connection_generation()
            database.mark_connection_verified(first_generation, "seller")

            clock[0] += 60
            database.save_refresh_token("rotated-token", 1_900_000_000)
            rotated_status = database.connection_status()
            self.assertEqual(database.connection_generation(), first_generation)
            self.assertTrue(rotated_status["verified"])
            self.assertEqual(rotated_status["verified_at"], 1_700_000_000)
            self.assertEqual(rotated_status["user_id"], "seller")

            # Rotation cannot silently rebind an already verified generation to a
            # different seller identity.
            database.save_refresh_token(
                "rotated-token-again", 1_900_000_000, "different-seller"
            )
            self.assertEqual(database.connection_status()["user_id"], "seller")
            self.assertTrue(database.connection_status()["verified"])

            database.save_refresh_token(
                "replacement-token", 1_900_000_000, "seller", new_grant=True
            )
            self.assertNotEqual(database.connection_generation(), first_generation)
            self.assertFalse(database.connection_status()["verified"])
            self.assertIsNone(database.connection_status()["verified_at"])

            database.clear_refresh_token()
            cleared_status = database.connection_status()
            self.assertFalse(cleared_status["connected"])
            self.assertFalse(cleared_status["verified"])
            self.assertIsNone(cleared_status["verified_at"])

    def test_verification_rejects_stale_generation_and_different_seller(self):
        with tempfile.TemporaryDirectory() as directory:
            database = AppDatabase(Path(directory) / "app.sqlite", b"s" * 32)
            self.addCleanup(database.close)
            database.save_refresh_token(
                "refresh-token", 9999999999, "expected-seller", new_grant=True
            )
            current_generation = database.connection_generation()

            with self.assertRaises(ConnectionChangedStorageError):
                database.mark_connection_verified("stale-generation", "expected-seller")
            with self.assertRaises(ConnectionChangedStorageError):
                database.mark_connection_verified(current_generation, "other-seller")

            status = database.connection_status()
            self.assertFalse(status["verified"])
            self.assertIsNone(status["verified_at"])

    def test_encrypts_tokens_and_consumes_state_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db" / "app.sqlite"
            database = AppDatabase(path, b"a" * 32)
            self.addCleanup(database.close)
            database.save_refresh_token("secret-refresh-token", 9999999999, "sandbox-user")
            self.assertEqual(database.get_refresh_token(), "secret-refresh-token")
            database._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            self.assertNotIn(b"secret-refresh-token", path.read_bytes())

            database.save_oauth_state("state", "session-1")
            self.assertFalse(database.consume_oauth_state("state", "session-2"))
            self.assertTrue(database.consume_oauth_state("state", "session-1"))
            self.assertFalse(database.consume_oauth_state("state", "session-1"))

    def test_pending_confirmation_is_session_bound_and_claimed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            database = AppDatabase(Path(directory) / "app.sqlite", b"b" * 32)
            self.addCleanup(database.close)
            database.save_refresh_token(
                "refresh-token", 9999999999, "sandbox-user", new_grant=True
            )
            token = database.create_pending_action(
                "session-1",
                "change_price",
                {"value": "20.00"},
                expected_generation=database.connection_generation(),
            )
            self.assertIsNone(database.claim_pending_action(token, "session-2"))
            results = []

            def claim():
                results.append(database.claim_pending_action(token, "session-1"))

            threads = [threading.Thread(target=claim) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            claimed = [result for result in results if result is not None]
            self.assertEqual(len(claimed), 1)
            database.complete_action(claimed[0].action_id, "succeeded", {"ok": True})
            self.assertIsNone(database.get_action(claimed[0].action_id, "session-2"))
            self.assertEqual(
                database.get_action(claimed[0].action_id, "session-1")["result"],
                {"ok": True},
            )
            self.assertEqual(len(database.recent_actions()), 1)

    def test_pending_action_kind_is_checked_without_consuming_the_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            database = AppDatabase(Path(directory) / "app.sqlite", b"k" * 32)
            self.addCleanup(database.close)
            database.save_refresh_token(
                "refresh-token", 9999999999, "sandbox-user", new_grant=True
            )
            generation = database.connection_generation()
            claim_token = database.create_pending_action(
                "session",
                "change_price",
                {"value": "20.00"},
                expected_generation=generation,
            )

            self.assertIsNone(
                database.get_pending_action(
                    claim_token, "session", expected_kind="create_draft"
                )
            )
            self.assertEqual(
                database.get_pending_action(
                    claim_token, "session", expected_kind="change_price"
                ).kind,
                "change_price",
            )
            self.assertIsNone(
                database.claim_pending_action(
                    claim_token, "session", expected_kind="create_draft"
                )
            )
            claimed = database.claim_pending_action(
                claim_token, "session", expected_kind="change_price"
            )
            self.assertIsNotNone(claimed)
            database.complete_action(claimed.action_id, "failed")

            cancel_token = database.create_pending_action(
                "session",
                "change_price",
                {"value": "21.00"},
                expected_generation=generation,
            )
            self.assertFalse(
                database.cancel_pending_action(
                    cancel_token, "session", expected_kind="create_draft"
                )
            )
            self.assertTrue(
                database.cancel_pending_action(
                    cancel_token, "session", expected_kind="change_price"
                )
            )
            with self.assertRaises(StorageError):
                database.get_pending_action(
                    cancel_token, "session", expected_kind="unexpected"
                )

    def test_seller_bound_pending_action_requires_current_verified_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.sqlite"
            database = AppDatabase(path, b"h" * 32)
            self.addCleanup(database.close)
            database.save_refresh_token(
                "refresh-token", 9999999999, "seller-one", new_grant=True
            )
            generation = database.connection_generation()

            with self.assertRaises(ConnectionChangedStorageError):
                database.create_pending_action(
                    "session",
                    "create_draft",
                    {"sku": "SKU-ONE"},
                    expected_generation=generation,
                    expected_user_id="seller-one",
                    resource_key="SKU-ONE",
                )
            database.mark_connection_verified(generation, "seller-one")
            with self.assertRaises(ConnectionChangedStorageError):
                database.create_pending_action(
                    "session",
                    "create_draft",
                    {"sku": "SKU-ONE"},
                    expected_generation=generation,
                    expected_user_id="seller-two",
                    resource_key="SKU-ONE",
                )
            with self.assertRaises(StorageError):
                database.create_pending_action(
                    "session",
                    "create_draft",
                    {"sku": "SKU-ONE"},
                    expected_generation=generation,
                    expected_user_id="seller-one",
                )
            with self.assertRaisesRegex(StorageError, "five minutes"):
                database.create_pending_action(
                    "session",
                    "create_draft",
                    {"sku": "SKU-ONE"},
                    expected_generation=generation,
                    ttl_seconds=301,
                    expected_user_id="seller-one",
                    resource_key="SKU-ONE",
                )

            token = database.create_pending_action(
                "session",
                "create_draft",
                {"sku": "SKU-ONE"},
                expected_generation=generation,
                expected_user_id="seller-one",
                resource_key="SKU-ONE",
            )
            row = database._connection.execute(
                "SELECT seller_hash, resource_hash FROM actions WHERE token_hash = ?",
                (hashlib.sha256(token.encode("utf-8")).hexdigest(),),
            ).fetchone()
            self.assertEqual(len(row["seller_hash"]), 64)
            self.assertEqual(len(row["resource_hash"]), 64)
            self.assertNotEqual(row["seller_hash"], "seller-one")
            self.assertNotEqual(row["resource_hash"], "SKU-ONE")
            with database._connection:
                database._connection.execute(
                    "UPDATE oauth_token SET verified_at = NULL WHERE id = 1"
                )
            self.assertIsNone(
                database.get_pending_action(
                    token, "session", expected_kind="create_draft"
                )
            )
            self.assertIsNone(
                database.claim_pending_action(
                    token, "session", expected_kind="create_draft"
                )
            )
            database.mark_connection_verified(generation, "seller-one")
            self.assertIsNotNone(
                database.get_pending_action(
                    token, "session", expected_kind="create_draft"
                )
            )
            database._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            contents = path.read_bytes()
            # oauth_token intentionally owns the verified user ID; the action row
            # contains only its domain-separated hash.
            self.assertNotIn(b"SKU-ONE", contents)

    def test_create_draft_reservation_covers_every_uncertain_or_created_status(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [1_700_000_000]
            database = AppDatabase(
                Path(directory) / "app.sqlite", b"j" * 32, now=lambda: clock[0]
            )
            self.addCleanup(database.close)
            database.save_refresh_token(
                "refresh-token", 1_800_000_000, "seller", new_grant=True
            )
            generation = database.connection_generation()
            database.mark_connection_verified(generation, "seller")

            for status in ("pending", "executing", "succeeded", "partial", "unknown"):
                with self.subTest(status=status):
                    resource = f"SKU-{status}"
                    token = database.create_pending_action(
                        "session",
                        "create_draft",
                        {"sku": resource},
                        expected_generation=generation,
                        expected_user_id="seller",
                        resource_key=resource,
                    )
                    if status != "pending":
                        claimed = database.claim_pending_action(
                            token, "session", expected_kind="create_draft"
                        )
                        if status != "executing":
                            database.complete_action(claimed.action_id, status)
                    with self.assertRaisesRegex(StorageError, "active draft reservation"):
                        database.create_pending_action(
                            "other-session",
                            "create_draft",
                            {"sku": resource},
                            expected_generation=generation,
                            expected_user_id="seller",
                            resource_key=resource,
                        )

            failed_token = database.create_pending_action(
                "session",
                "create_draft",
                {"sku": "SKU-failed"},
                expected_generation=generation,
                expected_user_id="seller",
                resource_key="SKU-failed",
            )
            failed = database.claim_pending_action(
                failed_token, "session", expected_kind="create_draft"
            )
            database.complete_action(failed.action_id, "failed")
            replacement = database.create_pending_action(
                "session",
                "create_draft",
                {"sku": "SKU-failed"},
                expected_generation=generation,
                expected_user_id="seller",
                resource_key="SKU-failed",
            )
            self.assertTrue(
                database.cancel_pending_action(
                    replacement, "session", expected_kind="create_draft"
                )
            )

            expiring = database.create_pending_action(
                "session",
                "create_draft",
                {"sku": "SKU-expired"},
                expected_generation=generation,
                ttl_seconds=1,
                expected_user_id="seller",
                resource_key="SKU-expired",
            )
            clock[0] += 2
            replacement = database.create_pending_action(
                "session",
                "create_draft",
                {"sku": "SKU-expired"},
                expected_generation=generation,
                expected_user_id="seller",
                resource_key="SKU-expired",
            )
            self.assertIsNone(
                database.get_pending_action(
                    expiring, "session", expected_kind="create_draft"
                )
            )
            self.assertIsNotNone(
                database.get_pending_action(
                    replacement, "session", expected_kind="create_draft"
                )
            )
            cancel_expired = database.create_pending_action(
                "session",
                "create_draft",
                {"sku": "SKU-cancel-expired"},
                expected_generation=generation,
                ttl_seconds=1,
                expected_user_id="seller",
                resource_key="SKU-cancel-expired",
            )
            clock[0] += 2
            self.assertFalse(
                database.cancel_pending_action(
                    cancel_expired, "session", expected_kind="create_draft"
                )
            )

    def test_checkpoints_and_append_only_events_store_only_safe_encrypted_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.sqlite"
            database = AppDatabase(path, b"n" * 32)
            self.addCleanup(database.close)
            database.save_refresh_token(
                "refresh-token", 9999999999, "seller", new_grant=True
            )
            generation = database.connection_generation()
            database.mark_connection_verified(generation, "seller")
            token = database.create_pending_action(
                "session",
                "create_draft",
                {"sku": "SKU-SAFE"},
                expected_generation=generation,
                expected_user_id="seller",
                resource_key="SKU-SAFE",
            )
            action = database.claim_pending_action(
                token, "session", expected_kind="create_draft"
            )
            database.update_action_checkpoint(
                action.action_id, "inventory_request_started", {"sku": "SKU-SAFE"}
            )
            database.update_action_checkpoint(
                action.action_id, "inventory_created", {"inventory_created": True}
            )
            database.update_action_checkpoint(
                action.action_id, "offer_request_started"
            )
            database.update_action_checkpoint(
                action.action_id, "offer_created", {"offer_id": "OFFER-SECRET"}
            )
            database.append_action_event(
                action.action_id,
                "verification_failed",
                ebay_error_id="25001",
                ebay_error_category="REQUEST",
                result={"safe_code": "READBACK_MISMATCH"},
            )
            with self.assertRaises(StorageError):
                database.update_action_checkpoint(
                    action.action_id,
                    "offer_created",
                    {"inventory_response": {"raw": "must-not-store"}},
                )
            with self.assertRaises(StorageError):
                database.append_action_event(action.action_id, "raw_response")

            database.complete_action(
                action.action_id,
                "unknown",
                error_message="RAW-EBAY-LONG-MESSAGE-SENTINEL",
                ebay_error_id="25001",
                ebay_error_category="REQUEST",
            )
            self.assertIsNone(database.get_action(action.action_id, "other-session"))
            stored = database.get_action(action.action_id, "session")
            self.assertEqual(stored["checkpoint"], "offer_created")
            self.assertEqual(
                stored["error_message"],
                "Draft creation outcome requires reconciliation",
            )
            self.assertEqual(
                stored["result"],
                {
                    "inventory_created": True,
                    "offer_id": "OFFER-SECRET",
                    "sku": "SKU-SAFE",
                },
            )
            self.assertEqual(database.action_events(action.action_id, "other-session"), [])
            events = database.action_events(action.action_id, "session")
            self.assertEqual(events[0]["event_type"], "confirmed")
            self.assertEqual(events[-1]["event_type"], "terminal_result")
            self.assertEqual(events[-1]["terminal_status"], "unknown")
            self.assertEqual(events[-1]["ebay_error_id"], "25001")
            self.assertEqual(events[-2]["result"], {"safe_code": "READBACK_MISMATCH"})
            with self.assertRaises(StorageError):
                database.append_action_event(action.action_id, "verification_failed")
            with self.assertRaises(sqlite3.IntegrityError):
                with database._connection:
                    database._connection.execute(
                        "UPDATE action_events SET event_type = 'confirmed' WHERE event_id = ?",
                        (events[0]["event_id"],),
                    )
            with self.assertRaises(sqlite3.IntegrityError):
                with database._connection:
                    database._connection.execute(
                        "DELETE FROM action_events WHERE event_id = ?",
                        (events[0]["event_id"],),
                    )
            database._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            contents = path.read_bytes()
            self.assertNotIn(b"OFFER-SECRET", contents)
            self.assertNotIn(b"must-not-store", contents)
            self.assertNotIn(b"RAW-EBAY-LONG-MESSAGE-SENTINEL", contents)

    def test_restart_marks_executing_unknown_without_erasing_checkpoint_result(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.sqlite"
            database = AppDatabase(path, b"z" * 32)
            database.save_refresh_token(
                "refresh-token", 9999999999, "seller", new_grant=True
            )
            generation = database.connection_generation()
            database.mark_connection_verified(generation, "seller")
            token = database.create_pending_action(
                "session",
                "create_draft",
                {"sku": "SKU-RESTART"},
                expected_generation=generation,
                expected_user_id="seller",
                resource_key="SKU-RESTART",
            )
            action = database.claim_pending_action(
                token, "session", expected_kind="create_draft"
            )
            database.update_action_checkpoint(
                action.action_id,
                "inventory_created",
                {"sku": "SKU-RESTART", "inventory_created": True},
            )
            original_envelope = database._connection.execute(
                "SELECT result_envelope FROM actions WHERE action_id = ?",
                (action.action_id,),
            ).fetchone()[0]
            pending_token = database.create_pending_action(
                "session",
                "create_draft",
                {"sku": "SKU-PENDING-AT-RESTART"},
                expected_generation=generation,
                expected_user_id="seller",
                resource_key="SKU-PENDING-AT-RESTART",
            )
            pending_id = database._connection.execute(
                "SELECT action_id FROM actions WHERE token_hash = ?",
                (hashlib.sha256(pending_token.encode("utf-8")).hexdigest(),),
            ).fetchone()[0]
            database.close()

            reopened = AppDatabase(path, b"z" * 32)
            self.addCleanup(reopened.close)
            migrated = reopened.get_action(action.action_id, "session")
            self.assertEqual(migrated["status"], "unknown")
            self.assertEqual(migrated["checkpoint"], "inventory_created")
            self.assertEqual(
                migrated["result"],
                {"sku": "SKU-RESTART", "inventory_created": True},
            )
            self.assertEqual(
                reopened._connection.execute(
                    "SELECT result_envelope FROM actions WHERE action_id = ?",
                    (action.action_id,),
                ).fetchone()[0],
                original_envelope,
            )
            events = reopened.action_events(action.action_id, "session")
            self.assertEqual(events[-1]["event_type"], "terminal_result")
            self.assertEqual(events[-1]["stage"], "inventory_created")
            self.assertEqual(events[-1]["terminal_status"], "unknown")
            self.assertIsNone(
                reopened.get_pending_action(
                    pending_token, "session", expected_kind="create_draft"
                )
            )
            self.assertEqual(reopened.get_action(pending_id, "session")["status"], "cancelled")
            self.assertEqual(
                reopened.action_events(pending_id, "session")[-1]["terminal_status"],
                "cancelled",
            )

    def test_schema_v4_migration_does_not_decrypt_action_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "version-4.sqlite"
            cipher = EnvelopeCipher(b"y" * 32)
            now = int(time.time())
            action_id = "4" * 32
            session_id = "legacy-session"
            result_envelope = cipher.encrypt(
                '{"offer_id":"LEGACY-SAFE"}', f"action:{action_id}:result"
            )
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                PRAGMA user_version = 4;
                CREATE TABLE encryption_metadata (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    check_envelope TEXT NOT NULL
                );
                CREATE TABLE oauth_token (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    refresh_token_envelope TEXT NOT NULL,
                    refresh_token_expires_at INTEGER,
                    user_id TEXT,
                    grant_generation TEXT NOT NULL,
                    granted_scopes TEXT,
                    verified_at INTEGER,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE oauth_states (
                    state_hash TEXT PRIMARY KEY,
                    session_hash TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    expected_grant_generation TEXT,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE actions (
                    action_id TEXT PRIMARY KEY,
                    token_hash TEXT UNIQUE NOT NULL,
                    session_hash TEXT NOT NULL,
                    grant_generation TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_envelope TEXT NOT NULL,
                    result_envelope TEXT,
                    error_message TEXT,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT INTO encryption_metadata VALUES (1, ?)",
                (cipher.encrypt("python-ebay-sandbox-key-check", "metadata:key-check"),),
            )
            connection.execute(
                "INSERT INTO oauth_token VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cipher.encrypt("refresh", "oauth-token:refresh"),
                    9999999999,
                    "seller",
                    "generation",
                    "[]",
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO actions VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                (
                    action_id,
                    "token-hash",
                    hashlib.sha256(session_id.encode("utf-8")).hexdigest(),
                    "generation",
                    "create_draft",
                    "executing",
                    "deliberately-not-an-envelope",
                    result_envelope,
                    now + 300,
                    now,
                    now,
                ),
            )
            connection.commit()
            connection.close()
            path.chmod(0o600)

            database = AppDatabase(path, b"y" * 32)
            self.addCleanup(database.close)
            self.assertEqual(
                database._connection.execute("PRAGMA user_version").fetchone()[0],
                AppDatabase.SCHEMA_VERSION,
            )
            self.assertTrue(
                {"seller_hash", "resource_hash", "checkpoint"}.issubset(
                    database._table_columns("actions")
                )
            )
            self.assertEqual(
                database._connection.execute(
                    "SELECT result_envelope FROM actions WHERE action_id = ?", (action_id,)
                ).fetchone()[0],
                result_envelope,
            )
            migrated = database.get_action(action_id, session_id)
            self.assertEqual(migrated["status"], "unknown")
            self.assertEqual(migrated["result"], {"offer_id": "LEGACY-SAFE"})
            self.assertEqual(
                database.action_events(action_id, session_id)[-1]["terminal_status"],
                "unknown",
            )

    def test_schema_v5_checkpoint_migration_preserves_rows_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "version-5.sqlite"
            key = b"5" * 32
            fixture = self._create_version_5_action_database(path, key)

            database = AppDatabase(path, key)
            self.addCleanup(database.close)
            self.assertEqual(
                database._connection.execute("PRAGMA user_version").fetchone()[0],
                AppDatabase.SCHEMA_VERSION,
            )
            action_row = database._connection.execute(
                """
                SELECT payload_envelope, result_envelope, status, checkpoint
                FROM actions WHERE action_id = ?
                """,
                (fixture["action_id"],),
            ).fetchone()
            self.assertEqual(action_row["payload_envelope"], fixture["payload_envelope"])
            self.assertEqual(action_row["result_envelope"], fixture["result_envelope"])
            self.assertEqual(action_row["status"], "succeeded")
            self.assertEqual(action_row["checkpoint"], "inventory_created")
            event_rows = database._connection.execute(
                """
                SELECT event_id, event_type, stage, terminal_status, result_envelope
                FROM action_events WHERE action_id = ? ORDER BY event_id
                """,
                (fixture["action_id"],),
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in event_rows],
                [
                    (
                        7,
                        "inventory_created",
                        "inventory_created",
                        None,
                        fixture["event_result_envelope"],
                    ),
                    (
                        11,
                        "terminal_result",
                        "inventory_created",
                        "succeeded",
                        fixture["event_result_envelope"],
                    ),
                ],
            )
            self.assertEqual(
                database.get_action(fixture["action_id"], fixture["session_id"])["result"],
                {"inventory_created": True, "sku": "LEGACY-V5"},
            )
            self.assertEqual(
                database._connection.execute("PRAGMA foreign_key_check").fetchall(), []
            )
            indexes = {
                row[0]
                for row in database._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
            }
            self.assertTrue(
                {
                    "actions_created_idx",
                    "action_events_action_idx",
                    "actions_active_draft_reservation_idx",
                }.issubset(indexes)
            )
            triggers = {
                row[0]
                for row in database._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                ).fetchall()
            }
            self.assertTrue(
                {"action_events_no_update", "action_events_no_delete"}.issubset(triggers)
            )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                database._connection.execute(
                    "UPDATE action_events SET created_at = created_at WHERE event_id = 7"
                )
            database._connection.rollback()
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                database._connection.execute("DELETE FROM action_events WHERE event_id = 7")
            database._connection.rollback()

            generation = database.connection_generation()
            token = database.create_pending_action(
                "new-session",
                "create_draft",
                {"sku": "NEW-V6-SKU"},
                expected_generation=generation,
                expected_user_id="seller",
                resource_key="NEW-V6-SKU",
            )
            claimed = database.claim_pending_action(
                token, "new-session", expected_kind="create_draft"
            )
            self.assertIsNotNone(claimed)
            database.update_action_checkpoint(
                claimed.action_id,
                "inventory_verified",
                {"sku": "NEW-V6-SKU", "inventory_verified": True},
            )
            database.complete_action(
                claimed.action_id,
                "succeeded",
                {"verified_unpublished": True},
            )
            new_events = database._connection.execute(
                """
                SELECT event_id, event_type, stage FROM action_events
                WHERE action_id = ? ORDER BY event_id
                """,
                (claimed.action_id,),
            ).fetchall()
            self.assertTrue(all(row["event_id"] > 11 for row in new_events))
            self.assertIn(
                ("inventory_verified", "inventory_verified"),
                [(row["event_type"], row["stage"]) for row in new_events],
            )

            database.close()
            reopened = AppDatabase(path, key)
            self.addCleanup(reopened.close)
            self.assertEqual(
                reopened._connection.execute("PRAGMA user_version").fetchone()[0],
                AppDatabase.SCHEMA_VERSION,
            )
            self.assertEqual(
                reopened.get_action(fixture["action_id"], fixture["session_id"])["result"],
                {"inventory_created": True, "sku": "LEGACY-V5"},
            )
            reopened_action = reopened.get_action(claimed.action_id, "new-session")
            self.assertEqual(reopened_action["status"], "succeeded")
            self.assertTrue(reopened_action["result"]["inventory_verified"])
            self.assertEqual(
                reopened._connection.execute("PRAGMA foreign_key_check").fetchall(), []
            )

    def test_schema_v5_partial_inventory_verified_marker_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "partial-version-5.sqlite"
            key = b"6" * 32
            fixture = self._create_version_5_action_database(
                path, key, partial_inventory_verified_marker=True
            )

            database = AppDatabase(path, key)
            self.addCleanup(database.close)
            definitions = {
                row["name"]: row["sql"]
                for row in database._connection.execute(
                    """
                    SELECT name, sql FROM sqlite_master
                    WHERE type = 'table' AND name IN ('actions', 'action_events')
                    """
                ).fetchall()
            }
            self.assertGreaterEqual(definitions["actions"].count("'inventory_verified'"), 1)
            self.assertGreaterEqual(
                definitions["action_events"].count("'inventory_verified'"), 2
            )
            self.assertEqual(
                database.get_action(fixture["action_id"], fixture["session_id"])["result"],
                {"inventory_created": True, "sku": "LEGACY-V5"},
            )

            generation = database.connection_generation()
            token = database.create_pending_action(
                "partial-session",
                "create_draft",
                {"sku": "PARTIAL-MARKER-SKU"},
                expected_generation=generation,
                expected_user_id="seller",
                resource_key="PARTIAL-MARKER-SKU",
            )
            claimed = database.claim_pending_action(
                token, "partial-session", expected_kind="create_draft"
            )
            self.assertIsNotNone(claimed)
            database.update_action_checkpoint(
                claimed.action_id,
                "inventory_verified",
                {"sku": "PARTIAL-MARKER-SKU", "inventory_verified": True},
            )
            event = database._connection.execute(
                """
                SELECT event_type, stage FROM action_events
                WHERE action_id = ? AND event_type = 'inventory_verified'
                """,
                (claimed.action_id,),
            ).fetchone()
            self.assertEqual(tuple(event), ("inventory_verified", "inventory_verified"))
            self.assertEqual(
                database._connection.execute("PRAGMA foreign_key_check").fetchall(), []
            )

    def test_wrong_encryption_key_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.sqlite"
            first = AppDatabase(path, b"c" * 32)
            first.close()
            with self.assertRaises(StorageError):
                AppDatabase(path, b"d" * 32)

    def test_second_process_handle_is_rejected_until_close(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.sqlite"
            first = AppDatabase(path, b"e" * 32)
            with self.assertRaisesRegex(StorageError, "already using"):
                AppDatabase(path, b"e" * 32)
            first.close()
            reopened = AppDatabase(path, b"e" * 32)
            reopened.close()

    def test_connection_change_cancels_pending_action(self):
        with tempfile.TemporaryDirectory() as directory:
            database = AppDatabase(Path(directory) / "app.sqlite", b"f" * 32)
            self.addCleanup(database.close)
            database.save_refresh_token("first", 9999999999, "seller", new_grant=True)
            token = database.create_pending_action(
                "session",
                "change_price",
                {"value": "20"},
                expected_generation=database.connection_generation(),
            )
            self.assertEqual(database.recent_actions(), [])
            database.save_refresh_token("second", 9999999999, "seller", new_grant=True)
            self.assertIsNone(database.claim_pending_action(token, "session"))

    def test_legacy_schema_backfills_generation_and_cancels_pending_review(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite"
            cipher = EnvelopeCipher(b"g" * 32)
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE encryption_metadata (
                    id INTEGER PRIMARY KEY,
                    check_envelope TEXT NOT NULL
                );
                CREATE TABLE oauth_token (
                    id INTEGER PRIMARY KEY,
                    refresh_token_envelope TEXT NOT NULL,
                    refresh_token_expires_at INTEGER,
                    user_id TEXT,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE oauth_states (
                    state_hash TEXT PRIMARY KEY,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE actions (
                    action_id TEXT PRIMARY KEY,
                    token_hash TEXT UNIQUE NOT NULL,
                    session_hash TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_envelope TEXT NOT NULL,
                    result_envelope TEXT,
                    error_message TEXT,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )
            now = int(time.time())
            connection.execute(
                "INSERT INTO encryption_metadata VALUES (1, ?)",
                (cipher.encrypt("python-ebay-sandbox-key-check", "metadata:key-check"),),
            )
            connection.execute(
                "INSERT INTO oauth_token VALUES (1, ?, ?, ?, ?)",
                (
                    cipher.encrypt("refresh", "oauth-token:refresh"),
                    9999999999,
                    "seller",
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO oauth_states VALUES ('old-state', ?)",
                (now + 300,),
            )
            connection.execute(
                "INSERT INTO actions VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)",
                (
                    "a" * 32,
                    "token-hash",
                    "session-hash",
                    "change_price",
                    "pending",
                    cipher.encrypt('{"value":"20"}', f"action:{'a' * 32}:payload"),
                    now + 300,
                    now,
                    now,
                ),
            )
            connection.commit()
            connection.close()
            path.chmod(0o600)

            database = AppDatabase(path, b"g" * 32)
            self.addCleanup(database.close)
            self.assertIsNotNone(database.connection_generation())
            self.assertFalse(database.connection_status()["verified"])
            self.assertIsNone(database.connection_status()["verified_at"])
            self.assertIn("verified_at", database._table_columns("oauth_token"))
            self.assertIn("granted_scopes", database._table_columns("oauth_token"))
            self.assertEqual(database.granted_scopes(), ())
            self.assertFalse(database.has_granted_scopes(["scope:anything"]))
            self.assertEqual(
                database._connection.execute("PRAGMA user_version").fetchone()[0],
                AppDatabase.SCHEMA_VERSION,
            )
            self.assertEqual(database.recent_actions()[0]["status"], "cancelled")
            state_count = database._connection.execute(
                "SELECT COUNT(*) FROM oauth_states"
            ).fetchone()[0]
            self.assertEqual(state_count, 0)

    def test_schema_v3_grant_migrates_with_unknown_scopes_and_cannot_gain_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "version-3.sqlite"
            cipher = EnvelopeCipher(b"m" * 32)
            now = int(time.time())
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                PRAGMA user_version = 3;
                CREATE TABLE encryption_metadata (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    check_envelope TEXT NOT NULL
                );
                CREATE TABLE oauth_token (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    refresh_token_envelope TEXT NOT NULL,
                    refresh_token_expires_at INTEGER,
                    user_id TEXT,
                    grant_generation TEXT NOT NULL,
                    verified_at INTEGER,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE oauth_states (
                    state_hash TEXT PRIMARY KEY,
                    session_hash TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT INTO encryption_metadata VALUES (1, ?)",
                (cipher.encrypt("python-ebay-sandbox-key-check", "metadata:key-check"),),
            )
            connection.execute(
                "INSERT INTO oauth_token VALUES (1, ?, ?, ?, ?, ?, ?)",
                (
                    cipher.encrypt("version-3-token", "oauth-token:refresh"),
                    9999999999,
                    "seller",
                    "version-3-generation",
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO oauth_states VALUES (?, ?, ?)",
                ("old-state-hash", "old-session-hash", now + 300),
            )
            connection.commit()
            connection.close()
            path.chmod(0o600)

            database = AppDatabase(path, b"m" * 32)
            self.addCleanup(database.close)
            self.assertEqual(database.get_refresh_token(), "version-3-token")
            self.assertTrue(database.connection_status()["verified"])
            self.assertEqual(database.granted_scopes(), ())
            self.assertFalse(database.has_granted_scopes(["scope:identity"]))
            database.save_refresh_token(
                "rotated-version-3-token",
                9999999999,
                granted_scopes=["scope:identity"],
            )
            self.assertEqual(database.granted_scopes(), ())
            self.assertFalse(database.has_granted_scopes(["scope:identity"]))
            self.assertIn("granted_scopes", database._table_columns("oauth_token"))
            self.assertTrue(
                {"intent", "expected_grant_generation"}.issubset(
                    database._table_columns("oauth_states")
                )
            )
            self.assertEqual(
                database._connection.execute("SELECT COUNT(*) FROM oauth_states").fetchone()[0],
                0,
            )
            self.assertEqual(
                database._connection.execute("PRAGMA user_version").fetchone()[0],
                AppDatabase.SCHEMA_VERSION,
            )


class ProductionEnvironmentErrorMessageTests(unittest.TestCase):
    """Confirm storage.py's environment-aware messages say "Production" when
    AppDatabase is configured for production, and "Sandbox" by default."""

    def test_scope_upgrade_grant_changed_message_says_production(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.sqlite"
            database = AppDatabase(path, b"p" * 32, environment="Production")
            self.addCleanup(database.close)
            with self.assertRaisesRegex(
                ConnectionChangedStorageError,
                "The Production seller grant changed during the scope upgrade",
            ):
                database.replace_refresh_token_for_scope_upgrade(
                    "new-refresh-token",
                    9999999999,
                    "seller",
                    ["scope:identity"],
                    "expected-generation",
                    "seller",
                )

    def test_connection_verified_user_id_required_message_says_production(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.sqlite"
            database = AppDatabase(path, b"p" * 32, environment="Production")
            self.addCleanup(database.close)
            with self.assertRaisesRegex(
                StorageError,
                "A Production seller user ID is required to verify the connection",
            ):
                database.mark_connection_verified("generation", "")

    def test_connection_changed_before_verified_message_says_production(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.sqlite"
            database = AppDatabase(path, b"p" * 32, environment="Production")
            self.addCleanup(database.close)
            with self.assertRaisesRegex(
                ConnectionChangedStorageError,
                "The Production seller connection changed before it was verified",
            ):
                database.mark_connection_verified("generation", "seller")

    def test_connection_changed_during_review_message_says_production(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.sqlite"
            database = AppDatabase(path, b"p" * 32, environment="Production")
            self.addCleanup(database.close)
            with self.assertRaisesRegex(
                ConnectionChangedStorageError,
                "The Production seller connection changed during review",
            ):
                database.create_pending_action(
                    "session",
                    "create_draft",
                    {"sku": "SKU-ONE"},
                    expected_generation="generation",
                )

    def test_duplicate_draft_reservation_message_says_production(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.sqlite"
            database = AppDatabase(path, b"p" * 32, environment="Production")
            self.addCleanup(database.close)
            database.save_refresh_token(
                "refresh-token", 9999999999, "seller", new_grant=True
            )
            generation = database.connection_generation()
            database.mark_connection_verified(generation, "seller")
            database.create_pending_action(
                "session",
                "create_draft",
                {"sku": "SKU-ONE"},
                expected_generation=generation,
                expected_user_id="seller",
                resource_key="SKU-ONE",
            )
            with self.assertRaisesRegex(
                StorageError,
                "This Production seller resource already has an active draft reservation",
            ):
                database.create_pending_action(
                    "other-session",
                    "create_draft",
                    {"sku": "SKU-ONE"},
                    expected_generation=generation,
                    expected_user_id="seller",
                    resource_key="SKU-ONE",
                )

    def test_sandbox_default_messages_are_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.sqlite"
            database = AppDatabase(path, b"p" * 32)
            self.addCleanup(database.close)
            with self.assertRaisesRegex(
                ConnectionChangedStorageError,
                "The Sandbox seller grant changed during the scope upgrade",
            ):
                database.replace_refresh_token_for_scope_upgrade(
                    "new-refresh-token",
                    9999999999,
                    "seller",
                    ["scope:identity"],
                    "expected-generation",
                    "seller",
                )
            with self.assertRaisesRegex(
                ConnectionChangedStorageError,
                "The Sandbox seller connection changed before it was verified",
            ):
                database.mark_connection_verified("generation", "seller")
            with self.assertRaisesRegex(
                ConnectionChangedStorageError,
                "The Sandbox seller connection changed during review",
            ):
                database.create_pending_action(
                    "session",
                    "create_draft",
                    {"sku": "SKU-ONE"},
                    expected_generation="generation",
                )
            database.save_refresh_token(
                "refresh-token", 9999999999, "seller", new_grant=True
            )
            generation = database.connection_generation()
            database.mark_connection_verified(generation, "seller")
            database.create_pending_action(
                "session",
                "create_draft",
                {"sku": "SKU-TWO"},
                expected_generation=generation,
                expected_user_id="seller",
                resource_key="SKU-TWO",
            )
            with self.assertRaisesRegex(
                StorageError,
                "This Sandbox seller resource already has an active draft reservation",
            ):
                database.create_pending_action(
                    "other-session",
                    "create_draft",
                    {"sku": "SKU-TWO"},
                    expected_generation=generation,
                    expected_user_id="seller",
                    resource_key="SKU-TWO",
                )


if __name__ == "__main__":
    unittest.main()
