from __future__ import annotations

import smtplib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ebay_app.alerts import Notifier
from ebay_app.engine import Engine
from ebay_app.storage import AppDatabase

from helpers import make_config


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout, fail=False):
        self.host, self.port, self.fail = host, port, fail
        self.calls = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        if self.fail:
            raise smtplib.SMTPAuthenticationError(535, b"bad password")
        self.calls.append(("login", user))

    def send_message(self, message):
        self.calls.append(("send", message["To"], message["Subject"], message.get_content()))


def configured(config):
    return replace(config, smtp_host="smtp.mail.me.com", smtp_port=587, smtp_user="me@icloud.com",
                   smtp_app_password="app-specific-pw", alert_email="me@icloud.com")


class NotifierTests(unittest.TestCase):
    def setUp(self):
        FakeSMTP.instances = []
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = make_config(Path(self.temp.name))

    def test_unconfigured_sends_nothing(self):
        notifier = Notifier(self.config, smtp_factory=FakeSMTP)
        self.assertFalse(notifier.configured)
        self.assertFalse(notifier.send("x", "y"))
        self.assertEqual(FakeSMTP.instances, [])

    def test_configured_uses_starttls_and_login_before_sending(self):
        notifier = Notifier(configured(self.config), smtp_factory=FakeSMTP)
        self.assertTrue(notifier.send("Hello", "Body"))
        calls = FakeSMTP.instances[0].calls
        self.assertEqual(calls[0], "starttls")
        self.assertEqual(calls[1], ("login", "me@icloud.com"))
        self.assertEqual(calls[2][1], "me@icloud.com")
        self.assertIn("Hello", calls[2][2])

    def test_send_failure_is_swallowed_and_never_logs_the_password(self):
        def failing(host, port, timeout):
            return FakeSMTP(host, port, timeout, fail=True)

        with self.assertLogs("ebay_engine", level="WARNING") as logs:
            self.assertFalse(Notifier(configured(self.config), smtp_factory=failing).send("x", "y"))
        self.assertNotIn("app-specific-pw", "\n".join(logs.output))


class EngineAlertTests(unittest.TestCase):
    def setUp(self):
        FakeSMTP.instances = []
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.t = [1_800_000_000.0]
        self.config = configured(replace(make_config(Path(self.temp.name)), engine_mode="shadow"))
        self.db = AppDatabase(self.config.database_path, self.config.data_encryption_key, now=lambda: self.t[0])
        self.addCleanup(self.db.close)
        self.engine = Engine(self.config, self.db, lambda: [], now=lambda: self.t[0],
                             notifier=Notifier(self.config, smtp_factory=FakeSMTP))

    def sent(self):
        return [c for i in FakeSMTP.instances for c in i.calls if isinstance(c, tuple) and c[0] == "send"]

    def test_pause_emails_once_not_on_every_repeat(self):
        self.engine.pause("first")
        self.engine.pause("second")
        self.assertEqual(len(self.sent()), 1)
        self.assertIn("PAUSED", self.sent()[0][2])

    def test_digest_only_when_something_is_waiting_and_at_most_every_six_hours(self):
        self.assertFalse(self.engine.maybe_send_digest())  # nothing to report
        self.db.record_engine_proposal(sku="A", kind="price", current_value="40.00", proposed_value="44.00",
                                       currency="GBP", reason="r", decision="queue", ttl_seconds=86400)
        self.t[0] += 6 * 3600
        self.assertTrue(self.engine.maybe_send_digest())
        self.assertIn("A: price 40.00 -> 44.00", self.sent()[-1][3])
        self.t[0] += 3600
        self.assertFalse(self.engine.maybe_send_digest())  # throttled
        self.t[0] += 6 * 3600
        self.assertTrue(self.engine.maybe_send_digest())


if __name__ == "__main__":
    unittest.main()
