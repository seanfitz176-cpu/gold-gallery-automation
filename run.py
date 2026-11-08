#!/usr/bin/env python3
from __future__ import annotations

import logging
import signal
from socketserver import ThreadingMixIn
from urllib.parse import urlsplit
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from ebay_app.config import (
    CANONICAL_CALLBACK_URL,
    ConfigError,
    load_config,
)
from ebay_app.account_deletion import poll_once
from ebay_app.ebay_client import EbaySandboxClient
from ebay_app.storage import AppDatabase, StorageError
from ebay_app.web import WebApplication


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
    and could leave the encrypted database connection unclosed).
    """


def _handle_sigterm(signum, frame) -> None:  # noqa: ARG001 - required signal handler signature
    raise ShutdownRequested()


def _install_sigterm_handler() -> None:
    signal.signal(signal.SIGTERM, _handle_sigterm)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    database = None
    try:
        if argv:
            raise ConfigError(
                "This application uses one permanent local OAuth configuration; "
                "startup flags are not supported"
            )
        config = load_config()
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
        server = make_server(
            config.host,
            config.port,
            application,
            server_class=ThreadingServer,
            handler_class=SafeRequestHandler,
        )
        logging.info(
            "eBay Sandbox app listening at http://%s:%s; OAuth callback=%s",
            config.host,
            config.port,
            CANONICAL_CALLBACK_URL,
        )
        _install_sigterm_handler()
        server.serve_forever()
        return 0
    except (KeyboardInterrupt, ShutdownRequested):
        logging.info("Stopping eBay Sandbox app")
        return 0
    except (ConfigError, StorageError, OSError) as error:
        logging.error("Cannot start: %s", error)
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
