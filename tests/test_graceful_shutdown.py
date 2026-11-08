"""Pre-production hardening pass (Section 5 -- reliability: graceful shutdown).

Before this pass, `run.py` and `run_production.py` only handled
`KeyboardInterrupt` (Ctrl+C). A `SIGTERM` -- what `kill`, a process
supervisor, or `docker stop` sends by default -- used Python's default
disposition, which terminates the process immediately and skips the
`finally: database.close()` block, risking an unclosed SQLite connection
during a write in a real deployment.

These tests do not send a real OS signal (that would affect the whole test
process) or start a real server. They exercise exactly what the signal
handler does -- translate `SIGTERM` into the same clean-shutdown exception
path as `KeyboardInterrupt` -- and confirm it is actually installed under
the name Python's `signal` module expects.
"""

from __future__ import annotations

import signal
import unittest


class _SigtermHandlerTestsMixin:
    module = None  # set by subclasses

    def setUp(self) -> None:
        self._original_handler = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, self._original_handler)

    def test_handler_raises_shutdown_requested(self):
        with self.assertRaises(self.module.ShutdownRequested):
            self.module._handle_sigterm(signal.SIGTERM, None)

    def test_install_registers_the_handler_for_sigterm(self):
        self.module._install_sigterm_handler()
        self.assertIs(signal.getsignal(signal.SIGTERM), self.module._handle_sigterm)

    def test_shutdown_requested_is_caught_alongside_keyboard_interrupt(self):
        # main()'s except clause must list both exceptions together so a
        # real SIGTERM reaches the identical logging + `finally` cleanup
        # path that Ctrl+C already used.
        import inspect

        source = inspect.getsource(self.module.main)
        self.assertIn("except (KeyboardInterrupt, ShutdownRequested)", source)
        self.assertIn("_install_sigterm_handler()", source)


class RunPySigtermTests(_SigtermHandlerTestsMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        import run

        self.module = run


class RunProductionPySigtermTests(_SigtermHandlerTestsMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        import run_production

        self.module = run_production


if __name__ == "__main__":
    unittest.main()
