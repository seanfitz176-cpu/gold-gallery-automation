from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from ebay_app.config import (
    CANONICAL_CALLBACK_PATH,
    CANONICAL_CALLBACK_URL,
    CANONICAL_HOST,
    CANONICAL_PORT,
    ConfigError,
    load_config,
)
from run import main


def canonical_env(**overrides: str) -> str:
    values = {
        "EBAY_ENVIRONMENT": "sandbox",
        "APP_FEATURE_STAGE": "connection",
        "EBAY_CLIENT_ID": "Example-Application-SBX-placeholder",
        "EBAY_CLIENT_SECRET": "SBX-placeholder-cert-id",
        "EBAY_RUNAME": "Example-Application-Runame",
        "APP_ADMIN_PASSWORD": "this-is-a-long-local-password",
        "APP_DATA_ENCRYPTION_KEY": "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg=",
        "HOST": CANONICAL_HOST,
        "PORT": str(CANONICAL_PORT),
    }
    values.update(overrides)
    return "".join(f"{name}={value}\n" for name, value in values.items())


class PermanentOAuthConfigurationTests(unittest.TestCase):
    def load(self, **overrides: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(canonical_env(**overrides), encoding="utf-8")
            os.chmod(path, 0o600)
            return load_config(path, {})

    def test_one_callback_tuple_is_fixed(self):
        config = self.load()
        self.assertEqual(CANONICAL_HOST, "127.0.0.1")
        self.assertEqual(CANONICAL_PORT, 5000)
        self.assertEqual(CANONICAL_CALLBACK_PATH, "/oauth/callback")
        self.assertEqual(
            CANONICAL_CALLBACK_URL,
            "http://127.0.0.1:5000/oauth/callback",
        )
        self.assertEqual(config.host, CANONICAL_HOST)
        self.assertEqual(config.port, CANONICAL_PORT)
        self.assertEqual(config.allowed_hosts, (CANONICAL_HOST,))
        self.assertEqual(config.runtime_mode, "local")
        self.assertFalse(config.cookie_secure)

    def test_alternate_hosts_and_ports_fail_at_startup(self):
        for host in ("localhost", "0.0.0.0", "::1", "example.test"):
            with self.subTest(host=host):
                with self.assertRaisesRegex(ConfigError, "HOST must remain"):
                    self.load(HOST=host)
        for port in ("3000", "4999", "5002"):
            with self.subTest(port=port):
                with self.assertRaisesRegex(ConfigError, "PORT must remain"):
                    self.load(PORT=port)

    def test_second_account_port_requires_an_account_label(self):
        with self.assertRaisesRegex(ConfigError, "APP_ACCOUNT_LABEL"):
            self.load(PORT="5001")
        config = self.load(PORT="5001", APP_ACCOUNT_LABEL="Second shop")
        self.assertEqual((config.port, config.account_label), (5001, "Second shop"))
        with self.assertRaisesRegex(ConfigError, "APP_ACCOUNT_LABEL"):
            self.load(PORT="5001", APP_ACCOUNT_LABEL="<script>")

    def test_deprecated_tunnel_runtime_settings_are_rejected(self):
        for name in (
            "APP_ALLOWED_HOSTS",
            "APP_COOKIE_SECURE",
            "OAUTH_TUNNEL_HOST",
            "OAUTH_RELAY",
        ):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ConfigError, "obsolete OAuth runtime"):
                    self.load(**{name: "obsolete"})

    def test_runame_must_be_an_opaque_identifier(self):
        for runame in (
            "https://example.test/oauth/callback",
            "/oauth/callback",
            "name?query=value",
            "name with spaces",
        ):
            with self.subTest(runame=runame):
                with self.assertRaisesRegex(ConfigError, "EBAY_RUNAME|opaque Sandbox RuName"):
                    self.load(EBAY_RUNAME=runame)

    def test_project_env_overrides_stale_inherited_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(canonical_env(), encoding="utf-8")
            os.chmod(path, 0o600)
            inherited = {
                "EBAY_CLIENT_ID": "stale-production-value",
                "EBAY_CLIENT_SECRET": "stale-production-value",
                "EBAY_RUNAME": "stale-runame",
                "HOST": "localhost",
                "PORT": "3000",
            }
            config = load_config(path, inherited)
            self.assertEqual(config.client_id, "Example-Application-SBX-placeholder")
            self.assertEqual(config.client_secret, "SBX-placeholder-cert-id")
            self.assertEqual(config.runame, "Example-Application-Runame")
            self.assertEqual(config.host, CANONICAL_HOST)
            self.assertEqual(config.port, CANONICAL_PORT)

    def test_legacy_startup_flags_are_not_supported(self):
        self.assertEqual(main(["--oauth-relay"]), 1)
        self.assertEqual(main(["--oauth-tunnel-host", "obsolete.example"]), 1)


if __name__ == "__main__":
    unittest.main()
