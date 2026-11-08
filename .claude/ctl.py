#!/usr/bin/env python3
"""Owner controls for autonomous Claude sessions. Run from the repo root.

    python3 .claude/ctl.py stop        # pause Claude immediately (next action is blocked)
    python3 .claude/ctl.py resume      # lift the pause
    python3 .claude/ctl.py status      # show pause state, current phase, pending checkpoints
    python3 .claude/ctl.py approve 2   # approve Phase 2's checkpoint so Claude can move on
    python3 .claude/ctl.py reject 2    # delete Phase 2's checkpoint note so Claude must redo it

Claude itself is blocked from running this script or touching the files it
manages -- these are yours alone.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_DIR = Path(__file__).resolve().parent
STOP = CLAUDE_DIR / "STOP"
CHECKPOINTS = CLAUDE_DIR / "checkpoints"


def status() -> None:
    print("Paused:  " + ("YES (.claude/STOP exists)" if STOP.exists() else "no"))
    notes = sorted(CHECKPOINTS.glob("phase-*.md")) if CHECKPOINTS.is_dir() else []
    if not notes:
        print("Checkpoints: none written yet")
    for note in notes:
        state = "approved" if note.with_suffix(".approved").exists() else "WAITING FOR YOU"
        print(f"  {note.name}: {state}")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "stop":
        STOP.write_text(f"paused {datetime.now(timezone.utc).isoformat()}\n", encoding="utf-8")
        print("Paused. Claude's next action will be blocked. In an interactive session, "
              "press Esc as well to interrupt the current step.")
    elif cmd == "resume":
        STOP.unlink(missing_ok=True)
        print("Resumed.")
    elif cmd == "status":
        status()
    elif cmd in ("approve", "reject") and len(argv) == 2 and argv[1].isdigit():
        note = CHECKPOINTS / f"phase-{argv[1]}.md"
        if not note.exists():
            print(f"No checkpoint note {note.name} to {cmd}.")
            return 1
        if cmd == "approve":
            note.with_suffix(".approved").write_text(
                f"approved {datetime.now(timezone.utc).isoformat()}\n", encoding="utf-8")
            print(f"Phase {argv[1]} approved. Tell Claude to continue.")
        else:
            note.unlink()
            print(f"Phase {argv[1]} checkpoint removed. Tell Claude what to change.")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
