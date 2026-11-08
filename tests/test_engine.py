from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from ebay_app.engine import Engine, EngineThread, LiveListing, describe
from ebay_app.storage import AppDatabase

from helpers import make_config


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(make_config(Path(self.temp.name)), engine_mode="shadow",
                              max_price_change_percent=Decimal("10"), require_price_floor=True)
        self.clock = Clock()
        self.database = AppDatabase(self.config.database_path, self.config.data_encryption_key,
                                    now=self.clock)
        self.addCleanup(self.database.close)
        self.listings = [LiveListing("A", "40.00", "GBP"), LiveListing("B", "50.00", "GBP"),
                         LiveListing("C", "10.00", "GBP")]
        self.reads = 0

    def reader(self):
        self.reads += 1
        return list(self.listings)

    def engine(self, **overrides) -> Engine:
        return Engine(replace(self.config, **overrides), self.database, self.reader, now=self.clock)

    def cost(self, sku, item="40.00", postage="3.20", margin=None, floor="10.00"):
        self.database.set_sku_cost(sku, item_cost=item, postage_cost=postage, fee_percent="0.00",
                                   currency="GBP", margin_percent=margin)
        if floor:
            self.database.set_price_limit(sku, floor, "GBP")


class EngineModeTests(EngineTestCase):
    def test_off_does_nothing_at_all(self):
        self.engine(engine_mode="off").tick()
        self.assertEqual(self.reads, 0)
        self.assertIsNone(self.database.get_engine_state("last_reprice_report"))

    def test_thread_never_starts_when_off(self):
        thread = EngineThread(self.engine(engine_mode="off"), interval_seconds=0.01)
        thread.start()
        self.assertIsNone(thread._thread)

    def test_pause_stops_jobs_and_survives_a_new_engine_instance(self):
        self.engine().pause("testing")
        fresh = self.engine()
        fresh.tick()
        self.assertEqual(self.reads, 0)
        self.assertEqual(fresh.paused_reason(), "testing")
        fresh.resume()
        fresh.tick()
        self.assertEqual(self.reads, 1)

    def test_three_consecutive_errors_pause_automatically(self):
        def broken():
            raise RuntimeError("eBay down")

        engine = Engine(self.config, self.database, broken, now=self.clock)
        for _ in range(3):
            engine.tick()
        self.assertIn("3 consecutive", engine.paused_reason())

    def test_reprice_runs_on_schedule_or_when_requested(self):
        engine = self.engine()
        engine.tick()
        engine.tick()
        self.assertEqual(self.reads, 1)  # not due again yet
        engine.request_reprice()
        engine.tick()
        self.assertEqual(self.reads, 2)
        self.clock.t += 24 * 3600
        engine.tick()
        self.assertEqual(self.reads, 3)


class EngineRepriceTests(EngineTestCase):
    def test_shadow_report_covers_every_listing_and_queues_proposals_only(self):
        self.cost("A", margin="30")          # 43.20 cost -> target 56.16, live 40 -> step 44.00
        self.cost("B")                       # no margin -> implied margin reported only
        rows = {r["sku"]: r for r in self.engine().run_reprice()}
        self.assertEqual(rows["A"]["status"], "proposed_queue")
        self.assertEqual(rows["A"]["proposed"], "44.00")
        self.assertEqual(rows["B"]["status"], "no_margin")
        self.assertEqual(rows["B"]["implied_margin"], "15.74")
        self.assertEqual(rows["C"]["status"], "no_cost")
        pending = self.database.list_engine_proposals()
        self.assertEqual([(p["sku"], p["proposed_value"], p["decision"]) for p in pending],
                         [("A", "44.00", "queue")])
        report = json.loads(self.database.get_engine_state("last_reprice_report"))
        self.assertEqual((report["listings"], report["proposals"]), (3, 1))

    def test_global_default_margin_applies_only_where_no_per_sku_margin(self):
        self.cost("A", margin="30")
        self.cost("B")
        rows = {r["sku"]: r for r in self.engine(engine_default_margin_percent=Decimal("20")).run_reprice()}
        self.assertEqual(rows["A"]["margin_used"], "30")
        self.assertEqual(rows["B"]["margin_used"], "20")

    def test_auto_mode_routes_only_in_limit_proposals_to_auto(self):
        self.cost("A", margin="30")               # has floor -> auto
        self.cost("B", margin="30", floor=None)   # no floor -> queue
        self.engine(engine_mode="auto-dry").run_reprice()
        decisions = {p["sku"]: p["decision"] for p in self.database.list_engine_proposals()}
        self.assertEqual(decisions, {"A": "auto", "B": "queue"})

    def test_a_new_run_supersedes_the_old_pending_proposal(self):
        self.cost("A", margin="30")
        engine = self.engine()
        engine.run_reprice()
        self.listings[0] = LiveListing("A", "44.00", "GBP")
        engine.run_reprice()
        pending = self.database.list_engine_proposals()
        self.assertEqual([p["proposed_value"] for p in pending], ["48.40"])
        self.assertEqual(len(self.database.list_engine_proposals(("superseded",))), 1)

    def test_currency_mismatch_and_unreadable_prices_are_skipped(self):
        self.cost("A", margin="30")
        self.listings = [LiveListing("A", "40.00", "EUR"), LiveListing("Z", "abc", "GBP")]
        rows = self.engine().run_reprice()
        self.assertEqual([r["status"] for r in rows], ["skipped", "skipped"])
        self.assertEqual(self.database.list_engine_proposals(), ())


class ProposalStorageTests(EngineTestCase):
    def test_compare_and_set_status_prevents_double_action(self):
        pid = self.database.record_engine_proposal(
            sku="A", kind="price", current_value="1", proposed_value="2", currency="GBP",
            reason="r", decision="queue", ttl_seconds=3600)
        self.assertTrue(self.database.set_engine_proposal_status(pid, "approved"))
        self.assertFalse(self.database.set_engine_proposal_status(pid, "approved"))

    def test_expired_proposals_leave_the_pending_list(self):
        self.database.record_engine_proposal(
            sku="A", kind="price", current_value="1", proposed_value="2", currency="GBP",
            reason="r", decision="queue", ttl_seconds=0)
        self.assertEqual(self.database.expire_engine_proposals(), 1)
        self.assertEqual(self.database.list_engine_proposals(), ())


class DescribeTests(unittest.TestCase):
    def test_headlines_are_honest_about_execution(self):
        self.assertEqual(describe({"mode": "off"})[0], "Engine off")
        self.assertEqual(describe({"mode": "shadow", "paused_reason": "x"})[0], "Engine PAUSED")
        self.assertIn("proposals only", describe({"mode": "auto", "executes": False})[0])


class EngineWebTests(unittest.TestCase):
    def setUp(self):
        from test_connection_phase import MigrationStageWebTests
        from ebay_app.web import WebApplication

        self.helper = MigrationStageWebTests("test_dashboard_and_connect_are_reachable_in_the_migration_stage")
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.config = replace(self.helper.config, feature_stage="listings", engine_mode="shadow")
        self.engine = Engine(self.config, self.helper.database, lambda: [], now=Clock())
        self.helper.config = self.config
        self.helper.app = WebApplication(self.config, self.helper.database, self.helper.ebay,
                                         connection_verifier=self.helper.verifier, engine=self.engine)

    def test_engine_page_pause_resume_and_dashboard_link(self):
        cookie = self.helper.login()
        status, _h, dashboard = self.helper.request("GET", "/", cookie=cookie)
        self.assertIn('href="/engine"', dashboard)
        status, _h, page = self.helper.request("GET", "/engine", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("proposals only", page)
        csrf = self.helper.hidden(page, "csrf_token")
        status, _h, _b = self.helper.request("POST", "/engine/pause", cookie=cookie, form={"csrf_token": csrf})
        self.assertEqual(status, 303)
        self.assertEqual(self.engine.paused_reason(), "Paused from the dashboard")
        self.helper.request("POST", "/engine/resume", cookie=cookie, form={"csrf_token": csrf})
        self.assertIsNone(self.engine.paused_reason())
        status, _h, _b = self.helper.request("POST", "/engine/pause", cookie=cookie, form={"csrf_token": "wrong"})
        self.assertNotEqual(status, 303)
        self.assertIsNone(self.engine.paused_reason())

    def test_reject_marks_a_proposal_without_any_change(self):
        pid = self.helper.database.record_engine_proposal(
            sku="A", kind="price", current_value="40.00", proposed_value="44.00", currency="GBP",
            reason="test", decision="queue", ttl_seconds=3600)
        cookie = self.helper.login()
        _s, _h, page = self.helper.request("GET", "/engine", cookie=cookie)
        csrf = self.helper.hidden(page, "csrf_token")
        status, _h, _b = self.helper.request("POST", "/engine/reject", cookie=cookie,
                                             form={"csrf_token": csrf, "proposal_id": str(pid)})
        self.assertEqual(status, 303)
        self.assertEqual(self.helper.database.get_engine_proposal(pid)["status"], "rejected")
        status, _h, _b = self.helper.request("POST", "/engine/approve", cookie=cookie,
                                             form={"csrf_token": csrf, "proposal_id": "abc"})
        self.assertEqual(status, 404)

    def test_engine_routes_are_locked_when_the_engine_is_off(self):
        from ebay_app.web import WebApplication

        config = replace(self.config, engine_mode="off")
        self.helper.app = WebApplication(config, self.helper.database, self.helper.ebay,
                                         connection_verifier=self.helper.verifier, engine=None)
        cookie = self.helper.login()
        status, _h, _b = self.helper.request("GET", "/engine", cookie=cookie)
        self.assertEqual(status, 404)
        _s, _h, dashboard = self.helper.request("GET", "/", cookie=cookie)
        self.assertNotIn('href="/engine"', dashboard)


if __name__ == "__main__":
    unittest.main()


class FakeEbayForExecution:
    """Just enough of EbaySandboxClient for PriceService + the engine."""

    def __init__(self, price="40.00", offers=None, change_error=None):
        self.price = price
        self.offers = offers
        self.change_error = change_error
        self.changes = []

    def list_offers(self, sku):
        if self.offers is not None:
            return self.offers
        return [{"offerId": "O1", "sku": sku, "status": "PUBLISHED"}]

    def get_offer(self, offer_id):
        return {"offerId": offer_id, "sku": "A", "status": "PUBLISHED",
                "pricingSummary": {"price": {"value": self.price, "currency": "GBP"}}}

    @staticmethod
    def offer_price(offer):
        price = offer.get("pricingSummary", {}).get("price", {})
        return {"value": str(price.get("value", "")), "currency": str(price.get("currency", ""))}

    def price_mutation_guard(self):
        import contextlib
        return contextlib.nullcontext()

    def change_price(self, **kwargs):
        if self.change_error is not None:
            raise self.change_error
        self.changes.append(kwargs)
        self.price = kwargs["new_price"]
        return {"offer_id": kwargs["offer_id"], "sku": kwargs["sku"],
                "price": kwargs["new_price"], "currency": kwargs["currency"]}


class EngineExecutionTests(EngineTestCase):
    def setUp(self):
        super().setUp()
        from ebay_app.config import DRAFT_SCOPES

        self.config = replace(self.config, feature_stage="prices", scopes=DRAFT_SCOPES,
                              runtime_mode="local", dry_run=False, max_actions_per_run=10)
        self.database.save_refresh_token("r", 9_999_999_999, "user", new_grant=True, granted_scopes=DRAFT_SCOPES)
        self.database.mark_connection_verified(self.database.connection_generation(), "user")
        self.listings = [LiveListing("A", "40.00", "GBP")]
        self.cost("A", margin="30")  # proposes 40.00 -> 44.00

    def make(self, ebay=None, **overrides):
        from ebay_app.prices import PriceService

        config = replace(self.config, **overrides)
        ebay = ebay or FakeEbayForExecution()
        engine = Engine(config, self.database, self.reader, now=self.clock,
                        ebay=ebay, price_service=PriceService(ebay, self.database, config))
        return engine, ebay

    def proposal(self):
        return self.database.list_engine_proposals(
            ("pending", "executed", "failed", "unknown", "superseded", "approved"))[0]

    def test_shadow_never_executes_automatically_but_approval_does(self):
        engine, ebay = self.make()
        engine.tick()
        self.assertEqual(ebay.changes, [])
        self.assertEqual(engine.execute_proposal(self.proposal()["proposal_id"]), "executed")
        self.assertEqual(ebay.changes[0]["new_price"], "44.00")
        record = self.proposal()
        self.assertEqual(record["status"], "executed")
        self.assertIsNotNone(record["action_id"])
        self.assertEqual(engine.execute_proposal(record["proposal_id"]), "not_pending")  # never twice

    def test_auto_dry_runs_in_limit_proposals_as_dry_runs_only(self):
        engine, ebay = self.make(engine_mode="auto-dry")
        engine.tick()
        self.assertEqual(ebay.changes, [])
        record = self.proposal()
        self.assertEqual(record["status"], "executed")
        self.assertIn("DRY RUN", record["outcome"])

    def test_auto_executes_for_real_and_respects_app_dry_run(self):
        engine, ebay = self.make(engine_mode="auto")
        engine.tick()
        self.assertEqual(len(ebay.changes), 1)
        self.database.delete_engine_state("last_reprice_at")
        ebay2 = FakeEbayForExecution(price="44.00")
        self.listings = [LiveListing("A", "44.00", "GBP")]
        engine2, _ = self.make(ebay=ebay2, engine_mode="auto", dry_run=True)
        engine2.tick()
        self.assertEqual(ebay2.changes, [])
        self.assertIn("DRY RUN", self.proposal()["outcome"])

    def test_live_price_drift_supersedes_without_changing_anything(self):
        engine, ebay = self.make()
        engine.run_reprice()
        ebay.price = "41.00"
        self.assertEqual(engine.execute_proposal(self.proposal()["proposal_id"]), "superseded")
        self.assertEqual(ebay.changes, [])

    def test_ambiguous_offers_fail_cleanly(self):
        engine, ebay = self.make(ebay=FakeEbayForExecution(offers=[]))
        engine.run_reprice()
        self.assertEqual(engine.execute_proposal(self.proposal()["proposal_id"]), "failed")
        self.assertIn("exactly one published Offer", self.proposal()["outcome"])

    def test_unknown_outcome_pauses_the_engine(self):
        from ebay_app.ebay_client import EbayError

        engine, ebay = self.make(ebay=FakeEbayForExecution(
            change_error=EbayError("lost", code="EBAY_CONNECTION_ERROR", outcome_unknown=True)))
        engine.run_reprice()
        self.assertEqual(engine.execute_proposal(self.proposal()["proposal_id"]), "unknown")
        self.assertIn("unknown", engine.paused_reason())

    def test_budget_leaves_the_proposal_pending(self):
        engine, ebay = self.make(engine_mode="auto", max_actions_per_run=1)
        engine.price_service._actions_this_run = 1
        engine.tick()
        record = self.proposal()
        self.assertEqual(record["status"], "pending")
        self.assertIn("budget", record["outcome"])
        self.assertEqual(ebay.changes, [])

    def test_floor_and_cap_still_enforced_by_price_service(self):
        engine, ebay = self.make()
        self.database.delete_price_limit("A")  # floors required -> PriceService refuses
        engine.run_reprice()
        self.assertEqual(engine.execute_proposal(self.proposal()["proposal_id"]), "failed")
        self.assertIn("PRICE_FLOOR_REQUIRED", self.proposal()["outcome"])
        self.assertEqual(ebay.changes, [])
