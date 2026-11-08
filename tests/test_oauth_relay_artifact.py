from __future__ import annotations

import json
import unittest
from pathlib import Path


class OAuthRelayArtifactTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / "oauth_relay_worker"
        self.source = (self.root / "src" / "index.js").read_text(encoding="utf-8")
        self.config = json.loads(
            (self.root / "wrangler.jsonc").read_text(encoding="utf-8")
        )

    def test_worker_has_one_fixed_loopback_destination_and_no_outbound_fetch(self):
        self.assertIn(
            'const LOCAL_CALLBACK = "http://127.0.0.1:5000/oauth/callback";',
            self.source,
        )
        self.assertNotIn("await fetch(", self.source)
        self.assertNotIn("Response.redirect", self.source)
        self.assertNotIn("console.", self.source)
        for forbidden in ("trycloudflare.com", "SBX-", "PRD-", "/Users/"):
            self.assertNotIn(forbidden, self.source)

    def test_worker_deployment_disables_preview_and_persisted_observability(self):
        self.assertEqual(self.config["name"], "ebay-sandbox-oauth-relay")
        self.assertTrue(self.config["workers_dev"])
        self.assertFalse(self.config["preview_urls"])
        self.assertFalse(self.config["observability"]["enabled"])
        self.assertFalse(
            self.config["observability"]["logs"]["invocation_logs"]
        )
        self.assertFalse(self.config["observability"]["traces"]["enabled"])
        self.assertNotIn("vars", self.config)


if __name__ == "__main__":
    unittest.main()
