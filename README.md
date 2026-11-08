# eBay Sandbox Console

A local, modular Python application for managing an eBay seller account
through OAuth. The application is deliberately fail-closed: it rejects a
mismatched keyset for the active environment, binds to the local computer,
minimizes displayed seller data, and keeps every unfinished feature
inaccessible.

The current development stage is **Phase 9: pre-production hardening**.
Phases 1–8 are complete and live-verified against eBay **Sandbox**; see
[PROJECT_STATUS.md](../PROJECT_STATUS.md) and [docs/ROADMAP.md](docs/ROADMAP.md)
for concise, portable context that can be supplied to any AI coding system.
Production write capability now exists for **price changes only**, behind
two separate, explicit opt-ins and a dry-run default -- see "Production
(Phase 9 preparation)" below. Quantity and image changes against a real
account remain intentionally rejected until each is separately reviewed.

## Current capability status

| Capability | Status |
| --- | --- |
| Sandbox OAuth, state validation, token exchange and refresh | Implemented |
| Encrypted local OAuth grant storage | Implemented |
| Minimal Sandbox account summary | Implemented |
| Active Inventory API listing summary | Implemented |
| Recent Fulfillment order summary | Implemented |
| Reviewed creation of one unpublished Inventory item and Offer | Implemented, live-verified in Sandbox |
| Price changes on a published Offer | Implemented, live-verified in Sandbox |
| Quantity/stock changes on a published Offer | Implemented, live-verified in Sandbox |
| Image linking (URL-based; not raw file upload) on a published Offer | Implemented, live-verified in Sandbox |
| Publishing an Offer, ending/withdrawing a listing | Not implemented (no route or eBay call exists) |
| Order fulfillment writes (shipment/tracking upload), refunds, cancellations | Not implemented (orders are read-only) |
| Bulk-creating many new unpublished Inventory items/Offers from a CSV file | Implemented as a standalone script, `bulk_upload_listings.py`, looping the same reviewed create-and-verify pipeline used for one draft at a time; needs no new opt-in beyond whichever of `drafts`/`prices`/`quantities`/`images` is already configured. Never publishes. Optional `image_urls` column (up to 24 https addresses, `|`-separated) is sent with each item and reconciled exactly on readback. |
| Bulk *editing* of existing listings, templates, reporting/analytics | Not implemented |
| Production read-only access (connection/account/listings/orders) | Implemented, live-verified against the real account |
| Production price, quantity, or image changes against a real account | Implemented, each behind its own two separate explicit opt-ins (`APP_ALLOW_PRODUCTION_ENVIRONMENT` plus the stage's own `APP_ALLOW_PRODUCTION_PRICE_WRITES` / `APP_ALLOW_PRODUCTION_QUANTITY_WRITES` / `APP_ALLOW_PRODUCTION_IMAGE_WRITES`); none of these three opt-ins is set in this project's own `.env.production` today, and each defaults to dry-run (`APP_DRY_RUN=true`) once it is, so no real write happens until that is also explicitly disabled |
| Publishing bulk-created UNPUBLISHED Offers | Implemented as a standalone script, `bulk_publish_listings.py`, behind its own `APP_FEATURE_STAGE=publish` (an island like `migration`) and, in Production, its own opt-in `APP_ALLOW_PRODUCTION_PUBLISH`. Re-verifies each Offer immediately before publishing and reads it back after; honours `APP_DRY_RUN` (default true in Production) and `APP_MAX_ACTIONS_PER_RUN`; writes a results CSV. See [docs/publish_design.md](docs/publish_design.md) |
| Bringing an existing classic (non-Inventory-API) listing under this app's management | Implemented as a one-off standalone script, `migrate_listing.py`, calling eBay's `bulkMigrateListing` endpoint; behind its own two separate explicit opt-ins (`APP_ALLOW_PRODUCTION_ENVIRONMENT` plus `APP_ALLOW_PRODUCTION_LISTING_MIGRATION`) and its own `APP_FEATURE_STAGE=migration`, not cumulative with `prices`/`quantities`/`images`; not set in this project's own `.env.production` today. There is no dry-run mode for this specific call -- see [claude/listing_migration_plan.md](claude/listing_migration_plan.md) |
| Production draft creation against a real account | Not implemented and intentionally rejected -- no opt-in exists for this stage yet |

## Safety boundary

- Sandbox App IDs and Cert IDs are validated against `EBAY_ENVIRONMENT=sandbox`;
  a mismatched keyset for the active environment is rejected at startup.
- The normal server listens only on loopback (`127.0.0.1:5000`).
- GET requests never change eBay data.
- Every eBay write (draft creation, price change, quantity change, image
  link) requires a local login, CSRF protection, an exact preview, and a
  separate **five-minute one-use** confirmation. If you review a change and
  wait longer than five minutes before confirming, the confirmation expires
  and you must start the review again -- nothing is silently retried.
- Confirmation re-reads the live Offer/Inventory item immediately before
  writing and refuses if anything (price, quantity, status, SKU) has changed
  since you reviewed it.
- There is no publish, delete, end-listing, refund, or shipment/tracking
  route. There is no bulk operation and no scheduled or background job of
  any kind -- every write is one human-confirmed action at a time.
- Ambiguous mutation results (`unknown`/`partial`) are recorded for
  reconciliation and are never retried automatically. See
  [docs/RUNBOOK.md](docs/RUNBOOK.md) for what to do when you see one.
- A **Production** environment exists in code (see below) but is dormant,
  read-only even when fully configured, and rejected unless a sibling
  `.env.production` file with an explicit opt-in is present.

Never point this repository at a live eBay account until a separately
reviewed Production write phase exists. Today, even a correctly-configured
`.env.production` can only ever reach read-only stages.

## Requirements

- macOS, Linux, or Windows
- Python 3.11 or newer
- Node.js 20 or newer only for testing/deploying the callback Worker
- An eBay Developers Sandbox keyset and Sandbox test seller
- A free Cloudflare account for the stable Sandbox OAuth callback Worker

The Python application uses the standard library plus `cryptography`. The
Cloudflare Worker has no npm runtime dependencies.

## Install

Open Terminal in the repository root (the folder containing this README), then:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

On Windows PowerShell, activate the environment with:

```powershell
.venv\Scripts\Activate.ps1
```

## Configure the private environment

Create a private `.env` from the safe template:

```sh
install -m 600 .env.example .env
```

If `install` is unavailable:

```sh
cp .env.example .env
chmod 600 .env
```

Generate two different local secrets:

```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"
python -c "import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

Use the first value for `APP_ADMIN_PASSWORD` and the second value for
`APP_DATA_ENCRYPTION_KEY`. Store both in a password manager. Never paste them
into an issue, chat, commit, screenshot, or Copilot prompt.

`APP_ADMIN_PASSWORD` may be stored as plaintext (as above) or, recommended,
as a salted hash so the real password never sits in the `.env` file at all:

```sh
python -c "from ebay_app.password_hash import hash_password; print(hash_password('your-chosen-password'))"
```

Paste the printed `pbkdf2_sha256$...` string in as `APP_ADMIN_PASSWORD` instead
of the plaintext password, and remember the plaintext password separately (in
your password manager) -- it cannot be recovered from the hash. Both forms are
accepted at login; an existing plaintext value keeps working unchanged.

Required values:

| Variable | Purpose |
| --- | --- |
| `EBAY_ENVIRONMENT` | Must remain `sandbox` in `.env` |
| `APP_FEATURE_STAGE` | Current value is `images` (cumulative: connection, account, listings, orders, drafts, prices, quantities, images) |
| `EBAY_CLIENT_ID` | Sandbox App ID |
| `EBAY_CLIENT_SECRET` | Rotated Sandbox Cert ID |
| `EBAY_RUNAME` | OAuth-enabled Sandbox RuName |
| `APP_ADMIN_PASSWORD` | Local console password, at least 20 characters |
| `APP_DATA_ENCRYPTION_KEY` | Base64 encoding of exactly 32 random bytes |
| `APP_DATABASE_PATH` | Private local SQLite path |

Marketplace and runtime defaults are documented in `.env.example`. This app
may also use a merchant location key and three business policy IDs. Set all
three policy IDs together or leave all three blank.

Do not put an eBay username, eBay password, OAuth access token, or OAuth refresh
token in `.env`. The browser flow obtains the grant and stores the refresh token
encrypted in the local database.

## Production (Phase 9 preparation)

The read-only `connection`/`account`/`listings`/`orders` stages are
live-verified against the real account. `prices`, `quantities`, and
`images` are also now reachable, but each only with its own **second,
separate, explicit opt-in** on top of the one below
(`APP_ALLOW_PRODUCTION_PRICE_WRITES` / `APP_ALLOW_PRODUCTION_QUANTITY_WRITES`
/ `APP_ALLOW_PRODUCTION_IMAGE_WRITES`, see the next section) -- and even then
each starts in dry-run by default, making no real eBay write until
`APP_DRY_RUN=false` is also explicitly set. None of these three opt-ins is
set in this project's real `.env.production` today -- each was added as a
structural gate ahead of its own separate, deliberate review of actually
using it, matching the same cautious rollout `prices` already went through.
`drafts` against a real account remains intentionally rejected outright, with
no opt-in path at all; see
[docs/phase9_production_design.md](docs/phase9_production_design.md) for
the open questions still blocking it. A separate, one-off `migration` stage
also exists -- see
[claude/listing_migration_plan.md](claude/listing_migration_plan.md) for what
it's for and how to use it; it is not part of the `prices`/`quantities`/
`images` progression above and earns no trust from it.

Production is loaded from a **separate file**, `.env.production`, using a
**separate entry point**, `run_production.py` -- never `run.py`, and never a
command-line flag. Copy `.env.production.example` to `.env.production`
(`install -m 600 .env.production.example .env.production`), fill in the real
Production keyset and a **new, distinct** `APP_DATABASE_PATH` and
`APP_DATA_ENCRYPTION_KEY` (reusing the Sandbox database or key is rejected at
startup), and set `APP_ALLOW_PRODUCTION_ENVIRONMENT=yes-i-understand-the-risk`
exactly as shown -- any other value refuses to start.

To additionally reach `APP_FEATURE_STAGE=prices`, `quantities`, or `images`,
also set that stage's own opt-in to the exact value
`ebay_app/config.py`'s `PRODUCTION_WRITE_OPT_INS` dict documents --
`APP_ALLOW_PRODUCTION_PRICE_WRITES` for `prices`,
`APP_ALLOW_PRODUCTION_QUANTITY_WRITES` for `quantities`, or
`APP_ALLOW_PRODUCTION_IMAGE_WRITES` for `images` -- a **separate** typed
opt-in from the one above, required in addition to it, and specific to that
one stage only (setting one stage's opt-in never unlocks another). Even
then, `APP_DRY_RUN` defaults to `true`, so nothing real-world happens until
you also set `APP_DRY_RUN=false` yourself. `APP_FEATURE_STAGE=drafts` is
rejected outright for Production regardless of any opt-in here -- it has no
opt-in path yet at all.

`APP_FEATURE_STAGE=migration` is separate again: set it together with
`APP_ALLOW_PRODUCTION_LISTING_MIGRATION=yes-i-understand-the-risk-of-migrating-a-live-listing`
only when you are about to run `migrate_listing.py` (see
[claude/listing_migration_plan.md](claude/listing_migration_plan.md)), then
set `APP_FEATURE_STAGE` back to whatever you actually use day to day --
`migration` is a one-off, not a stage to leave selected. There is no
`APP_DRY_RUN` rehearsal for this specific call.

`bulk_upload_listings.py` (bulk-creating many new unpublished Inventory
items/Offers from a CSV file) needs no opt-in of its own -- it runs under
whichever of `drafts`/`prices`/`quantities`/`images` is already configured
(in Production, that stage's own opt-in above must already be set; `drafts`
itself is still rejected outright). Run it the same way as any other
standalone script:

```sh
python3 bulk_upload_listings.py path/to/listings.csv               # Sandbox
python3 bulk_upload_listings.py path/to/listings.csv --production    # Production
```

It never publishes anything -- every row it creates is an UNPUBLISHED
Offer, exactly like the dashboard's own one-item draft flow. An optional
`image_urls` column takes up to 24 https image addresses separated by `|`
(the first is the main picture); they are sent in the Inventory item's
`product.imageUrls` and must read back exactly, in order, or the row is
reported `unknown`. Loading the CSV never contacts the image hosts.

## Autonomous Claude sessions

`.claude/` holds guard rails for letting Claude Code work through
`PLAN.md` unattended: a gate hook that blocks secrets, live eBay commands,
pushes and edits while a checkpoint awaits approval, and a Stop hook that
keeps Claude working until the phase is done. Your controls:

```sh
python3 .claude/ctl.py stop | resume | status | approve N | reject N
```

Run autonomous sessions in a clean clone with **no** `.env`/`.env.production`
-- the gate refuses to run the test suite beside a real `.env.production`.

```sh
source .venv/bin/activate
python run_production.py
```

Every page served under Production carries a red "LIVE -- REAL EBAY ACCOUNT"
banner, and `/health` reports `"environment":"production"` truthfully.
Startup also logs the active environment loudly to the terminal.

**The write-phase kill switch:** `APP_DRY_RUN` (default `true` once
`prices` is reached, shown explicitly in `.env.production.example`) makes
`PriceService.confirm()` claim and journal every confirmation exactly as
normal but never call eBay at all -- the result page says `DRY RUN`
unmistakably. Set `APP_DRY_RUN=false` only when you are genuinely ready for
a real write to reach the live account. **To stop Production entirely:**
press Control+C once, or delete/rename `.env.production` and it will refuse
to start again. See [docs/RUNBOOK.md](docs/RUNBOOK.md) for the full
rollback procedure and how to fall back to Sandbox.

## Stable Sandbox OAuth callback

The Python server stays private on `127.0.0.1`. A small stateless Cloudflare
Worker receives eBay's HTTPS callback and redirects the same browser tab to the
fixed local callback:

```text
http://127.0.0.1:5000/oauth/callback
```

Worker source and tests are in `oauth_relay_worker/`.

### Deploy the Worker

1. In Cloudflare, create a Worker named exactly
   `ebay-sandbox-oauth-relay`.
2. Replace the starter code with
   `oauth_relay_worker/src/index.js`.
3. Deploy it.
4. Open the Worker's **Domains** tab:
   - keep the main Production `workers.dev` address enabled and Public;
   - keep Preview URLs disabled;
   - do not enable Cloudflare Access.
5. Open the Worker's **Settings** tab and find **Observability**:
   - set Logs to Disabled;
   - keep Traces disabled;
   - configure no exports.
6. Copy the stable Production `workers.dev` base address and open
   `<worker-base-url>/privacy`. The privacy page must load before continuing.

`wrangler.jsonc` preserves these settings for a future CLI deployment. A manual
Dashboard code paste does not automatically apply that file.

### Configure eBay's Sandbox RuName

In the eBay Developers Sandbox keyset, edit the OAuth-enabled RuName and set:

```text
Privacy Policy URL: <worker-base-url>/privacy
Auth Accepted URL:  <worker-base-url>/oauth/callback
Auth Declined URL:  <worker-base-url>/oauth/callback
```

Select OAuth, save, and confirm the row remains OAuth Enabled. The
`EBAY_RUNAME` value is eBay's generated RuName--not one of these URLs. A real
Production launch needs its own separate Worker and its own Production
RuName; do not reuse the Sandbox Worker or RuName for Production.

## Authorize or upgrade the Sandbox grant

Authorization and normal local use now share one permanent startup command:

```sh
source .venv/bin/activate
python run.py
```

Then:

1. Open exactly `http://127.0.0.1:5000`.
2. Sign in with `APP_ADMIN_PASSWORD`.
3. Select the authorization button shown on the dashboard.
4. Select **Continue to eBay Sandbox**.
5. Sign in on eBay using the complete Sandbox `TESTUSER_...` username and its
   separate Sandbox password.
6. Review the requested permissions and approve them.
7. The stable Worker returns the same browser to
   `http://127.0.0.1:5000/oauth/callback`.
8. On the dashboard, select **Verify Sandbox connection**.

Do not stop or restart the app during this sequence. The login cookie, OAuth
state and callback all remain on the same loopback host and port.

## Normal daily use

```sh
source .venv/bin/activate
python run.py
```

Open `http://127.0.0.1:5000`.

To stop safely:

1. Sign out of the local console.
2. Return to Terminal.
3. Press **Control+C** once.

The encrypted Sandbox grant remains in `instance/` and survives a restart.
A `SIGTERM` (for example from a process manager) also now shuts down
cleanly, closing the database exactly as Control+C does.

## Draft, price, quantity and image workflow

Every write shares the same reviewed shape:

1. validates an allowlisted form;
2. performs read-only eBay checks (for a new draft: generates an app-owned
   SKU with a random suffix and checks it; for an existing Offer: re-reads
   the live price, quantity, status and SKU);
3. stores the exact review payload encrypted;
4. shows the final values for you to check;
5. requires a separate, session-bound, CSRF-protected, **five-minute
   one-use** confirmation before anything is sent to eBay.

Preparation never changes eBay. Only the separate confirmation performs the
one allowed write, and confirmation re-verifies nothing has changed since you
reviewed it (rejecting the write if it has). After confirmation, the app
reads the record back and checks the result. An `unknown` or `partial`
result means the write's outcome could not be confirmed either way -- see
[docs/RUNBOOK.md](docs/RUNBOOK.md) before doing anything else with that SKU.

The first real confirmation of any new capability against a live Sandbox
account must be treated as manual acceptance testing: inspect the exact
preview, use only a disposable Sandbox SKU/listing, confirm explicitly, then
inspect the result in eBay Sandbox. Never perform that manual write from an
automated test.

## Tests

Python tests:

```sh
source .venv/bin/activate
python -m unittest discover -s tests -v
```

Worker tests:

```sh
cd oauth_relay_worker
node --test test/index.test.mjs
cd ..
```

Dependency health:

```sh
python -m pip check
```

All automated eBay tests use mocked transport. They do not contact eBay and do
not mutate a seller account. The verified baseline is recorded in
[PROJECT_STATUS.md](../PROJECT_STATUS.md).

## Diagnostic commands

Stop `run.py` before using either command because the private database permits
only one application process:

```sh
python check_connection.py
python check_account.py
```

The first command performs a sanitized connection check. The second performs the
minimal Phase 2 account read. Neither prints credentials or raw eBay responses.

## Security notes

- `.env`, `.env.production`, `instance/`, virtual environments, databases,
  logs, caches, private keys, and certificate files are excluded by
  `.gitignore`.
- The refresh token and every stored action payload/result are encrypted with
  AES-GCM in SQLite.
- Request logging removes query strings so OAuth codes and state values are not
  logged by the Python server.
- The stable Worker has no storage binding, outbound fetch, application logging,
  cookies, or user-selected redirect destination.
- `APP_ADMIN_PASSWORD` can be changed while the app is stopped. It may hold
  either the plaintext password or a `pbkdf2_sha256$...` hash from
  `ebay_app/password_hash.hash_password` (recommended) -- see the setup
  section above.
- Do **not** directly change `APP_DATA_ENCRYPTION_KEY` for an existing database.
  No supported lossless key-rotation command exists yet; changing it makes the
  database inaccessible until the old key is restored.
- No automated backup of the SQLite database exists yet. If you need to keep
  history beyond what's on disk, copy `instance/` (while the app is stopped)
  to a location of your choosing.

If a Sandbox Cert ID, Production Cert ID, or local password is exposed,
rotate it and update only the local `.env`/`.env.production`. Never commit
the replacement.

## Common problems

- **Blank local page:** keep `python run.py` running and open
  `http://127.0.0.1:5000/health`. It reports only that the process is alive
  and which environment is active, not database or token health.
- **Sandbox sign-in rejected:** use the complete Sandbox test username, including
  its `TESTUSER_` prefix, and the separate Sandbox password.
- **Safari cannot reach the callback:** start from `127.0.0.1`, keep
  `python run.py` running, and verify that both eBay Auth Accepted and Auth
  Declined URLs use the stable Worker address ending in `/oauth/callback`.
- **`invalid_request`:** confirm `.env` contains the OAuth-enabled Sandbox
  RuName, not the callback URL.
- **No active listings:** the page shows Inventory API-managed listings only;
  older Trading API or Seller Hub listings may not appear.
- **No orders:** an empty result is valid until a separate Sandbox buyer
  completes a test checkout from the connected Sandbox seller.
- **"Confirmation expired" when confirming a review:** the one-use
  confirmation window is five minutes from when you opened the review. Go
  back and review the change again -- nothing was sent to eBay.
- **Confirm button double-clicked or the page reloaded mid-confirmation:**
  the confirmation is one-use; a second submission is safely rejected rather
  than sent twice. If the result you see doesn't match what you expected,
  check the action's result page before retrying anything.
- **Unknown or partial write outcome:** do not retry the same SKU/Offer.
  See [docs/RUNBOOK.md](docs/RUNBOOK.md) for the reconciliation steps.

## Project layout

```text
.
├── ebay_app/                 Python application modules
├── tests/                    Offline Python unit/integration tests
├── oauth_relay_worker/       Stable Cloudflare callback relay and Node tests
├── static/                   Local console stylesheet
├── docs/ROADMAP.md           Controlled phase order
├── docs/phase9_production_design.md   Open questions for a future write-capable Production phase
├── docs/RUNBOOK.md           What to do when something goes wrong
├── run.py                    Local Sandbox server entry point
├── run_production.py         Local Production (read-only) server entry point
├── check_connection.py       Sanitized connection diagnostic
├── check_account.py          Sanitized account diagnostic
├── .env.example               Safe Sandbox configuration template
├── .env.production.example    Safe Production configuration template
├── .gitignore                Private/generated file exclusions
├── README.md                 Python installation and operation guide
└── .github/copilot-instructions.md
```

Repository status and safety boundaries are documented in
`../PROJECT_STATUS.md`.

## Private GitHub transfer

This repository is not yet under version control. Create a **private** empty
GitHub repository. From this project folder:

```sh
git init
git status --short --ignored
git add .
git status --short
```

Before committing, confirm `.env`, `.env.production`, `.venv/`, `instance/`,
databases, logs, and cache folders do not appear in the staged list. Use Git,
GitHub Desktop, or a similar Git client so `.gitignore` is applied; do not
drag the unsanitized Finder folder into GitHub's browser upload page.

Continue with the private repository's own commit and push instructions. Review
the staged files again before every first push.
