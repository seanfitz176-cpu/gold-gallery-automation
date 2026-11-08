# Build plan for autonomous Claude sessions

## One goal, two layers
The project goal is an eBay seller account that runs itself, with you making
only the high-level decisions. Both halves of this repository serve that goal
and follow the same oversight model: **act autonomously inside limits you
set, stop at checkpoints for your approval, and let you override at any
time.**

| | Layer 1: Claude building the app | Layer 2: the app running the account |
|---|---|---|
| Works autonomously | through `PLAN.md` (Stop hook keeps it going) | engine jobs reprice and relist (`ebay_app/engine.py`) |
| Hard limits | gate hook: no secrets, no live eBay calls, no push to `main` | price floor, max change %, actions per run, `APP_DRY_RUN` |
| Your checkpoints | `.claude/checkpoints/phase-N.md` + `ctl.py approve N` | approval queue on `/engine` + modes off → shadow → auto-dry → auto |
| Override any time | `ctl.py stop` / Esc | **Pause engine** button, auto-pause on uncertainty, stop the app |
| Record | git history + checkpoint notes | encrypted action journal + audit export |

Layer 1 never touches your real eBay account. Every live eBay run is yours.
Layer 2 can only change listings through the same already-gated services
that every manual change uses.

Claude works through this file top to bottom. The Stop hook
(`.claude/hooks/stop_gate.py`) keeps it working until every task in the
current phase is ticked. It then writes `.claude/checkpoints/phase-N.md` and
waits. The gate hook (`.claude/hooks/gate.py`) blocks every other edit until
the owner runs `python3 .claude/ctl.py approve N`.

Standing rules, which every phase inherits from `.github/copilot-instructions.md`
and the repo's `CLAUDE.md`:
- Mocked tests only. Claude never starts the app or runs a script against eBay.
  Every live Sandbox or Production run is the owner's own step.
- Claude never reads or edits `.env`, `.env.production`, `instance/` or any
  credential file, and never pushes.
- Each task ships with its own tests. Tick a task only when the full suite
  passes (`python3 -m unittest discover -s tests`).
- Keep diffs narrow, and preserve every existing safety gate (stage allowlist,
  opt-ins, dry-run, budget, reconciliation, journaling).

Owner controls: `python3 .claude/ctl.py stop | resume | status | approve N | reject N`.
In an interactive session, Esc interrupts immediately.

## Phase 1 — Images on bulk-created listings
- [x] Optional `image_urls` CSV column (pipe-separated https URLs, max 24, duplicates rejected)
- [x] Pass images through the verified Inventory-item create and reconcile them on readback
- [x] Tests for parsing, validation and the create/verify path
- CHECKPOINT: owner reviews, then runs one Sandbox bulk upload with images

## Phase 2 — Publish step for bulk-created drafts
- [x] Design note in `docs/` covering stage, opt-in env var, dry-run behaviour, budget, and failed/partial/unknown handling for publish
- [x] `publish_offer` client method behind its own mutation guard and allowlist entry
- [x] `bulk_publish_listings.py` script mirroring `bulk_upload_listings.py`'s pre-flight, typed confirmation, dry-run default in production and per-row results
- [x] Production opt-in (`APP_ALLOW_PRODUCTION_PUBLISH`) added to `PRODUCTION_WRITE_OPT_INS`, with the config tests
- [x] Tests, including outcome-unknown when eBay's response is lost
- CHECKPOINT: owner reviews the design and code, then pilots publishing in Sandbox

## Phase 3 — Autonomy engine design (no code)
- [x] `docs/autonomy_engine.md` covering: job scheduler, the policy model (actions inside the floor/cap/budget limits run automatically, everything else goes to an approval queue), approval-queue UI, notifications, the pause-all control, and the dry-run-first rollout
- [x] List of the owner's business decisions this needs (owner chose: margin over cost, relist when in stock, email; D1–D4 still open)
- CHECKPOINT: owner signs off the design and answers the decisions

## Phase 4 — Scheduler, policy engine and approval queue (dry-run only)
- [x] Per-SKU cost table: storage (schema v11), pricing maths (`engine_pricing.py`), `import_costs.py` CSV import, engine settings in config
- [x] Dashboard page listing costs, implied current margin, target and proposal per SKU (read-only `/engine` page)
- [x] `engine_proposals` table, scheduler thread inside the app process, `APP_ENGINE_MODE` (off/shadow/auto-dry/auto), persistent pause, auto-pause after 3 errors
- [x] Reprice job (margin over cost, clamped to max change %, floor) and policy decision, shadow mode only
- [x] Approval queue (Approve/Reject on `/engine`) executing through the existing `PriceService` with a fixed engine session; auto-dry/auto run in-limit proposals; unknown outcome pauses the engine
- [ ] Relist/restock job (quantity restore; ended listings queue a republish proposal only)
- [ ] Email alerts via smtplib (immediate on unknown/pause/token failure; 6-hourly digest)
- [ ] Order watch and health check jobs (alerts only)

## Phase 5 — Second seller account
- [ ] Tasks to be written once the owner confirms how the two accounts should be separated
