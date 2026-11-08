from __future__ import annotations

import unittest

from ebay_app.audit_export import (
    parse_date_to_timestamp,
    summarize_row,
    to_csv,
    to_table,
)


def _record(**overrides):
    base = {
        "action_id": "a" * 32,
        "kind": "change_price",
        "status": "succeeded",
        "checkpoint": None,
        "created_at": 1_800_000_000,
        "updated_at": 1_800_000_010,
        "error_message": None,
        "payload": {"sku": "GG-1", "offer_id": "OFFER-1", "expected_price": "10.00", "new_price": "15.00"},
        "result": {"sku": "GG-1", "offer_id": "OFFER-1", "price": "15.00", "currency": "GBP", "dry_run": False},
    }
    base.update(overrides)
    return base


class SummarizeRowTests(unittest.TestCase):
    def test_change_price_shows_expected_and_new_price_as_before_after(self):
        row = summarize_row(_record())
        self.assertEqual(row["before"], "10.00")
        self.assertEqual(row["after"], "15.00")
        self.assertEqual(row["sku"], "GG-1")
        self.assertEqual(row["offer_id"], "OFFER-1")
        self.assertEqual(row["currency"], "GBP")
        self.assertEqual(row["dry_run"], "false")

    def test_change_price_dry_run_is_labelled_true_and_falls_back_to_payloads_new_price(self):
        row = summarize_row(
            _record(
                result={"sku": "GG-1", "dry_run": True, "price": None},
            )
        )
        self.assertEqual(row["dry_run"], "true")
        # result["price"] is None here, so "after" must come from the payload instead.
        self.assertEqual(row["after"], "15.00")

    def test_change_quantity_shows_expected_and_new_quantity(self):
        row = summarize_row(
            _record(
                kind="change_quantity",
                payload={"sku": "GG-2", "expected_quantity": 3, "new_quantity": 7},
                result={"sku": "GG-2", "quantity": 7},
            )
        )
        self.assertEqual(row["before"], "3")
        self.assertEqual(row["after"], "7")

    def test_add_image_shows_joined_expected_urls_and_the_new_url(self):
        row = summarize_row(
            _record(
                kind="add_image",
                payload={
                    "sku": "GG-3",
                    "expected_image_urls": ["https://example.com/a.jpg"],
                    "new_image_url": "https://example.com/b.jpg",
                },
                result={"sku": "GG-3", "image_url": "https://example.com/b.jpg"},
            )
        )
        self.assertEqual(row["before"], "https://example.com/a.jpg")
        self.assertEqual(row["after"], "https://example.com/b.jpg")

    def test_create_draft_has_no_before_and_shows_the_created_offer_as_after(self):
        row = summarize_row(
            _record(
                kind="create_draft",
                payload={"sku": "GG-4"},
                result={"sku": "GG-4", "offer_id": "OFFER-4"},
            )
        )
        self.assertEqual(row["before"], "")
        self.assertEqual(row["after"], "OFFER-4")

    def test_failed_action_with_no_result_shows_blank_after_and_the_error_message(self):
        row = summarize_row(
            _record(
                status="failed",
                result=None,
                error_message="Category not valid for this site",
            )
        )
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["after"], "15.00")  # falls back to the payload's new_price
        self.assertEqual(row["dry_run"], "")  # unknown, not "false" -- no result was recorded
        self.assertEqual(row["error_message"], "Category not valid for this site")

    def test_timestamps_are_rendered_as_iso8601_utc(self):
        row = summarize_row(_record(created_at=1_700_000_000, updated_at=1_700_000_060))
        self.assertEqual(row["created_at"], "2023-11-14T22:13:20Z")
        self.assertEqual(row["updated_at"], "2023-11-14T22:14:20Z")

    def test_missing_timestamp_renders_as_empty_string_not_an_error(self):
        row = summarize_row(_record(created_at=None, updated_at=None))
        self.assertEqual(row["created_at"], "")
        self.assertEqual(row["updated_at"], "")


class RenderTests(unittest.TestCase):
    def test_to_csv_includes_a_header_and_one_row_per_record(self):
        rendered = to_csv([_record(), _record(action_id="b" * 32)])
        lines = rendered.strip("\r\n").split("\r\n")
        self.assertEqual(len(lines), 3)  # header + 2 rows
        self.assertTrue(lines[0].startswith("action_id,kind,status,"))
        self.assertIn("a" * 32, lines[1])
        self.assertIn("b" * 32, lines[2])

    def test_to_csv_on_an_empty_list_is_just_the_header(self):
        rendered = to_csv([])
        self.assertEqual(rendered.strip("\r\n").split("\r\n"), [rendered.strip("\r\n")])
        self.assertTrue(rendered.startswith("action_id,"))

    def test_to_table_on_an_empty_list_says_so_plainly(self):
        self.assertEqual(to_table([]), "No actions match the given filters.")

    def test_to_table_renders_a_header_and_one_line_per_record(self):
        rendered = to_table([_record()])
        lines = rendered.split("\n")
        self.assertEqual(len(lines), 3)  # header, separator, one row
        self.assertIn("ACTION_ID", lines[0])
        self.assertIn("a" * 32, lines[2])


class ParseDateToTimestampTests(unittest.TestCase):
    def test_parses_a_plain_date_to_midnight_utc(self):
        self.assertEqual(parse_date_to_timestamp("2026-11-08"), 1_794_096_000)

    def test_end_of_day_parses_to_23_59_59_utc(self):
        self.assertEqual(
            parse_date_to_timestamp("2026-11-08", end_of_day=True), 1_794_182_399
        )
        # end-of-day is always exactly one second before the next midnight.
        self.assertEqual(
            parse_date_to_timestamp("2026-11-08", end_of_day=True) + 1,
            parse_date_to_timestamp("2026-11-09"),
        )

    def test_rejects_an_unparseable_date(self):
        with self.assertRaises(ValueError):
            parse_date_to_timestamp("not-a-date")

    def test_rejects_a_differently_formatted_date(self):
        with self.assertRaises(ValueError):
            parse_date_to_timestamp("08/11/2026")


if __name__ == "__main__":
    unittest.main()
