"""Tests for the autonomous-session guard rails in .claude/.

Each test runs the real hook script as a subprocess, exactly as Claude Code
does, against a throwaway project directory (CLAUDE_PROJECT_DIR), so the
real repository's STOP/checkpoint state is never touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / ".claude" / "hooks" / "gate.py"
STOP_GATE = REPO / ".claude" / "hooks" / "stop_gate.py"
CTL = REPO / ".claude" / "ctl.py"

PLAN = """# Plan
## Phase 1 — First
- [x] done thing
## Phase 2 — Second
- [ ] open thing
- [ ] another thing
"""


class HookTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / ".claude" / "checkpoints").mkdir(parents=True)
        (self.root / "PLAN.md").write_text(PLAN, encoding="utf-8")

    def run_hook(self, script: Path, event: dict) -> subprocess.CompletedProcess:
        env = dict(os.environ, CLAUDE_PROJECT_DIR=str(self.root))
        return subprocess.run(
            [sys.executable, str(script)],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    def gate(self, tool: str, **tool_input) -> subprocess.CompletedProcess:
        return self.run_hook(GATE, {"tool_name": tool, "tool_input": tool_input})

    def assertBlocked(self, result):
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("BLOCKED", result.stderr)

    def assertAllowed(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)


class GateHookTests(HookTestCase):
    def test_ordinary_work_is_allowed(self):
        self.assertAllowed(self.gate("Read", file_path=str(self.root / "ebay_app/web.py")))
        self.assertAllowed(self.gate("Edit", file_path=str(self.root / "ebay_app/web.py")))
        self.assertAllowed(self.gate("Bash", command="python3 -m unittest discover -s tests"))
        self.assertAllowed(self.gate("Read", file_path=str(self.root / ".env.example")))
        self.assertAllowed(self.gate("Read", file_path=str(self.root / ".env.production.example")))

    def test_kill_switch_blocks_everything(self):
        (self.root / ".claude" / "STOP").write_text("paused", encoding="utf-8")
        self.assertBlocked(self.gate("Read", file_path=str(self.root / "README.md")))
        self.assertBlocked(self.gate("Bash", command="ls"))

    def test_secret_files_are_blocked_in_every_tool(self):
        for path in (".env", ".env.production", ".env.bak.20260922000429",
                     "instance/ebay.sqlite", ".cf_api_token"):
            with self.subTest(path=path):
                self.assertBlocked(self.gate("Read", file_path=str(self.root / path)))
                self.assertBlocked(self.gate("Edit", file_path=str(self.root / path)))
        self.assertBlocked(self.gate("Bash", command="cat .env.production"))
        self.assertBlocked(self.gate("Bash", command="grep CLIENT .env"))

    def test_live_ebay_commands_are_blocked(self):
        for command in (
            "python3 run.py",
            "python3 run_production.py",
            "python3 bulk_upload_listings.py listings.csv",
            "python3 bulk_publish_listings.py listings.csv",
            "python3 migrate_listing.py --item-id 1",
            "python3 find_pilot_candidate.py --production",
            "curl https://api.ebay.com/sell/inventory/v1/offer",
            "npx wrangler deploy",
        ):
            with self.subTest(command=command):
                self.assertBlocked(self.gate("Bash", command=command))

    def test_owner_only_actions_are_blocked(self):
        for command in (
            "git push origin main",
            "git reset --hard HEAD~1",
            "rm .claude/STOP",
            "touch .claude/checkpoints/phase-1.approved",
            "python3 .claude/ctl.py approve 1",
        ):
            with self.subTest(command=command):
                self.assertBlocked(self.gate("Bash", command=command))
        for path in (".claude/checkpoints/phase-1.approved", ".claude/STOP",
                     ".claude/hooks/gate.py", ".claude/settings.json"):
            with self.subTest(path=path):
                self.assertBlocked(self.gate("Write", file_path=str(self.root / path)))

    def test_pending_checkpoint_freezes_every_other_edit(self):
        note = self.root / ".claude" / "checkpoints" / "phase-1.md"
        note.write_text("summary", encoding="utf-8")
        self.assertBlocked(self.gate("Edit", file_path=str(self.root / "ebay_app/web.py")))
        self.assertAllowed(self.gate("Edit", file_path=str(note)))
        self.assertAllowed(self.gate("Read", file_path=str(self.root / "ebay_app/web.py")))
        note.with_suffix(".approved").write_text("ok", encoding="utf-8")
        self.assertAllowed(self.gate("Edit", file_path=str(self.root / "ebay_app/web.py")))

    def test_tests_refuse_to_run_beside_a_real_production_env_file(self):
        (self.root / ".env.production").write_text("", encoding="utf-8")
        self.assertBlocked(self.gate("Bash", command="python3 -m unittest discover -s tests"))
        self.assertBlocked(self.gate("Bash", command="pytest -q"))

    def test_unparseable_input_fails_closed(self):
        env = dict(os.environ, CLAUDE_PROJECT_DIR=str(self.root))
        result = subprocess.run([sys.executable, str(GATE)], input="not json",
                                capture_output=True, text=True, env=env, timeout=30)
        self.assertBlocked(result)


class StopHookTests(HookTestCase):
    def stop(self, **extra):
        return self.run_hook(STOP_GATE, {"hook_event_name": "Stop", **extra})

    def decision(self, result):
        return json.loads(result.stdout) if result.stdout.strip() else None

    def approve(self, phase: int):
        cp = self.root / ".claude" / "checkpoints"
        (cp / f"phase-{phase}.md").write_text("note", encoding="utf-8")
        (cp / f"phase-{phase}.approved").write_text("ok", encoding="utf-8")

    def test_first_unapproved_phase_with_ticked_tasks_asks_for_a_checkpoint(self):
        decision = self.decision(self.stop())
        self.assertEqual(decision["decision"], "block")
        self.assertIn("phase-1.md", decision["reason"])

    def test_open_tasks_keep_claude_working(self):
        self.approve(1)
        decision = self.decision(self.stop())
        self.assertEqual(decision["decision"], "block")
        self.assertIn("open thing", decision["reason"])

    def test_stopping_is_allowed_when_paused_waiting_looping_or_finished(self):
        (self.root / ".claude" / "STOP").write_text("x", encoding="utf-8")
        self.assertIsNone(self.decision(self.stop()))
        (self.root / ".claude" / "STOP").unlink()

        self.assertIsNone(self.decision(self.stop(stop_hook_active=True)))

        (self.root / ".claude" / "checkpoints" / "phase-1.md").write_text("n", encoding="utf-8")
        self.assertIsNone(self.decision(self.stop()))

        self.approve(1)
        self.approve(2)
        self.assertIsNone(self.decision(self.stop()))


class OwnerControlTests(HookTestCase):
    def ctl(self, *args):
        # ctl.py manages the .claude directory it lives in, so run a copy
        # inside the throwaway project.
        copy = self.root / ".claude" / "ctl.py"
        copy.write_text(CTL.read_text(encoding="utf-8"), encoding="utf-8")
        return subprocess.run([sys.executable, str(copy), *args],
                              capture_output=True, text=True, timeout=30)

    def test_stop_resume_approve_reject(self):
        self.ctl("stop")
        self.assertTrue((self.root / ".claude" / "STOP").exists())
        self.ctl("resume")
        self.assertFalse((self.root / ".claude" / "STOP").exists())

        self.assertEqual(self.ctl("approve", "1").returncode, 1)  # nothing to approve yet
        note = self.root / ".claude" / "checkpoints" / "phase-1.md"
        note.write_text("n", encoding="utf-8")
        self.assertIn("WAITING FOR YOU", self.ctl("status").stdout)
        self.ctl("approve", "1")
        self.assertTrue(note.with_suffix(".approved").exists())
        self.ctl("reject", "1")
        self.assertFalse(note.exists())


if __name__ == "__main__":
    unittest.main()
