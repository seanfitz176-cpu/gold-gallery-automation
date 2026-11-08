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
