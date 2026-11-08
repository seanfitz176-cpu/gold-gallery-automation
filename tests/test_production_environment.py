"""Phase 9, slice 2 (see docs/phase9_production_design.md).

Covers the two small, centrally-applied changes that make the running
application's environment unmistakable once a production ``AppConfig`` ever
reaches it: the ``/health`` endpoint reporting the real environment instead
of a hardcoded "sandbox", and the red "LIVE" banner stamped onto every HTML
page at the single WSGI response chokepoint in ``WebApplication.__call__``.

Also covers ``run_production.py``, the dedicated production entry point:
that it resolves to a sibling file literally named ``.env.production`` (the
same filename ``ebay_app.config`` requires before it will ever treat a
config as production), and that -- exactly like ``run.py`` -- it accepts no
startup arguments.

None of this makes production reachable with real credentials: no test here
supplies anything but synthetic, clearly-fake values, and
``run_production.py`` still only loads a real ``.env.production`` if one is
ever placed beside it, which nothing in this repository does.
"""

from __future__ import annotations

import io
import re
import tempfile
import unittest
import urllib.parse
from dataclasses import replace
from pathlib import Path

from ebay_app.config import (
    CONNECTION_SCOPES,
    PRODUCTION_CONSENT_URL,
    SANDBOX_CONSENT_URL,
)
from ebay_app.ebay_client import EbayError
from ebay_app.storage import AppDatabase
from ebay_app.web import WebApplication, _stamp_production_labels

from helpers import make_config


class _FakeEbay:
    def __init__(self) -> None:
        self.mutations = 0

    def authorization_url(self, _session_id: str) -> str:
        return "https://auth.ebay.com/oauth2/authorize?state=test"


class _WebTestCase(unittest.TestCase):
    """Minimal WSGI test harness, mirroring ConnectionStageWebTests."""

    environment = "sandbox"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(
            make_config(Path(self.temp.name)),
            feature_stage="connection",
            scopes=CONNECTION_SCOPES,
            runtime_mode="local",
            environment=self.environment,
            # make_config() always returns a sandbox-shaped AppConfig, and
            # dataclasses.replace() only overrides the fields given here --
            # it does not re-derive consent_url from environment the way
            # the real load_config() does in config.py. Set it explicitly
            # so this harness actually mirrors a real per-environment
            # config instead of silently leaving every "production" test
            # config with the sandbox consent_url.
            consent_url=(
                PRODUCTION_CONSENT_URL
                if self.environment == "production"
                else SANDBOX_CONSENT_URL
            ),
        )
        self.database = AppDatabase(self.config.database_path, self.config.data_encryption_key)
        self.addCleanup(self.database.close)
        self.ebay = _FakeEbay()
        self.app = WebApplication(self.config, self.database, self.ebay)

    def request(self, method, path, *, cookie="", form=None):
        body = urllib.parse.urlencode(form or {}).encode("utf-8")
        captured = {}

        def start_response(status, headers):
            captured["status"] = int(status.split(" ", 1)[0])
            captured["headers"] = dict(headers)

        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "REMOTE_ADDR": "127.0.0.1",
            "HTTP_HOST": "127.0.0.1:5000",
            "HTTP_COOKIE": cookie,
            "wsgi.input": io.BytesIO(body),
            "CONTENT_LENGTH": str(len(body)),
            "CONTENT_TYPE": "application/x-www-form-urlencoded" if method == "POST" else "",
        }
        response_body = b"".join(self.app(environ, start_response)).decode("utf-8")
        return captured["status"], captured["headers"], response_body

    @staticmethod
    def hidden(body, name):
        match = re.search(rf'name="{name}" value="([^"]+)"', body)
        if not match:
            raise AssertionError(f"Missing hidden field {name}")
        return match.group(1)

    def login(self):
        status, headers, body = self.request("GET", "/login")
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        csrf = self.hidden(body, "csrf_token")
        status, headers, _body = self.request(
            "POST",
            "/login",
            cookie=cookie,
            form={"csrf_token": csrf, "password": self.config.admin_password},
        )
        self.assertEqual(status, 303)
        return headers["Set-Cookie"].split(";", 1)[0]


class SandboxEnvironmentIndicatorTests(_WebTestCase):
    environment = "sandbox"

    def test_health_reports_sandbox(self):
        status, _headers, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertIn('"environment":"sandbox"', body)

    def test_no_pages_carry_the_production_banner(self):
        status, _headers, login_body = self.request("GET", "/login")
        self.assertEqual(status, 200)
        self.assertNotIn("env-banner", login_body)
        self.assertNotIn("LIVE", login_body)

        # An error page goes through the exact same WSGI response path.
        # /listings is locked at the "connection" stage this harness uses,
        # so it reliably produces a 404 error_page once authenticated.
        cookie = self.login()
        status, _headers, error_body = self.request("GET", "/listings", cookie=cookie)
        self.assertEqual(status, 404)
        self.assertNotIn("env-banner", error_body)


class ProductionEnvironmentIndicatorTests(_WebTestCase):
    environment = "production"

    def test_health_reports_production(self):
        status, _headers, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertIn('"environment":"production"', body)
        self.assertNotIn("sandbox", body)

    def test_login_page_carries_the_production_banner(self):
        status, _headers, body = self.request("GET", "/login")
        self.assertEqual(status, 200)
        self.assertIn('class="env-banner"', body)
        self.assertIn("LIVE", body)
        self.assertIn("REAL EBAY ACCOUNT", body)
        # The banner must appear inside <body>, before the page's own content.
        self.assertLess(body.index("env-banner"), body.index("<main"))

    def test_error_pages_also_carry_the_production_banner(self):
        # Error pages go through the same __call__ exception handling paths,
        # not the ordinary _dispatch success path -- the banner must not be
        # something only "happy path" pages get.
        cookie = self.login()
        status, _headers, body = self.request("GET", "/listings", cookie=cookie)
        self.assertEqual(status, 404)
        self.assertIn('class="env-banner"', body)

    def test_banner_is_never_stamped_onto_non_html_responses(self):
        status, headers, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertNotIn("env-banner", body)
        self.assertEqual(headers["Content-Type"], "application/json")

        status, headers, body = self.request("GET", "/static/style.css")
        self.assertEqual(status, 200)
        self.assertNotIn("env-banner {", "")  # sanity: css itself is untouched by the stamp
        self.assertNotIn("<div class=\"env-banner\"", body)


class ProductionLabelConsistencyTests(_WebTestCase):
    """Phase 9 follow-up: the three literal "Sandbox" chrome strings that
    ``views.layout()`` writes on every page (title suffix, header label,
    footer line) must not keep calling a real eBay account "Sandbox"
    directly underneath the banner above shouting that it is not. Fixed at
    the same WSGI chokepoint as the banner itself, in ``_stamp_production_labels``.
    """

    environment = "production"

    def test_login_page_says_production_not_sandbox(self):
        status, _headers, body = self.request("GET", "/login")
        self.assertEqual(status, 200)
        # Not a blanket ban on the word "Sandbox" -- the banner itself
        # legitimately says "...not Sandbox" by way of contrast. What must
        # be gone is the chrome that used to assert this page IS Sandbox.
        self.assertNotIn("eBay Sandbox Console", body)
        self.assertNotIn("eBay Sandbox</title>", body)
        self.assertIn("eBay Production Console", body)
        self.assertIn("eBay Production</title>", body)

    def test_footer_says_production_not_sandbox_only(self):
        status, _headers, body = self.request("GET", "/login")
        self.assertEqual(status, 200)
        self.assertNotIn("Sandbox only", body)
        self.assertIn("Production — a real eBay account.", body)

    def test_error_pages_also_say_production(self):
        # Error pages go through __call__'s exception handling, not the
        # ordinary _dispatch success path -- the relabelling must not be
        # something only "happy path" pages get, exactly like the banner.
        # The error message text itself may legitimately mention "Sandbox"
        # (e.g. "locked until a later Sandbox development stage") -- only
        # the page chrome is asserted here, not every occurrence of the word.
        cookie = self.login()
        status, _headers, body = self.request("GET", "/listings", cookie=cookie)
        self.assertEqual(status, 404)
        self.assertNotIn("eBay Sandbox Console", body)
        self.assertNotIn("eBay Sandbox</title>", body)
        self.assertNotIn("Sandbox only", body)
        self.assertIn("eBay Production Console", body)

    def test_relabelling_appears_after_the_banner_not_instead_of_it(self):
        # This is additive to the banner, never a replacement for it.
        status, _headers, body = self.request("GET", "/login")
        self.assertEqual(status, 200)
        self.assertIn('class="env-banner"', body)
        self.assertIn("eBay Production Console", body)


class DashboardPageProductionLabelTests(_WebTestCase):
    """Phase 9 follow-up #2: ``dashboard_page()`` itself (the per-stage
    body rendered at ``/``) has its own, separate set of hardcoded
    "Sandbox" strings -- distinct from the shared ``layout()`` chrome
    covered by ``ProductionLabelConsistencyTests`` above and NOT reached by
    ``_stamp_production_labels``'s fixed-string chokepoint. These must be
    fixed by threading an explicit ``environment`` argument into
    ``dashboard_page()`` itself, from ``WebApplication.__call__``'s call site.
    """

    environment = "production"

    def test_listings_stage_title_and_account_link_say_production(self):
        cookie = self.login()
        self.app.config = replace(self.config, feature_stage="listings")
        status, _headers, body = self.request("GET", "/", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Active Production listings", body)
        self.assertNotIn("Active Sandbox listings", body)

    def test_connection_stage_says_production_throughout(self):
        cookie = self.login()
        self.app.config = replace(self.config, feature_stage="connection")
        status, _headers, body = self.request("GET", "/", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Production connection", body)
        self.assertIn("Authorize eBay Production", body)
        self.assertIn("Start eBay's Production consent flow", body)
        self.assertNotIn("Sandbox connection", body)
        self.assertNotIn("Authorize eBay Sandbox", body)

    def test_prices_stage_title_and_boundary_text_say_production(self):
        cookie = self.login()
        self.app.config = replace(self.config, feature_stage="prices")
        status, _headers, body = self.request("GET", "/", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Edit a Production listing price", body)
        self.assertIn("already-published Production Offer", body)
        self.assertNotIn("Sandbox listing price", body)
        self.assertNotIn("Sandbox Offer", body)

    def test_account_stage_view_link_says_production(self):
        cookie = self.login()
        self.app.config = replace(self.config, feature_stage="account")
        status, _headers, body = self.request("GET", "/", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Production account", body)
        self.assertNotIn("Sandbox account", body)
        self.assertNotIn("View Sandbox account information", body)


class SandboxLabelUnchangedTests(_WebTestCase):
    """Sandbox pages must keep saying "Sandbox" exactly as before -- the
    relabelling in ``_stamp_production_labels`` only ever runs for
    ``environment == "production"`` (see ``WebApplication.__call__``), so
    nothing here should ever be exercised against a Sandbox config.
    """

    environment = "sandbox"

    def test_login_page_still_says_sandbox(self):
        status, _headers, body = self.request("GET", "/login")
        self.assertEqual(status, 200)
        self.assertIn("eBay Sandbox Console", body)
        self.assertIn("Sandbox only", body)
        self.assertIn("eBay Sandbox</title>", body)
        self.assertNotIn("eBay Production Console", body)

    def test_dashboard_page_listings_stage_still_says_sandbox(self):
        cookie = self.login()
        self.app.config = replace(self.config, feature_stage="listings")
        status, _headers, body = self.request("GET", "/", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Active Sandbox listings", body)
        self.assertNotIn("Active Production listings", body)


class StampProductionLabelsPureFunctionTests(unittest.TestCase):
    """Direct coverage of ``_stamp_production_labels``'s own contract,
    independent of the full WSGI stack: read-only-on-failure, and it must
    never touch a page it has no anchor for.
    """

    def test_replaces_all_three_sandbox_anchors(self):
        from ebay_app.views import layout

        body = layout(
            "Dashboard", "<p>hi</p>", authenticated=False, csrf_token="x"
        ).encode("utf-8")
        stamped = _stamp_production_labels(body).decode("utf-8")
        self.assertNotIn("eBay Sandbox Console", stamped)
        self.assertNotIn("eBay Sandbox</title>", stamped)
        self.assertNotIn("Sandbox only", stamped)
        self.assertIn("eBay Production Console", stamped)
        self.assertIn("eBay Production</title>", stamped)
        self.assertIn("Production — a real eBay account.", stamped)

    def test_a_body_with_none_of_the_anchors_is_returned_unchanged(self):
        body = b"<html><body>not a views.layout() page at all</body></html>"
        self.assertEqual(_stamp_production_labels(body), body)

    def test_each_anchor_is_replaced_at_most_once(self):
        from ebay_app.views import layout

        # A plain layout() page (no body content of its own mentioning
        # "Sandbox") has the three chrome anchors and nothing else -- after
        # stamping, none of the three specific phrases should remain.
        body = layout(
            "Dashboard", "<p>no sandbox mentions here</p>", authenticated=False, csrf_token="x"
        ).encode("utf-8")
        stamped = _stamp_production_labels(body)
        self.assertEqual(stamped.count(b"eBay Sandbox Console"), 0)
        self.assertEqual(stamped.count(b"Sandbox only"), 0)
        self.assertNotIn(b"eBay Sandbox</title>", stamped)


class AuthorizationUrlValidationTests(_WebTestCase):
    """Regression coverage for the real-world bug where /connect in a
    production AppConfig always raised "The generated Sandbox authorization
    destination is not allowed" -- _validate_sandbox_authorization_url (as
    it was then named) hardcoded auth.sandbox.ebay.com as the only accepted
    host, so it rejected every production authorization URL even though
    config.py had already resolved the correct auth.ebay.com consent_url for
    that environment. Fixed by deriving the expected host/path from
    self.config.consent_url instead of a hardcoded sandbox-only literal.
    """

    environment = "production"

    def test_connect_succeeds_in_production_with_the_real_production_host(self):
        # _FakeEbay.authorization_url returns an auth.ebay.com URL -- exactly
        # what EbaySandboxClient.authorization_url produces for a real
        # production AppConfig (consent_url resolves to PRODUCTION_CONSENT_URL
        # in config.py). Before the fix, this request always failed closed
        # with INVALID_AUTHORIZATION_URL even though the URL was correct.
        cookie = self.login()
        status, _headers, body = self.request("GET", "/", cookie=cookie)
        self.assertEqual(status, 200)
        csrf = self.hidden(body, "csrf_token")
        status, _headers, body = self.request(
            "POST", "/connect", cookie=cookie, form={"csrf_token": csrf}
        )
        self.assertEqual(status, 200)
        self.assertIn("https://auth.ebay.com/oauth2/authorize", body)
        self.assertNotIn("not allowed", body)
        self.assertNotIn("is invalid", body)

    def test_validate_authorization_url_accepts_the_real_production_host(self):
        app = self.app
        url = "https://auth.ebay.com/oauth2/authorize?state=abc"
        self.assertEqual(app._validate_authorization_url(url), url)

    def test_validate_authorization_url_rejects_the_sandbox_host(self):
        app = self.app
        with self.assertRaises(EbayError) as ctx:
            app._validate_authorization_url(
                "https://auth.sandbox.ebay.com/oauth2/authorize?state=abc"
            )
        self.assertEqual(ctx.exception.code, "INVALID_AUTHORIZATION_URL")

    def test_validate_authorization_url_rejects_wrong_scheme_and_path(self):
        app = self.app
        with self.assertRaises(EbayError):
            app._validate_authorization_url("http://auth.ebay.com/oauth2/authorize")
        with self.assertRaises(EbayError):
            app._validate_authorization_url("https://auth.ebay.com/wrong/path")


class SandboxAuthorizationUrlValidationTests(_WebTestCase):
    environment = "sandbox"

    def test_validate_authorization_url_accepts_the_real_sandbox_host(self):
        app = self.app
        url = "https://auth.sandbox.ebay.com/oauth2/authorize?state=abc"
        self.assertEqual(app._validate_authorization_url(url), url)

    def test_validate_authorization_url_rejects_the_production_host(self):
        app = self.app
        with self.assertRaises(EbayError) as ctx:
            app._validate_authorization_url("https://auth.ebay.com/oauth2/authorize?state=abc")
        self.assertEqual(ctx.exception.code, "INVALID_AUTHORIZATION_URL")


class RunProductionEntryPointTests(unittest.TestCase):
    def test_resolves_to_a_sibling_file_literally_named_env_production(self):
        import run_production

        self.assertEqual(run_production.PRODUCTION_ENV_PATH.name, ".env.production")
        self.assertEqual(
            run_production.PRODUCTION_ENV_PATH.parent,
            Path(run_production.__file__).resolve().parent,
        )

    def test_shares_the_same_repo_root_as_run_py(self):
        import run
        import run_production

        default_sandbox_path = Path(run.__file__).resolve().parent / ".env"
        self.assertEqual(
            run_production.PRODUCTION_ENV_PATH.parent, default_sandbox_path.parent
        )
        self.assertNotEqual(run_production.PRODUCTION_ENV_PATH, default_sandbox_path)

    def test_rejects_any_startup_argument_exactly_like_run_py(self):
        import run_production

        self.assertEqual(run_production.main(["--anything"]), 1)

    def test_refuses_to_start_with_no_env_production_file_present(self):
        import run_production

        # In this test environment there is no real .env.production beside
        # run_production.py, so this must fail closed (return 1), never
        # start a server, and never raise an unhandled exception.
        self.assertEqual(run_production.main(), 1)


class CapabilityDescriptionTests(unittest.TestCase):
    """run_production.py's startup log line must always state the real,
    current write-capability state plainly -- see the module docstring and
    the Section 6 kill switch (AppConfig.dry_run, ebay_app/config.py). Kept
    as its own pure function specifically so this has direct coverage
    without needing to start a real server (see the entry-point tests
    above, which never reach the server-start line at all)."""

    def test_every_read_only_stage_says_read_only(self):
        import run_production

        with tempfile.TemporaryDirectory() as directory:
            for stage in ("connection", "account", "listings", "orders"):
                with self.subTest(stage=stage):
                    config = replace(
                        make_config(Path(directory)), feature_stage=stage, dry_run=False
                    )
                    self.assertEqual(
                        run_production._capability_description(config), "read-only"
                    )

    def test_prices_with_dry_run_says_dry_run_and_no_real_write(self):
        import run_production

        with tempfile.TemporaryDirectory() as directory:
            config = replace(
                make_config(Path(directory)), feature_stage="prices", dry_run=True
            )
            description = run_production._capability_description(config)
            self.assertIn("DRY RUN", description)
            self.assertIn("no real eBay write will be made", description)

    def test_prices_without_dry_run_says_live_writes_unmistakably(self):
        import run_production

        with tempfile.TemporaryDirectory() as directory:
            config = replace(
                make_config(Path(directory)), feature_stage="prices", dry_run=False
            )
            description = run_production._capability_description(config)
            self.assertIn("LIVE WRITES", description)
            self.assertIn("WILL be made", description)

    def test_quantities_and_images_capability_descriptions_match_prices_shape(self):
        # quantities/images now have their own structural production gate
        # (see PRODUCTION_WRITE_OPT_INS, ebay_app/config.py) -- this pins that
        # _capability_description reports each one plainly too, the same way
        # it already did for "prices".
        import run_production

        for stage in ("quantities", "images"):
            with self.subTest(stage=stage, dry_run=True):
                with tempfile.TemporaryDirectory() as directory:
                    config = replace(
                        make_config(Path(directory)), feature_stage=stage, dry_run=True
                    )
                    description = run_production._capability_description(config)
                    self.assertIn("DRY RUN", description)
                    self.assertIn("no real eBay write will be made", description)
                    self.assertIn(stage.upper(), description)
            with self.subTest(stage=stage, dry_run=False):
                with tempfile.TemporaryDirectory() as directory:
                    config = replace(
                        make_config(Path(directory)), feature_stage=stage, dry_run=False
                    )
                    description = run_production._capability_description(config)
                    self.assertIn("LIVE WRITES", description)
                    self.assertIn("WILL be made", description)
                    self.assertIn(stage.upper(), description)

    def test_migration_capability_description_never_claims_a_dry_run(self):
        # migrate_listing() has no dry-run branch at all -- unlike every
        # other write-capable stage, APP_DRY_RUN has no effect on it. The
        # startup message must say so plainly, in both directions of
        # config.dry_run, rather than ever implying a dry run applies here.
        import run_production

        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                with tempfile.TemporaryDirectory() as directory:
                    config = replace(
                        make_config(Path(directory)),
                        feature_stage="migration",
                        dry_run=dry_run,
                    )
                    description = run_production._capability_description(config)
                    self.assertIn("MIGRATION", description)
                    self.assertIn("NO DRY RUN", description)
                    self.assertIn("always makes a real eBay write", description)
                    self.assertNotIn("DRY RUN, no real eBay write will be made", description)


class ViewFunctionProductionLabelTests(unittest.TestCase):
    """Phase 9 follow-up #3: the remaining per-page view functions outside
    ``dashboard_page()`` and the shared ``layout()`` chrome -- the OAuth
    hand-off page, the privacy notice, and the draft/price/quantity/image
    review and confirmation pages -- each took their own hardcoded "Sandbox"
    text and needed their own ``environment`` argument threaded in from
    ``WebApplication``. These are pure functions, so covered directly
    without the WSGI harness.
    """

    def test_oauth_handoff_page_says_production_not_sandbox(self):
        from ebay_app.views import oauth_handoff_page

        rendered = oauth_handoff_page(
            "https://auth.ebay.com/authorize?state=x",
            "csrf-token",
            "listings",
            "Production",
        )
        self.assertIn("Continue to eBay Production", rendered)
        self.assertIn("read-only access to the Production user", rendered)
        self.assertNotIn("Ready to open eBay Sandbox", rendered)
        self.assertNotIn("Sandbox user's identity", rendered)
        self.assertNotIn("Continue to eBay Sandbox", rendered)

    def test_privacy_page_says_production_not_sandbox(self):
        from ebay_app.views import privacy_page

        rendered = privacy_page("Production")
        self.assertIn("private eBay Production development tool", rendered)
        self.assertIn("Production user's identity", rendered)
        self.assertIn("unpublished Offer in the Production", rendered)
        self.assertNotIn("private eBay Sandbox development tool", rendered)
        self.assertNotIn("Sandbox user's identity", rendered)

    def test_draft_review_page_says_production_not_sandbox(self):
        from ebay_app.views import draft_review_page

        draft = {
            "sku": "ITEM-PRD-ABCDEF123456",
            "title": "Test item",
            "condition": "NEW",
            "category_id": "12345",
            "quantity": 1,
            "currency": "GBP",
            "price": "10.00",
            "description": "A test item",
            "aspects": {},
        }
        rendered = draft_review_page(
            "token", draft, "csrf-token", {"environment": "Production"}, "Production"
        )
        self.assertIn("two persistent Production records", rendered)
        self.assertIn("Production suffix", rendered)
        self.assertIn("Create unpublished Production offer", rendered)
        self.assertNotIn("persistent Sandbox records", rendered)
        self.assertNotIn("Sandbox suffix", rendered)
        self.assertNotIn("Create unpublished Sandbox offer", rendered)

    def test_draft_action_page_says_production_not_sandbox(self):
        from ebay_app.views import draft_action_page

        action = {"status": "succeeded", "result": {}}
        rendered = draft_action_page(action, [], "csrf-token", "Production")
        self.assertIn("Unpublished Production offer created and verified", rendered)
        self.assertIn("Unpublished Production offer result", rendered)
        self.assertNotIn("Unpublished Sandbox offer created", rendered)
        self.assertNotIn("Unpublished Sandbox offer result", rendered)

    def test_price_action_page_says_production_not_sandbox(self):
        from ebay_app.views import price_action_page

        action = {"status": "succeeded", "result": {}}
        rendered = price_action_page(action, "csrf-token", "Production")
        self.assertIn("The Production Offer price was changed and confirmed", rendered)
        self.assertIn("Production price change result", rendered)
        self.assertNotIn("The Sandbox Offer price was changed", rendered)
        self.assertNotIn("Sandbox price change result", rendered)

    def test_quantity_action_page_says_production_not_sandbox(self):
        from ebay_app.views import quantity_action_page

        action = {"status": "succeeded", "result": {}}
        rendered = quantity_action_page(action, "csrf-token", "Production")
        self.assertIn("The Production Offer quantity was changed and confirmed", rendered)
        self.assertIn("Production quantity change result", rendered)
        self.assertNotIn("The Sandbox Offer quantity was changed", rendered)
        self.assertNotIn("Sandbox quantity change result", rendered)

    def test_image_action_page_says_production_not_sandbox(self):
        from ebay_app.views import image_action_page

        action = {"status": "succeeded", "result": {}}
        rendered = image_action_page(action, "csrf-token", "Production")
        self.assertIn("The Production listing image was added and confirmed", rendered)
        self.assertIn("Production image addition result", rendered)
        self.assertNotIn("The Sandbox listing image was added", rendered)
        self.assertNotIn("Sandbox image addition result", rendered)

    def test_oauth_handoff_page_describes_the_real_scope_for_every_write_stage(self):
        """Regression test for a real bug found 2026-11-06 (the fifth
        instance of the same underlying class of bug found while debugging
        the migration stage's first live use): oauth_handoff_page() had its
        own separate, independently-hardcoded if/elif chain by feature_stage
        that never had entries for prices/quantities/images/migration, so
        all four silently fell into the generic ``else`` branch and told the
        user "read-only access to the Production user's identity... No
        listing, order, price or inventory API is enabled" -- even though
        each of these stages actually requests DRAFT_SCOPES (Inventory
        write) and is about to ask eBay for exactly that. The underlying
        OAuth request was never wrong (authorization_url() always encodes
        config.scopes correctly) -- only the on-screen explanation shown to
        the owner immediately before they click through to eBay's real
        consent screen was misleading."""
        from ebay_app.views import oauth_handoff_page

        for stage in ("prices", "quantities", "images", "migration"):
            with self.subTest(stage=stage):
                rendered = oauth_handoff_page(
                    "https://auth.ebay.com/authorize?state=x",
                    "csrf-token",
                    stage,
                    "Production",
                )
                self.assertIn("Inventory permission", rendered)
                self.assertNotIn(
                    "No listing, order, price or inventory API is enabled",
                    rendered,
                )
                self.assertNotIn(
                    "read-only access to the Production user's identity</div>",
                    rendered,
                )

    def test_defaults_are_unchanged_and_still_say_sandbox(self):
        # None of the above functions must flip their public default away
        # from "Sandbox" -- every existing caller that omits the new
        # ``environment`` argument (tests and any dormant code path alike)
        # must keep behaving exactly as before.
        from ebay_app.views import (
            draft_action_page,
            image_action_page,
            oauth_handoff_page,
            price_action_page,
            privacy_page,
            quantity_action_page,
        )

        self.assertIn("Sandbox", privacy_page())
        self.assertIn(
            "Sandbox",
            oauth_handoff_page(
                "https://auth.ebay.com/authorize?state=x", "csrf-token", "listings"
            ),
        )
        action = {"status": "succeeded", "result": {}}
        self.assertIn("Sandbox", draft_action_page(action, [], "csrf-token"))
        self.assertIn("Sandbox", price_action_page(action, "csrf-token"))
        self.assertIn("Sandbox", quantity_action_page(action, "csrf-token"))
        self.assertIn("Sandbox", image_action_page(action, "csrf-token"))


if __name__ == "__main__":
    unittest.main()
