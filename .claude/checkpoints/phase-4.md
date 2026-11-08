# Checkpoint: Phase 4, the autonomy engine

## What it does now
- **Reprices** to your margin over cost: cost × (1 + margin), stepped by your max
  change %, never below break-even or your floor. An item with no margin set is
  reported, never repriced. Runs daily, and again after a cost upload.
- **Relists or restocks** from one-off instructions in the cost CSV
  (`stock_quantity`), applied once and then forgotten so it can't oversell.
  Ended listings get advice only; the engine never republishes by itself.
- **Watches orders** and emails you about new ones. The first run only records
  existing orders.
- **Health check:** a lost connection or missing permission pauses it straight
  away. Other errors pause it after 3 in a row.
- **Email:** sent immediately on any pause, plus a digest at most every 6 hours,
  only when something is waiting or has happened. It stays off until you enter
  the SMTP settings.
- **Modes:** off (the default) → shadow (you approve each change) → auto-dry
  (rehearsals only) → auto (real changes, still bound by `APP_DRY_RUN`).
- **Every change** goes through the existing price or quantity services: floor,
  max change, one shared actions budget, a re-check of the live value and the
  dry-run switch.
- **Dashboard:** the `/engine` page shows the report, Approve, Reject or Dismiss,
  and Pause or Resume.

## Tests
`python3 -m unittest discover -s tests` gives 738/739. The only failure is the
known `PROJECT_STATUS.md` check, which lives outside the repository.

## Untested, and needs you (Sandbox)
1. Stop the app and run `python3 import_costs.py costs.csv` (columns:
   sku,item_cost,postage_cost,margin_percent,stock_quantity).
2. Set `APP_ENGINE_MODE=shadow` in `.env`, start the app and open the
   Autonomy engine card.
3. Check that the current margins and proposed prices look right. Approve one
   proposal, with `APP_FEATURE_STAGE=prices` and a price floor set for that SKU.
4. Optional: add the SMTP settings to `.env` to receive the emails.

## Next (Phase 5): the second seller account
I need to know how the two accounts should be separated: for example, one app
instance per account with separate databases, or one app switching between them.

Approve with `python3 .claude/ctl.py approve 4`, or just say so.
