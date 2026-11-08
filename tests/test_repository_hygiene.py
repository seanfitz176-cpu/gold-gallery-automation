from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RepositoryHygieneTests(unittest.TestCase):
    def test_private_and_generated_files_are_ignored(self):
        entries = {
            line.strip()
            for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        for required in (
            ".env",
            ".env.*",
            "!.env.example",
            "!.env.production.example",
            "instance/",
            ".venv/",
            "__pycache__/",
            "*.sqlite",
            "*.log",
            "*.key",
            "*.pem",
        ):
            with self.subTest(required=required):
                self.assertIn(required, entries)

    def test_env_example_has_the_complete_safe_key_set(self):
        values = {}
        for raw_line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            key, value = line.split("=", 1)
            self.assertNotIn(key, values)
            values[key] = value

        self.assertEqual(
            set(values),
            {
                "EBAY_ENVIRONMENT",
                "APP_FEATURE_STAGE",
                "EBAY_CLIENT_ID",
                "EBAY_CLIENT_SECRET",
                "EBAY_RUNAME",
                "APP_ADMIN_PASSWORD",
                "APP_DATA_ENCRYPTION_KEY",
                "APP_DATABASE_PATH",
                "EBAY_MARKETPLACE_ID",
                "EBAY_CONTENT_LANGUAGE",
                "EBAY_CURRENCY",
                "EBAY_MERCHANT_LOCATION_KEY",
                "EBAY_FULFILLMENT_POLICY_ID",
                "EBAY_PAYMENT_POLICY_ID",
                "EBAY_RETURN_POLICY_ID",
                "EBAY_REQUEST_TIMEOUT_SECONDS",
                "EBAY_ORDER_LOOKBACK_DAYS",
                "APP_MAX_PRICE_CHANGE_PERCENT",
                "APP_MAX_ACTIONS_PER_RUN",
                "APP_ENGINE_MODE",
                "APP_ENGINE_DEFAULT_MARGIN_PERCENT",
                "APP_ENGINE_REPRICE_HOURS",
                "APP_ENGINE_RELIST_MINUTES",
                "APP_ENGINE_ORDERS_MINUTES",
                "APP_REQUIRE_PRICE_FLOOR",
                "APP_DRY_RUN",
                "HOST",
                "PORT",
            },
        )
        for key in (
            "EBAY_CLIENT_ID",
            "EBAY_CLIENT_SECRET",
            "EBAY_RUNAME",
            "APP_ADMIN_PASSWORD",
            "APP_DATA_ENCRYPTION_KEY",
        ):
            self.assertTrue(values[key].startswith("replace-with-"))

        rendered = "\n".join(f"{key}={value}" for key, value in values.items())
        self.assertNotRegex(rendered, r"/Users/|TESTUSER_|trycloudflare\.com")
        self.assertNotRegex(rendered, r"(?:SBX|PRD)-[A-Za-z0-9]{8,}")

    def test_env_production_example_has_the_complete_safe_key_set(self):
        # Mirrors test_env_example_has_the_complete_safe_key_set above, for
        # the Production template added in the Phase 9 pre-production
        # hardening pass. This file must exist, must not be swallowed by the
        # ".env.*" gitignore pattern, and must default to the safest
        # possible Production configuration (read-only stage, no real
        # secrets, distinct database path from the Sandbox template).
        values = {}
        for raw_line in (ROOT / ".env.production.example").read_text(
            encoding="utf-8"
        ).splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            key, value = line.split("=", 1)
            self.assertNotIn(key, values)
            values[key] = value

        self.assertEqual(
            set(values),
            {
                "EBAY_ENVIRONMENT",
                "APP_ALLOW_PRODUCTION_ENVIRONMENT",
                "APP_FEATURE_STAGE",
                "EBAY_CLIENT_ID",
                "EBAY_CLIENT_SECRET",
                "EBAY_RUNAME",
                "APP_ADMIN_PASSWORD",
                "APP_DATA_ENCRYPTION_KEY",
                "APP_DATABASE_PATH",
                "EBAY_MARKETPLACE_ID",
                "EBAY_CONTENT_LANGUAGE",
                "EBAY_CURRENCY",
                "EBAY_MERCHANT_LOCATION_KEY",
                "EBAY_FULFILLMENT_POLICY_ID",
                "EBAY_PAYMENT_POLICY_ID",
                "EBAY_RETURN_POLICY_ID",
                "EBAY_REQUEST_TIMEOUT_SECONDS",
                "EBAY_ORDER_LOOKBACK_DAYS",
                "APP_MAX_PRICE_CHANGE_PERCENT",
                "APP_MAX_ACTIONS_PER_RUN",
                "APP_ENGINE_MODE",
                "APP_ENGINE_DEFAULT_MARGIN_PERCENT",
                "APP_ENGINE_REPRICE_HOURS",
                "APP_ENGINE_RELIST_MINUTES",
                "APP_ENGINE_ORDERS_MINUTES",
                "APP_REQUIRE_PRICE_FLOOR",
                "APP_DRY_RUN",
                "HOST",
                "PORT",
            },
        )
        self.assertEqual(values["EBAY_ENVIRONMENT"], "production")
        self.assertEqual(values["APP_ALLOW_PRODUCTION_ENVIRONMENT"], "yes-i-understand-the-risk")
        self.assertIn(
            values["APP_FEATURE_STAGE"], ("connection", "account", "listings", "orders")
        )
        # The example must never ship with the write-phase kill switch
        # already turned off.
        self.assertEqual(values["APP_DRY_RUN"], "true")
        self.assertNotEqual(values["APP_DATABASE_PATH"], "./instance/ebay-sandbox.sqlite")
        for key in (
            "EBAY_CLIENT_ID",
            "EBAY_CLIENT_SECRET",
            "EBAY_RUNAME",
            "APP_ADMIN_PASSWORD",
            "APP_DATA_ENCRYPTION_KEY",
        ):
            self.assertTrue(values[key].startswith("replace-with-"))

        rendered = "\n".join(f"{key}={value}" for key, value in values.items())
        self.assertNotRegex(rendered, r"/Users/|TESTUSER_|trycloudflare\.com")
        self.assertNotRegex(rendered, r"(?:SBX|PRD)-[A-Za-z0-9]{8,}")

    def test_project_documents_exist_and_have_no_absolute_home_paths(self):
        paths = (
            ROOT / "README.md",
            ROOT.parent / "PROJECT_STATUS.md",
            ROOT / ".github/copilot-instructions.md",
            ROOT / "docs/ROADMAP.md",
        )
        for path in paths:
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file())
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("/Users/", text)
                self.assertIsNone(
                    re.search(r"https://[^/\s]*trycloudflare\.com", text, re.IGNORECASE)
                )

    def test_active_python_artifacts_share_one_callback_configuration(self):
        active_paths = (
            ROOT / ".env.example",
            ROOT / "README.md",
            ROOT / "run.py",
            ROOT / "run_production.py",
            ROOT / "ebay_app/config.py",
            ROOT / "ebay_app/web.py",
            ROOT / "oauth_relay_worker/README.md",
            ROOT / "oauth_relay_worker/src/index.js",
        )
        rendered = "\n".join(
            path.read_text(encoding="utf-8") for path in active_paths
        )
        for forbidden in (
            "127.0.0.1:3000",
            "localhost:5000",
            "/auth/ebay/callback",
            "--oauth-relay",
            "--oauth-tunnel-host",
            "trycloudflare.com",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, rendered)
        self.assertIn("CANONICAL_HOST = \"127.0.0.1\"", rendered)
        self.assertIn("CANONICAL_PORT = 5000", rendered)
        self.assertIn('CANONICAL_CALLBACK_PATH = "/oauth/callback"', rendered)
        self.assertIn(
            "http://127.0.0.1:5000/oauth/callback",
            rendered,
        )


if __name__ == "__main__":
    unittest.main()
