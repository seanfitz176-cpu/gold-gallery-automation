from __future__ import annotations

import hmac
import logging
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .config import (
    AppConfig,
    CANONICAL_HOST,
    CANONICAL_PORT,
    DRAFT_SCOPES,
    LISTING_SCOPES,
    ORDER_SCOPES,
    SETUP_SCOPES,
    granted_scopes_cover,
)
from .account import AccountService
from .account_deletion import maybe_poll
from .connection import ConnectionVerifier
from .drafts import DraftService
from .prices import PriceService
from .quantities import QuantityService
from .images import ImageService
from .ebay_client import (
    DraftPartialError,
    EbayError,
    EbaySandboxClient,
    PriceChangedError,
    SkuExistsError,
)
from .sessions import Session, SessionManager
from .password_hash import verify_password
from .listings import ListingService
from .orders import OrderService
from .storage import AppDatabase, ConnectionChangedStorageError
from .validation import (
    ValidationError,
    display_form_values,
    validate_draft,
    validate_new_price,
    validate_price_limit_form,
)
from .views import (
    action_page,
    account_deletion_banner_page,
    account_page,
    active_listings_page,
    dashboard_page,
    draft_form_page,
    draft_review_page,
    draft_action_page,
    error_page,
    listings_page,
    login_page,
    oauth_handoff_page,
    recent_orders_page,
    privacy_page,
    price_limits_page,
    price_review_page,
    price_action_page,
    quantity_review_page,
    quantity_action_page,
    image_review_page,
    image_action_page,
    engine_page,
)

ENGINE_ROUTES = frozenset({"/engine", "/engine/pause", "/engine/resume", "/engine/run",
                           "/engine/approve", "/engine/reject"})


class RequestError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


OAUTH_CALLBACK_KEYS = frozenset({"state", "code", "error"})


@dataclass
class Response:
    body: bytes = b""
    status: int = 200
    content_type: str = "text/html; charset=utf-8"
    headers: List[Tuple[str, str]] = field(default_factory=list)
    session: Optional[Session] = None
    clear_cookie: bool = False

    @classmethod
    def html(cls, value: str, status: int = 200) -> "Response":
        return cls(value.encode("utf-8"), status)

    @classmethod
    def redirect(cls, location: str, status: int = 303) -> "Response":
        return cls(b"", status, headers=[("Location", location)])


# Phase 9 (see docs/phase9_production_design.md): every page shares exactly
# one HTML shell, ``views.layout()``, so the unmissable environment banner is
# stamped on at this single point -- the WSGI response chokepoint in
# ``WebApplication.__call__`` -- rather than threaded through every one of
# the ~20 page-rendering functions and their call sites. This runs on every
# response, including error pages, with no way for a new page to forget it.
_PRODUCTION_BANNER_TEXT = (
    "\u26a0 LIVE \u2014 REAL EBAY ACCOUNT: actions here affect a real eBay "
    "seller account, not Sandbox."
)
_PRODUCTION_BANNER_HTML = (
    '<div class="env-banner" role="alert">' + _PRODUCTION_BANNER_TEXT + "</div>"
).encode("utf-8")


def _stamp_production_banner(body: bytes) -> bytes:
    """Insert the production banner into an already-rendered HTML page.

    A pure, read-only-on-failure transform: every page produced by
    ``views.layout()`` contains the literal anchor below exactly once, so
    this either inserts the banner right after it or, if the anchor is
    somehow missing (a malformed or non-``layout()`` body), returns the
    body completely unchanged -- a missing banner is a visible cosmetic
    gap, never a reason to corrupt or fail the actual response.
    """

    anchor = b"<body>"
    if anchor not in body:
        return body
    return body.replace(anchor, anchor + _PRODUCTION_BANNER_HTML, 1)


# ``views.layout()`` always writes the literal words "Sandbox" (title
# suffix, header label, footer) because that constant string is correct
# for every page in the only environment that existed when it was written.
# Now that Production exists too, those three spots would keep calling a
# real eBay account "Sandbox" right underneath the banner above shouting
# that it is not -- an active contradiction on the same page, not just a
# missing label. Fixed at the same chokepoint as the banner, for the same
# reason given above: one point that can't be forgotten, instead of three
# edits repeated across ~20 page functions and their call sites.
_SANDBOX_TITLE_SUFFIX = " \u00b7 eBay Sandbox</title>".encode("utf-8")
_PRODUCTION_TITLE_SUFFIX = " \u00b7 eBay Production</title>".encode("utf-8")

_SANDBOX_HEADER_LABEL = b"<strong>eBay Sandbox Console</strong>"
_PRODUCTION_HEADER_LABEL = b"<strong>eBay Production Console</strong>"

_SANDBOX_FOOTER_TEXT = (
    "Sandbox only \u00b7 Every eBay change requires a separate confirmation."
).encode("utf-8")
_PRODUCTION_FOOTER_TEXT = (
    "<strong>Production \u2014 a real eBay account.</strong> Every eBay "
    "change requires a separate confirmation."
).encode("utf-8")


def _stamp_production_labels(body: bytes) -> bytes:
    """Swap the three literal "Sandbox" labels in an already-rendered page.

    Same pure, read-only-on-failure contract as ``_stamp_production_banner``:
    each anchor is replaced only if present exactly as expected, and a
    missing anchor leaves that spot untouched rather than guessed at or
    failing the response. Whether the one real write-capable stage is in
    dry-run or not is intentionally not repeated here -- ``price_action_page``
    already states that plainly, right where a write is actually confirmed,
    and this chokepoint only needs to stop asserting "Sandbox" on a page
    that is not.
    """

    if _SANDBOX_TITLE_SUFFIX in body:
        body = body.replace(_SANDBOX_TITLE_SUFFIX, _PRODUCTION_TITLE_SUFFIX, 1)
    if _SANDBOX_HEADER_LABEL in body:
        body = body.replace(_SANDBOX_HEADER_LABEL, _PRODUCTION_HEADER_LABEL, 1)
    if _SANDBOX_FOOTER_TEXT in body:
        body = body.replace(_SANDBOX_FOOTER_TEXT, _PRODUCTION_FOOTER_TEXT, 1)
    return body


class Request:
    MAX_BODY_BYTES = 128 * 1024
    MAX_QUERY_CHARS = 8 * 1024

    def __init__(self, environ: Mapping[str, object]) -> None:
        self.environ = environ
        self.method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        self.path = str(environ.get("PATH_INFO", "/"))
        query_string = str(environ.get("QUERY_STRING", ""))
        if len(self.path) > 2048 or len(query_string) > self.MAX_QUERY_CHARS:
            raise RequestError("Request target is too large", 414)
        try:
            self.query = urllib.parse.parse_qs(
                query_string,
                keep_blank_values=True,
                max_num_fields=30,
            )
        except ValueError as exc:
            raise RequestError("Invalid query string") from exc
        self.remote_addr = str(environ.get("REMOTE_ADDR", "unknown"))
        self.cookie = str(environ.get("HTTP_COOKIE", ""))
        self.host_header = str(environ.get("HTTP_HOST", ""))
        self._form: Optional[Dict[str, List[str]]] = None

    @property
    def form(self) -> Dict[str, List[str]]:
        if self._form is not None:
            return self._form
        content_type = str(self.environ.get("CONTENT_TYPE", "")).split(";", 1)[0].lower()
        if content_type != "application/x-www-form-urlencoded":
            raise RequestError("Forms must use application/x-www-form-urlencoded")
        try:
            length = int(str(self.environ.get("CONTENT_LENGTH", "0") or "0"))
        except ValueError as exc:
            raise RequestError("Invalid Content-Length") from exc
        if length < 0 or length > self.MAX_BODY_BYTES:
            raise RequestError("Request body is too large", 413)
        stream = self.environ.get("wsgi.input")
        raw = stream.read(length) if stream and hasattr(stream, "read") else b""
        try:
            self._form = urllib.parse.parse_qs(
                raw.decode("utf-8"), keep_blank_values=True, max_num_fields=30
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise RequestError("Invalid form body") from exc
        return self._form

    @staticmethod
    def one(values: Mapping[str, List[str]], name: str, required: bool = True) -> str:
        found = values.get(name, [])
        if len(found) > 1:
            raise RequestError(f"{name} may only be supplied once")
        value = found[0] if found else ""
        if required and not value:
            raise RequestError(f"{name} is required")
        return value


class LoginFailureLimiter:
    def __init__(self, now=time.time) -> None:
        self.now = now
        self._entries: Dict[str, Tuple[int, float]] = {}
        self._lock = threading.Lock()

    def record_failure(self, key: str) -> None:
        with self._lock:
            count, reset = self._entries.get(key, (0, self.now() + 300))
            if reset <= self.now():
                count, reset = 0, self.now() + 300
            count += 1
            self._entries[key] = (count, reset)
            if count > 5:
                raise RequestError("Too many failed sign-in attempts; wait five minutes", 429)

    def clear(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)


class WebApplication:
    def __init__(
        self,
        config: AppConfig,
        database: AppDatabase,
        ebay: EbaySandboxClient,
        connection_verifier: Optional[ConnectionVerifier] = None,
        account_service: Optional[AccountService] = None,
        listing_service: Optional[ListingService] = None,
        order_service: Optional[OrderService] = None,
        draft_service: Optional[DraftService] = None,
        price_service: Optional[PriceService] = None,
        quantity_service: Optional[QuantityService] = None,
        image_service: Optional[ImageService] = None,
        sessions: Optional[SessionManager] = None,
        logger: Optional[logging.Logger] = None,
        engine: Optional[Any] = None,
    ) -> None:
        if (
            config.host != CANONICAL_HOST
            or config.port != CANONICAL_PORT
            or config.allowed_hosts != (CANONICAL_HOST,)
            or config.cookie_secure
            or config.runtime_mode != "local"
        ):
            raise ValueError(
                "WebApplication requires the permanent "
                "127.0.0.1:5000 local OAuth configuration"
            )
        self.config = config
        self.database = database
        self.ebay = ebay
        self.connection_verifier = connection_verifier or ConnectionVerifier(
            ebay, database, environment=config.environment.capitalize()
        )
        self.account_service = account_service or AccountService(
            ebay, database, environment=config.environment.capitalize()
        )
        self.listing_service = listing_service or ListingService(
            ebay, database, environment=config.environment.capitalize()
        )
        self.order_service = order_service or OrderService(
            ebay,
            database,
            config.order_lookback_days,
            environment=config.environment.capitalize(),
        )
        self.draft_service = draft_service or DraftService(ebay, database, config)
        self.price_service = price_service or PriceService(ebay, database, config)
        self.quantity_service = quantity_service or QuantityService(ebay, database, config)
        self.image_service = image_service or ImageService(ebay, database, config)
        self.sessions = sessions or SessionManager(config.cookie_secure)
        self.logger = logger or logging.getLogger("ebay_sandbox_app")
        self.login_limiter = LoginFailureLimiter()
        # The autonomy engine (ebay_app/engine.py), when APP_ENGINE_MODE is not off.
        self.engine = engine
        self.style = (Path(__file__).resolve().parents[1] / "static" / "style.css").read_bytes()

    def __call__(self, environ, start_response):
        session, _new_session = self.sessions.get(str(environ.get("HTTP_COOKIE", "")))
        try:
            request = Request(environ)
            self._validate_host(request.host_header)
            response = self._dispatch(request, session)
        except RequestError as error:
            response = Response.html(
                error_page("Invalid request", str(error), session.csrf_token, session.authenticated),
                error.status,
            )
        except EbayError as error:
            self.logger.warning("eBay request failed: code=%s status=%s", error.code, error.status)
            status = error.status if error.status in {400, 401, 403, 404, 409, 429} else 502
            response = Response.html(
                error_page(
                    f"eBay {self.config.environment.capitalize()} error",
                    str(error),
                    session.csrf_token,
                    session.authenticated,
                ),
                status,
            )
        except Exception:
            self.logger.exception("Unhandled application error")
            response = Response.html(
                error_page(
                    "Application error",
                    "The request could not be completed. No automatic eBay retry was made.",
                    session.csrf_token,
                    session.authenticated,
                ),
                500,
            )

        if self.config.environment == "production" and response.content_type.startswith(
            "text/html"
        ):
            response.body = _stamp_production_banner(response.body)
            response.body = _stamp_production_labels(response.body)

        effective_session = response.session or session
        headers = list(response.headers)
        headers.extend(
            [
                ("Content-Type", response.content_type),
                ("Content-Length", str(len(response.body))),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
                ("Referrer-Policy", "no-referrer"),
                ("X-Frame-Options", "DENY"),
                (
                    "Content-Security-Policy",
                    "default-src 'none'; style-src 'self'; img-src https:; "
                    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
                ),
                (
                    "Permissions-Policy",
                    "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
                ),
            ]
        )
        headers.append(
            ("Set-Cookie", self.sessions.cookie_header(effective_session, response.clear_cookie))
        )
        start_response(f"{response.status} {HTTPStatus(response.status).phrase}", headers)
        return [response.body]

    def _validate_host(self, host_header: str) -> None:
        if len(host_header) > 255:
            raise RequestError("Invalid Host header", 400)
        try:
            parsed = urllib.parse.urlsplit(f"//{host_header}")
            port = parsed.port
        except ValueError as exc:
            raise RequestError("Invalid Host header", 400) from exc
        if (
            parsed.hostname != CANONICAL_HOST
            or port != CANONICAL_PORT
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise RequestError("Host header is not allowed", 400)

    @staticmethod
    def _check_csrf(request: Request, session: Session) -> None:
        token = Request.one(request.form, "csrf_token")
        if not hmac.compare_digest(token, session.csrf_token):
            raise RequestError("The form expired or failed CSRF validation", 403)

    @staticmethod
    def _require_auth(session: Session) -> None:
        if not session.authenticated:
            raise RequestError("Sign in to use this application", 401)

    def _validate_authorization_url(self, value: str) -> str:
        # The expected host depends on which environment this config is for
        # (auth.sandbox.ebay.com vs auth.ebay.com) -- derived from the same
        # consent_url the config itself already resolved per-environment in
        # config.py, so this never needs its own hardcoded sandbox/production
        # branching and can never drift out of sync with it.
        expected = urllib.parse.urlsplit(self.config.consent_url)
        try:
            parsed = urllib.parse.urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise EbayError(
                "The generated authorization destination is invalid",
                code="INVALID_AUTHORIZATION_URL",
            ) from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname != expected.hostname
            or port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != expected.path
            or parsed.fragment
        ):
            raise EbayError(
                "The generated authorization destination is not allowed",
                code="INVALID_AUTHORIZATION_URL",
            )
        return value

    def _dispatch(self, request: Request, session: Session) -> Response:
        if request.remote_addr not in {"127.0.0.1", "::1"}:
            raise RequestError("This local application accepts only this computer", 403)
        if request.path == "/health" and request.method == "GET":
            health_body = ('{"status":"alive","environment":"%s"}' % self.config.environment).encode(
                "ascii"
            )
            return Response(health_body, 200, "application/json")
        if request.path == "/static/style.css" and request.method == "GET":
            return Response(self.style, 200, "text/css; charset=utf-8")
        if request.path == "/privacy" and request.method == "GET":
            return Response.html(privacy_page(self.config.environment.capitalize()))
        if request.path == "/login":
            return self._login(request, session)
        if request.path == "/oauth/callback" and request.method == "GET":
            return self._oauth_callback(request, session)

        if not session.authenticated:
            return Response.redirect("/login")

        try:
            maybe_poll(self.config, self.ebay, self.database, logger=self.logger)
        except Exception:
            # A failed or skipped poll must never block ordinary use of the
            # application; see ebay_app/account_deletion.py for the actual
            # (always internally-caught) error handling this is a backstop for.
            self.logger.exception("Unexpected error while polling for account-deletion notifications")

        deletion_state = self.database.get_deletion_state()
        if deletion_state.get("detected_at") is not None:
            if request.path == "/account-deletion":
                return self._account_deletion(request, session, deletion_state)
            if request.path != "/logout":
                return Response.html(
                    account_deletion_banner_page(deletion_state, session.csrf_token)
                )

        stage_routes = {
            "connection": {"/", "/connect", "/connection/verify", "/logout"},
            "account": {"/", "/account", "/connect", "/connection/verify", "/logout"},
            "listings": {
                "/",
                "/account",
                "/listings",
                "/connect",
                "/connection/verify",
                "/logout",
            },
            "orders": {
                "/",
                "/account",
                "/listings",
                "/orders",
                "/connect",
                "/connection/verify",
                "/logout",
            },
            "drafts": {
                "/",
                "/account",
                "/listings",
                "/orders",
                "/drafts/new",
                "/drafts/review",
                "/actions/confirm",
                "/actions/cancel",
                "/connect",
                "/connection/verify",
                "/logout",
            },
            "prices": {
                "/",
                "/account",
                "/listings",
                "/orders",
                "/drafts/new",
                "/drafts/review",
                "/prices/review",
                "/price-limits",
                "/price-limits/delete",
                "/actions/confirm",
                "/actions/cancel",
                "/connect",
                "/connection/verify",
                "/logout",
            },
            "quantities": {
                "/",
                "/account",
                "/listings",
                "/orders",
                "/drafts/new",
                "/drafts/review",
                "/prices/review",
                "/quantities/review",
                "/price-limits",
                "/price-limits/delete",
                "/actions/confirm",
                "/actions/cancel",
                "/connect",
                "/connection/verify",
                "/logout",
            },
            "images": {
                "/",
                "/account",
                "/listings",
                "/orders",
                "/drafts/new",
                "/drafts/review",
                "/prices/review",
                "/quantities/review",
                "/images/review",
                "/price-limits",
                "/price-limits/delete",
                "/actions/confirm",
                "/actions/cancel",
                "/connect",
                "/connection/verify",
                "/logout",
            },
            "setup": {"/", "/connect", "/connection/verify", "/logout"},
            # Migration has no dashboard UI of its own -- the actual write
            # happens through the standalone migrate_listing.py script, not
            # through any route here. This stage exists in the dashboard
            # only so the owner can sign in and reconnect OAuth for the
            # broader write/Inventory scope migration needs, exactly like
            # "setup" above.
            "migration": {"/", "/connect", "/connection/verify", "/logout"},
            # Publish, like migration, happens only through a standalone
            # script (bulk_publish_listings.py); the dashboard exists for
            # sign-in and the OAuth permission upgrade only.
            "publish": {"/", "/connect", "/connection/verify", "/logout"},
        }
        # ``full`` is retained only for the dormant later-stage unit tests. The
        # environment loader rejects it, so a real process can run only an
        # explicitly allowlisted current stage.
        dynamic_draft_result = (
            self.config.feature_stage in {"drafts", "prices", "quantities", "images"}
            and request.method == "GET"
            and re.fullmatch(r"/actions/[a-f0-9]{32}", request.path) is not None
        )
        engine_route = (
            request.path in ENGINE_ROUTES
            and self.engine is not None
            and self.config.engine_mode != "off"
        )
        if (
            self.config.feature_stage != "full"
            and request.path not in stage_routes.get(self.config.feature_stage, set())
            and not dynamic_draft_result
            and not engine_route
        ):
            raise RequestError(
                f"This feature is locked until a later {self.config.environment.capitalize()} "
                "development stage is enabled",
                404,
            )

        if request.path == "/logout" and request.method == "POST":
            self._check_csrf(request, session)
            self.sessions.destroy(session)
            response = Response.redirect("/login")
            response.clear_cookie = True
            return response
        if request.path == "/" and request.method == "GET":
            flashes = list(session.flashes)
            session.flashes.clear()
            return Response.html(
                dashboard_page(
                    self.database.connection_status(),
                    self.database.recent_actions(),
                    session.csrf_token,
                    flashes,
                    self.config.environment.capitalize(),
                    self.config.feature_stage,
                    self.config.runtime_mode,
                    self.config.feature_stage
                    in {"listings", "orders", "drafts", "prices", "quantities", "images"}
                    and granted_scopes_cover(
                        self.database.granted_scopes(), LISTING_SCOPES
                    ),
                    self.config.feature_stage
                    in {"orders", "drafts", "prices", "quantities", "images"}
                    and granted_scopes_cover(
                        self.database.granted_scopes(), ORDER_SCOPES
                    ),
                    self.config.feature_stage in {"drafts", "prices", "quantities", "images"}
                    and granted_scopes_cover(
                        self.database.granted_scopes(), DRAFT_SCOPES
                    ),
                    self.config.feature_stage == "setup"
                    and granted_scopes_cover(
                        self.database.granted_scopes(), SETUP_SCOPES
                    ),
                    self.config.feature_stage == "prices"
                    and granted_scopes_cover(
                        self.database.granted_scopes(), DRAFT_SCOPES
                    ),
                    self.config.feature_stage == "quantities"
                    and granted_scopes_cover(
                        self.database.granted_scopes(), DRAFT_SCOPES
                    ),
                    self.config.feature_stage == "images"
                    and granted_scopes_cover(
                        self.database.granted_scopes(), DRAFT_SCOPES
                    ),
                    self.config.feature_stage == "migration"
                    and granted_scopes_cover(
                        self.database.granted_scopes(), DRAFT_SCOPES
                    ),
                    self.config.feature_stage == "publish"
                    and granted_scopes_cover(
                        self.database.granted_scopes(), DRAFT_SCOPES
                    ),
                ).replace("</main>", self._engine_dashboard_card() + "</main>", 1)
            )
        if request.path == "/connect" and request.method == "POST":
            self._check_csrf(request, session)
            authorization_url = self._validate_authorization_url(
                self.ebay.authorization_url(session.session_id)
            )
            return Response.html(
                oauth_handoff_page(
                    authorization_url,
                    session.csrf_token,
                    self.config.feature_stage,
                    self.config.environment.capitalize(),
                )
            )
        if request.path == "/connection/verify" and request.method == "POST":
            self._check_csrf(request, session)
            result = self.connection_verifier.verify()
            session.flashes.append(
                (
                    "success",
                    f"{self.config.environment.capitalize()} connection verified at {result.checked_at}.",
                )
            )
            return Response.redirect("/")
        if request.path == "/account" and request.method == "GET":
            summary = self.account_service.retrieve()
            return Response.html(account_page(summary.as_dict(), session.csrf_token))
        if request.path == "/listings" and request.method == "GET":
            if self.config.feature_stage in {"listings", "orders", "drafts"}:
                summary = self.listing_service.retrieve()
                return Response.html(
                    active_listings_page(
                        summary.listing_dicts(),
                        summary.environment,
                        summary.retrieved_at,
                        session.csrf_token,
                    )
                )
            return Response.html(
                listings_page(
                    self.ebay.list_listings(),
                    session.csrf_token,
                    self.config.feature_stage,
                )
            )
        if request.path == "/orders" and request.method == "GET":
            if self.config.feature_stage not in {
                "orders",
                "drafts",
                "prices",
                "quantities",
                "images",
            }:
                raise RequestError(
                    f"This feature is locked until a later {self.config.environment.capitalize()} "
                    "development stage is enabled",
                    404,
                )
            summary = self.order_service.retrieve()
            return Response.html(
                recent_orders_page(
                    summary.order_dicts(),
                    summary.environment,
                    summary.retrieved_at,
                    summary.lookback_days,
                    session.csrf_token,
                )
            )
        if request.path == "/drafts/new" and request.method == "GET":
            return Response.html(
                draft_form_page(session.csrf_token, environment=self.config.environment.capitalize())
            )
        if request.path == "/drafts/review" and request.method == "POST":
            return self._review_draft(request, session)
        if request.path == "/prices/review" and request.method == "POST":
            return self._review_price(request, session)
        if request.path == "/price-limits" and request.method == "GET":
            return self._price_limits(request, session)
        if request.path == "/price-limits" and request.method == "POST":
            return self._save_price_limit(request, session)
        if request.path == "/price-limits/delete" and request.method == "POST":
            return self._delete_price_limit(request, session)
        if engine_route and request.path == "/engine" and request.method == "GET":
            flashes = list(session.flashes)
            session.flashes.clear()
            return Response.html(engine_page(
                self.engine.status(),
                self.database.list_engine_proposals(("pending",), limit=200),
                session.csrf_token,
                self.config.environment.capitalize(),
                flashes,
            ))
        if engine_route and request.method == "POST" and request.path in {"/engine/approve", "/engine/reject"}:
            self._check_csrf(request, session)
            raw_id = Request.one(request.form, "proposal_id").strip()
            if not raw_id.isdigit() or len(raw_id) > 12:
                raise RequestError("Proposal not found", 404)
            proposal_id = int(raw_id)
            if request.path == "/engine/reject":
                if self.database.set_engine_proposal_status(proposal_id, "rejected", outcome="Rejected by the owner."):
                    session.flashes.append(("info", f"Proposal {proposal_id} rejected; nothing changed."))
                else:
                    session.flashes.append(("warning", f"Proposal {proposal_id} is no longer pending."))
                return Response.redirect("/engine")
            if self.engine.paused_reason():
                session.flashes.append(("warning", "Resume the engine before approving proposals."))
                return Response.redirect("/engine")
            outcome = self.engine.execute_proposal(
                proposal_id, force_dry_run=self.config.engine_mode == "auto-dry"
            )
            record = self.database.get_engine_proposal(proposal_id) or {}
            kind = {"executed": "success", "budget": "warning", "superseded": "info"}.get(outcome, "error")
            if outcome == "not_pending":
                message = f"Proposal {proposal_id} is no longer pending, or execution is unavailable."
            else:
                message = f"Proposal {proposal_id}: {outcome}. {record.get('outcome') or ''}".strip()
            session.flashes.append((kind, message))
            return Response.redirect("/engine")
        if engine_route and request.method == "POST" and request.path in {"/engine/pause", "/engine/resume", "/engine/run"}:
            self._check_csrf(request, session)
            if request.path == "/engine/pause":
                self.engine.pause("Paused from the dashboard")
                session.flashes.append(("warning", "Engine paused. No job will run until you resume it."))
            elif request.path == "/engine/resume":
                self.engine.resume()
                session.flashes.append(("success", "Engine resumed."))
            else:
                self.engine.request_reprice()
                session.flashes.append(("info", "A price check will run on the next engine tick (within a minute)."))
            return Response.redirect("/engine")
        if request.path == "/quantities/review" and request.method == "POST":
            return self._review_quantity(request, session)
        if request.path == "/images/review" and request.method == "POST":
            return self._review_image(request, session)
        if request.path == "/actions/confirm" and request.method == "POST":
            return self._confirm_action(request, session)
        if request.path == "/actions/cancel" and request.method == "POST":
            return self._cancel_action(request, session)
        match = re.fullmatch(r"/actions/([a-f0-9]{32})", request.path)
        if match and request.method == "GET":
            # The route is already admin-authenticated. Completed safe journals
            # remain readable after logout or restart so uncertain writes can be
            # reconciled even though in-memory browser sessions do not survive.
            action = self.database.get_action_for_admin(match.group(1))
            if not action:
                raise RequestError("Action not found", 404)
            if self.config.feature_stage == "drafts":
                if action.get("kind") != "create_draft":
                    raise RequestError("Action not found", 404)
                return Response.html(
                    draft_action_page(
                        action,
                        self.database.action_events_for_admin(match.group(1)),
                        session.csrf_token,
                        self.config.environment.capitalize(),
                    )
                )
            if self.config.feature_stage in {"prices", "quantities", "images"}:
                if action.get("kind") == "create_draft":
                    return Response.html(
                        draft_action_page(
                            action,
                            self.database.action_events_for_admin(match.group(1)),
                            session.csrf_token,
                            self.config.environment.capitalize(),
                        )
                    )
                if action.get("kind") == "change_price":
                    return Response.html(
                        price_action_page(
                            action,
                            session.csrf_token,
                            self.config.environment.capitalize(),
                        )
                    )
                if action.get("kind") == "change_quantity" and self.config.feature_stage in {
                    "quantities",
                    "images",
                }:
                    return Response.html(
                        quantity_action_page(
                            action,
                            session.csrf_token,
                            self.config.environment.capitalize(),
                        )
                    )
                if (
                    action.get("kind") == "add_image"
                    and self.config.feature_stage == "images"
                ):
                    return Response.html(
                        image_action_page(
                            action,
                            session.csrf_token,
                            self.config.environment.capitalize(),
                        )
                    )
                raise RequestError("Action not found", 404)
            return Response.html(
                action_page(action, session.csrf_token, self.config.environment.capitalize())
            )
        raise RequestError("Page not found", 404)

    def _engine_dashboard_card(self) -> str:
        """A link to the engine page on the dashboard, only when it is on."""
        if self.engine is None or self.config.engine_mode == "off":
            return ""
        from .engine import describe
        from .views import h

        headline, _detail = describe(self.engine.status())
        return (f'<section class="card"><h2>Autonomy engine</h2><p>{h(headline)}</p>'
                '<p><a href="/engine">Open the engine page</a></p></section>')

    def _account_deletion(
        self, request: Request, session: Session, deletion_state: Mapping[str, Any]
    ) -> Response:
        if request.method == "POST":
            self._check_csrf(request, session)
            self.database.acknowledge_owner_deletion()
            return Response.redirect("/")
        if request.method != "GET":
            raise RequestError("Method not allowed", 405)
        return Response.html(account_deletion_banner_page(deletion_state, session.csrf_token))

    def _login(self, request: Request, session: Session) -> Response:
        if request.method == "GET":
            if session.authenticated:
                return Response.redirect("/")
            return Response.html(login_page(session.csrf_token))
        if request.method != "POST":
            raise RequestError("Method not allowed", 405)
        self._check_csrf(request, session)
        password = Request.one(request.form, "password")
        if not verify_password(password, self.config.admin_password):
            self.login_limiter.record_failure(request.remote_addr)
            return Response.html(login_page(session.csrf_token, "Incorrect password"), 401)
        self.login_limiter.clear(request.remote_addr)
        authenticated = self.sessions.authenticate(session)
        response = Response.redirect("/")
        response.session = authenticated
        return response

    def _oauth_callback(self, request: Request, session: Session) -> Response:
        try:
            self._require_auth(session)
            unexpected = set(request.query).difference(OAUTH_CALLBACK_KEYS)
            if unexpected:
                raise RequestError("OAuth callback contains unsupported parameters")
            state = Request.one(request.query, "state")
            code = Request.one(request.query, "code", required=False)
            error = Request.one(request.query, "error", required=False)
            if bool(code) == bool(error):
                raise RequestError(
                    "OAuth callback must contain exactly one authorization result"
                )
            if error:
                state_record = self.database.consume_oauth_state_record(
                    state, session.session_id
                )
                if state_record is None:
                    raise EbayError(
                        "OAuth state is invalid, expired, or already used",
                        code="INVALID_STATE",
                    )
                message = (
                    (
                        "Inventory permission was declined; the existing connection is unchanged."
                        if self.config.feature_stage == "drafts"
                        else "Read-only permission was declined; the existing connection is unchanged."
                    )
                    if state_record.intent == "scope_upgrade"
                    else "eBay connection was declined; no token was stored."
                )
                session.flashes.append(("warning", message))
                return Response.redirect("/")
            result = self.ebay.complete_authorization(state, code, session.session_id)
            if result.get("upgraded"):
                feature = {
                    "orders": "order",
                    "drafts": "unpublished-offer",
                }.get(self.config.feature_stage, "listing")
                qualifier = "" if self.config.feature_stage == "drafts" else "Read-only "
                message = f"{qualifier}{feature} authorization received. Verify the replacement connection next."
            else:
                message = (
                    f"{self.config.environment.capitalize()} authorization received. Run the separate "
                    "verification check next."
                )
            session.flashes.append(
                (
                    "success",
                    message,
                )
            )
        except (RequestError, EbayError) as error:
            if isinstance(error, EbayError):
                reason = error.code
                status = error.status
            else:
                reason = "INVALID_CALLBACK_REQUEST"
                status = error.status
            self.logger.warning(
                "OAuth callback failed: reason=%s status=%s",
                reason,
                status,
            )
            if session.authenticated:
                session.flashes.append(
                    ("error", f"{self.config.environment.capitalize()} authorization failed: {error}")
                )
        return Response.redirect("/")

    def _review_draft(self, request: Request, session: Session) -> Response:
        self._check_csrf(request, session)
        if self.config.feature_stage in {"drafts", "prices"}:
            try:
                review = self.draft_service.prepare_review(
                    request.form,
                    session.session_id,
                )
            except ValidationError as error:
                return Response.html(
                    draft_form_page(
                        session.csrf_token,
                        display_form_values(request.form),
                        error.errors,
                        self.config.environment.capitalize(),
                    ),
                    400,
                )
            return Response.html(
                draft_review_page(
                    review.action_token,
                    review.draft,
                    session.csrf_token,
                    review.settings,
                    self.config.environment.capitalize(),
                )
            )

        # Dormant backwards-compatible path for older isolated unit fixtures.
        # Real runtime configuration can never select ``full``.
        try:
            draft = validate_draft(request.form, self.config.currency)
        except ValidationError as error:
            return Response.html(
                draft_form_page(
                    session.csrf_token,
                    display_form_values(request.form),
                    error.errors,
                    self.config.environment.capitalize(),
                ),
                400,
            )
        generation = self.database.connection_generation()
        if generation is None:
            raise RequestError(
                f"Connect an eBay {self.config.environment.capitalize()} seller account first", 409
            )
        if self.ebay.get_inventory_item(draft["sku"]) is not None:
            raise SkuExistsError(draft["sku"])
        try:
            token = self.database.create_pending_action(
                session.session_id,
                "create_draft",
                draft,
                expected_generation=generation,
            )
        except ConnectionChangedStorageError as error:
            raise RequestError(
                f"The {self.config.environment.capitalize()} seller connection changed during review; "
                "review again",
                409,
            ) from error
        settings = {
            "environment": self.config.environment.capitalize(),
            "marketplace": self.config.marketplace_id,
            "merchant_location_key": self.config.merchant_location_key,
            "fulfillment_policy_id": self.config.fulfillment_policy_id,
            "payment_policy_id": self.config.payment_policy_id,
            "return_policy_id": self.config.return_policy_id,
        }
        return Response.html(
            draft_review_page(
                token,
                draft,
                session.csrf_token,
                settings,
                self.config.environment.capitalize(),
            )
        )

    def _review_price(self, request: Request, session: Session) -> Response:
        self._check_csrf(request, session)
        if self.config.feature_stage == "prices":
            try:
                review = self.price_service.prepare_review(
                    request.form,
                    session.session_id,
                )
            except ValidationError as error:
                raise RequestError(
                    next(iter(error.errors.values()), str(error))
                ) from error
            return Response.html(
                price_review_page(
                    review.action_token,
                    {
                        "sku": review.sku,
                        "offer_id": review.offer_id,
                        "currency": review.currency,
                        "expected_price": review.expected_price,
                        "new_price": review.new_price,
                    },
                    session.csrf_token,
                )
            )

        # Dormant backwards-compatible path for older isolated unit fixtures.
        # Real runtime configuration can never select ``full`` here either.
        offer_id = Request.one(request.form, "offer_id").strip()
        sku = Request.one(request.form, "sku").strip()
        if not re.fullmatch(r"[^\x00-\x20]{1,100}", offer_id) or not 1 <= len(sku) <= 50:
            raise RequestError("Offer ID or SKU is invalid")
        try:
            new_price = validate_new_price(Request.one(request.form, "new_price"))
        except ValidationError as error:
            raise RequestError(error.errors.get("new_price", str(error))) from error
        generation = self.database.connection_generation()
        if generation is None:
            raise RequestError(
                f"Connect an eBay {self.config.environment.capitalize()} seller account first", 409
            )
        offer = self.ebay.get_offer(offer_id)
        if offer.get("offerId") != offer_id:
            raise RequestError("eBay returned a different Offer than the one selected", 409)
        if offer.get("sku") != sku:
            raise RequestError("The Offer does not match the selected SKU", 409)
        if offer.get("status") != "PUBLISHED":
            raise RequestError("Only published Offers can use the targeted price update", 409)
        current = self.ebay.offer_price(offer)
        if not current["value"] or not current["currency"]:
            raise EbayError("The current Offer price is missing", code="INVALID_EBAY_RESPONSE")
        if current["value"] == new_price:
            raise RequestError("Enter a price different from the current Offer price")
        payload = {
            "offer_id": offer_id,
            "sku": sku,
            "expected_price": current["value"],
            "currency": current["currency"],
            "new_price": new_price,
        }
        try:
            token = self.database.create_pending_action(
                session.session_id,
                "change_price",
                payload,
                expected_generation=generation,
            )
        except ConnectionChangedStorageError as error:
            raise RequestError(
                f"The {self.config.environment.capitalize()} seller connection changed during review; "
                "review again",
                409,
            ) from error
        return Response.html(price_review_page(token, payload, session.csrf_token))

    def _formatted_price_limits(self) -> List[Dict[str, Any]]:
        formatted = []
        for limit in self.database.list_price_limits():
            updated_at = limit.get("updated_at")
            display_updated = ""
            if isinstance(updated_at, int):
                display_updated = datetime.fromtimestamp(updated_at, timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S UTC"
                )
            formatted.append({**limit, "updated_at": display_updated})
        return formatted

    def _price_limits(self, request: Request, session: Session) -> Response:
        flashes = list(session.flashes)
        session.flashes.clear()
        return Response.html(
            price_limits_page(
                self._formatted_price_limits(),
                str(self.config.max_price_change_percent),
                session.csrf_token,
                flashes=flashes,
            )
        )

    def _save_price_limit(self, request: Request, session: Session) -> Response:
        self._check_csrf(request, session)
        try:
            cleaned = validate_price_limit_form(request.form)
        except ValidationError as error:
            return Response.html(
                price_limits_page(
                    self._formatted_price_limits(),
                    str(self.config.max_price_change_percent),
                    session.csrf_token,
                    display_form_values(request.form),
                    error.errors,
                ),
                400,
            )
        self.database.set_price_limit(
            cleaned["sku"],
            cleaned["floor_price"],
            self.config.currency,
            cleaned["max_change_percent"],
        )
        session.flashes.append(
            ("success", f"Saved the price limit for SKU {cleaned['sku']}.")
        )
        return Response.redirect("/price-limits")

    def _delete_price_limit(self, request: Request, session: Session) -> Response:
        self._check_csrf(request, session)
        sku = Request.one(request.form, "sku").strip()
        removed = self.database.delete_price_limit(sku)
        if removed:
            session.flashes.append(("success", f"Removed the price limit for SKU {sku}."))
        else:
            session.flashes.append(("info", f"No price limit was set for SKU {sku}."))
        return Response.redirect("/price-limits")

    def _review_quantity(self, request: Request, session: Session) -> Response:
        self._check_csrf(request, session)
        try:
            review = self.quantity_service.prepare_review(
                request.form,
                session.session_id,
            )
        except ValidationError as error:
            raise RequestError(
                next(iter(error.errors.values()), str(error))
            ) from error
        return Response.html(
            quantity_review_page(
                review.action_token,
                {
                    "sku": review.sku,
                    "offer_id": review.offer_id,
                    "expected_quantity": review.expected_quantity,
                    "new_quantity": review.new_quantity,
                },
                session.csrf_token,
            )
        )

    def _review_image(self, request: Request, session: Session) -> Response:
        self._check_csrf(request, session)
        try:
            review = self.image_service.prepare_review(
                request.form,
                session.session_id,
            )
        except ValidationError as error:
            raise RequestError(
                next(iter(error.errors.values()), str(error))
            ) from error
        return Response.html(
            image_review_page(
                review.action_token,
                {
                    "sku": review.sku,
                    "offer_id": review.offer_id,
                    "new_image_url": review.new_image_url,
                    "content_type": review.content_type,
                    "content_length": review.content_length,
                },
                session.csrf_token,
            )
        )

    def _confirmed_redirect_or_clear_error(
        self, action_id: Optional[str], token: str, session: Session, message: str
    ) -> Response:
        """Redirect to a freshly confirmed action, or raise a clearer error.

        ``claim_pending_action`` is a strict one-use claim, by design: a
        second ``/actions/confirm`` submission for the same token -- a
        genuine repeat, a double-click, a resubmitted form, a network retry
        racing the first request -- must always fail here with an explicit,
        auditable error, never a silent second success or a second write.
        That one-use behaviour is unchanged. What was confusing before is
        only the wording: when this exact token already completed a real
        confirm attempt, the error now says so plainly instead of leaving it
        ambiguous whether the original click did anything at all. A token
        that was cancelled, or never claimed in the first place, still gets
        the original message unchanged.
        """

        if action_id is not None:
            return Response.redirect(f"/actions/{action_id}")
        replay_id = self.database.find_confirmed_action_id_by_token(
            token, session.session_id
        )
        if replay_id is not None:
            raise RequestError(
                f"{message}. It already completed on an earlier submission of "
                "this same confirmation -- check the dashboard's recent "
                "completed confirmations for its result before trying again.",
                409,
            )
        raise RequestError(message, 409)

    def _confirm_action(self, request: Request, session: Session) -> Response:
        self._check_csrf(request, session)
        token = Request.one(request.form, "action_token")
        if self.config.feature_stage == "drafts":
            action_id = self.draft_service.confirm(token, session.session_id)
            return self._confirmed_redirect_or_clear_error(
                action_id,
                token,
                session,
                "This draft confirmation is expired, cancelled, or already used",
            )
        if self.config.feature_stage in {"prices", "quantities", "images"}:
            # ``prices``, ``quantities``, and ``images`` are cumulative over
            # ``drafts`` (every earlier stage's action kind can still be
            # pending), so a non-consuming peek at the token's own recorded
            # kind decides which service confirms it -- no service ever
            # claims an action it did not create, and each kind is only
            # routed to a service whose own stage actually grants it.
            pending = self.database.get_pending_action(token, session.session_id)
            kind = pending.kind if pending is not None else None
            if kind == "add_image" and self.config.feature_stage == "images":
                action_id = self.image_service.confirm(token, session.session_id)
            elif kind == "change_quantity" and self.config.feature_stage in {
                "quantities",
                "images",
            }:
                action_id = self.quantity_service.confirm(token, session.session_id)
            elif kind == "change_price":
                action_id = self.price_service.confirm(token, session.session_id)
            else:
                action_id = self.draft_service.confirm(token, session.session_id)
            return self._confirmed_redirect_or_clear_error(
                action_id,
                token,
                session,
                "This confirmation is expired, cancelled, or already used",
            )

        # Dormant backwards-compatible path for isolated later-stage fixtures.
        action = self.database.claim_pending_action(token, session.session_id)
        if action is None:
            raise RequestError("This confirmation is expired, cancelled, or already used", 409)
        try:
            if action.kind == "create_draft":
                result = self.ebay.create_draft(
                    action.payload,
                    expected_generation=action.grant_generation,
                )
            else:
                result = self.ebay.change_price(
                    **action.payload,
                    expected_generation=action.grant_generation,
                )
            self.database.complete_action(action.action_id, "succeeded", result=result)
        except DraftPartialError as error:
            status = "unknown" if error.outcome_unknown else "partial"
            self.database.complete_action(
                action.action_id,
                status,
                result=error.details,
                error_message=str(error),
            )
        except EbayError as error:
            status = "unknown" if error.outcome_unknown else "failed"
            error_result = {"code": error.code}
            if error.details is not None:
                error_result["details"] = error.details
            self.database.complete_action(
                action.action_id,
                status,
                result=error_result,
                error_message=str(error),
            )
        except Exception:
            self.database.complete_action(
                action.action_id,
                "unknown",
                error_message="The application failed after confirmation; reconcile in eBay before retrying",
            )
            raise
        return Response.redirect(f"/actions/{action.action_id}")

    def _cancel_action(self, request: Request, session: Session) -> Response:
        self._check_csrf(request, session)
        token = Request.one(request.form, "action_token")
        if self.config.feature_stage == "drafts":
            cancelled = self.draft_service.cancel(token, session.session_id)
        elif self.config.feature_stage in {"prices", "quantities", "images"}:
            pending = self.database.get_pending_action(token, session.session_id)
            kind = pending.kind if pending is not None else None
            if kind == "add_image" and self.config.feature_stage == "images":
                cancelled = self.image_service.cancel(token, session.session_id)
            elif kind == "change_quantity" and self.config.feature_stage in {
                "quantities",
                "images",
            }:
                cancelled = self.quantity_service.cancel(token, session.session_id)
            elif kind == "change_price":
                cancelled = self.price_service.cancel(token, session.session_id)
            else:
                cancelled = self.draft_service.cancel(token, session.session_id)
        else:
            cancelled = self.database.cancel_pending_action(token, session.session_id)
        if not cancelled:
            raise RequestError("This confirmation is expired, cancelled, or already used", 409)
        session.flashes.append(("success", "Pending action cancelled; no eBay change was made."))
        return Response.redirect("/")
