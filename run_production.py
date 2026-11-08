#!/usr/bin/env python3
"""Dedicated Production entry point -- see docs/phase9_production_design.md.

Deliberately a separate script from run.py, never a --env flag on it: the
project's existing single-permanent-configuration philosophy (run.py refuses
any startup argument) extends here by requiring a different *file*, not a
different flag, so an ordinary Sandbox habit of "just run run.py" can never
accidentally reach a real account, and there is no flag to mistype.

This script loads the file literally named ".env.production", beside this
script, and only that file. Every real gate that decides whether that file
is even allowed to mean "production" -- the file's own EBAY_ENVIRONMENT
value, its explicit APP_ALLOW_PRODUCTION_ENVIRONMENT opt-in, and a
read-only-only APP_FEATURE_STAGE -- is enforced inside
``ebay_app/config.py``, not here; this script only refuses to run at all if,
after all of that, the resulting config somehow did not come back as
production (defense in depth, not the primary gate).

Every feature stage under this configuration is read-only
(connection/account/listings/orders) UNLESS the owner has also separately,
explicitly opted in to "prices", "quantities", or "images" (see
PRODUCTION_WRITE_OPT_INS in ebay_app/config.py) -- each stage has its own
independent opt-in, and every other write-capable stage (currently "drafts"
and "setup") remains structurally unreachable here regardless of anything in
.env.production, until it too is given its own entry in that dict after its
own separate review. Even once a stage is reachable, AppConfig.dry_run
defaults to true for a production config, so no real eBay write happens
until the owner also explicitly sets APP_DRY_RUN=false. The startup log line
below always states the real, current state of both of these plainly.
"""

from __future__ import annotations

import logging
import signal
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import urlsplit
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from ebay_app.config import (
    CANONICAL_CALLBACK_URL,
    ConfigError,
    PRODUCTION_ENV_FILENAME,
    PRODUCTION_READ_ONLY_STAGES,
    PRODUCTION_WRITE_OPT_INS,
    load_config,
)
from ebay_app.account_deletion import poll_once
from ebay_app.ebay_client import EbaySandboxClient
from ebay_app.storage import AppDatabase, StorageError
from ebay_app.web import WebApplication
from ebay_app.engine import (
    Engine,
    EngineThread,
    describe,
    listing_reader_from_service,
    order_reader_from_service,
)
from ebay_app.alerts import Notifier

# Resolved once, at import time, exactly the way run.py resolves its own
# default ".env" path -- a fixed sibling file, never a value that can be
# influenced by an environment variable, a working directory, or an
# argument.
PRODUCTION_ENV_PATH = Path(__file__).resolve().parent / PRODUCTION_ENV_FILENAME


class ThreadingServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class SafeRequestHandler(WSGIRequestHandler):
    def log_request(self, code="-", size="-") -> None:
        # Never log OAuth codes or state query values. A malformed request
        # line (garbage bytes, or e.g. a TLS handshake sent to this plain-HTTP
        # server) can reach here before the base handler ever sets
        # self.command/self.path, so both are read defensively rather than
        # assumed present.
        command = getattr(self, "command", None) or "-"
        raw_path = getattr(self, "path", None)
        path = urlsplit(raw_path).path if raw_path else "-"
        logging.getLogger("http").info(
            "%s %s %s %s", self.client_address[0], command, path, code
        )


class ShutdownRequested(Exception):
    """Raised by the SIGTERM handler so `kill`/a process manager stops the

    server through the exact same clean-shutdown path as Ctrl+C, instead of
    Python's default disposition (which would skip the `finally` block below
    and could leave the encrypted database connection unclosed on a real
    production account).
    """


def _handle_sigterm(signum, frame) -> None:  # noqa: ARG001 - required signal handler signature
    raise ShutdownRequested()


def _install_sigterm_handler() -> None:
    signal.signal(signal.SIGTERM, _handle_sigterm)


def _capability_description(config) -> str:
    """The real, current write-capability state, stated plainly for the
    startup log line -- see the module docstring. Kept as its own pure
    function (rather than inlined in main()) specifically so it has direct
    unit test coverage without needing to start a real server."""
    if config.feature_stage not in PRODUCTION_READ_ONLY_STAGES:
        # Only a stage with its own entry in PRODUCTION_WRITE_OPT_INS can
        # reach this branch at all -- config.py's own gate structurally
        # refuses every other write-capable stage before main() ever runs.
        assert config.feature_stage in PRODUCTION_WRITE_OPT_INS
        label = config.feature_stage.upper()
        if config.feature_stage == "migration":
            # migrate_listing() has no dry-run branch at all -- unlike
            # prices/quantities/images, APP_DRY_RUN is not wired into it,
            # so migrate_listing.py always makes a real eBay write when it
            # runs. Saying "DRY RUN" here regardless of config.dry_run
            # would be actively misleading, not just imprecise.
            return f"{label} ENABLED -- NO DRY RUN for this stage; migrate_listing.py always makes a real eBay write"
        if config.dry_run:
            return f"{label} CHANGES ENABLED -- DRY RUN, no real eBay write will be made"
        return f"{label} CHANGES ENABLED -- LIVE WRITES, real eBay writes WILL be made"
    return "read-only"


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    database = None
    engine_thread = None
    try:
        if argv:
            raise ConfigError(
                "This application uses one permanent production configuration; "
                "startup flags are not supported"
            )
        config = load_config(PRODUCTION_ENV_PATH)
        if config.environment != "production":
            # Structurally unreachable given load_config()'s own gates today
            # (a config loaded from a file literally named .env.production
            # either satisfies every production gate or load_config() itself
            # raises ConfigError) -- but fail loudly rather than silently
            # run real traffic through whatever load_config() decided this
            # file actually was.
            raise ConfigError(
                f"{PRODUCTION_ENV_PATH} did not resolve to the production "
                "environment; refusing to start"
            )
        database = AppDatabase(
            config.database_path,
            config.data_encryption_key,
            environment=config.environment.capitalize(),
        )
        ebay = EbaySandboxClient(config, database)
        try:
            startup_poll = poll_once(config, ebay, database)
            if startup_poll.configured and startup_poll.error:
                logging.warning("Account-deletion startup poll failed: %s", startup_poll.error)
            elif startup_poll.configured and startup_poll.owner_deletion_detected:
                logging.warning(
                    "eBay reported this application's connected account closed; "
                    "a blocking confirmation banner will be shown until acknowledged"
                )
        except Exception:
            # Best-effort only: a failed startup poll must never stop the
            # application from starting (see ebay_app/account_deletion.py).
            logging.exception("Unexpected error during the account-deletion startup poll")
        application = WebApplication(config, database, ebay)
        if config.engine_mode != "off":
            engine = Engine(
                config, database, listing_reader_from_service(application.listing_service),
                ebay=ebay, price_service=application.price_service,
                quantity_service=application.quantity_service,
                notifier=Notifier(config),
                # Order watch only where the stage grants order permission;
                # elsewhere it would just trip the permission health check.
                order_reader=(
                    order_reader_from_service(application.order_service)
                    if config.feature_stage in {"orders", "drafts", "prices", "quantities", "images"}
                    else None
                ),
            )
            application.engine = engine
            engine_thread = EngineThread(engine)
            engine_thread.start()
            headline, detail = describe(engine.status())
            logging.warning("%s. %s", headline, detail)
        server = make_server(
            config.host,
            config.port,
            application,
            server_class=ThreadingServer,
            handler_class=SafeRequestHandler,
        )
        logging.warning(
            "*** PRODUCTION -- REAL EBAY ACCOUNT *** listening at http://%s:%s; "
            "OAuth callback=%s; feature stage=%s (%s)",
            config.host,
            config.port,
            CANONICAL_CALLBACK_URL,
            config.feature_stage,
            _capability_description(config),
        )
        _install_sigterm_handler()
        server.serve_forever()
        return 0
    except (KeyboardInterrupt, ShutdownRequested):
        logging.info("Stopping production eBay app")
        return 0
    except (ConfigError, StorageError, OSError) as error:
        logging.error("Cannot start: %s", error)
        return 1
    finally:
        if engine_thread is not None:
            engine_thread.stop()
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
