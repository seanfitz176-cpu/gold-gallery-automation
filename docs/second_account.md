# Running a second eBay seller account (Phase 5)

**Design (your choice): one separate copy of the app per account.** Each copy
has its own settings file, encrypted database, costs, engine, budget and
audit log. Nothing is shared, so one account's data or actions can never
leak into the other's.

## How the two copies differ
| | Main account | Second account |
|---|---|---|
| Folder | `python_app/` | e.g. `python_app_second/` (a second clone of the repo) |
| `PORT` | `5000` | `5001` |
| `APP_ACCOUNT_LABEL` | blank, or e.g. `Main shop` | required, e.g. `Second shop` |
| Database and encryption key | its own | its **own**: new `APP_DATABASE_PATH`, new `APP_DATA_ENCRYPTION_KEY` |
| eBay keyset (Client ID, Secret, RuName) | same | same. One registered app can serve any number of seller accounts. |
| Account-deletion Worker | same | same. It already handles every connected user. |

Only ports 5000 and 5001 are accepted. Port 5001 is refused unless
`APP_ACCOUNT_LABEL` is set, and every page and alert email names the
account.

## One-time setup (all steps are yours, in your own Terminal)
1. `git clone https://github.com/seanfitz176-cpu/gold-gallery-automation python_app_second`
   next to `python_app`, then create its virtual environment the same way.
2. Copy your `.env.production` into the new folder (`install -m 600 ...`) and
   change these lines only:
   - `APP_DATABASE_PATH`: a new path inside the new folder;
   - `APP_DATA_ENCRYPTION_KEY`: a new key (`openssl rand -base64 32`);
   - `APP_ADMIN_PASSWORD`: a new password;
   - `APP_ACCOUNT_LABEL=Second shop`;
   - leave `PORT` at 5000 **for now**.
3. **Connect the second eBay account (port 5000, once).** Stop the main
   copy. In the second folder, run `python3 run_production.py`. Open
   http://127.0.0.1:5000. If your browser is signed in to the main account
   on ebay.co.uk, sign out first or use a private window. Connect, sign in
   as the **second** seller, then verify the connection. The page header
   shows "Account: Second shop", so you can tell the copies apart.
4. Stop it, set `PORT=5001` in the second copy's `.env.production`, and
   start both copies. The main one is at http://127.0.0.1:5000, the second
   at http://127.0.0.1:5001.

eBay sign-in always returns to port 5000, so the copy on 5001 refuses to
start a sign-in and tells you so. To reconnect the second account later,
repeat step 3.

## Engine, costs and alerts per account
- Each copy has its own cost table. Run `import_costs.py` **inside that
  copy's folder**, with that copy stopped.
- Engine mode, margins, floors and budgets are set separately in each
  copy's settings file, so you can run the second account in `shadow` while
  the main one is on `auto`.
- Alert emails are tagged `[eBay Production - Second shop]`.

## Not supported (deliberately)
- Two copies sharing one database or one settings file.
- More than two simultaneous copies. Ask if a third account is needed.
