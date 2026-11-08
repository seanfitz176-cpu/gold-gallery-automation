# Checkpoint: Phase 3, autonomy engine design

The design is in `docs/autonomy_engine.md`. No code changed.

## Main points
- The engine runs as a background thread **inside** the existing app, because the
  database only allows one process at a time.
- Jobs only *propose* changes. A fixed rule decides whether each proposal runs
  automatically (inside all your limits) or waits in your approval queue.
- Everything executes through the existing, already-tested services. That means
  the same journaling, the same live-data check before every write, and the same
  dry-run switch and budget. **No new path that writes to eBay.**
- Repricing uses cost × (1 + margin), moving at most your max change % per run and
  never going below the floor. A SKU with no cost recorded is never repriced.
- Relisting works by restoring quantity, with eBay's out-of-stock option turned on.
  Ended listings only ever go to your queue.
- There are four modes: off → shadow → auto-dry → auto. You can pause from a
  dashboard button, and the engine pauses itself on any uncertain outcome.
- Email alerts are sent immediately for problems, plus a 6-hourly digest.

## What I need from you to start Phase 4
- **D1.** The default margin, e.g. 30%.
- **D2.** Whether cost means the item only, or item + postage + eBay fees.
- **D3.** Whether you'll type in or upload a cost per SKU, or want it calculated
  from gold melt value.
- **D4.** Whether the job timings are OK: reprice every 6 h, relist check hourly,
  orders every 30 min.

Approve with `python3 .claude/ctl.py approve 3`, or reply with your answers.
