# CLAUDE.md — Gold Gallery Automation (eBay seller-ops platform)

This file is the standing brief for any Claude session (scheduled, device-bridge,
or otherwise) picking up work on this codebase cold, with no memory of prior
conversations. Read this in full before touching anything.

## What this project is

An eBay seller-automation platform for Sean Fitzgerald's UK-based gold/jewellery
reselling business ("Gold Gallery"). It talks to eBay's official REST APIs
(Inventory, Fulfillment, Identity, Taxonomy) to create and manage listings, read
orders, and -- gradually, deliberately, stage by stage -- make real account writes.
The long-term goal (not yet reached) is for the account to run itself day to day,
with human input reserved for high-level decisions. Dynamic repricing, auto-relist,
buyer messaging, and order-fulfillment writes are explicit future phases -- **not
designed, not started** -- gated on everything below first being proven live.

Codebase location: `~/Documents/Ebay API/python_app` on Sean's Mac ("macbook-air"),
reached from a cloud session via the remote-devices device bridge
(`mcp__remote-devices__device_bash` etc.) -- paths there look like
`$HOME/mnt/Documents/Ebay API/python_app`. There is no other copy of the real
running app; a GitHub remote exists (`seanfitz176-cpu/gold-gallery-automation`,
private) purely for version history and backup, not as a second place the app runs.

## The single most important fact about this project

**Two environments, never allowed to touch each other:**
- **Sandbox** (`.env`) -- fake eBay test account, fake data. This is where almost
  all development and testing happens, including anything a Claude session does
  on its own initiative.
- **Production** (`.env.production`) -- Sean's real eBay seller account, real
  listings, real buyers, real money. `config.py` enforces hard structural gates
  so a Sandbox config can never accidentally reach Production or vice versa
  (separate credential files, separate database paths/encryption keys that are
  checked against each other and refused if they collide, separate API origins).

**The feature-stage gate.** `APP_FEATURE_STAGE` (in whichever `.env*` file is
active) picks exactly one capability level: `connection` / `account` / `listings`
/ `orders` (read-only) -> `drafts` -> `prices` -> `quantities` -> `images` ->
`migration` (one-off, not meant to stay selected) -> `setup`. Stages are NOT
cumulative except where documented (`prices`/`quantities`/`images` each build on
`drafts`'s scopes). In Production, write-capable stages additionally require
their own separate, explicit opt-in env var (e.g.
`APP_ALLOW_PRODUCTION_PRICE_WRITES=yes-i-understand-the-risk-of-automated-price-changes`)
-- being in `environment=production` is never enough by itself. `drafts` and
`setup` have no Production opt-in path at all; they are structurally unreachable
in Production regardless of anything in the env file.

**`.env.production` is Sean's file to edit, never Claude's.** Every single change
to it -- the feature stage, the dry-run flag, any opt-in -- is his own deliberate,
separate decision. A Claude session reads it only indirectly (via non-secret-safe
tooling like `check_pilot_preconditions.py`, or targeted greps for key *names*,
never values) and never edits it, never prints its contents, never reconstructs
or guesses a credential from it.

**Real writes always need Sean's own per-action confirmation.** This is not a
permission that can be pre-authorized, generalized from a standing instruction,
or unlocked by any amount of tooling (git, scheduled tasks, device access). Every
real mutation in this app already requires a live, session-bound, CSRF-protected,
one-use confirmation token *inside the running app itself* -- and on top of that,
no Claude session should ever attempt, encourage skipping, or work around that
confirmation step for a real Production write. Sandbox writes (fake data) are
fine to do autonomously; Production writes are not, ever, without Sean watching
and confirming in the moment.

## Safety philosophy baked into the code -- mirror it in anything new

Every write-capable service (`DraftService`, `PriceService`, `QuantityService`,
`ImageService`, `BulkListingService`) follows the same shape: re-read the current
live state immediately before mutating, compare it to what was shown at review
time, and classify any failure into one of three outcomes rather than a single
generic "failed":
- **`failed`** -- no eBay mutation was made; safe to just retry.
- **`partial`** -- an earlier step of a multi-step write succeeded (e.g. the
  Inventory item exists) but a later step didn't (e.g. no Offer) -- retrying
  naively will NOT detect or fix this; it needs a human to look.
- **`unknown`** -- the mutation call itself got an ambiguous response (timeout,
  connection error, or a response that doesn't definitively confirm success or
  failure) -- the write may or may not have actually happened; never assumed
  either way, never auto-retried.

This exact three-way split was missing from `BulkListingService.create_batch()`
when it was first built and had to be added afterward (see
"Known Quirks & Minor Fixes" / Production Rollout History in the fuller project
doc if you have access to it) -- any new write path must have this from the start,
not bolted on later.

Ambiguous/uncertain outcomes are **journaled, never auto-retried**. The admin
action journal (`ebay_app/storage.py`'s `actions` / `action_events` tables) is
append-only and encrypted at rest; `recent_actions()` currently caps at 50 rows
with no filtering -- a fuller queryable/exportable audit report does not exist yet
(flagged as an open gap in the Phase 9 readiness report).

## Working conventions -- follow these exactly

- **Editing files on the Mac**: always via `device_bash`, using `sed -i`, a short
  Python read-modify-write script, or a heredoc -- never by retyping file content
  from earlier tool output, which can be silently truncated.
- **Running the test suite** (the real `.env.production` file must never be
  present during a test run, since the suite asserts things about a clean
  environment): always
  ```
  mv .env.production .env.production.testhide
  python3 -m unittest discover -s tests          # or -p "test_foo.py" to target one file
  mv .env.production.testhide .env.production
  ```
  every single time, even if the run fails partway through -- never leave
  `.env.production` renamed. Use the system `/usr/bin/python3` (3.10.12) -- the
  repo's own `.venv/bin/python3` symlink is broken in the device-bridge
  environment.
- **Every change needs a passing test.** Never leave the suite red. As of
  2026-11-08 the suite is at 649/649 (grows with each feature/fix).
- **Script conventions**: standalone scripts at the repo root
  (`check_account.py`, `find_pilot_candidate.py`, `migrate_listing.py`,
  `bulk_upload_listings.py`, etc.) follow a consistent shape -- `load_config()`
  for Sandbox, `load_config(PRODUCTION_ENV_PATH)` behind an explicit
  `--production` flag, a `main(argv) -> int` entry point, no credentials ever
  printed, and a real eBay write (if any) gated behind an explicit typed
  confirmation the user must type themselves.
- **Git discipline**: before any commit, review every staged file
  (`git status --short`) and run a secret-pattern scan --
  `grep -rn -E "SBX-|PRD-[0-9a-f]{8}|AKIA[0-9A-Z]{16}|-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY"`
  -- manually inspecting every hit before committing. Never stage `.env`,
  `.env.production`, or anything `.gitignore` already excludes (it's
  comprehensive -- read it before assuming something needs adding).
  **Pushing to GitHub currently requires Sean to type a personal access token
  interactively in his own Terminal** -- there is no credential helper configured
  yet, and no Claude session (cloud or device-bridge) has standing push
  credentials of its own. Commit locally; leave pushing to Sean, or ask him to
  push when a useful batch of work has accumulated.

## Where things actually are

- `ebay_app/` -- the application package: `config.py` (environment/stage gating),
  `ebay_client.py` (HTTP + OAuth + mutation guards), `storage.py` (encrypted
  SQLite, action journal, locking), `drafts.py` / `prices.py` / `quantities.py`
  / `images.py` / `bulk_listings.py` (the write-capable services), `listings.py`
  / `orders.py` / `account.py` (read-only services), `web.py` / `views.py`
  (the local WSGI dashboard), `migrate_listing.py`'s backing logic, account
  deletion handling.
- `tests/` -- the full suite; one file per module/feature, following the existing
  naming pattern (`test_<module>.py`).
- `docs/ROADMAP.md` -- phase-by-phase history (currently stale past Phase 9 slice
  2; the fuller, more current account of everything built since lives in the
  `claude/architecture.md` project doc on claude.ai, if this session has access
  to the "Ebay Api" Project -- a device-bridge-only session may not).
- `docs/phase9_production_design.md`, `docs/phase9_production_readiness_report.md`,
  `docs/RUNBOOK.md` -- Production design, a point-in-time readiness audit, and
  operational runbook (rollback steps, common failures).
- `claude/listing_migration_plan.md`, `claude/supervised_dry_run_plan.md` --
  step-by-step plans for bringing a classic listing under management, and for
  the supervised real-Production pricing pilot, respectively (these currently
  live in the claude.ai Project, not the git repo).
- `run.py` -- Sandbox entry point. `run_production.py` -- Production entry point
  (separate on purpose; no `--env` flag exists or should be added).
- `.env.example` / `.env.production.example` -- safe templates; the real
  `.env` / `.env.production` files are gitignored and never committed.

## Current status (as of 2026-11-08) -- what's actually done vs. not

**Done and live-verified in Sandbox:** connection/account/listings/orders reads;
draft (unpublished Inventory item + Offer) creation; price changes; quantity
changes; image-URL attachment. All four of the last write stages have their own
dry-run + per-run action budget plumbing.

**Done and live-verified in Production:** read-only connection/account/listings/
orders; the Marketplace Account Deletion notification endpoint (registered and
verified with eBay); one real classic listing migrated into Inventory API
management via the new `migration` stage. Production write capability
(`prices`/`quantities`/`images`) exists in code with its own opt-in gates, but
**no opt-in is set in the real `.env.production` yet** -- reaching it in
Production is Sean's own future, separate decision, documented step-by-step in
`claude/supervised_dry_run_plan.md`.

**Built and tested but never run live:** the bulk-CSV-upload feature
(`bulk_listings.py` / `bulk_upload_listings.py`) -- 11 tests, fully passing, but
blocked on Sean reconnecting the Sandbox OAuth grant (it had lapsed as of
2026-11-08) and then choosing when to run it for the first time. Deliberately
does not publish anything or attach images -- both flagged as separate,
not-yet-requested future decisions in the module's own docstring.

**Not started, not designed:** dynamic repricing, auto-relist, buyer messaging,
order fulfillment writes. `docs/ROADMAP.md` and `docs/phase9_production_design.md`
both explicitly state these are future phases, planned only after Phase 9 is
live and proven -- don't start designing or building any of them without Sean
explicitly asking for that specific phase.

**Open, low-priority, explicitly deferred by Sean ("not urgent")**: a stale
Sandbox credential backup file (`.env.bak.20260922000429`) not rotated since
2026-09-22; an ambiguous rotation date on `.cf_api_token`. Optional, never
required: migrating the admin dashboard password from plaintext to a hash
(`ebay_app/password_hash.py` already supports this -- it's backward compatible,
so the legacy plaintext form keeps working until Sean chooses to migrate it).

## What genuinely needs Sean, not more Claude work

A lot of what's left is not a coding bottleneck -- be honest about this rather
than inventing busywork:
- Reconnecting Sandbox OAuth (browser-based login, only he can do it) before
  the bulk-upload feature can be tested live.
- The entire supervised Production pricing pilot
  (`claude/supervised_dry_run_plan.md`) is deliberately human-paced: watching
  real listing state, confirming each step himself, repeating across several
  separate runs before trusting it -- this cannot be compressed or delegated.
- Decisions only he can make: whether/how bulk-created drafts should eventually
  publish or get images; whether and when to move Production into
  `quantities`/`images`; which future phase (if any) to scope next.

A scheduled/recurring Claude task was considered and explicitly rejected by Sean
(2026-11-08) -- this is finite work to finish, not an open-ended maintenance job,
so don't re-propose a recurring cadence unless he asks for one again.
