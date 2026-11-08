# Checkpoint: Phase 5, second seller account

## What changed
- **One deliberate relaxation of a safety rule:** the app used to accept only port
  5000. It now also accepts **5001**, but only when `APP_ACCOUNT_LABEL` is set.
  Every other port is still refused, and the Host header must match the copy's own
  port.
- A copy on 5001 **refuses to start eBay sign-in**, because eBay's relay always
  returns to 5000. It explains how to connect: run once on 5000, then switch back.
- The account name appears at the top of every page and in every alert email's
  subject. Engine links in emails use the copy's own port.
- `docs/second_account.md` covers the full setup. Each copy gets its own folder,
  database, encryption key, password, costs, engine settings and budget, while
  sharing the same eBay keyset.

## Tests
740 + 4 = **743/744**. The only failure is the known `PROJECT_STATUS.md` check.
The old test that rejected 5001 now checks that 5001 is rejected without a label
and accepted with one, and that 5002 is still rejected.

## Needs you
Follow `docs/second_account.md`. The one step needing care is signing in as the
**second** seller (step 3): use a private browser window so eBay doesn't reuse
your main account's sign-in.

Approve with `python3 .claude/ctl.py approve 5`, or just say so.
