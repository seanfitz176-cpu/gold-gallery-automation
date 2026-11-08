# Phase 9 Production Readiness Report

**Scope:** eBay Sandbox Console (`python_app/`) — pre-production hardening and finalisation pass, per the brief given 2026-09-23. Read-only audit across security, testing, eBay API compliance, reliability, production safety controls, and rollout planning, followed by a small set of safe, tested fixes. **No new features were added. Nothing was switched to Production. No call was made to eBay's real Production environment.**

**Architecture note (read this first):** the brief assumes a multi-route-group platform with scheduled jobs. What actually exists is a single local-only WSGI console: one human logs in, reviews one change, and explicitly confirms it — there are no scheduled/background jobs, no bulk operations, and no second actor that could create most classic concurrency scenarios. That isn't a shortfall against the brief; it's why several items below are marked N/A rather than forced into a shape that doesn't fit. It also means the whole system currently makes very light, human-paced use of eBay's APIs.

**Test suite:** 339/339 passing (up from 329 at the start of this pass; +10 tests added in this pass, 0 regressions, 0 skips).

---

## 1. Checklist

Legend: [DONE] done and verified · [DECISION] needs your decision · [BLOCK] blocking gap

### Section 1 — Security

| # | Item | Status |
|---|---|---|
| 1.1 | No hardcoded credentials; not in git history | [DONE] (repo has no git history at all yet -- nothing to leak) |
| 1.2 | `.env` gitignored, safe `.env.example` | [DONE] |
| 1.3 | Sandbox/Production fully separate, cross-call impossible, environment logged at startup, fails closed on mixed/incomplete config | [DONE] |
| 1.4 | OAuth token flow: refresh, failure handling, encrypted storage, min-scope, safe revoked-token handling | [DONE] (alerting is console-log only -- see 5.3) |
| 1.5 | Auth on every endpoint; no debug/admin routes | [DONE] |
| 1.6 | Input validation/sanitisation (SKU, price, qty, title, description, aspects, IDs) | [DONE] |
| 1.7 | Injection protection (SQL/HTML) | [DONE] |
| 1.8 | Rate limiting | [DECISION] login only; `/connect` and `/actions/confirm` unlimited (low risk on loopback-only, single-user) |
| 1.9 | CORS | [DONE] (none set -- correct for this app) |
| 1.10 | Security headers | [DONE] (`Permissions-Policy` added this pass) |
| 1.11 | HTTPS-only | [DONE] (loopback HTTP by design; public hops are HTTPS) |
| 1.12 | No stack traces/secrets/paths leaked in errors | [DONE] |
| 1.13 | eBay Marketplace Account Deletion/Closure notification endpoint | [BLOCK] does not exist anywhere in the codebase |
| 1.14 | Other eBay webhook signature verification | N/A -- no other webhook exists |
| 1.15 | Buyer PII minimisation, retention, log redaction | [DONE] minimisation and redaction; [DECISION] no explicit retention period is set for the stored refresh token / action journal |
| 1.16 | Dependency audit | [DECISION] the project's one real dependency (`cryptography`) has no known CVE, but the live `.venv` is shared/bloated with ~150 unrelated packages -- re-audit from a clean venv (steps now in `docs/RUNBOOK.md`) |

### Section 2 — Testing

| # | Item | Status |
|---|---|---|
| 2.1 | Full suite passes | [DONE] 339/339 |
| 2.2 | Coverage per route group reported | [DONE] see below |
| 2.3 | Unit tests: validation, token refresh | [DONE] |
| 2.4 | Unit tests: pricing/fee-margin calculation | N/A -- no fee/margin calculation exists in the app (price is seller-entered) |
| 2.5 | Integration tests: create/revise/end listing, offer lifecycle, publish, stock, orders, shipment, refunds | [DONE] for everything the app implements (draft/create, price, quantity, image, order-read); N/A for publish/end-listing/shipment/refunds -- none of these exist in the app |
| 2.6 | Failure-path tests (401/403/404/409/429/500/503, timeouts, malformed, partial batch) | [DECISION] 401/404/409/timeout/malformed are covered; no test exercises a 429 or 500/503 response, though the handling code for both exists |
| 2.7 | Idempotency tests | [DONE] including a real multi-threaded race test |
| 2.8 | Concurrency tests | [DONE] for the one scenario this architecture can actually produce (two requests racing one confirmation token); other classic scenarios (two workers, mid-order stock race) are structurally unreachable -- no second writer exists |
| 2.9 | Scheduled-job tests | N/A -- confirmed no scheduler/background job exists anywhere |

Coverage by route group (test count): connection/OAuth 12, account 7, listings (read) 13, orders (read) 14, drafts 14, prices 30, quantities 32, images 35, eBay-client transport/pagination 24, storage/DB 25, plus 10 new this pass (headers, shutdown, hygiene).

### Section 3 — Debugging and fixes

| # | Item | Status |
|---|---|---|
| 3.1 | Unhandled exceptions / silent catches | [DONE] none found -- every `except Exception` rolls back, marks the action `unknown`, or is a narrow safe fallback |
| 3.2 | Money uses `Decimal`, never `float` | [DONE] confirmed throughout; `float` is used only for OAuth expiry seconds |
| 3.3 | Timezone handling | [DONE] everything is UTC-aware and fails closed on a naive timestamp; the app never converts to Europe/London (everything shown is UTC-labelled), so there's no BST/GMT bug -- but also no local-time display |
| 3.4 | Pagination off-by-one | [BLOCK] found, not fixed -- needs your decision (see section 3 below) |
| 3.5 | Leftover debug prints / commented code / TODOs / temp data | [DONE] none found |
| 3.6 | Sandbox-only assumptions | [DECISION] one hardcoded Sandbox location key in a standalone setup script (not the running app) -- see section 3 |

### Section 4 — eBay API compliance and limits

| # | Item | Status |
|---|---|---|
| 4.1 | Daily call-limit estimate vs. usage | [DONE] usage is human-paced and structurally low-volume; no Growth Check indicated |
| 4.2 | Exponential backoff + jitter on 429/5xx | [BLOCK-STYLE GAP] does not exist -- one attempt per call |
| 4.3 | `Retry-After` respected | does not exist |
| 4.4 | Circuit breaker | does not exist |
| 4.5 | Batching where supported | [DECISION] intentionally not used (one item per call, matching the one-action-at-a-time design) -- not a gap at current volume |
| 4.6 | Deprecated API usage | [DONE] none -- modern REST only |
| 4.7 | Marketplace/currency/site/policy configurable, not hardcoded | [DONE] |
| 4.8 | Sandbox-only IDs hardcoded in source | [DECISION] one: `SANDBOX-WAREHOUSE-1` in `setup_business_policies.py` |

### Section 5 — Reliability and observability

| # | Item | Status |
|---|---|---|
| 5.1 | Structured logging with correlation IDs, redacted | [DECISION] redaction is real but ad hoc; no correlation ID, no structured (JSON) format |
| 5.2 | Health check verifies config/DB/token | [DECISION] `/health` currently reports "process alive" + active environment only |
| 5.3 | Alerting (token failure, repeated errors, stock negative, price outlier, account warnings) | none -- console log only |
| 5.4 | Graceful shutdown | [DONE] SIGTERM now handled this pass, alongside existing Ctrl+C handling |
| 5.5 | Database backup + tested restore | none exists |

### Section 6 — Production safety controls

| # | Item | Status |
|---|---|---|
| 6.1 | Global kill switch | does not exist (today's de-facto kill switch is removing `.env.production` -- documented in `docs/RUNBOOK.md` this pass) |
| 6.2 | Dry-run mode | does not exist |
| 6.3 | Max price-change % / price floor | does not exist -- only a universal GBP 0.01 to GBP 999,999,999.99 bound |
| 6.4 | Max bulk create/revise/end per run | N/A -- no bulk path exists; every write is one action at a time |
| 6.5 | Confirmation for bulk end/delete | N/A -- no end/delete capability exists at all |
| 6.6 | Audit log of writes (what/when/before/after) | [DECISION] the data exists (encrypted, append-only, includes before/after values) but there is no queryable/exportable report view |

### Section 7 — Rollout and rollback plan

See section 7 below -- plan drafted. Read-only Production launch is structurally ready today; write-capable Production launch is blocked on Sections 1.13 and 6.

### Section 8 — Anything missed

| # | Item | Status |
|---|---|---|
| 8.1 | README accuracy | [DONE] fixed this pass (was describing Phase 5 as current, and understated three shipped features) |
| 8.2 | `../PROJECT_STATUS.md` accuracy | [DONE] fixed this pass (was equally stale) |
| 8.3 | TODOs/FIXMEs left in code | [DONE] none found |
| 8.4 | Git repository | not yet initialised -- open item for you, instructions already in README |
| 8.5 | Dev-only config defaults | [DECISION] `.env.example`'s `APP_FEATURE_STAGE=drafts` is fine for Sandbox but must not be blindly copied into `.env.production` -- now called out explicitly in the new `.env.production.example` |
| 8.6 | Runbook for common failures | [DONE] added this pass (`docs/RUNBOOK.md`) |
| 8.7 | Undocumented surprises (5-minute confirm TTL, reconciliation steps) | [DONE] documented this pass in README and RUNBOOK |
| 8.8 | Stray files outside the repo | [DECISION] a transfer bundle (`ebay-api-phase5-transfer-2026-08-30.zip`, `TRANSFER_CONTEXT.md`) sits in the parent `Ebay API` folder, outside git scope -- not inspected, flagging for your attention |

---

## 2. Changes made this pass, by section

**Section 1 (Security):**
- Added the `Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=()` header to every response in `ebay_app/web.py` (matches the value already used by the Cloudflare OAuth relay Worker). Covered by 3 new tests in `tests/test_security_headers.py`, which also lock in the full existing header set (nothing previously tested it at all).

**Section 5 (Reliability):**
- `run.py` and `run_production.py` now catch `SIGTERM` and shut down through the exact same clean path as Ctrl+C (closing the database via the existing `finally` block), instead of Python's default disposition, which would skip that cleanup. Covered by 6 new tests in `tests/test_graceful_shutdown.py`.

**Section 8 (Documentation):**
- `README.md` rewritten to reflect the actual current state: Phase 9 (not 5), an accurate capability table, a new "Production (Phase 9 preparation)" section documenting `run_production.py`/`.env.production`, the five-minute confirmation window called out explicitly, and an expanded "Common problems" list covering expired confirmations, double-submits, and unknown/partial outcomes.
- `../PROJECT_STATUS.md` rewritten with the same corrections, an accurate test count (339, up from the stale 157), and a "Recommended next coding tasks" section pointing at this report's blocking items instead of a now-superseded suggestion.
- `docs/RUNBOOK.md` created: concrete steps for reconciling an `unknown`/`partial` action, an expired confirmation, stopping/restarting into the right environment, and rebuilding a clean dependency environment before trusting a `pip-audit` result.
- `.env.production.example` created as a safe Production configuration template (mirrors `.env.example`), with a matching `!.env.production.example` exception added to `.gitignore`, and a new hygiene test (`tests/test_repository_hygiene.py`) that checks it has the complete safe key set and the safest defaults (read-only stage, distinct database path, no real secrets).

**Nothing else in the codebase was changed.** In particular, no change was made to pricing/stock/validation business logic, to the pagination code, to the OAuth/token flow, or to anything that touches a live eBay account.

Full test run after all changes: **339 passed, 0 failed, 0 skipped.**

---

## 3. Blocking issues (must resolve before Phase 9 can go further)

1. **eBay's Marketplace Account Deletion/Closure notification endpoint does not exist.** eBay requires this (challenge-code echo + payload signature verification, plus real data deletion/anonymisation) to keep a Production keyset active. This needs its own implementation and review -- it is a genuine new feature, correctly out of scope for this hardening pass, but it blocks Production candidacy entirely, not just writes. I did not build this, per your instruction not to add features this phase.

2. **No Production write capability exists at all**, and no Section 6 safety controls (kill switch, dry-run, price/quantity limits, audit report) exist to govern it once it's built. These are business decisions as much as engineering ones (what price-change % is "safe" for your account is your call, not mine) -- flagging for your decision rather than guessing.

3. **The inventory/offer pagination bug** (section 3.4 above): `list_inventory_items` and `list_offers` in `ebay_app/ebay_client.py` send `offset` as the loop's page counter (0, 1, 2...) instead of a running item count (`page * limit`), while `list_orders` in the same file does it correctly. This is currently invisible in Sandbox because test catalogs fit on one page, and it's actually locked in on purpose by two existing tests (`test_ebay_client.py`) that assert this exact behaviour. Against a real account with more than 200 inventory items or more than 100 offers for one SKU, the client's own internal consistency checks would very likely catch the resulting overlap and raise an error rather than silently returning wrong data -- but I don't know if that was a deliberate simplification for Sandbox-only scale or an oversight, and fixing it means overturning two tests that currently assert it's intentional. I'm not comfortable guessing which, so I left it alone and did not fix it. If you'd like me to fix it (change the offset calculation, update the two tests, add a regression test with a synthetic 3-page catalog), say so and I will, with its own before/after diff for you to review.

None of these three block continuing to use the app in Sandbox as it exists today.

---

## 4. Risks if you proceed to Production as things stand today, ranked by severity

1. **Highest -- losing Production API access entirely.** Without the Account Deletion notification endpoint, eBay can suspend or refuse the Production keyset outright. This isn't a "nice to have" hardening item; it's a hard eBay platform requirement.
2. **High -- an operator error with no safety net.** There is no price floor, no max-change-%, and no kill switch. Today this is low-consequence only because there is no write capability yet; the moment a write-capable Production phase ships without these, a mistaken confirmation on a real listing has no automatic guard rail beyond the universal GBP 0.01 minimum.
3. **Medium -- silent failures going unnoticed.** No alerting exists beyond a console log line. If a token refresh starts failing, or eBay starts returning errors, on a real account, nobody is notified unless someone happens to be watching the terminal.
4. **Medium -- an unrecoverable local outage.** No database backup/restore exists. Losing `instance/` loses the encrypted OAuth grant and the entire action/audit history (not eBay-side data, which is unaffected, but your local operating history).
5. **Low-medium -- the pagination bug**, if a real account's catalog ever exceeds one page for these two calls; the app's own consistency checks would likely raise a loud error rather than corrupt data, but it hasn't been proven against a real large catalog.
6. **Low -- no backoff/circuit breaker on 429/5xx.** Given the human-paced, low-volume usage pattern, this is unlikely to matter in practice, but a bad run of eBay-side errors would surface as repeated manual failures rather than being smoothed over automatically.
7. **Low -- rate limiting only on login**, and no correlation IDs in logs. Neither matters much for a single-operator, loopback-only app, but both would matter more if the app's exposure ever changed.

---

## 5. GO / NO-GO recommendation

**NO-GO for Production writes. GO for continuing exactly as today (Sandbox, read-only Production exploration only) once you've reviewed this report.**

The application is well-built for what it currently does: Sandbox operation is solid, the input validation and injection protections are thorough, the one-use confirmation and reconciliation-over-retry design is genuinely careful, and the test suite is real (339 passing, meaningfully exercising failure paths, idempotency, and even a live-threaded concurrency race). The documentation gaps this pass found and fixed were real, but none of them were safety-critical -- they were the kind of drift that happens when a project moves faster than its README.

What blocks Production is not fragility in what exists -- it's two things that don't exist yet and were correctly left out of this hardening pass because building them is feature work, not hardening: the eBay-mandated Account Deletion notification endpoint, and the Section 6 safety controls that should gate any real-money write. Both need their own scoped, reviewed phase, as your own brief anticipated ("Phase 9 will switch to the eBay Production environment... will be a separately reviewed design").

Read-only Production access (viewing your real connection/account/listings/orders) is structurally safe to try whenever you're ready -- `run_production.py` already enforces read-only stages at config-load time, independent of anything else in this report.

---

## 6. Rollout and rollback plan (for when you decide to proceed)

**Now -- read-only Production only (safe to do today):**
1. Create a real Production keyset in the eBay Developer Portal.
2. Deploy a second, separate Cloudflare Worker (e.g. `ebay-production-oauth-relay`) with the same hardening as the Sandbox one -- never reuse the Sandbox Worker or RuName.
3. Copy `.env.production.example` to `.env.production` (`install -m 600 .env.production.example .env.production`), fill in the real keyset, and give it a new database path and encryption key -- the app refuses to start if these collide with Sandbox's.
4. `python run_production.py`. Confirm the red "LIVE" banner and that `/health` reports `"production"`. Complete OAuth consent and verify connection/account/listings/orders read correctly, with zero writes attempted (none are possible yet).

**Before any write capability is even designed:**
5. Implement and live-verify the Account Deletion/Closure notification endpoint.
6. Design and get your sign-off on Section 6's safety controls (kill switch, dry-run, price/quantity limits, audit report) -- these are business decisions, not just code.

**Once a write phase exists (future, separately reviewed):**
7. First write in dry-run mode, logging what would be sent without sending it.
8. A pilot: one or two low-value real listings, one write, monitored end to end via the (by-then-existing) audit report, before enabling anything else.
9. Gradually re-enable prices, then quantities, then images, one stage at a time, exactly as each was proven in Sandbox.

**Rollback, any time:** Ctrl+C (or now, SIGTERM) stops cleanly; deleting or renaming `.env.production` prevents any restart into Production; `python run.py` always falls back to Sandbox with no configuration change needed. Reconciling an interrupted or ambiguous write is a manual Seller Hub check -- steps are now in `docs/RUNBOOK.md`.

---

## Open items for you to decide

- Should I fix the pagination offset bug now (small, tested, but overturns two tests that currently assert the opposite on purpose)?
- Do you want the Account Deletion/Closure notification endpoint scoped as the next phase?
- What price-change-%, price floor, and max-per-run limits do you actually want, so Section 6 can be designed?
- The stray `ebay-api-phase5-transfer-2026-08-30.zip` / `TRANSFER_CONTEXT.md` in the parent folder -- keep, move, or delete?
- Should this repository be `git init`'d now (README already has the steps) so future changes are actually version-controlled?
