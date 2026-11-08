"""Tests for the ``publish`` stage: config gate, client publish + readback,
the bulk publish service (plan, dry run, budget, outcome classification),
and the dashboard/route wiring. All HTTP is mocked."""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ebay_app.bulk_publish import BulkPublishService, PlannedOffer, load_skus, summarize
from ebay_app.config import (
    DRAFT_SCOPES,
    PRODUCTION_ENV_FILENAME,
    PRODUCTION_WRITE_OPT_INS,
    ConfigError,
    load_config,
    scopes_for_stage,
)
from ebay_app.ebay_client import EbayError, EbaySandboxClient
from ebay_app.storage import AppDatabase

from helpers import FakeTransport, adopt_test_access_token, json_response, make_config
from test_config_validation import valid_production_env_text

LISTING_ID = "110551234567"


def offer(offer_id: str, sku: str, status: str = "UNPUBLISHED", listing_id: str | None = None) -> dict:
    body = {"offerId": offer_id, "sku": sku, "status": status}
    if listing_id:
        body["listing"] = {"listingId": listing_id}
    return body


def offers_page(*items: dict) -> dict:
    return {"offers": list(items), "total": len(items)}


class PublishConfigTests(unittest.TestCase):
    def write(self, directory: str, extra: str) -> Path:
        path = Path(directory) / PRODUCTION_ENV_FILENAME
        text = valid_production_env_text().replace("APP_FEATURE_STAGE=listings", extra)
        path.write_text(text, encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def test_production_publish_needs_its_own_exact_opt_in_and_defaults_to_dry_run(self):
        env_var, value = PRODUCTION_WRITE_OPT_INS["publish"]
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ConfigError, env_var):
                load_config(self.write(d, "APP_FEATURE_STAGE=publish"), {})
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ConfigError, env_var):
                load_config(self.write(d, f"APP_FEATURE_STAGE=publish\n{env_var}=yes"), {})
        with tempfile.TemporaryDirectory() as d:
            config = load_config(self.write(d, f"APP_FEATURE_STAGE=publish\n{env_var}={value}"), {})
            self.assertEqual(config.feature_stage, "publish")
            self.assertTrue(config.dry_run)
            self.assertEqual(config.scopes, DRAFT_SCOPES)

    def test_other_stage_opt_ins_do_not_unlock_publish(self):
        for stage in ("prices", "quantities", "images", "migration"):
            env_var, value = PRODUCTION_WRITE_OPT_INS[stage]
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as d:
                with self.assertRaises(ConfigError):
                    load_config(self.write(d, f"APP_FEATURE_STAGE=publish\n{env_var}={value}"), {})

    def test_publish_uses_the_draft_scope(self):
        self.assertEqual(scopes_for_stage("publish"), DRAFT_SCOPES)


class PublishTestCase(unittest.TestCase):
    stage = "publish"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(
            make_config(Path(self.temp.name)),
            feature_stage=self.stage,
            scopes=DRAFT_SCOPES,
            runtime_mode="local",
            dry_run=False,
            max_actions_per_run=10,
        )
        self.database = AppDatabase(self.config.database_path, self.config.data_encryption_key)
        self.addCleanup(self.database.close)
        self.database.save_refresh_token(
            "stored-refresh", 9_999_999_999, "internal-sandbox-user",
            new_grant=True, granted_scopes=DRAFT_SCOPES,
        )
        self.database.mark_connection_verified(
            self.database.connection_generation(), "internal-sandbox-user"
        )

    def client(self, responses, config=None) -> EbaySandboxClient:
        client = EbaySandboxClient(config or self.config, self.database, transport=FakeTransport(responses))
        adopt_test_access_token(client)
        return client

    def binding(self):
        return self.database.verified_connection_binding()


class PublishClientTests(PublishTestCase):
    def test_publish_rechecks_posts_with_no_body_and_verifies_readback(self):
        client = self.client([
            json_response(200, offer("O1", "SKU-1")),
            json_response(200, {"listingId": LISTING_ID, "warnings": []}),
            json_response(200, offer("O1", "SKU-1", "PUBLISHED", LISTING_ID)),
        ])
        with client.publish_mutation_guard():
            result = client.publish_offer(self.binding(), offer_id="O1", sku="SKU-1")
        self.assertEqual(result["listing_id"], LISTING_ID)
        calls = client.transport.calls
        self.assertEqual([c["method"] for c in calls], ["GET", "POST", "GET"])
        self.assertTrue(calls[1]["url"].endswith("/sell/inventory/v1/offer/O1/publish"))
        self.assertIn(calls[1]["body"], (None, b""))

    def test_mismatched_or_already_published_offer_is_never_posted(self):
        for body, code in (
            (offer("O1", "OTHER-SKU"), "PUBLISH_OFFER_MISMATCH"),
            (offer("O1", "SKU-1", "PUBLISHED", LISTING_ID), "OFFER_ALREADY_PUBLISHED"),
        ):
            with self.subTest(code=code):
                client = self.client([json_response(200, body)])
                with self.assertRaises(EbayError) as raised:
                    client.publish_offer(self.binding(), offer_id="O1", sku="SKU-1")
                self.assertEqual(raised.exception.code, code)
                self.assertFalse(raised.exception.outcome_unknown)
                self.assertEqual(len(client.transport.calls), 1)

    def test_clean_rejection_is_not_unknown_and_keeps_ebay_detail(self):
        client = self.client([
            json_response(200, offer("O1", "SKU-1")),
            json_response(400, {"errors": [{"errorId": 25002, "message": "Item specific Brand is missing"}]}),
        ])
        with self.assertRaises(EbayError) as raised:
            client.publish_offer(self.binding(), offer_id="O1", sku="SKU-1")
        self.assertFalse(raised.exception.outcome_unknown)
        self.assertIn("Brand", str(raised.exception.details))

    def test_lost_or_unverifiable_responses_are_unknown(self):
        cases = {
            "no listing id": [
                json_response(200, offer("O1", "SKU-1")),
                json_response(200, {}),
            ],
            "server error": [
                json_response(200, offer("O1", "SKU-1")),
                json_response(500, {}),
            ],
            "readback still unpublished": [
                json_response(200, offer("O1", "SKU-1")),
                json_response(200, {"listingId": LISTING_ID}),
                json_response(200, offer("O1", "SKU-1")),
            ],
            "readback fails": [
                json_response(200, offer("O1", "SKU-1")),
                json_response(200, {"listingId": LISTING_ID}),
                json_response(500, {}),
            ],
        }
        for name, responses in cases.items():
            with self.subTest(case=name):
                client = self.client(responses)
                with self.assertRaises(EbayError) as raised:
                    client.publish_offer(self.binding(), offer_id="O1", sku="SKU-1")
                self.assertTrue(raised.exception.outcome_unknown, name)

    def test_publish_is_locked_outside_the_publish_stage(self):
        for stage in ("drafts", "prices", "quantities", "images", "migration"):
            with self.subTest(stage=stage):
                config = replace(self.config, feature_stage=stage)
                client = self.client([json_response(200, offer("O1", "SKU-1"))], config=config)
                with self.assertRaises(EbayError) as guard:
                    with client.publish_mutation_guard():
                        pass
                self.assertEqual(guard.exception.code, "MUTATION_LOCKED")
                with self.assertRaises(EbayError) as call:
                    client.publish_offer(self.binding(), offer_id="O1", sku="SKU-1")
                self.assertEqual(call.exception.code, "MUTATION_LOCKED")
                # Only the read-only pre-check GET was made; the POST was refused locally.
                self.assertEqual([c["method"] for c in client.transport.calls], ["GET"])

    def test_allowlist_permits_only_the_exact_publish_path(self):
        client = self.client([])
        client._assert_mutation_allowed("POST", "/sell/inventory/v1/offer/O1/publish")
        for method, path in (
            ("POST", "/sell/inventory/v1/offer/O1/withdraw"),
            ("POST", "/sell/inventory/v1/offer/a/b/publish"),
            ("DELETE", "/sell/inventory/v1/offer/O1"),
            ("POST", "/sell/inventory/v1/offer"),
            ("POST", "/sell/inventory/v1/bulk_publish_offer"),
        ):
            with self.subTest(path=path), self.assertRaises(EbayError):
                client._assert_mutation_allowed(method, path)


class BulkPublishServiceTests(PublishTestCase):
    def test_plan_is_read_only_and_classifies_each_sku(self):
        client = self.client([
            json_response(200, offers_page(offer("O1", "A"))),
            json_response(200, offers_page(offer("O2", "B", "PUBLISHED", LISTING_ID))),
            json_response(404, {"errors": []}),
            json_response(200, offers_page(offer("O3", "D"), offer("O4", "D"))),
        ])
        planned = BulkPublishService(client, self.database, self.config).plan(["A", "B", "C", "D"])
        self.assertEqual([p.status for p in planned], ["ready", "already_published", "no_offer", "ambiguous"])
        self.assertTrue(all(c["method"] == "GET" for c in client.transport.calls))

    def test_dry_run_reverifies_but_never_posts(self):
        config = replace(self.config, dry_run=True)
        client = self.client([json_response(200, offer("O1", "A"))], config=config)
        service = BulkPublishService(client, self.database, config)
        results = service.publish_batch(self.binding(), [PlannedOffer("A", "O1", "ready", "")])
        self.assertEqual(results[0].status, "dry_run")
        self.assertEqual([c["method"] for c in client.transport.calls], ["GET"])

    def test_budget_caps_attempts_and_skips_the_rest_untouched(self):
        config = replace(self.config, dry_run=True, max_actions_per_run=2)
        client = self.client([json_response(200, offer("O1", "A")), json_response(200, offer("O2", "B"))],
                             config=config)
        service = BulkPublishService(client, self.database, config)
        planned = [PlannedOffer(s, f"O{i}", "ready", "") for i, s in enumerate("ABC", start=1)]
        results = service.publish_batch(self.binding(), planned)
        self.assertEqual([r.status for r in results], ["dry_run", "dry_run", "skipped_budget"])
        self.assertEqual(len(client.transport.calls), 2)

    def test_outcomes_are_classified_and_one_failure_never_stops_the_batch(self):
        client = self.client([
            # A: published
            json_response(200, offer("O1", "A")),
            json_response(200, {"listingId": LISTING_ID}),
            json_response(200, offer("O1", "A", "PUBLISHED", LISTING_ID)),
            # B: clean rejection
            json_response(200, offer("O2", "B")),
            json_response(400, {"errors": [{"errorId": 25002, "message": "No picture"}]}),
            # C: lost response
            json_response(200, offer("O3", "C")),
            json_response(500, {}),
            # D: became published since planning
            json_response(200, offer("O4", "D", "PUBLISHED", "9")),
        ])
        service = BulkPublishService(client, self.database, self.config)
        planned = [PlannedOffer(s, f"O{i}", "ready", "") for i, s in enumerate("ABCD", start=1)]
        planned.append(PlannedOffer("E", None, "no_offer", "No Offer exists for this SKU."))
        results = service.publish_batch(self.binding(), planned)
        self.assertEqual([r.status for r in results],
                         ["published", "failed", "unknown", "already_published", "no_offer"])
        self.assertEqual(results[0].listing_id, LISTING_ID)
        self.assertIn("MAY be live", results[2].detail)
        self.assertEqual(dict(summarize(results))["published"], 1)

    def test_wrong_stage_or_unverified_grant_is_rejected_before_any_call(self):
        client = self.client([])
        with self.assertRaises(EbayError) as wrong:
            BulkPublishService(client, self.database, replace(self.config, feature_stage="images")).verified_binding()
        self.assertEqual(wrong.exception.code, "PUBLISH_STAGE_LOCKED")
        self.database.save_refresh_token(
            "new-refresh", 9_999_999_999, "internal-sandbox-user",
            new_grant=True, granted_scopes=DRAFT_SCOPES,
        )
        with self.assertRaises(EbayError) as unverified:
            BulkPublishService(client, self.database, self.config).verified_binding()
        self.assertEqual(unverified.exception.code, "CONNECTION_NOT_VERIFIED")
        self.assertEqual(client.transport.calls, [])

    def test_load_skus_reads_the_upload_csv_and_drops_blanks_and_duplicates(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "listings.csv"
            path.write_text("SKU,title\nA,x\n,y\nB,z\nA,again\n", encoding="utf-8")
            self.assertEqual(load_skus(path), ["A", "B"])
            path.write_text("title\nx\n", encoding="utf-8")
            with self.assertRaises(EbayError):
                load_skus(path)


class PublishStageWebTests(unittest.TestCase):
    """The publish stage must expose only sign-in/connect/verify, show its
    own boundary text, and offer the permission upgrade -- the exact set of
    places the migration stage originally got wrong."""

    def setUp(self):
        from test_connection_phase import MigrationStageWebTests

        self.helper = MigrationStageWebTests("test_dashboard_and_connect_are_reachable_in_the_migration_stage")
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        from ebay_app.web import WebApplication

        self.helper.config = replace(self.helper.config, feature_stage="publish")
        self.helper.app = WebApplication(
            self.helper.config, self.helper.database, self.helper.ebay,
            connection_verifier=self.helper.verifier,
        )

    def test_dashboard_shows_the_publish_boundary_and_other_routes_stay_locked(self):
        cookie = self.helper.login()
        status, _headers, dashboard = self.helper.request("GET", "/", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Publishing listings", dashboard)
        self.assertIn("bulk_publish_listings.py", dashboard)
        self.assertNotIn("Phase 1 boundary", dashboard)
        csrf = self.helper.hidden(dashboard, "csrf_token")
        status, _headers, body = self.helper.request("POST", "/connect", cookie=cookie, form={"csrf_token": csrf})
        self.assertEqual(status, 200)
        self.assertIn("publish existing unpublished offers", body)
        for path in ("/listings", "/orders", "/account", "/drafts/new"):
            with self.subTest(path=path):
                status, _headers, _body = self.helper.request("GET", path, cookie=cookie)
                self.assertEqual(status, 404)


class PublishCapabilityDescriptionTests(unittest.TestCase):
    def test_startup_line_reports_dry_run_or_live_for_publish(self):
        import run_production

        config = replace(make_config(Path(tempfile.gettempdir())), feature_stage="publish", dry_run=True)
        self.assertIn("PUBLISH CHANGES ENABLED -- DRY RUN", run_production._capability_description(config))
        config = replace(config, dry_run=False)
        self.assertIn("LIVE WRITES", run_production._capability_description(config))


if __name__ == "__main__":
    unittest.main()
