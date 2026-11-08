from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ebay_app.ebay_client import EbayError, EbaySandboxClient
from ebay_app.engine import Engine, LiveListing
from ebay_app.engine_pricing import market_note, summarize_market
from ebay_app.storage import AppDatabase

from helpers import FakeTransport, json_response, make_config

D = Decimal


class MarketMathTests(unittest.TestCase):
    def test_summary(self):
        m = summarize_market([D("30"), D("10"), D("20"), D("40")])
        self.assertEqual((m.count, m.low, m.median, m.high), (4, D("10"), D("25.00"), D("40")))
        self.assertEqual(summarize_market([]).count, 0)

    def test_far_above_median_must_queue_otherwise_informational(self):
        m = summarize_market([D("40"), D("44"), D("48")])
        note, must = market_note(D("60.00"), m)
        self.assertTrue(must)
        self.assertIn("above the market median", note)
        self.assertEqual(market_note(D("50.00"), m), (None, False))
        note, must = market_note(D("30.00"), m)
        self.assertFalse(must)
        self.assertIn("below the cheapest", note)

    def test_too_few_comparables_never_blocks(self):
        note, must = market_note(D("999"), summarize_market([D("10")]))
        self.assertFalse(must)
        self.assertIn("Only 1", note)


class ComparableSearchClientTests(unittest.TestCase):
    def test_uses_app_token_filters_currency_and_excludes_own_listing(self):
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            db = AppDatabase(config.database_path, config.data_encryption_key)
            self.addCleanup(db.close)
            transport = FakeTransport([
                json_response(200, {"access_token": "app-token", "expires_in": 7200}),
                json_response(200, {"itemSummaries": [
                    {"legacyItemId": "111", "price": {"value": "40.00", "currency": config.currency}},
                    {"legacyItemId": "OWN", "price": {"value": "1.00", "currency": config.currency}},
                    {"legacyItemId": "222", "price": {"value": "45.50", "currency": "USD"}},
                    {"legacyItemId": "333", "price": {"value": "bad", "currency": config.currency}},
                ]}),
            ])
            client = EbaySandboxClient(config, db, transport=transport)
            prices = client.search_comparable_listings("  Gold   ring  ", exclude_listing_id="OWN")
            self.assertEqual(prices, [D("40.00")])
            call = transport.calls[1]
            self.assertEqual(call["method"], "GET")
            self.assertIn("/buy/browse/v1/item_summary/search", call["url"])
            query = parse_qs(urlsplit(call["url"]).query)
            self.assertEqual(query["q"], ["Gold ring"])
            self.assertIn("FIXED_PRICE", query["filter"][0])
            self.assertEqual(call["headers"].get("Authorization"), "Bearer app-token")


class EngineMarketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(make_config(Path(self.temp.name)), engine_mode="auto-dry",
                              feature_stage="prices", engine_market_check=True,
                              max_price_change_percent=D("50"))
        self.db = AppDatabase(self.config.database_path, self.config.data_encryption_key)
        self.addCleanup(self.db.close)
        self.db.set_sku_cost("A", item_cost="40.00", postage_cost="0.00", fee_percent="0.00",
                             currency="GBP", margin_percent="50")  # target 60.00
        self.db.set_price_limit("A", "10.00", "GBP")
        self.listing = LiveListing("A", "50.00", "GBP", 1, "Gold ring", "OWN")

    def engine(self, market_reader, **overrides):
        return Engine(replace(self.config, **overrides), self.db, lambda: [self.listing],
                      now=lambda: 1_800_000_000.0, market_reader=market_reader)

    def test_target_far_above_market_is_queued_not_auto(self):
        rows = self.engine(lambda l: [D("40"), D("42"), D("44")]).run_reprice()
        self.assertEqual(rows[0]["market_median"], "42.00")
        self.assertEqual(rows[0]["status"], "proposed_queue")
        self.assertIn("above the market median", self.db.list_engine_proposals()[0]["reason"])

    def test_in_line_with_market_stays_auto(self):
        rows = self.engine(lambda l: [D("55"), D("60"), D("62")]).run_reprice()
        self.assertEqual(rows[0]["status"], "proposed_auto")

    def test_refused_access_is_reported_once_and_never_an_engine_error(self):
        calls = []

        def refused(listing):
            calls.append(listing.sku)
            raise EbayError("forbidden", status=403, code="EBAY_ERROR")

        engine = self.engine(refused)
        rows = engine.run_reprice()
        self.assertEqual(rows[0]["status"], "proposed_auto")
        report = json.loads(self.db.get_engine_state("last_reprice_report"))
        self.assertEqual(report["market_check"], "unavailable (EBAY_ERROR)")
        self.assertEqual(len(calls), 1)
        self.assertIsNone(engine.paused_reason())

    def test_off_by_default_makes_no_calls(self):
        calls = []
        self.engine(lambda l: calls.append(1) or [], engine_market_check=False).run_reprice()
        self.assertEqual(calls, [])
