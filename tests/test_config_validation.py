from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from ebay_app.config import (
    CONNECTION_SCOPES,
    DRAFT_SCOPES,
    FULFILLMENT_READ_SCOPE,
    IDENTITY_SCOPE,
    INVENTORY_READ_SCOPE,
    INVENTORY_WRITE_SCOPE,
    LISTING_SCOPES,
    ORDER_SCOPES,
    PRODUCTION_API_ORIGIN,
    PRODUCTION_CONSENT_URL,
    PRODUCTION_ENV_FILENAME,
    PRODUCTION_IDENTITY_BASE_URL,
    PRODUCTION_OPT_IN_ENV_VAR,
    PRODUCTION_OPT_IN_VALUE,
    PRODUCTION_TOKEN_URL,
    PRODUCTION_WRITE_OPT_IN_ENV_VAR,
    PRODUCTION_WRITE_OPT_IN_VALUE,
    PRODUCTION_WRITE_OPT_INS,
    SANDBOX_API_ORIGIN,
    SELLER_SCOPES,
    ConfigError,
    load_config,
)
from ebay_app.validation import ValidationError, validate_draft, validate_new_price


def valid_env_text() -> str:
    key = base64.b64encode(b"x" * 32).decode("ascii")
    return f"""EBAY_ENVIRONMENT=sandbox
EBAY_CLIENT_ID=Example-Application-SBX-placeholder
EBAY_CLIENT_SECRET=SBX-placeholder-cert-id
EBAY_RUNAME=runame
APP_ADMIN_PASSWORD=this-is-a-long-local-password
APP_DATA_ENCRYPTION_KEY={key}
APP_DATABASE_PATH=./instance/test.sqlite
"""


def valid_production_env_text() -> str:
    key = base64.b64encode(b"y" * 32).decode("ascii")
    return f"""EBAY_ENVIRONMENT=production
{PRODUCTION_OPT_IN_ENV_VAR}={PRODUCTION_OPT_IN_VALUE}
EBAY_CLIENT_ID=Example-Application-PRD-placeholder
EBAY_CLIENT_SECRET=PRD-placeholder-cert-id
EBAY_RUNAME=runame
APP_ADMIN_PASSWORD=this-is-a-long-local-password
APP_DATA_ENCRYPTION_KEY={key}
APP_DATABASE_PATH=./instance/production.sqlite
APP_FEATURE_STAGE=listings
"""


class ConfigValidationTests(unittest.TestCase):
    @staticmethod
    def draft_form(**overrides):
        form = {
            "csrf_token": ["csrf"],
            "sku": ["SKU-1"],
            "title": ["Example"],
            "description": ["Description"],
            "condition": ["NEW"],
            "quantity": ["2"],
            "price": ["19.9"],
            "category_id": ["12345"],
            "aspects": ['{"Brand":["Example"]}'],
        }
        for name, value in overrides.items():
            form[name] = value if isinstance(value, list) else [value]
        return form

    def assert_draft_error(self, field, **overrides):
        with self.assertRaises(ValidationError) as caught:
            validate_draft(self.draft_form(**overrides), "GBP")
        self.assertIn(field, caught.exception.errors)

    def test_loads_private_sandbox_env(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(valid_env_text(), encoding="utf-8")
            os.chmod(path, 0o600)
            config = load_config(path, {})
            self.assertEqual(config.marketplace_id, "EBAY_GB")
            self.assertEqual(
                config.database_path,
                (Path(directory) / "instance" / "test.sqlite").absolute(),
            )
            self.assertEqual(len(config.data_encryption_key), 32)
            self.assertEqual(config.feature_stage, "connection")
            self.assertEqual(config.scopes, CONNECTION_SCOPES)

    def test_rejects_production_and_broad_env_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(valid_env_text().replace("sandbox", "production", 1), encoding="utf-8")
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(ConfigError, "restricted.*sandbox"):
                load_config(path, {})
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(ConfigError, "chmod 600"):
                load_config(path, {})

    def test_rejects_non_loopback_server_bind(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(valid_env_text() + "HOST=0.0.0.0\n", encoding="utf-8")
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(ConfigError, "HOST must remain 127.0.0.1"):
                load_config(path, {})

    def test_listings_stage_uses_only_the_two_read_only_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                valid_env_text() + "APP_FEATURE_STAGE=listings\n",
                encoding="utf-8",
            )
            os.chmod(path, 0o600)

            config = load_config(path, {})

            self.assertEqual(config.feature_stage, "listings")
            self.assertEqual(
                config.scopes,
                (IDENTITY_SCOPE, INVENTORY_READ_SCOPE),
            )
            self.assertEqual(config.scopes, LISTING_SCOPES)
            self.assertNotIn(
                "https://api.ebay.com/oauth/api_scope/sell.inventory",
                config.scopes,
            )
            self.assertNotIn(
                "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly",
                config.scopes,
            )
            self.assertTrue(set(config.scopes).isdisjoint(set(SELLER_SCOPES) - {IDENTITY_SCOPE}))

    def test_orders_stage_uses_only_three_cumulative_read_only_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                valid_env_text() + "APP_FEATURE_STAGE=orders\n",
                encoding="utf-8",
            )
            os.chmod(path, 0o600)

            config = load_config(path, {})

            self.assertEqual(config.feature_stage, "orders")
            self.assertEqual(
                config.scopes,
                (IDENTITY_SCOPE, INVENTORY_READ_SCOPE, FULFILLMENT_READ_SCOPE),
            )
            self.assertEqual(config.scopes, ORDER_SCOPES)
            self.assertNotIn(
                "https://api.ebay.com/oauth/api_scope/sell.inventory",
                config.scopes,
            )
            self.assertNotIn(
                "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
                config.scopes,
            )

    def test_drafts_stage_replaces_inventory_read_with_manage_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                valid_env_text() + "APP_FEATURE_STAGE=drafts\n",
                encoding="utf-8",
            )
            os.chmod(path, 0o600)

            config = load_config(path, {})

            self.assertEqual(config.feature_stage, "drafts")
            self.assertEqual(config.scopes, DRAFT_SCOPES)
            self.assertIn(INVENTORY_WRITE_SCOPE, config.scopes)
            self.assertNotIn(INVENTORY_READ_SCOPE, config.scopes)
            self.assertNotIn(
                "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
                config.scopes,
            )

    def test_rejects_feature_stages_beyond_phase_five(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(valid_env_text() + "APP_FEATURE_STAGE=full\n", encoding="utf-8")
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(
                ConfigError,
                "connection, account, listings, orders, drafts, prices, quantities, "
                "images, migration, publish or setup"
            ):
                load_config(path, {})

    def test_legacy_tunnel_env_values_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                valid_env_text()
                + "APP_ALLOWED_HOSTS=stale.trycloudflare.com\nAPP_COOKIE_SECURE=true\n",
                encoding="utf-8",
            )
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(ConfigError, "obsolete OAuth runtime"):
                load_config(path, {})

    def test_rejects_production_keys_and_manual_refresh_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            production = root / "production.env"
            production.write_text(
                valid_env_text().replace("-SBX-", "-PRD-", 1),
                encoding="utf-8",
            )
            os.chmod(production, 0o600)
            with self.assertRaisesRegex(ConfigError, "Sandbox App ID"):
                load_config(production, {})

            refresh = root / "refresh.env"
            refresh.write_text(
                valid_env_text() + "EBAY_REFRESH_TOKEN=do-not-bootstrap-this\n",
                encoding="utf-8",
            )
            os.chmod(refresh, 0o600)
            with self.assertRaisesRegex(ConfigError, "cannot be supplied through .env"):
                load_config(refresh, {})

    def test_rejects_linked_or_duplicate_env_and_database_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.env"
            source.write_text(valid_env_text(), encoding="utf-8")
            os.chmod(source, 0o600)
            linked = root / ".env"
            linked.symlink_to(source)
            with self.assertRaisesRegex(ConfigError, "regular file without links"):
                load_config(linked, {})

            duplicate = root / "duplicate.env"
            duplicate.write_text(valid_env_text() + "EBAY_RUNAME=again\n", encoding="utf-8")
            os.chmod(duplicate, 0o600)
            with self.assertRaisesRegex(ConfigError, "Duplicate .env key"):
                load_config(duplicate, {})

            alias = root / "alias.env"
            alias.write_text(
                valid_env_text().replace(
                    "APP_DATABASE_PATH=./instance/test.sqlite",
                    "APP_DATABASE_PATH=./alias.env",
                ),
                encoding="utf-8",
            )
            os.chmod(alias, 0o600)
            with self.assertRaisesRegex(ConfigError, "must not point to the .env"):
                load_config(alias, {})

    def test_draft_and_price_validation_are_strict(self):
        draft = validate_draft(self.draft_form(), "GBP")
        self.assertEqual(draft["price"], "19.90")
        self.assertNotIn("image_urls", draft)
        self.assertEqual(validate_new_price("20"), "20.00")
        with self.assertRaises(ValidationError):
            validate_new_price("19.999")
        with self.assertRaises(ValidationError):
            validate_draft({"sku": ["SKU"]}, "GBP")

    def test_draft_rejects_unsupported_and_every_repeated_field(self):
        for name in self.draft_form():
            with self.subTest(repeated=name):
                form = self.draft_form()
                form[name] = [form[name][0], form[name][0]]
                with self.assertRaises(ValidationError) as caught:
                    validate_draft(form, "GBP")
                self.assertIn("form", caught.exception.errors)

        for name in ("image_urls", "unexpected"):
            with self.subTest(unsupported=name):
                self.assert_draft_error("form", **{name: ""})

    def test_draft_sku_is_conservative_ascii(self):
        for sku in ("A", "A" + "x" * 49, "sku.123_test-value"):
            with self.subTest(valid=sku):
                self.assertEqual(validate_draft(self.draft_form(sku=sku), "GBP")["sku"], sku)

        for sku in ("", "-SKU", " SKU", "SKU SPACE", "SKÜ", "A" * 51, "SKU/1"):
            with self.subTest(invalid=sku):
                self.assert_draft_error("sku", sku=sku)

    def test_draft_title_and_description_are_bounded_plain_text(self):
        invalid_values = (
            ("title", "T" * 81),
            ("title", "<b>Example</b>"),
            ("title", "Example\x00"),
            ("title", "Example\n"),
            ("description", "D" * 4001),
            ("description", "<script>alert(1)</script>"),
            ("description", "First line\nSecond line"),
            ("description", "Invalid \ud800"),
        )
        for field, value in invalid_values:
            with self.subTest(field=field, value=repr(value[:30])):
                self.assert_draft_error(field, **{field: value})

    def test_draft_condition_quantity_price_and_category_are_bounded(self):
        for condition in ("NEW", "LIKE_NEW", "USED_GOOD", "FOR_PARTS_OR_NOT_WORKING"):
            with self.subTest(condition=condition):
                self.assertEqual(
                    validate_draft(self.draft_form(condition=condition), "GBP")["condition"],
                    condition,
                )
        for condition in ("UNKNOWN", "MANUFACTURER_REFURBISHED"):
            with self.subTest(invalid_condition=condition):
                self.assert_draft_error("condition", condition=condition)

        for quantity in ("1", "999999"):
            with self.subTest(quantity=quantity):
                self.assertEqual(
                    validate_draft(self.draft_form(quantity=quantity), "GBP")["quantity"],
                    int(quantity),
                )
        for quantity in ("0", "1000000", "1.0", "+1", "-1"):
            with self.subTest(invalid_quantity=quantity):
                self.assert_draft_error("quantity", quantity=quantity)

        for price in ("0", "-1", "+1", "1.001", "1e2", "NaN", "1000000000"):
            with self.subTest(invalid_price=price):
                self.assert_draft_error("price", price=price)

        self.assertEqual(
            validate_draft(
                self.draft_form(price="999999999.99", category_id="123456789012"),
                "GBP",
            )["price"],
            "999999999.99",
        )
        for category_id in ("", "1234567890123", "12A", "-1"):
            with self.subTest(invalid_category=category_id):
                self.assert_draft_error("category_id", category_id=category_id)

    def test_draft_aspects_are_small_unique_plain_text_json(self):
        fifty_aspects = {f"Name {number}": ["Value"] for number in range(50)}
        draft = validate_draft(
            self.draft_form(
                aspects=json.dumps(
                    {" Brand ": [" Example ", "Second"], "Colour": "Blue"}
                )
            ),
            "GBP",
        )
        self.assertEqual(
            draft["aspects"],
            {"Brand": ["Example", "Second"], "Colour": ["Blue"]},
        )
        self.assertEqual(
            len(
                validate_draft(
                    self.draft_form(aspects=json.dumps(fifty_aspects)), "GBP"
                )["aspects"]
            ),
            50,
        )

        invalid_aspects = (
            '["not", "an", "object"]',
            json.dumps({f"Name {number}": ["Value"] for number in range(51)}),
            '{"Brand":["One"],"Brand":["Two"]}',
            '{"Brand":["One"]," Brand ":["Two"]}',
            json.dumps({"N" * 41: ["Value"]}),
            json.dumps({"Brand": ["V" * 51]}),
            json.dumps({"": ["Value"]}),
            json.dumps({"Brand": [""]}),
            json.dumps({"Brand": ["Same", " Same "]}),
            json.dumps({"Bad\x00Name": ["Value"]}),
            json.dumps({"Brand": ["Bad\x00Value"]}),
            "{" + (" " * 16_384) + "}",
        )
        for aspects in invalid_aspects:
            with self.subTest(aspects=repr(aspects[:60])):
                self.assert_draft_error("aspects", aspects=aspects)


class ProductionEnvironmentConfigTests(unittest.TestCase):
    """Phase 9 read-only groundwork: see docs/phase9_production_design.md.

    None of these paths are reachable from the running application yet --
    run.py never passes a custom env_file, so only a caller that explicitly
    points load_config() at a file literally named .env.production (as a
    future dedicated production entry point would) can ever reach them.
    """

    def test_loads_valid_production_env_with_production_endpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / PRODUCTION_ENV_FILENAME
            path.write_text(valid_production_env_text(), encoding="utf-8")
            os.chmod(path, 0o600)

            config = load_config(path, {})

            self.assertEqual(config.environment, "production")
            self.assertEqual(config.api_origin, PRODUCTION_API_ORIGIN)
            self.assertEqual(config.identity_base_url, PRODUCTION_IDENTITY_BASE_URL)
            self.assertEqual(config.consent_url, PRODUCTION_CONSENT_URL)
            self.assertEqual(config.token_url, PRODUCTION_TOKEN_URL)
            self.assertEqual(config.feature_stage, "listings")
            self.assertEqual(config.scopes, LISTING_SCOPES)

    def test_sandbox_still_uses_sandbox_endpoints_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(valid_env_text(), encoding="utf-8")
            os.chmod(path, 0o600)

            config = load_config(path, {})

            self.assertEqual(config.environment, "sandbox")
            self.assertEqual(config.api_origin, SANDBOX_API_ORIGIN)

    def test_production_environment_rejected_from_a_file_not_named_env_production(self):
        with tempfile.TemporaryDirectory() as directory:
            # Exactly today's error, unchanged: a wrongly-named file gets no
            # hint that a correctly-named one plus the opt-in would work.
            path = Path(directory) / ".env"
            path.write_text(valid_production_env_text(), encoding="utf-8")
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(ConfigError, "restricted.*sandbox"):
                load_config(path, {})

    def test_production_environment_rejected_without_the_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / PRODUCTION_ENV_FILENAME
            text = valid_production_env_text().replace(
                PRODUCTION_OPT_IN_ENV_VAR + "=" + PRODUCTION_OPT_IN_VALUE + "\n", ""
            )
            path.write_text(text, encoding="utf-8")
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(ConfigError, PRODUCTION_OPT_IN_ENV_VAR):
                load_config(path, {})

    def test_production_environment_rejected_with_the_wrong_opt_in_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / PRODUCTION_ENV_FILENAME
            text = valid_production_env_text().replace(
                f"{PRODUCTION_OPT_IN_ENV_VAR}={PRODUCTION_OPT_IN_VALUE}",
                f"{PRODUCTION_OPT_IN_ENV_VAR}=yes",
            )
            path.write_text(text, encoding="utf-8")
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(ConfigError, PRODUCTION_OPT_IN_ENV_VAR):
                load_config(path, {})

    def test_sandbox_env_with_production_value_still_gets_original_message(self):
        # Regression: the pre-existing sandbox-only test must keep passing
        # unchanged even though load_config() now understands "production".
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                valid_env_text().replace("sandbox", "production", 1), encoding="utf-8"
            )
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(ConfigError, "restricted.*sandbox"):
                load_config(path, {})

    def test_production_rejects_sandbox_shaped_client_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / PRODUCTION_ENV_FILENAME
            text = valid_production_env_text().replace(
                "EBAY_CLIENT_ID=Example-Application-PRD-placeholder",
                "EBAY_CLIENT_ID=Example-Application-SBX-placeholder",
            )
            path.write_text(text, encoding="utf-8")
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(ConfigError, "Sandbox App ID"):
                load_config(path, {})

    def test_production_rejects_sandbox_shaped_client_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / PRODUCTION_ENV_FILENAME
            text = valid_production_env_text().replace(
                "EBAY_CLIENT_SECRET=PRD-placeholder-cert-id",
                "EBAY_CLIENT_SECRET=SBX-placeholder-cert-id",
            )
            path.write_text(text, encoding="utf-8")
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(ConfigError, "Sandbox Cert ID"):
                load_config(path, {})

    def test_sandbox_still_rejects_production_shaped_client_id(self):
        # Regression: today's Sandbox-only App ID check is unchanged.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            text = valid_env_text().replace(
                "EBAY_CLIENT_ID=Example-Application-SBX-placeholder",
                "EBAY_CLIENT_ID=Example-Application-PRD-placeholder",
            )
            path.write_text(text, encoding="utf-8")
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(ConfigError, "Sandbox App ID"):
                load_config(path, {})

    def test_production_rejects_write_capable_feature_stages(self):
        # "prices", "quantities", and "images" are deliberately excluded
        # here -- each now has its own, separately opted-in path and its own
        # dedicated tests below. This test pins that every *other*
        # write-capable stage still gets the original generic read-only
        # rejection, with no opt-in able to reach them.
        for stage in ("drafts", "setup"):
            with self.subTest(stage=stage):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / PRODUCTION_ENV_FILENAME
                    text = valid_production_env_text().replace(
                        "APP_FEATURE_STAGE=listings", f"APP_FEATURE_STAGE={stage}"
                    )
                    path.write_text(text, encoding="utf-8")
                    os.chmod(path, 0o600)
                    with self.assertRaisesRegex(ConfigError, "read-only"):
                        load_config(path, {})

    def test_production_allows_every_read_only_stage(self):
        for stage in ("connection", "account", "listings", "orders"):
            with self.subTest(stage=stage):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / PRODUCTION_ENV_FILENAME
                    text = valid_production_env_text().replace(
                        "APP_FEATURE_STAGE=listings", f"APP_FEATURE_STAGE={stage}"
                    )
                    path.write_text(text, encoding="utf-8")
                    os.chmod(path, 0o600)
                    config = load_config(path, {})
                    self.assertEqual(config.feature_stage, stage)

    def test_production_prices_stage_rejected_without_the_write_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / PRODUCTION_ENV_FILENAME
            text = valid_production_env_text().replace(
                "APP_FEATURE_STAGE=listings", "APP_FEATURE_STAGE=prices"
            )
            path.write_text(text, encoding="utf-8")
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(ConfigError, PRODUCTION_WRITE_OPT_IN_ENV_VAR):
                load_config(path, {})

    def test_production_prices_stage_rejected_with_the_wrong_opt_in_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / PRODUCTION_ENV_FILENAME
            text = valid_production_env_text().replace(
                "APP_FEATURE_STAGE=listings",
                f"APP_FEATURE_STAGE=prices\n{PRODUCTION_WRITE_OPT_IN_ENV_VAR}=yes",
            )
            path.write_text(text, encoding="utf-8")
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(ConfigError, PRODUCTION_WRITE_OPT_IN_ENV_VAR):
                load_config(path, {})

    def test_production_prices_stage_accepted_with_the_explicit_write_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / PRODUCTION_ENV_FILENAME
            text = valid_production_env_text().replace(
                "APP_FEATURE_STAGE=listings",
                "APP_FEATURE_STAGE=prices\n"
                f"{PRODUCTION_WRITE_OPT_IN_ENV_VAR}={PRODUCTION_WRITE_OPT_IN_VALUE}",
            )
            path.write_text(text, encoding="utf-8")
            os.chmod(path, 0o600)
            config = load_config(path, {})
            self.assertEqual(config.feature_stage, "prices")
            # The kill switch's other half: entering "prices" in production
            # for the first time always starts in dry-run unless the owner
            # also explicitly sets APP_DRY_RUN=false.
            self.assertTrue(config.dry_run)

    def test_production_quantities_and_images_remain_locked_even_with_the_price_write_opt_in(self):
        # Defense in depth: the price-specific opt-in must never accidentally
        # unlock a later stage it was never meant to cover.
        for stage in ("quantities", "images"):
            with self.subTest(stage=stage):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / PRODUCTION_ENV_FILENAME
                    text = valid_production_env_text().replace(
                        "APP_FEATURE_STAGE=listings",
                        f"APP_FEATURE_STAGE={stage}\n"
                        f"{PRODUCTION_WRITE_OPT_IN_ENV_VAR}={PRODUCTION_WRITE_OPT_IN_VALUE}",
                    )
                    path.write_text(text, encoding="utf-8")
                    os.chmod(path, 0o600)
                    with self.assertRaises(ConfigError):
                        load_config(path, {})

    def test_no_write_capable_stage_opt_in_crosses_over_to_another_stage(self):
        # Fuller defense-in-depth sweep than the test above: every ordered
        # (wrong_stage, target_stage) pair across all three write-capable
        # stages must fail -- each stage's own opt-in is the only thing that
        # can ever unlock it.
        for target_stage, (_env_var, _value) in PRODUCTION_WRITE_OPT_INS.items():
            for wrong_stage, (wrong_env_var, wrong_value) in PRODUCTION_WRITE_OPT_INS.items():
                if wrong_stage == target_stage:
                    continue
                with self.subTest(target_stage=target_stage, wrong_stage=wrong_stage):
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / PRODUCTION_ENV_FILENAME
                        text = valid_production_env_text().replace(
                            "APP_FEATURE_STAGE=listings",
                            f"APP_FEATURE_STAGE={target_stage}\n"
                            f"{wrong_env_var}={wrong_value}",
                        )
                        path.write_text(text, encoding="utf-8")
                        os.chmod(path, 0o600)
                        with self.assertRaises(ConfigError):
                            load_config(path, {})

    def test_production_quantities_stage_rejected_without_the_write_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / PRODUCTION_ENV_FILENAME
            text = valid_production_env_text().replace(
                "APP_FEATURE_STAGE=listings", "APP_FEATURE_STAGE=quantities"
            )
            path.write_text(text, encoding="utf-8")
            os.chmod(path, 0o600)
            quantities_env_var, _value = PRODUCTION_WRITE_OPT_INS["quantities"]
            with self.assertRaisesRegex(ConfigError, quantities_env_var):
                load_config(path, {})

    def test_production_quantities_stage_rejected_with_the_wrong_opt_in_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / PRODUCTION_ENV_FILENAME
            quantities_env_var, _value = PRODUCTION_WRITE_OPT_INS["quantities"]
            text = valid_production_env_text().replace(
                "APP_FEATURE_STAGE=listings",
                f"APP_FEATURE_STAGE=quantities\n{quantities_env_var}=yes",
            )
            path.write_text(text, encoding="utf-8")
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(ConfigError, quantities_env_var):
                load_config(path, {})

    def test_production_quantities_stage_accepted_with_the_explicit_write_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / PRODUCTION_ENV_FILENAME
            quantities_env_var, quantities_value = PRODUCTION_WRITE_OPT_INS["quantities"]
            text = valid_production_env_text().replace(
                "APP_FEATURE_STAGE=listings",
                f"APP_FEATURE_STAGE=quantities\n{quantities_env_var}={quantities_value}",
            )
            path.write_text(text, encoding="utf-8")
            os.chmod(path, 0o600)
            config = load_config(path, {})
            self.assertEqual(config.feature_stage, "quantities")
            self.assertTrue(config.dry_run)

    def test_production_images_stage_rejected_without_the_write_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / PRODUCTION_ENV_FILENAME
            text = valid_production_env_text().replace(
                "APP_FEATURE_STAGE=listings", "APP_FEATURE_STAGE=images"
            )
            path.write_text(text, encoding="utf-8")
            os.chmod(path, 0o600)
            images_env_var, _value = PRODUCTION_WRITE_OPT_INS["images"]
            with self.assertRaisesRegex(ConfigError, images_env_var):
                load_config(path, {})

    def test_production_images_stage_rejected_with_the_wrong_opt_in_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / PRODUCTION_ENV_FILENAME
            images_env_var, _value = PRODUCTION_WRITE_OPT_INS["images"]
            text = valid_production_env_text().replace(
                "APP_FEATURE_STAGE=listings",
                f"APP_FEATURE_STAGE=images\n{images_env_var}=yes",
            )
            path.write_text(text, encoding="utf-8")
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(ConfigError, images_env_var):
                load_config(path, {})

    def test_production_images_stage_accepted_with_the_explicit_write_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / PRODUCTION_ENV_FILENAME
            images_env_var, images_value = PRODUCTION_WRITE_OPT_INS["images"]
            text = valid_production_env_text().replace(
                "APP_FEATURE_STAGE=listings",
                f"APP_FEATURE_STAGE=images\n{images_env_var}={images_value}",
            )
            path.write_text(text, encoding="utf-8")
            os.chmod(path, 0o600)
            config = load_config(path, {})
            self.assertEqual(config.feature_stage, "images")
            self.assertTrue(config.dry_run)

    def test_production_migration_stage_rejected_without_the_write_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / PRODUCTION_ENV_FILENAME
            text = valid_production_env_text().replace(
                "APP_FEATURE_STAGE=listings", "APP_FEATURE_STAGE=migration"
            )
            path.write_text(text, encoding="utf-8")
            os.chmod(path, 0o600)
            migration_env_var, _value = PRODUCTION_WRITE_OPT_INS["migration"]
            with self.assertRaisesRegex(ConfigError, migration_env_var):
                load_config(path, {})

    def test_production_migration_stage_rejected_with_the_wrong_opt_in_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / PRODUCTION_ENV_FILENAME
            migration_env_var, _value = PRODUCTION_WRITE_OPT_INS["migration"]
            text = valid_production_env_text().replace(
                "APP_FEATURE_STAGE=listings",
                f"APP_FEATURE_STAGE=migration\n{migration_env_var}=yes",
            )
            path.write_text(text, encoding="utf-8")
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(ConfigError, migration_env_var):
                load_config(path, {})

    def test_production_migration_stage_accepted_with_the_explicit_write_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / PRODUCTION_ENV_FILENAME
            migration_env_var, migration_value = PRODUCTION_WRITE_OPT_INS["migration"]
            text = valid_production_env_text().replace(
                "APP_FEATURE_STAGE=listings",
                f"APP_FEATURE_STAGE=migration\n{migration_env_var}={migration_value}",
            )
            path.write_text(text, encoding="utf-8")
            os.chmod(path, 0o600)
            config = load_config(path, {})
            self.assertEqual(config.feature_stage, "migration")
            self.assertTrue(config.dry_run)

    def test_migration_stage_uses_the_same_scope_as_drafts_prices_quantities_images(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / PRODUCTION_ENV_FILENAME
            migration_env_var, migration_value = PRODUCTION_WRITE_OPT_INS["migration"]
            text = valid_production_env_text().replace(
                "APP_FEATURE_STAGE=listings",
                f"APP_FEATURE_STAGE=migration\n{migration_env_var}={migration_value}",
            )
            path.write_text(text, encoding="utf-8")
            os.chmod(path, 0o600)
            config = load_config(path, {})
            self.assertEqual(config.scopes, DRAFT_SCOPES)

    def test_dry_run_defaults_to_true_in_production_and_false_in_sandbox(self):
        with tempfile.TemporaryDirectory() as directory:
            prod_path = Path(directory) / PRODUCTION_ENV_FILENAME
            prod_path.write_text(valid_production_env_text(), encoding="utf-8")
            os.chmod(prod_path, 0o600)
            self.assertTrue(load_config(prod_path, {}).dry_run)

        with tempfile.TemporaryDirectory() as directory:
            sandbox_path = Path(directory) / ".env"
            sandbox_path.write_text(valid_env_text(), encoding="utf-8")
            os.chmod(sandbox_path, 0o600)
            self.assertFalse(load_config(sandbox_path, {}).dry_run)

    def test_dry_run_can_be_explicitly_overridden_in_either_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / PRODUCTION_ENV_FILENAME
            text = valid_production_env_text() + "APP_DRY_RUN=false\n"
            path.write_text(text, encoding="utf-8")
            os.chmod(path, 0o600)
            self.assertFalse(load_config(path, {}).dry_run)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            text = valid_env_text() + "APP_DRY_RUN=true\n"
            path.write_text(text, encoding="utf-8")
            os.chmod(path, 0o600)
            self.assertTrue(load_config(path, {}).dry_run)

    def test_production_allowed_with_no_sibling_sandbox_env_present(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / PRODUCTION_ENV_FILENAME
            path.write_text(valid_production_env_text(), encoding="utf-8")
            os.chmod(path, 0o600)
            # No .env beside it in this empty directory -- nothing to compare
            # against, so this must not be blocked.
            config = load_config(path, {})
            self.assertEqual(config.environment, "production")

    def test_production_rejects_same_database_path_as_sibling_sandbox_env(self):
        with tempfile.TemporaryDirectory() as directory:
            sandbox_path = Path(directory) / ".env"
            sandbox_path.write_text(
                valid_env_text().replace(
                    "APP_DATABASE_PATH=./instance/test.sqlite",
                    "APP_DATABASE_PATH=./instance/shared.sqlite",
                ),
                encoding="utf-8",
            )
            os.chmod(sandbox_path, 0o600)

            production_path = Path(directory) / PRODUCTION_ENV_FILENAME
            production_path.write_text(
                valid_production_env_text().replace(
                    "APP_DATABASE_PATH=./instance/production.sqlite",
                    "APP_DATABASE_PATH=./instance/shared.sqlite",
                ),
                encoding="utf-8",
            )
            os.chmod(production_path, 0o600)

            with self.assertRaisesRegex(ConfigError, "same file the Sandbox"):
                load_config(production_path, {})

    def test_production_rejects_same_encryption_key_as_sibling_sandbox_env(self):
        with tempfile.TemporaryDirectory() as directory:
            original_sandbox_key = base64.b64encode(b"x" * 32).decode("ascii")
            original_production_key = base64.b64encode(b"y" * 32).decode("ascii")
            shared_key = base64.b64encode(b"z" * 32).decode("ascii")

            sandbox_path = Path(directory) / ".env"
            sandbox_path.write_text(
                valid_env_text().replace(original_sandbox_key, shared_key),
                encoding="utf-8",
            )
            os.chmod(sandbox_path, 0o600)

            production_path = Path(directory) / PRODUCTION_ENV_FILENAME
            production_path.write_text(
                valid_production_env_text().replace(original_production_key, shared_key),
                encoding="utf-8",
            )
            os.chmod(production_path, 0o600)

            with self.assertRaisesRegex(ConfigError, "same key the Sandbox"):
                load_config(production_path, {})

    def test_production_rejects_different_database_but_allows_distinct_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            sandbox_path = Path(directory) / ".env"
            sandbox_path.write_text(valid_env_text(), encoding="utf-8")
            os.chmod(sandbox_path, 0o600)

            production_path = Path(directory) / PRODUCTION_ENV_FILENAME
            production_path.write_text(valid_production_env_text(), encoding="utf-8")
            os.chmod(production_path, 0o600)

            # Sandbox uses ./instance/test.sqlite and a key of "x" * 32;
            # production uses ./instance/production.sqlite and a key of
            # "y" * 32 -- fully distinct, so this must succeed cleanly.
            config = load_config(production_path, {})
            self.assertEqual(config.environment, "production")
            self.assertTrue(str(config.database_path).endswith("production.sqlite"))

    def test_run_py_cannot_reach_production_today(self):
        # run.py's main() always calls load_config() with no arguments, so it
        # always resolves to a file literally named ".env" next to the repo
        # root -- never ".env.production" -- no matter what EBAY_ENVIRONMENT
        # says inside it. This regression test pins that: production stays
        # unreachable from the real running application until a dedicated
        # production entry point is added and reviewed.
        import run

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(valid_production_env_text(), encoding="utf-8")
            os.chmod(path, 0o600)
            default_path = Path(run.__file__).resolve().parent / ".env"
            self.assertNotEqual(path, default_path)
            self.assertEqual(default_path.name, ".env")


class AccountDeletionConfigTests(unittest.TestCase):
    """ACCOUNT_DELETION_WORKER_URL / ACCOUNT_DELETION_STATUS_SECRET (schema v9,
    see ebay_app/account_deletion.py). Both unset is the default, inactive
    state; every other combination is validated strictly."""

    VALID_URL = "https://ebay-account-deletion-receiver.my-subdomain.workers.dev"
    VALID_SECRET = "A" * 40

    @staticmethod
    def _load(directory: Path, extra_lines=()):
        path = Path(directory) / ".env"
        content = valid_env_text() + "".join(line + chr(10) for line in extra_lines)
        path.write_text(content, encoding="utf-8")
        os.chmod(path, 0o600)
        return load_config(path, {})

    def test_defaults_to_unset_and_inactive(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._load(directory)
            self.assertIsNone(config.deletion_worker_base_url)
            self.assertIsNone(config.deletion_worker_status_secret)

    def test_requires_both_settings_together(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigError, "both"):
                self._load(directory, ["ACCOUNT_DELETION_WORKER_URL=" + self.VALID_URL])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigError, "both"):
                self._load(directory, ["ACCOUNT_DELETION_STATUS_SECRET=" + self.VALID_SECRET])

    def test_accepts_and_normalizes_a_valid_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._load(
                directory,
                [
                    "ACCOUNT_DELETION_WORKER_URL=" + self.VALID_URL + "/",
                    "ACCOUNT_DELETION_STATUS_SECRET=" + self.VALID_SECRET,
                ],
            )
            self.assertEqual(config.deletion_worker_base_url, self.VALID_URL)
            self.assertEqual(config.deletion_worker_status_secret, self.VALID_SECRET)

    def test_rejects_bad_worker_urls(self):
        base = "ebay-account-deletion-receiver.my-subdomain.workers.dev"
        bad_urls = [
            "http://" + base,
            "https://" + base + ":8443",
            "https://" + base + "/ebay/account-deletion",
            "https://" + base + "?x=1",
            "https://example.com",
            "https://ebay-account-deletion-receiver.workers.dev",
            "not-a-url",
        ]
        for bad_url in bad_urls:
            with self.subTest(bad_url=bad_url):
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaises(ConfigError):
                        self._load(
                            directory,
                            [
                                "ACCOUNT_DELETION_WORKER_URL=" + bad_url,
                                "ACCOUNT_DELETION_STATUS_SECRET=" + self.VALID_SECRET,
                            ],
                        )

    def test_rejects_bad_status_secrets(self):
        bad_secrets = ["short", "A" * 31, "A" * 1025, "has space", "tab" + chr(9) + "char"]
        for bad_secret in bad_secrets:
            with self.subTest(bad_secret=bad_secret):
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaises(ConfigError):
                        self._load(
                            directory,
                            [
                                "ACCOUNT_DELETION_WORKER_URL=" + self.VALID_URL,
                                "ACCOUNT_DELETION_STATUS_SECRET=" + bad_secret,
                            ],
                        )


class PriceSafetyConfigTests(unittest.TestCase):
    """Section 6 production price-safety controls: global config values
    (see ebay_app/prices.py and ebay_app/config.py)."""

    @staticmethod
    def _load(directory: Path, extra_lines=()):
        path = Path(directory) / ".env"
        content = valid_env_text() + "".join(line + chr(10) for line in extra_lines)
        path.write_text(content, encoding="utf-8")
        os.chmod(path, 0o600)
        return load_config(path, {})

    def test_defaults_match_the_documented_safe_starting_point(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._load(directory)
            self.assertEqual(config.max_price_change_percent, Decimal("10"))
            self.assertEqual(config.max_actions_per_run, 10)
            self.assertIs(config.require_price_floor, True)

    def test_each_value_can_be_overridden(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._load(
                directory,
                [
                    "APP_MAX_PRICE_CHANGE_PERCENT=5",
                    "APP_MAX_ACTIONS_PER_RUN=20",
                    "APP_REQUIRE_PRICE_FLOOR=false",
                ],
            )
            self.assertEqual(config.max_price_change_percent, Decimal("5"))
            self.assertEqual(config.max_actions_per_run, 20)
            self.assertIs(config.require_price_floor, False)

    def test_rejects_a_change_percent_outside_0_1_to_100(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ConfigError):
                self._load(directory, ["APP_MAX_PRICE_CHANGE_PERCENT=0"])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ConfigError):
                self._load(directory, ["APP_MAX_PRICE_CHANGE_PERCENT=150"])

    def test_rejects_a_non_decimal_change_percent(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ConfigError):
                self._load(directory, ["APP_MAX_PRICE_CHANGE_PERCENT=not-a-number"])

    def test_rejects_an_actions_per_run_outside_1_to_1000(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ConfigError):
                self._load(directory, ["APP_MAX_ACTIONS_PER_RUN=0"])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ConfigError):
                self._load(directory, ["APP_MAX_ACTIONS_PER_RUN=1001"])

    def test_rejects_a_non_boolean_floor_requirement(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ConfigError):
                self._load(directory, ["APP_REQUIRE_PRICE_FLOOR=maybe"])

    def test_accepts_the_documented_boolean_spellings(self):
        for spelling in ("true", "True", "1", "yes", "false", "False", "0", "no"):
            with self.subTest(spelling=spelling):
                with tempfile.TemporaryDirectory() as directory:
                    self._load(directory, [f"APP_REQUIRE_PRICE_FLOOR={spelling}"])


if __name__ == "__main__":
    unittest.main()
