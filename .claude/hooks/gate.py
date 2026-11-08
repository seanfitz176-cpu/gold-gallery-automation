#!/usr/bin/env python3
"""PreToolUse gate for autonomous Claude Code sessions on this repository.

Runs before EVERY tool call Claude makes. Exit code 2 blocks the call and
shows stderr to Claude; exit code 0 allows it. (Exit 1 would NOT block --
Claude Code treats it as a non-blocking error -- so every refusal here is
exit 2.)

What it enforces, in order:

1. Kill switch. While `.claude/STOP` exists, every tool call is blocked.
   The owner creates/removes it with `python3 .claude/ctl.py stop|resume`.
2. Secrets. No tool may read, write, or reference real credential or data
   files (`.env`, `.env.production`, `.env.bak.*`, `instance/`, databases,
   `.cf_api_token`). The `.example` templates are allowed.
3. Live eBay. No command may start the app or run any script that talks to
   eBay (Sandbox or Production). Tests use mocked transport and stay
   allowed. Live runs are always the owner's, in their own Terminal.
4. Owner-only controls. Claude may not create, edit, or delete the STOP
   file or any checkpoint approval, and may not push, rewrite history, or
   run wrangler.
5. Checkpoints. When Claude has written `.claude/checkpoints/phase-N.md`
   and the owner has not yet approved it (`phase-N.approved`), Claude may
   not change any file except that checkpoint note -- it has to wait.

Stdlib only, matching the project's minimal-dependency rule.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
CLAUDE_DIR = ROOT / ".claude"
STOP_FILE = CLAUDE_DIR / "STOP"
CHECKPOINT_DIR = CLAUDE_DIR / "checkpoints"

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# Real secret/data files. `.env.example` and `.env.production.example` are
# deliberately NOT matched (negative lookahead for ".example").
SECRET_PATTERNS = [
    re.compile(r"(^|[\s/'\"=])\.env(?!\.example\b|\.production\.example\b)(\.[\w.-]+)?($|[\s'\";|&)])"),
    re.compile(r"\.env\.bak"),
    re.compile(r"(^|[\s/'\"])instance(/|$|[\s'\"])"),
    re.compile(r"\.cf_api_token"),
    re.compile(r"\.(sqlite3?|db)(\b|$)"),
    re.compile(r"\.production_admin_password"),
    re.compile(r"\.dev\.vars"),
]

# Anything that would start the app or contact eBay for real.
LIVE_COMMAND_PATTERNS = [
    re.compile(r"\brun(_production)?\.py\b"),
    re.compile(r"--production\b"),
    re.compile(r"\b(migrate_listing|bulk_upload_listings|bulk_publish_listings|publish_test_offer|"
               r"setup_business_policies|find_category|find_pilot_candidate|"
               r"check_connection|check_account|check_pilot_preconditions|"
               r"export_audit_log|import_costs)\.py\b"),
    re.compile(r"\bebay\.com\b"),
    re.compile(r"\bwrangler\b"),
]

OWNER_ONLY_COMMAND_PATTERNS = [
    re.compile(r"\bgit\s+push\b"),
    re.compile(r"\bgit\s+(reset\s+--hard|rebase|filter-branch|push\s+--force)\b"),
    re.compile(r"\.claude/STOP\b|\bSTOP\b.*\.claude|\.claude.*\bSTOP\b"),
    re.compile(r"\.approved\b"),
    re.compile(r"\.claude/ctl\.py\b"),
    re.compile(r"\.claude/(hooks|settings\.json)"),
]


def block(reason: str) -> None:
    print(f"BLOCKED by .claude/hooks/gate.py: {reason}", file=sys.stderr)
    sys.exit(2)


def pending_checkpoints() -> list[str]:
    if not CHECKPOINT_DIR.is_dir():
        return []
    pending = []
    for note in sorted(CHECKPOINT_DIR.glob("phase-*.md")):
        if not note.with_suffix(".approved").exists():
            pending.append(note.name)
    return pending


def collect_paths(tool_input: dict) -> list[str]:
    paths = []
    for key in ("file_path", "path", "notebook_path", "pattern", "glob"):
        value = tool_input.get(key)
        if isinstance(value, str):
            paths.append(value)
    return paths


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception:
        block("could not parse hook input; failing closed")
    tool = event.get("tool_name", "")
    tool_input = event.get("tool_input") or {}

    # 1. Kill switch -- overrides everything.
    if STOP_FILE.exists():
        block("the owner has paused this session (.claude/STOP exists). "
              "Stop working and wait; do not try to work around this.")

    command = tool_input.get("command", "") if tool == "Bash" else ""
    paths = collect_paths(tool_input)
    haystacks = paths + ([command] if command else [])

    # 2. Secrets.
    for text in haystacks:
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                block("that touches a credential or local data file. Those are "
                      "owner-only; use the .example templates instead.")

    if command:
        # 3. Live eBay.
        for pattern in LIVE_COMMAND_PATTERNS:
            if pattern.search(command):
                block("that would start the app or contact eBay. Live runs are "
                      "the owner's own step. Use the mocked test suite "
                      "(python3 -m unittest discover -s tests) instead.")
        # 3b. The suite's RunProductionEntryPointTests call run_production.main()
        # directly, which would start a LIVE server if a real .env.production
        # sits beside it. Claude may not move that file, so refuse to run
        # tests here at all -- autonomous sessions belong in a clean clone.
        if re.search(r"\b(unittest|pytest)\b", command) and (ROOT / ".env.production").exists():
            block("a real .env.production is present in this folder, and the test "
                  "suite could start a live production server. Run autonomous "
                  "sessions in a clean clone with no .env.production instead.")
        # 4. Owner-only controls.
        for pattern in OWNER_ONLY_COMMAND_PATTERNS:
            if pattern.search(command):
                block("that is an owner-only action (push, history rewrite, "
                      "kill switch, approvals, or the hook configuration).")

    if tool in WRITE_TOOLS:
        for p in paths:
            resolved = str(Path(p))
            if resolved.endswith(".approved") or resolved.endswith("/STOP") \
                    or "/.claude/hooks/" in resolved or resolved.endswith(".claude/settings.json") \
                    or resolved.endswith(".claude/ctl.py"):
                block("approvals, the kill switch, and the hook configuration "
                      "are owner-only.")

        # 5. Checkpoints awaiting approval freeze all other edits.
        pending = pending_checkpoints()
        if pending:
            allowed = {str((CHECKPOINT_DIR / name).resolve()) for name in pending}
            for p in paths:
                target = str((ROOT / p).resolve()) if not os.path.isabs(p) else str(Path(p).resolve())
                if target not in allowed:
                    block(f"checkpoint {', '.join(pending)} is waiting for owner "
                          "approval. Do not change anything else; stop and wait.")

    sys.exit(0)


if __name__ == "__main__":
    main()
