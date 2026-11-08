from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from ebay_app.engine_pricing import (
    CostRowError,
    SkuCost,
    break_even_price,
    decide_reprice,
    implied_margin_percent,
    load_cost_csv,
    parse_cost_row,
    target_price,
)
from ebay_app.storage import AppDatabase

from helpers import make_config

D = Decimal


def cost(item="40.00", postage="3.20", fee="0", margin=None) -> SkuCost:
    return SkuCost("SKU-1", D(item), D(postage), D(fee), D(margin) if margin else None, "GBP")


class PricingArithmeticTests(unittest.TestCase):
    def test_private_seller_target_break_even_and_implied_margin(self):
        c = cost()  # total 43.20, no fees
        self.assertEqual(break_even_price(c), D("43.20"))
        self.assertEqual(target_price(c, D("30")), D("56.16"))
        self.assertEqual(implied_margin_percent(c, D("56.16")), D("30.00"))
        self.assertEqual(implied_margin_percent(c, D("40.00")), D("-7.41"))

    def test_fees_are_taken_from_the_sale_price(self):
        c = cost(item="100.00", postage="0", fee="10")
        self.assertEqual(break_even_price(c), D("111.12"))  # rounded UP, never below cost
        self.assertEqual(target_price(c, D("20")), D("133.33"))
        self.assertEqual(implied_margin_percent(c, D("133.33")), D("20.00"))


class DecideRepriceTests(unittest.TestCase):
    def decide(self, c, live, **kw):
        args = dict(default_margin_percent=None, max_change_percent=D("10"),
                    floor_price=D("1.00"), require_floor=True)
        args.update(kw)
        return decide_reprice(c, D(live), **args)

    def test_no_margin_anywhere_means_report_only_never_reprice(self):
        d = self.decide(cost(), "50.00")
        self.assertIsNone(d.proposed)
        self.assertFalse(d.within_limits)
        self.assertEqual(d.implied_margin, D("15.74"))
        self.assertIn("No margin set", d.reason)

    def test_per_sku_margin_beats_the_global_default(self):
        d = self.decide(cost(margin="30"), "56.16", default_margin_percent=D("80"))
        self.assertEqual(d.margin_used, D("30"))
        self.assertIsNone(d.proposed)  # already on target

    def test_large_gaps_are_closed_in_capped_steps(self):
        up = self.decide(cost(margin="30"), "40.00")
        self.assertEqual(up.proposed, D("44.00"))  # +10% cap, target 56.16
        self.assertTrue(up.within_limits)
        down = self.decide(cost(margin="30"), "80.00")
        self.assertEqual(down.proposed, D("72.00"))

    def test_small_gaps_reach_the_target_exactly(self):
        d = self.decide(cost(margin="30"), "54.00")
        self.assertEqual(d.proposed, D("56.16"))

    def test_tolerance_suppresses_penny_changes(self):
        d = self.decide(cost(margin="30"), "56.00")
        self.assertIsNone(d.proposed)

    def test_never_below_break_even_or_floor_and_such_cases_are_queued(self):
        d = self.decide(cost(margin="0"), "60.00", floor_price=D("50.00"))
        self.assertEqual(d.target, D("50.00"))
        self.assertEqual(d.proposed, D("54.00"))
        self.assertFalse(d.within_limits)
        self.assertIn("below the minimum", d.reason)

    def test_missing_floor_while_floors_are_required_is_queued(self):
        d = self.decide(cost(margin="30"), "54.00", floor_price=None)
        self.assertEqual(d.proposed, D("56.16"))
        self.assertFalse(d.within_limits)
        d = self.decide(cost(margin="30"), "54.00", floor_price=None, require_floor=False)
        self.assertTrue(d.within_limits)


class CostRowTests(unittest.TestCase):
    def test_valid_row_defaults_fee_to_zero_and_margin_to_none(self):
        c = parse_cost_row({"sku": "A", "item_cost": "10", "postage_cost": ""}, "GBP")
        self.assertEqual((c.item_cost, c.postage_cost, c.fee_percent, c.margin_percent),
                         (D("10.00"), D("0.00"), D("0.00"), None))

    def test_invalid_rows_are_rejected(self):
        for row in (
            {"sku": "", "item_cost": "1"},
            {"sku": "A", "item_cost": "abc"},
            {"sku": "A", "item_cost": "-1"},
            {"sku": "A", "item_cost": "1.234"},
            {"sku": "A", "item_cost": "0", "postage_cost": "0"},
            {"sku": "A", "item_cost": "1", "fee_percent": "60"},
            {"sku": "A", "item_cost": "1", "margin_percent": "NaN"},
        ):
            with self.subTest(row=row), self.assertRaises(CostRowError):
                parse_cost_row(row, "GBP")

    def test_csv_collects_errors_without_blocking_valid_rows(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "costs.csv"
            path.write_text("SKU,Item_Cost,margin_percent\nA,10,30\nB,oops,\nA,11,\nC,5,\n", encoding="utf-8")
            valid, errors = load_cost_csv(path, "GBP")
            self.assertEqual([c.sku for c in valid], ["A", "C"])
            self.assertEqual([e[0] for e in errors], [3, 4])
            path.write_text("sku\nA\n", encoding="utf-8")
            with self.assertRaises(CostRowError):
                load_cost_csv(path, "GBP")


class SkuCostStorageTests(unittest.TestCase):
    def test_set_get_list_replace_delete(self):
        with tempfile.TemporaryDirectory() as d:
            config = make_config(Path(d))
            db = AppDatabase(config.database_path, config.data_encryption_key)
            self.addCleanup(db.close)
            db.set_sku_cost("A", item_cost="10.00", postage_cost="3.20", fee_percent="0.00", currency="GBP")
            self.assertIsNone(db.get_sku_cost("A")["margin_percent"])
            db.set_sku_cost("A", item_cost="12.00", postage_cost="3.20", fee_percent="0.00",
                            currency="GBP", margin_percent="30.00")
            row = db.get_sku_cost("A")
            self.assertEqual((row["item_cost"], row["margin_percent"]), ("12.00", "30.00"))
            self.assertEqual(SkuCost.from_row(row).total_cost, D("15.20"))
            self.assertEqual(len(db.list_sku_costs()), 1)
            self.assertTrue(db.delete_sku_cost("A"))
            self.assertIsNone(db.get_sku_cost("A"))


if __name__ == "__main__":
    unittest.main()


class EngineConfigTests(unittest.TestCase):
    def load(self, extra: str):
        import os
        from ebay_app.config import load_config
        from test_config_validation import valid_env_text

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / ".env"
            path.write_text(valid_env_text() + extra, encoding="utf-8")
            os.chmod(path, 0o600)
            return load_config(path, {})

    def test_defaults_are_off_with_no_margin_and_daily_reprice(self):
        config = self.load("")
        self.assertEqual(config.engine_mode, "off")
        self.assertIsNone(config.engine_default_margin_percent)
        self.assertEqual(
            (config.engine_reprice_hours, config.engine_relist_minutes, config.engine_orders_minutes),
            (24, 60, 30),
        )
        self.assertIsNone(self.load("APP_ENGINE_DEFAULT_MARGIN_PERCENT=\n").engine_default_margin_percent)
        self.assertEqual(self.load("APP_ENGINE_DEFAULT_MARGIN_PERCENT=35\n").engine_default_margin_percent, D("35"))

    def test_invalid_modes_and_auto_outside_write_stages_are_rejected(self):
        from ebay_app.config import ConfigError

        with self.assertRaises(ConfigError):
            self.load("APP_ENGINE_MODE=turbo\n")
        self.assertEqual(self.load("APP_ENGINE_MODE=shadow\n").engine_mode, "shadow")
        for mode in ("auto-dry", "auto"):
            with self.subTest(mode=mode), self.assertRaisesRegex(ConfigError, "APP_FEATURE_STAGE"):
                self.load(f"APP_ENGINE_MODE={mode}\nAPP_FEATURE_STAGE=listings\n")
