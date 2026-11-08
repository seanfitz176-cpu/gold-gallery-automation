# Phase 9 design: production preparation

Status: **Draft for review — no code has been written against this design.**
Nothing here is implemented. Per the README and `.github/copilot-instructions.md`,
Production credentials must not be added, and no Production code path may be
built, until a design like this one is reviewed and approved.

## Goal

Let the owner eventually connect this application to a **real** eBay seller
account, without weakening anything Phases 1-8 already guarantee for
Sandbox, and without a moment where a Sandbox-shaped mistake (a stray test
script, a copied `.env`, a reused database file) could reach a real listing,
a real buyer, or real money.

## Non-goals (explicitly out of scope for Phase 9 itself)

- No new selling capability. Phase 9 does not add anything Phases 5-8 don't
  already do (drafts, price changes, quantity changes, one image add). It
  only changes *which account* those same actions can target, and only once
  a human has deliberately opted in.
- No bulk editing, templates, CSV import/export, reporting, dynamic
  repricing, auto-relist, buyer messaging, or order fulfillment. Those stay
  future phases, planned only after Phase 9 is live and proven.
- No hosting changes. The project stays a local application on the owner's
  own Mac; Phase 9 is about credential/data separation and safeguards, not
  about deploying anywhere.

## Current state this design builds on

- `load_config()` (`ebay_app/config.py`) already hard-fails unless
  `EBAY_ENVIRONMENT=sandbox`, and already rejects any App ID without
  `-SBX-` or any Cert ID not starting with `SBX-`. This is listed as an
  "invariant that requires approval to change" in
  `.github/copilot-instructions.md`, and this design does not remove it —
  it adds a second, real environment next to it, gated far more tightly
  than Sandbox ever was.
- There is exactly one `.env`, one SQLite database path
  (`APP_DATABASE_PATH`), and one encryption key today. Nothing currently
  stops the same file or key from being reused if someone just changed
  `EBAY_ENVIRONMENT` by hand.
- The dashboard and every page already say "Sandbox" in various places, but
  there is no single, unmissable, consistent environment banner — because
  until now there was only ever one possible environment to be in.
- `_assert_mutation_allowed()` gates writes by `feature_stage`
  (`drafts`/`prices`/`quantities`/`images`), not by environment. Today
  environment and stage are independent: nothing currently prevents
  `EBAY_ENVIRONMENT` and `APP_FEATURE_STAGE` from being set to any
  combination.

## Proposed design

### 1. A second, completely separate credential and data set

- A new `EBAY_ENVIRONMENT=production` value becomes valid, but only once
  everything below exists — until then it stays rejected exactly as today.
- Production credentials live in a **separate env file**
  (`.env.production`, never `.env`), loaded only when a new, separate
  launch flag is used (e.g. `python run.py --env production` or a
  dedicated `run_production.py` entry point) — not by editing a value
  inside the file already used for Sandbox. This makes "I meant to test in
  Sandbox but was actually pointed at production" structurally harder: it
  requires picking a different file and a different command, not flipping
  one line.
- A **separate SQLite database path** and a **separate
  `APP_DATA_ENCRYPTION_KEY`** are required for production and validated to
  be different from whatever Sandbox is currently configured with (fail
  closed if they match). This stops a copy-pasted `.env` from silently
  reusing Sandbox's stored refresh token, granted scopes, or action
  history against a real account.
- App ID / Cert ID validation flips: a production config must **not**
  contain `-SBX-` / start with `SBX-` (today's Sandbox-only checks), the
  same way Sandbox today rejects anything that looks like a production
  credential. Both directions fail closed.

### 2. Explicit, unmissable environment indicators

- Every page (dashboard, listings, orders, review screens, confirmation
  screens, result pages) gets a persistent, high-contrast banner naming the
  live environment — "SANDBOX" in its current neutral styling stays as is;
  a new "LIVE — REAL EBAY ACCOUNT" banner in a distinct, deliberately
  alarming color (e.g. red) appears everywhere once `EBAY_ENVIRONMENT=production`.
- The confirmation screen for *any* mutation in production additionally
  repeats the environment and the literal seller/account identifier being
  acted on, directly above the confirm button — not just at the top of the
  page — since that is the screen where a moment's inattention has real
  consequences.
- Server startup logs the active environment loudly (plain text, not just a
  flag buried in config) so a terminal glance is enough to know which
  account a running process is pointed at.

### 3. Stronger deployment controls beyond the environment flag alone

Two independent things must both be true before any production write is
possible — not just one:

- `EBAY_ENVIRONMENT=production` (as today's mechanism, extended).
- A new, separate `APP_ALLOW_PRODUCTION_WRITES=yes-i-understand-the-risk`
  style flag (an exact literal string, not a boolean), defaulting unset/
  closed. This is deliberately awkward to set by accident — it must be a
  conscious, typed acknowledgement, not a stray `true` copied from another
  setting.
- Even with both set, production starts **read-only**: a new
  `production-readonly` stage (mirroring today's `connection`/`account`/
  `listings`/`orders` reads) is the only reachable stage at first. Advancing
  a real account past read-only into `drafts`/`prices`/`quantities`/`images`
  requires the same kind of explicit, reviewed sign-off this project has
  used for every Sandbox phase — i.e. Phase 9 itself only delivers
  read-only production access; write capability against a real account
  becomes its own later phase(s), reusing the existing services but
  re-approved one at a time for production specifically.
- A single, obvious "kill switch": clearing `APP_ALLOW_PRODUCTION_WRITES`
  (or removing `.env.production`) immediately and completely disables every
  production write path on next restart, with no other code change needed.

### 4. Mutation journaling and reconciliation, unchanged in kind but separated in fact

- The existing checkpointed-action pattern (`create_pending_action` →
  `claim_pending_action` → `complete_action`) is reused as-is — it is
  already environment-agnostic and already fails closed on ambiguity. The
  only change is that production's actions live in production's own
  database file, never mixed with Sandbox's history.
- Given real consequences, production's reconciliation-required
  (`outcome_unknown`) results should be surfaced more insistently than
  Sandbox's today — e.g. the dashboard's "recent completed confirmations"
  table visually separates or flags any `unknown`/`partial` production
  result rather than listing it identically to a routine `succeeded` one.

### 5. Testing requirements before this ships

- Every new environment-selection and validation rule needs mocked unit
  tests exactly like every existing config rule (`tests/test_config.py`
  already covers the Sandbox-only checks this design extends).
- No test may contact real eBay endpoints; production's own read-only
  calls get exercised the same way Sandbox's are today — against a fake
  transport.
- A manual, written pre-flight checklist (separate from automated tests)
  is run once, by the owner, before the first real `EBAY_ENVIRONMENT=production`
  launch: confirms `.env.production` exists with production-only
  credentials, confirms the production database path and encryption key
  are new and distinct from Sandbox's, confirms `APP_ALLOW_PRODUCTION_WRITES`
  is unset (so the very first production run is verified read-only before
  anything else is touched).

## Open questions for the owner (need answers before implementation starts)

1. **First real capability.** Once read-only production access is proven,
   which single write should be approved first — a price change on one
   already-existing real listing (closest to how Phase 6 was proven), or
   something else? Sequencing it as its own small, reviewed phase (like
   every phase so far) rather than "all of Phases 5-8 at once" is the
   recommended default.
2. **Which real eBay account.** Is there already a specific, existing
   production seller account this should connect to, or does that account
   still need to be set up/verified first?
3. **Launch mechanism preference.** Is a separate `--env production` CLI
   flag / separate entry-point script an acceptable way to select
   production (as proposed above), or is a different mechanism preferred
   (e.g. a separate copy of the whole application directory)?
4. **Anything beyond this design's scope that should be pulled in now**
   (e.g. basic monitoring/alerting on production mutation failures) rather
   than deferred to a later phase?

## Recommended next step

Once the owner answers the open questions above and approves or amends this
design, implementation proceeds the same way every prior phase did: one
small, reviewed, test-covered change at a time, starting with the
`EBAY_ENVIRONMENT=production` + separate-credentials + read-only-only
groundwork (items 1-2 above), live-verified read-only against the real
account before anything resembling a write is even scaffolded.
