# Runbook: common failures and how to handle them

This is the operational companion to README.md's "Common problems" section.
It exists because the pre-production hardening pass (2026-09) found that
several real failure scenarios were left undocumented anywhere a human
operator would actually look at the moment they hit them. Nothing here
changes application behaviour -- it only writes down what the code already
does.

## An action shows `unknown` or `partial` on its result page

The app never automatically retries an ambiguous eBay write. If a
confirmation's HTTP call to eBay times out, disconnects, or returns a
response the app cannot fully parse, the action is recorded as `unknown`;
if eBay's own response indicates only part of a request succeeded, it is
recorded as `partial`. In both cases:

1. **Do not resubmit the same SKU/Offer through the app.** The one-use
   confirmation token for that action is already spent and cannot be reused;
   starting a fresh review is a *new* action against whatever state eBay is
   actually in, so check that state first.
2. Open the action's own result page (`/actions/{id}`, linked from the
   dashboard's recent-actions list) and note exactly what was attempted:
   the kind of write, the SKU/Offer ID, and the before/after values shown.
3. Sign in to eBay Sandbox's (or, in a future Production phase, the real)
   Seller Hub directly and look up the same SKU/Offer. Compare its current
   price/quantity/status/images against what the action page shows was
   intended.
4. If eBay's state already matches what you intended, no further action is
   needed -- the write succeeded and only the app's own confirmation of that
   fact failed.
5. If eBay's state does not match, decide the correct value by hand and,
   once you're confident of eBay's actual current state, start a **new**
   review from the app for that SKU/Offer as normal -- the app's
   immediately-before-write re-verification will catch you if eBay's state
   has moved on again since you checked.
6. Never try to "force" a retry by editing the database directly.

## A confirmation says "expired"

Every review has a five-minute one-use confirmation window
(`storage.py`'s `ttl_seconds`). If you take longer than five minutes between
opening a review and submitting its confirmation, the token is no longer
valid and confirming returns a controlled error -- eBay was never contacted.
Simply go back and start the review again. This is not a bug and nothing
needs reconciling.

## The confirm button was clicked twice, or the page was reloaded mid-submit

The confirmation token is one-use, enforced by an atomic, single-winner
database claim (`storage.py`, `claim_pending_action`). A second submission
of the same token is safely rejected -- it can never cause the write to
happen twice. If you're unsure whether the first click went through, check
the action's result page rather than clicking confirm again.

## Stopping the app

- **Sandbox (`run.py`) or Production (`run_production.py`), normal case:**
  press Control+C once in the terminal running it. The database is closed
  cleanly before the process exits.
- **If the process was started by something other than an interactive
  terminal** (a process manager, a `systemd` unit, `docker stop`, etc.): a
  `SIGTERM` now also shuts down cleanly (added in the pre-production
  hardening pass) -- it goes through the exact same code path as
  Control+C, closing the database before exiting.
- **There is no in-app kill switch yet.** The only way to stop a running
  Production process today is Control+C / SIGTERM as above, or removing
  network access to the machine. Building an in-app, no-redeploy kill
  switch is an open, flagged item for the phase that actually adds
  Production write capability -- see docs/phase9_production_design.md.

## Preventing a restart into Production

Delete or rename `.env.production`, or edit it so
`APP_ALLOW_PRODUCTION_ENVIRONMENT` is anything other than the exact literal
string `yes-i-understand-the-risk`. Either change makes `run_production.py`
refuse to start (fail-closed, logged, exit code 1) -- there is no other way
to reach Production from this codebase.

## Falling back to Sandbox

Run `python run.py` instead of `python run_production.py`. `run.py` always
and only loads the sibling `.env` file and is structurally incapable of
resolving to Production regardless of that file's contents -- no
configuration change is needed to "fall back."

## Restoring the database after loss or corruption

There is no automated backup today (flagged in the production-readiness
review as an open item -- see README.md's Security notes). If you have not
manually copied the `instance/` directory while the app was stopped, the
encrypted refresh token and the full action/audit history since the last
manual copy cannot be recovered, and you will need to re-authorize the
OAuth grant from scratch (README.md's "Authorize or upgrade the Sandbox
grant" / the equivalent Production section). No listing, price, or quantity
data itself is lost on eBay's side -- only this app's local record of past
actions and its cached OAuth grant.

## Dependency or environment audit

Before trusting a `pip-audit` (or similar) result, rebuild the virtual
environment strictly from `requirements.txt` in an otherwise-empty `.venv`.
A `.venv` that has accumulated packages from other projects will report
vulnerabilities that have nothing to do with this application and will
obscure whether this project's one real dependency, `cryptography`, is
actually current:

```sh
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pip-audit
python -m pip-audit
```
