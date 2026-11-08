from __future__ import annotations

import ast
import base64
import os
import re
import stat
import urllib.parse
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Mapping, MutableMapping, Optional, Tuple


class ConfigError(RuntimeError):
    pass


KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MARKETPLACE_PATTERN = re.compile(r"^EBAY_[A-Z]{2,3}$")
LANGUAGE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})+$")
VISIBLE_PATTERN = re.compile(r"^[\x21-\x7e]+$")
RUNAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,512}$")

# One permanent local OAuth destination is used throughout the application.
# eBay still receives the opaque RuName as redirect_uri; eBay's configured
# HTTPS callback relay forwards the browser to this loopback URL.
CANONICAL_HOST = "127.0.0.1"
CANONICAL_PORT = 5000
# Phase 5 (second seller account, docs/second_account.md): one extra port is
# allowed so a second, fully separate copy of the app can run at the same
# time. eBay's OAuth relay always returns to CANONICAL_PORT, so an instance
# on SECONDARY_PORT can never start an eBay sign-in -- it is connected once
# while temporarily run on 5000, then used day to day on 5001.
SECONDARY_PORT = 5001
ALLOWED_PORTS = (CANONICAL_PORT, SECONDARY_PORT)
CANONICAL_CALLBACK_PATH = "/oauth/callback"
CANONICAL_CALLBACK_URL = (
    f"http://{CANONICAL_HOST}:{CANONICAL_PORT}{CANONICAL_CALLBACK_PATH}"
)
LOCAL_ALLOWED_HOSTS: Tuple[str, ...] = (CANONICAL_HOST,)

IDENTITY_SCOPE = "https://api.ebay.com/oauth/api_scope/commerce.identity.readonly"
INVENTORY_READ_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.inventory.readonly"
INVENTORY_WRITE_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.inventory"
FULFILLMENT_READ_SCOPE = (
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly"
)

CONNECTION_SCOPES: Tuple[str, ...] = (IDENTITY_SCOPE,)
LISTING_SCOPES: Tuple[str, ...] = (IDENTITY_SCOPE, INVENTORY_READ_SCOPE)
ORDER_SCOPES: Tuple[str, ...] = (
    IDENTITY_SCOPE,
    INVENTORY_READ_SCOPE,
    FULFILLMENT_READ_SCOPE,
)

# Phase 5 replaces the Inventory read-only scope with eBay's Inventory manage
# scope. eBay recommends omitting a read-only scope when the corresponding
# manage scope is present. The write scope still satisfies the earlier listing
# and order screens' Inventory reads.
DRAFT_SCOPES: Tuple[str, ...] = (
    IDENTITY_SCOPE,
    INVENTORY_WRITE_SCOPE,
    FULFILLMENT_READ_SCOPE,
)

# Reserved only for dormant later-stage test fixtures. Real stages never request
# the broad Inventory write permission contained here.
SELLER_SCOPES: Tuple[str, ...] = (
    IDENTITY_SCOPE,
    INVENTORY_WRITE_SCOPE,
    FULFILLMENT_READ_SCOPE,
)

# One-time Sandbox setup: create the merchant location and the three business
# policies (fulfillment, payment, return) a Phase 5 offer requires. This is
# the only real stage that ever requests Account-management scope. It adds
# Account scope on top of the existing Phase 5 grant; it does not replace it.
ACCOUNT_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.account"
SETUP_SCOPES: Tuple[str, ...] = (
    IDENTITY_SCOPE,
    INVENTORY_WRITE_SCOPE,
    FULFILLMENT_READ_SCOPE,
    ACCOUNT_SCOPE,
)

# Production preparation (Phase 9, read-only groundwork only -- see
# docs/phase9_production_design.md). Entering the production environment at
# all, even read-only, requires every one of these deliberate, structural
# conditions, all checked in ``load_config``: a distinct, correctly named
# credentials file, an explicit typed opt-in written inside that file, and
# (checked separately, once the feature stage is parsed) a feature stage
# limited to read-only capability. None of them can be satisfied by editing
# the ordinary Sandbox ``.env``. There is deliberately no flag yet that
# unlocks a production *write* -- that is its own later, separately
# reviewed phase.
PRODUCTION_ENV_FILENAME = ".env.production"
PRODUCTION_OPT_IN_ENV_VAR = "APP_ALLOW_PRODUCTION_ENVIRONMENT"
PRODUCTION_OPT_IN_VALUE = "yes-i-understand-the-risk"

# A second, separate, explicit opt-in -- deliberately distinct from the one
# above, and distinct *per write-capable stage* -- required before a
# production config may select "prices", "quantities", or "images" at all.
# Being in environment=production is not by itself enough to reach any of
# these; the matching opt-in below must also be set, in the same file, in
# addition to APP_FEATURE_STAGE=<stage>. Each stage's opt-in unlocks only
# that stage -- setting the "prices" opt-in does nothing for "quantities" or
# "images", and so on. Every other write-capable stage (currently "drafts"
# and "setup") has no opt-in at all and remains structurally unreachable in
# production regardless of anything in this file, until it is given its own
# entry here after its own separate review -- matching the gradual prices ->
# quantities -> images rollout plan. Adding an entry below is a code change
# that makes a stage *reachable*; it does not by itself enable it -- the
# owner must still separately choose to write that stage's own opt-in line
# into their real .env.production.
PRODUCTION_WRITE_OPT_INS: Dict[str, Tuple[str, str]] = {
    "prices": (
        "APP_ALLOW_PRODUCTION_PRICE_WRITES",
        "yes-i-understand-the-risk-of-automated-price-changes",
    ),
    "quantities": (
        "APP_ALLOW_PRODUCTION_QUANTITY_WRITES",
        "yes-i-understand-the-risk-of-automated-quantity-changes",
    ),
    "images": (
        "APP_ALLOW_PRODUCTION_IMAGE_WRITES",
        "yes-i-understand-the-risk-of-automated-image-changes",
    ),
    # Migration is a one-off operation, not a recurring automated change: it
    # brings one existing classic (non-Inventory-API) listing under this
    # app's management by calling eBay's bulkMigrateListing endpoint, using
    # the same sell.inventory (write) scope every other write-capable stage
    # already uses. It is deliberately its own stage, not cumulative with
    # prices/quantities/images, and gets its own separate opt-in -- exactly
    # like every other write-capable stage here.
    "migration": (
        "APP_ALLOW_PRODUCTION_LISTING_MIGRATION",
        "yes-i-understand-the-risk-of-migrating-a-live-listing",
    ),
    # Publishing makes an existing UNPUBLISHED Offer live and visible to
    # buyers via eBay's publishOffer call (same sell.inventory write scope).
    # Like migration it is its own script-only island, not cumulative with
    # prices/quantities/images, and like prices/quantities/images it honours
    # APP_DRY_RUN (which defaults to true in production).
    "publish": (
        "APP_ALLOW_PRODUCTION_PUBLISH",
        "yes-i-understand-the-risk-of-publishing-live-listings",
    ),
}
# Back-compat names for the "prices" entry -- unchanged from before this
# dict existed, since other modules and tests already import these two names
# directly.
PRODUCTION_WRITE_OPT_IN_ENV_VAR, PRODUCTION_WRITE_OPT_IN_VALUE = PRODUCTION_WRITE_OPT_INS["prices"]
ENGINE_MODES: Tuple[str, ...] = ("off", "shadow", "auto-dry", "auto")
PRODUCTION_READ_ONLY_STAGES: Tuple[str, ...] = ("connection", "account", "listings", "orders")

SANDBOX_API_ORIGIN = "https://api.sandbox.ebay.com"
SANDBOX_IDENTITY_BASE_URL = "https://apiz.sandbox.ebay.com/commerce/identity/v1"
SANDBOX_CONSENT_URL = "https://auth.sandbox.ebay.com/oauth2/authorize"
SANDBOX_TOKEN_URL = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"

PRODUCTION_API_ORIGIN = "https://api.ebay.com"
PRODUCTION_IDENTITY_BASE_URL = "https://apiz.ebay.com/commerce/identity/v1"
PRODUCTION_CONSENT_URL = "https://auth.ebay.com/oauth2/authorize"
PRODUCTION_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"


def granted_scopes_cover(
    granted: Tuple[str, ...],
    required: Tuple[str, ...],
) -> bool:
    """Return whether a recorded grant supplies the requested capabilities.

    eBay's Inventory manage scope includes Inventory reads, even though its
    literal scope string differs from ``sell.inventory.readonly``. Every other
    scope remains an exact, fail-closed match.
    """

    granted_set = set(granted)
    for scope in required:
        if scope in granted_set:
            continue
        if scope == INVENTORY_READ_SCOPE and INVENTORY_WRITE_SCOPE in granted_set:
            continue
        return False
    return bool(required)


def scopes_for_stage(feature_stage: str, fallback: Tuple[str, ...] = ()) -> Tuple[str, ...]:
    """Return the smallest fixed permission set allowed for a real feature stage."""

    if feature_stage in {"connection", "account"}:
        return CONNECTION_SCOPES
    if feature_stage == "listings":
        return LISTING_SCOPES
    if feature_stage == "orders":
        return ORDER_SCOPES
    if feature_stage == "drafts":
        return DRAFT_SCOPES
    if feature_stage == "prices":
        # Price changes use the Sell Inventory API's bulk update endpoint,
        # covered by the same sell.inventory (write) scope drafts already use
        # -- no additional OAuth consent is required to reach this stage.
        return DRAFT_SCOPES
    if feature_stage == "quantities":
        # Quantity changes use the same bulk update endpoint as price changes,
        # covered by the same sell.inventory (write) scope -- no additional
        # OAuth consent is required to reach this stage either.
        return DRAFT_SCOPES
    if feature_stage == "images":
        # Adding a listing image is a targeted Inventory-item PUT, covered by
        # the same sell.inventory (write) scope every earlier real stage
        # already uses -- no additional OAuth consent is required here either.
        return DRAFT_SCOPES
    if feature_stage == "migration":
        # bulkMigrateListing is part of the Sell Inventory API, gated by the
        # same sell.inventory (write) scope drafts/prices/quantities/images
        # already use -- no additional OAuth consent is required to reach
        # this stage either.
        return DRAFT_SCOPES
    if feature_stage == "publish":
        # publishOffer is part of the Sell Inventory API, gated by the same
        # sell.inventory (write) scope -- no additional OAuth consent needed.
        return DRAFT_SCOPES
    if feature_stage == "setup":
        return SETUP_SCOPES
    # ``full`` exists only for dormant, directly constructed unit-test fixtures.
    # ``load_config`` never permits a real process to select it.
    if feature_stage == "full" and fallback:
        return tuple(fallback)
    raise ConfigError("Unsupported application feature stage")


def _parse_env_value(raw: str, line_number: int) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise ConfigError(f"Invalid quoted value on .env line {line_number}") from exc
        if not isinstance(parsed, str):
            raise ConfigError(f".env line {line_number} must contain a string value")
        return parsed
    return value.split(" #", 1)[0].strip()


def load_dotenv(path: Path, environ: Optional[MutableMapping[str, str]] = None) -> None:
    target = os.environ if environ is None else environ
    if not path.exists():
        raise ConfigError(f"Missing environment file: {path}")
    details = os.lstat(path)
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        raise ConfigError(f"{path} must be a regular file without links")
    if details.st_size > 64 * 1024:
        raise ConfigError(f"{path} is unexpectedly large")
    if stat.S_IMODE(details.st_mode) & 0o077:
        raise ConfigError(f"{path} must be private; run: chmod 600 {path}")

    seen = set()
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigError(f"Invalid .env entry on line {line_number}")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not KEY_PATTERN.fullmatch(key):
            raise ConfigError(f"Invalid .env key on line {line_number}")
        if key in seen:
            raise ConfigError(f"Duplicate .env key on line {line_number}: {key}")
        seen.add(key)
        # The private project .env is authoritative. This prevents stale values
        # inherited from a Terminal session from silently selecting a different
        # App ID, Cert ID, RuName, host, or port.
        target[key] = _parse_env_value(raw_value, line_number)


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def _integer(env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _decimal(
    env: Mapping[str, str], name: str, default: Decimal, minimum: Decimal, maximum: Decimal
) -> Decimal:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = Decimal(raw.strip())
    except InvalidOperation as exc:
        raise ConfigError(f"{name} must be a decimal number") from exc
    if not value.is_finite() or not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _boolean(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def _check_production_data_isolation(
    selected_path: Path,
    database_path: Path,
    encryption_key: bytes,
) -> None:
    """Fail closed if production would share Sandbox's database or key.

    Both ``.env`` and ``.env.production`` normally sit beside each other in
    the same project directory. When a Sandbox ``.env`` is found there, this
    performs a read-only comparison -- never a write, never merged into the
    real process environment -- so a copy-pasted or forgotten
    ``APP_DATABASE_PATH``/``APP_DATA_ENCRYPTION_KEY`` can never silently
    point a production run at Sandbox's stored refresh token, granted
    scopes, or action history, or let it reuse the same encryption key. If
    no Sandbox ``.env`` exists beside it, there is nothing to compare
    against; that is fine on a machine that only ever runs production.
    """

    sibling_path = selected_path.parent / ".env"
    if not sibling_path.exists():
        return
    sibling_environ: MutableMapping[str, str] = {}
    try:
        load_dotenv(sibling_path, sibling_environ)
    except ConfigError as exc:
        raise ConfigError(
            "Could not verify the production database and encryption key are "
            f"separate from the Sandbox .env beside it ({exc}); fix or remove "
            "that file before starting production"
        ) from exc

    sibling_database_value = sibling_environ.get(
        "APP_DATABASE_PATH", "./instance/ebay-sandbox.sqlite"
    ).strip()
    sibling_database_path = Path(sibling_database_value)
    if not sibling_database_path.is_absolute():
        sibling_database_path = (sibling_path.parent / sibling_database_path).absolute()
    if sibling_database_path == database_path:
        raise ConfigError(
            "APP_DATABASE_PATH must not be the same file the Sandbox .env beside "
            "it uses"
        )

    sibling_encoded_key = sibling_environ.get("APP_DATA_ENCRYPTION_KEY", "").strip()
    if sibling_encoded_key:
        try:
            sibling_key = base64.b64decode(sibling_encoded_key, validate=True)
        except (ValueError, TypeError):
            sibling_key = None
        if sibling_key is not None and sibling_key == encryption_key:
            raise ConfigError(
                "APP_DATA_ENCRYPTION_KEY must not be the same key the Sandbox "
                ".env beside it uses"
            )


@dataclass(frozen=True)
class AppConfig:
    client_id: str
    client_secret: str
    runame: str
    admin_password: str
    data_encryption_key: bytes
    marketplace_id: str
    content_language: str
    currency: str
    merchant_location_key: Optional[str]
    fulfillment_policy_id: Optional[str]
    payment_policy_id: Optional[str]
    return_policy_id: Optional[str]
    database_path: Path
    host: str
    port: int
    allowed_hosts: Tuple[str, ...]
    cookie_secure: bool
    request_timeout_seconds: int
    order_lookback_days: int
    initial_refresh_token: Optional[str]
    feature_stage: str = "connection"
    scopes: Tuple[str, ...] = CONNECTION_SCOPES
    runtime_mode: str = "local"
    environment: str = "sandbox"
    api_origin: str = SANDBOX_API_ORIGIN
    identity_base_url: str = SANDBOX_IDENTITY_BASE_URL
    consent_url: str = SANDBOX_CONSENT_URL
    token_url: str = SANDBOX_TOKEN_URL
    # Account Deletion/Closure notification receiver (see
    # ebay_app/account_deletion.py and account_deletion_worker/). Both unset
    # (the default) means the feature is entirely inactive: no polling, no
    # outbound calls, no behaviour change. Set in .env.production once the
    # Cloudflare Worker is deployed and registered with eBay.
    deletion_worker_base_url: Optional[str] = None
    deletion_worker_status_secret: Optional[str] = None
    # Section 6 production price-safety controls (see PriceService). These
    # apply regardless of environment so Sandbox testing exercises the same
    # limits Production will enforce. Defaults are deliberately conservative
    # starting points the owner can change at any time in .env.production --
    # none of them require a code change.
    max_price_change_percent: Decimal = Decimal("10")
    max_actions_per_run: int = 10
    require_price_floor: bool = True
    # Autonomy engine (docs/autonomy_engine.md). "off" is today's behaviour.
    # There is deliberately no default margin: None means a SKU without its
    # own margin_percent is reported but never repriced (see §10 D1).
    engine_mode: str = "off"
    engine_default_margin_percent: Optional[Decimal] = None
    engine_reprice_hours: int = 24
    engine_relist_minutes: int = 60
    engine_orders_minutes: int = 30
    # Shown on every page and email when set; required on SECONDARY_PORT.
    account_label: Optional[str] = None
    # Email alerts (ebay_app/alerts.py). All unset = alerts off. The app
    # password is a credential the owner enters; it is never logged or shown.
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_user: Optional[str] = None
    smtp_app_password: Optional[str] = None
    alert_email: Optional[str] = None
    engine_digest_hours: int = 6
    # Read-only comparison with similar live listings (Browse API). Off by
    # default: eBay may require Buy API approval for production use.
    engine_market_check: bool = False
    # The kill switch's other half: when true, PriceService.confirm() claims
    # and journals the action exactly as normal but never calls eBay at all,
    # recording a result with "dry_run": True instead. Defaults to false
    # (today's existing, already-live-verified Sandbox behaviour is
    # unchanged) EXCEPT that a production config defaults it to true --
    # entering the "prices" stage in production for the first time always
    # starts in dry-run unless the owner explicitly sets APP_DRY_RUN=false
    # themselves, separately from (and after) the write opt-in above.
    dry_run: bool = False


def load_config(
    env_file: Optional[Path] = None,
    environ: Optional[MutableMapping[str, str]] = None,
) -> AppConfig:
    env = os.environ if environ is None else environ
    default_path = Path(__file__).resolve().parents[1] / ".env"
    selected_path = env_file or default_path
    load_dotenv(selected_path, env)

    deprecated_runtime_keys = (
        "APP_ALLOWED_HOSTS",
        "APP_COOKIE_SECURE",
        "OAUTH_TUNNEL_HOST",
        "OAUTH_RELAY",
    )
    configured_deprecated = [name for name in deprecated_runtime_keys if env.get(name, "").strip()]
    if configured_deprecated:
        raise ConfigError(
            "Remove obsolete OAuth runtime settings; the callback is permanently "
            f"{CANONICAL_CALLBACK_URL}"
        )

    configured_environment = env.get("EBAY_ENVIRONMENT", "sandbox").strip().lower()
    _sandbox_restricted_error = ConfigError(
        "This application is intentionally restricted to EBAY_ENVIRONMENT=sandbox"
    )
    if configured_environment == "sandbox":
        environment = "sandbox"
    elif configured_environment == "production":
        # Two deliberate, structural conditions gate production, neither of
        # which the ordinary Sandbox .env can ever satisfy by accident: the
        # file must literally be named .env.production, and it must contain
        # an explicit, typed opt-in. Anything short of both gets the exact
        # same generic message a plain "not sandbox" value gets -- no hint is
        # given about how close a wrong attempt came.
        if selected_path.name != PRODUCTION_ENV_FILENAME:
            raise _sandbox_restricted_error
        if env.get(PRODUCTION_OPT_IN_ENV_VAR, "").strip() != PRODUCTION_OPT_IN_VALUE:
            raise ConfigError(
                f"Set {PRODUCTION_OPT_IN_ENV_VAR}={PRODUCTION_OPT_IN_VALUE} in "
                f"{PRODUCTION_ENV_FILENAME} to deliberately enable the production "
                "environment"
            )
        environment = "production"
    else:
        raise _sandbox_restricted_error

    feature_stage = env.get("APP_FEATURE_STAGE", "connection").strip().lower()
    if feature_stage not in {
        "connection",
        "account",
        "listings",
        "orders",
        "drafts",
        "prices",
        "quantities",
        "images",
        "migration",
        "publish",
        "setup",
    }:
        raise ConfigError(
            "APP_FEATURE_STAGE must be connection, account, listings, orders, drafts, "
            "prices, quantities, images, migration, publish or setup"
        )
    if environment == "production" and feature_stage not in PRODUCTION_READ_ONLY_STAGES:
        write_opt_in = PRODUCTION_WRITE_OPT_INS.get(feature_stage)
        if write_opt_in is None:
            raise ConfigError(
                "The production environment currently permits only read-only feature "
                "stages (connection, account, listings, orders) or, with each stage's "
                "own separate explicit opt-in, prices, quantities, or images -- every "
                "other write-capable stage remains locked until it is separately "
                "reviewed and approved"
            )
        write_opt_in_env_var, write_opt_in_value = write_opt_in
        if env.get(write_opt_in_env_var, "").strip() != write_opt_in_value:
            raise ConfigError(
                f"Set {write_opt_in_env_var}={write_opt_in_value} in "
                f"{PRODUCTION_ENV_FILENAME} to deliberately enable automated "
                f"{feature_stage} changes against the real account"
            )

    admin_password = _required(env, "APP_ADMIN_PASSWORD")
    if len(admin_password) < 20 or len(admin_password) > 256:
        raise ConfigError("APP_ADMIN_PASSWORD must contain between 20 and 256 characters")
    encoded_key = _required(env, "APP_DATA_ENCRYPTION_KEY")
    try:
        encryption_key = base64.b64decode(encoded_key, validate=True)
    except (ValueError, TypeError) as exc:
        raise ConfigError("APP_DATA_ENCRYPTION_KEY must be valid base64") from exc
    if len(encryption_key) != 32:
        raise ConfigError("APP_DATA_ENCRYPTION_KEY must encode exactly 32 bytes")

    marketplace_id = env.get("EBAY_MARKETPLACE_ID", "EBAY_GB").strip().upper()
    if not MARKETPLACE_PATTERN.fullmatch(marketplace_id):
        raise ConfigError("EBAY_MARKETPLACE_ID is invalid")
    content_language = env.get("EBAY_CONTENT_LANGUAGE", "en-GB").strip()
    if not LANGUAGE_PATTERN.fullmatch(content_language):
        raise ConfigError("EBAY_CONTENT_LANGUAGE is invalid")
    currency = env.get("EBAY_CURRENCY", "GBP").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ConfigError("EBAY_CURRENCY must be a three-letter currency code")

    private_values = {
        name: _required(env, name)
        for name in (
            "EBAY_CLIENT_ID",
            "EBAY_CLIENT_SECRET",
            "EBAY_RUNAME",
        )
    }
    for name, value in private_values.items():
        if len(value) > 512 or not VISIBLE_PATTERN.fullmatch(value):
            raise ConfigError(f"{name} contains unsupported characters")
    if not RUNAME_PATTERN.fullmatch(private_values["EBAY_RUNAME"]):
        raise ConfigError(
            "EBAY_RUNAME must be the opaque Sandbox RuName identifier, not a URL or path"
        )
    if environment == "sandbox":
        if "-SBX-" not in private_values["EBAY_CLIENT_ID"].upper():
            raise ConfigError("EBAY_CLIENT_ID must be a Sandbox App ID containing -SBX-")
        if not private_values["EBAY_CLIENT_SECRET"].upper().startswith("SBX-"):
            raise ConfigError("EBAY_CLIENT_SECRET must be a rotated Sandbox Cert ID")
    else:
        if "-SBX-" in private_values["EBAY_CLIENT_ID"].upper():
            raise ConfigError(
                "EBAY_CLIENT_ID looks like a Sandbox App ID; a Production App ID "
                "is required here"
            )
        if private_values["EBAY_CLIENT_SECRET"].upper().startswith("SBX-"):
            raise ConfigError(
                "EBAY_CLIENT_SECRET looks like a Sandbox Cert ID; a Production "
                "Cert ID is required here"
            )
    optional_seller_values = {
        name: env.get(name, "").strip() or None
        for name in (
            "EBAY_MERCHANT_LOCATION_KEY",
            "EBAY_FULFILLMENT_POLICY_ID",
            "EBAY_PAYMENT_POLICY_ID",
            "EBAY_RETURN_POLICY_ID",
        )
    }
    for name, value in optional_seller_values.items():
        if value and (len(value) > 512 or not VISIBLE_PATTERN.fullmatch(value)):
            raise ConfigError(f"{name} contains unsupported characters")
    policy_values = [
        optional_seller_values["EBAY_FULFILLMENT_POLICY_ID"],
        optional_seller_values["EBAY_PAYMENT_POLICY_ID"],
        optional_seller_values["EBAY_RETURN_POLICY_ID"],
    ]
    if any(policy_values) and not all(policy_values):
        raise ConfigError("Set all three eBay policy IDs, or leave all three unset")

    database_value = env.get("APP_DATABASE_PATH", "./instance/ebay-sandbox.sqlite").strip()
    database_path = Path(database_value)
    if not database_path.is_absolute():
        database_path = (selected_path.parent / database_path).absolute()
    if database_path == selected_path.absolute():
        raise ConfigError("APP_DATABASE_PATH must not point to the .env credentials file")
    if environment == "production":
        _check_production_data_isolation(selected_path, database_path, encryption_key)

    if env.get("EBAY_REFRESH_TOKEN", "").strip():
        raise ConfigError(
            "EBAY_REFRESH_TOKEN cannot be supplied through .env; use the browser OAuth flow"
        )
    configured_host = env.get("HOST", CANONICAL_HOST).strip().lower()
    if configured_host != CANONICAL_HOST:
        raise ConfigError(
            f"HOST must remain {CANONICAL_HOST} for the permanent local OAuth callback"
        )
    configured_port = _integer(env, "PORT", CANONICAL_PORT, 1, 65535)
    account_label = env.get("APP_ACCOUNT_LABEL", "").strip()
    if account_label and (
        len(account_label) > 40
        or not re.fullmatch(r"[A-Za-z0-9 ._-]+", account_label)
    ):
        raise ConfigError("APP_ACCOUNT_LABEL must be 1-40 letters, digits, spaces, dots, dashes or underscores")
    if configured_port not in ALLOWED_PORTS:
        raise ConfigError(
            f"PORT must remain {CANONICAL_PORT} for the permanent local OAuth callback "
            f"(or {SECONDARY_PORT} for a labelled second-account copy)"
        )
    if configured_port == SECONDARY_PORT and not account_label:
        raise ConfigError(
            f"PORT {SECONDARY_PORT} is only for a second-account copy: set APP_ACCOUNT_LABEL "
            "(e.g. APP_ACCOUNT_LABEL=Second shop) so the two accounts can never be confused"
        )
    if environment == "production":
        api_origin = PRODUCTION_API_ORIGIN
        identity_base_url = PRODUCTION_IDENTITY_BASE_URL
        consent_url = PRODUCTION_CONSENT_URL
        token_url = PRODUCTION_TOKEN_URL
    else:
        api_origin = SANDBOX_API_ORIGIN
        identity_base_url = SANDBOX_IDENTITY_BASE_URL
        consent_url = SANDBOX_CONSENT_URL
        token_url = SANDBOX_TOKEN_URL
    deletion_worker_base_url = env.get("ACCOUNT_DELETION_WORKER_URL", "").strip() or None
    deletion_worker_status_secret = env.get("ACCOUNT_DELETION_STATUS_SECRET", "").strip() or None
    if bool(deletion_worker_base_url) != bool(deletion_worker_status_secret):
        raise ConfigError(
            "Set both ACCOUNT_DELETION_WORKER_URL and ACCOUNT_DELETION_STATUS_SECRET, "
            "or leave both unset to keep the account-deletion feature inactive"
        )
    if deletion_worker_base_url is not None:
        try:
            parsed_worker_url = urllib.parse.urlsplit(deletion_worker_base_url)
        except ValueError as exc:
            raise ConfigError("ACCOUNT_DELETION_WORKER_URL is invalid") from exc
        worker_host = parsed_worker_url.hostname or ""
        if (
            parsed_worker_url.scheme != "https"
            or parsed_worker_url.port not in {None, 443}
            or parsed_worker_url.username is not None
            or parsed_worker_url.password is not None
            or parsed_worker_url.path not in {"", "/"}
            or parsed_worker_url.query
            or parsed_worker_url.fragment
            or not re.fullmatch(r"[a-z0-9-]+\.[a-z0-9-]+\.workers\.dev", worker_host)
        ):
            raise ConfigError(
                "ACCOUNT_DELETION_WORKER_URL must be a bare "
                "https://<name>.<subdomain>.workers.dev address with no path, "
                "query or fragment"
            )
        deletion_worker_base_url = f"https://{worker_host}"
        if not (32 <= len(deletion_worker_status_secret) <= 1024) or not VISIBLE_PATTERN.fullmatch(
            deletion_worker_status_secret
        ):
            raise ConfigError(
                "ACCOUNT_DELETION_STATUS_SECRET must be 32-1024 visible ASCII characters"
            )
    engine_mode = env.get("APP_ENGINE_MODE", "off").strip().lower() or "off"
    if engine_mode not in ENGINE_MODES:
        raise ConfigError("APP_ENGINE_MODE must be off, shadow, auto-dry or auto")
    if engine_mode in {"auto-dry", "auto"} and feature_stage not in {"prices", "quantities", "images"}:
        raise ConfigError(
            "APP_ENGINE_MODE auto-dry/auto needs APP_FEATURE_STAGE prices, quantities or images "
            "(the engine executes only through those already-gated services)"
        )
    config = AppConfig(
        client_id=private_values["EBAY_CLIENT_ID"],
        client_secret=private_values["EBAY_CLIENT_SECRET"],
        runame=private_values["EBAY_RUNAME"],
        admin_password=admin_password,
        data_encryption_key=encryption_key,
        marketplace_id=marketplace_id,
        content_language=content_language,
        currency=currency,
        merchant_location_key=optional_seller_values["EBAY_MERCHANT_LOCATION_KEY"],
        fulfillment_policy_id=optional_seller_values["EBAY_FULFILLMENT_POLICY_ID"],
        payment_policy_id=optional_seller_values["EBAY_PAYMENT_POLICY_ID"],
        return_policy_id=optional_seller_values["EBAY_RETURN_POLICY_ID"],
        database_path=database_path,
        host=CANONICAL_HOST,
        port=configured_port,
        account_label=account_label or None,
        allowed_hosts=LOCAL_ALLOWED_HOSTS,
        cookie_secure=False,
        request_timeout_seconds=_integer(env, "EBAY_REQUEST_TIMEOUT_SECONDS", 15, 3, 60),
        order_lookback_days=_integer(env, "EBAY_ORDER_LOOKBACK_DAYS", 30, 1, 730),
        initial_refresh_token=None,
        feature_stage=feature_stage,
        scopes=scopes_for_stage(feature_stage),
        runtime_mode="local",
        environment=environment,
        api_origin=api_origin,
        identity_base_url=identity_base_url,
        consent_url=consent_url,
        token_url=token_url,
        deletion_worker_base_url=deletion_worker_base_url,
        deletion_worker_status_secret=deletion_worker_status_secret,
        max_price_change_percent=_decimal(
            env, "APP_MAX_PRICE_CHANGE_PERCENT", Decimal("10"), Decimal("0.1"), Decimal("100")
        ),
        max_actions_per_run=_integer(env, "APP_MAX_ACTIONS_PER_RUN", 10, 1, 1000),
        require_price_floor=_boolean(env, "APP_REQUIRE_PRICE_FLOOR", True),
        dry_run=_boolean(env, "APP_DRY_RUN", environment == "production"),
        engine_mode=engine_mode,
        engine_default_margin_percent=(
            _decimal(env, "APP_ENGINE_DEFAULT_MARGIN_PERCENT", Decimal("0"), Decimal("0"), Decimal("1000"))
            if env.get("APP_ENGINE_DEFAULT_MARGIN_PERCENT", "").strip()
            else None
        ),
        engine_reprice_hours=_integer(env, "APP_ENGINE_REPRICE_HOURS", 24, 1, 168),
        engine_relist_minutes=_integer(env, "APP_ENGINE_RELIST_MINUTES", 60, 5, 1440),
        engine_orders_minutes=_integer(env, "APP_ENGINE_ORDERS_MINUTES", 30, 5, 1440),
        smtp_host=env.get("APP_SMTP_HOST", "").strip() or None,
        smtp_port=_integer(env, "APP_SMTP_PORT", 587, 1, 65535),
        smtp_user=env.get("APP_SMTP_USER", "").strip() or None,
        smtp_app_password=env.get("APP_SMTP_APP_PASSWORD", "").strip() or None,
        alert_email=env.get("APP_ALERT_EMAIL", "").strip() or None,
        engine_digest_hours=_integer(env, "APP_ENGINE_DIGEST_HOURS", 6, 1, 168),
        engine_market_check=_boolean(env, "APP_ENGINE_MARKET_CHECK", False),
    )
    return config
