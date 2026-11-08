#!/usr/bin/env python3
"""Stop hook: keeps an autonomous session working through PLAN.md.

Runs whenever Claude tries to finish its turn. Claude is allowed to stop
when any of these is true:

- the owner paused the session (`.claude/STOP` exists);
- a checkpoint note is waiting for owner approval;
- every task in PLAN.md is ticked;
- this hook has already sent Claude back once in this stop sequence
  (`stop_hook_active`), which prevents an infinite loop.

Otherwise it blocks the stop and tells Claude to carry on with the next
unticked task, or to write the checkpoint note if the phase is finished.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
CLAUDE_DIR = ROOT / ".claude"
PLAN = ROOT / "PLAN.md"

PHASE_RE = re.compile(r"^##\s+Phase\s+(\d+)\b(.*)$")
TASK_RE = re.compile(r"^\s*-\s+\[( |x|X)\]\s+(.*)$")


def allow() -> None:
    sys.exit(0)


def current_phase() -> tuple[int, str, list[str]] | None:
    """First phase whose checkpoint has not been approved, with its open tasks."""
    if not PLAN.is_file():
        return None
    phases: list[tuple[int, str, list[tuple[bool, str]]]] = []
    for line in PLAN.read_text(encoding="utf-8").splitlines():
        m = PHASE_RE.match(line)
        if m:
            phases.append((int(m.group(1)), m.group(2).strip(" -—:"), []))
            continue
        t = TASK_RE.match(line)
        if t and phases:
            phases[-1][2].append((t.group(1) != " ", t.group(2).strip()))
    for number, title, tasks in phases:
        if not (CLAUDE_DIR / "checkpoints" / f"phase-{number}.approved").exists():
            return number, title, [text for done, text in tasks if not done]
    return None


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception:
        allow()  # never trap the session because of a parsing problem
    if event.get("stop_hook_active"):
        allow()
    if (CLAUDE_DIR / "STOP").exists():
        allow()
    checkpoints = CLAUDE_DIR / "checkpoints"
    if checkpoints.is_dir():
        for note in checkpoints.glob("phase-*.md"):
            if not note.with_suffix(".approved").exists():
                allow()  # waiting on the owner -- stopping is correct

    phase = current_phase()
    if phase is None:
        allow()  # everything approved, or no plan
    number, title, open_tasks = phase
    if open_tasks:
        reason = (f"PLAN.md Phase {number} ({title}) still has open tasks. "
                  f"Continue with: '{open_tasks[0]}'. Tick each task in PLAN.md "
                  "when it is done and its tests pass.")
    else:
        reason = (f"Every task in PLAN.md Phase {number} ({title}) is ticked. Run "
                  "the full test suite, then write .claude/checkpoints/"
                  f"phase-{number}.md summarising what changed, the exact test "
                  "command and totals, anything untested, and what the owner must "
                  "check -- then stop and wait for approval.")
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


if __name__ == "__main__":
    main()
