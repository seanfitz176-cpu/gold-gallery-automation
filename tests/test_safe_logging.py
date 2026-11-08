from __future__ import annotations

import unittest
from unittest import mock

import run
import run_production
from run import SafeRequestHandler


class SafeRequestLoggingTests(unittest.TestCase):
    def test_request_log_omits_oauth_query_values(self):
        handler = SafeRequestHandler.__new__(SafeRequestHandler)
        handler.client_address = ("127.0.0.1", 50000)
        handler.command = "GET"
        handler.path = "/oauth/callback?code=private-code&state=private-state"

        logger = mock.Mock()
        with mock.patch("run.logging.getLogger", return_value=logger):
            handler.log_request("303")

        logger.info.assert_called_once()
        rendered_arguments = " ".join(str(value) for value in logger.info.call_args.args)
        self.assertIn("/oauth/callback", rendered_arguments)
        self.assertNotIn("private-code", rendered_arguments)
        self.assertNotIn("private-state", rendered_arguments)
        self.assertNotIn("?", rendered_arguments)

    def test_request_log_survives_a_malformed_request_line(self):
        # A garbled request (random bytes, or a TLS handshake sent to this
        # plain-HTTP server -- both happen in practice from stray local
        # connections) makes the base handler's parse_request() fail before
        # self.command/self.path are ever set. log_request must not crash
        # while trying to log that failed request.
        handler = SafeRequestHandler.__new__(SafeRequestHandler)
        handler.client_address = ("127.0.0.1", 50000)
        # Deliberately no .command / .path set, mirroring the real failure.

        logger = mock.Mock()
        with mock.patch("run.logging.getLogger", return_value=logger):
            handler.log_request("400")  # must not raise AttributeError

        logger.info.assert_called_once()
        rendered_arguments = " ".join(str(value) for value in logger.info.call_args.args)
        self.assertIn("400", rendered_arguments)

    def test_production_handler_also_survives_a_malformed_request_line(self):
        # run_production.py keeps its own separate copy of this handler
        # (deliberately -- see the project's one-permanent-configuration
        # philosophy); it needs the exact same defensive fix.
        handler = run_production.SafeRequestHandler.__new__(run_production.SafeRequestHandler)
        handler.client_address = ("127.0.0.1", 50000)

        logger = mock.Mock()
        with mock.patch("run_production.logging.getLogger", return_value=logger):
            handler.log_request("400")  # must not raise AttributeError

        logger.info.assert_called_once()


if __name__ == "__main__":
    unittest.main()
