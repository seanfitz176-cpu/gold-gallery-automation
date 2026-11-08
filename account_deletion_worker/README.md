# eBay account deletion receiver (Cloudflare Worker)

A small, standalone Cloudflare Worker that does two things:

1. Answers eBay's ownership check (the "challenge handshake") for the
   Marketplace Account Deletion/Closure notification endpoint that eBay
   requires for every app with Production API access.
2. Stores incoming deletion notifications in a queue, so that your local
   Python application can collect them later, check that they really came from
   eBay, and delete that user's data.

It is completely separate from `oauth_relay_worker/`. It has its own name, its
own configuration and its own deploy, so a fault here cannot affect the OAuth
relay. The local Python side that collects from this queue and verifies each
entry lives in `ebay_app/account_deletion.py` and
`ebay_app/notification_signature.py`.

## Security model

- **No eBay credentials, ever.** This Worker never holds, receives or uses an
  eBay Client ID, Client Secret or OAuth token. It makes no calls to eBay. Do
  not add any.
- **It does not verify eBay's signature.** Checking the ECDSA (Elliptic Curve
  Digital Signature Algorithm) signature in the `x-ebay-signature` header
  needs eBay's public key, and getting that key needs an eBay OAuth token.
  Only your local application has one, so that is where verification happens.
  Everything in the queue is **untrusted** until your local app has verified
  it. Each stored entry is marked `"signatureVerified": false`.
- **The exact bytes are kept.** Each entry stores the raw request body exactly
  as eBay sent it, plus a SHA-256 hash of it (`rawBodySha256`) and the raw
  signature header. The signature only checks out against the exact original
  bytes, not a re-serialised copy.
- **Forged posts cannot replace real ones.** Each entry's storage key is built
  from a hash of the `notificationId` and a hash of the raw body. A forged
  post that reuses a real `notificationId` is stored as a separate entry next
  to the real one; it cannot overwrite it.
- **Same defensive style as the OAuth relay:** host pinning, a 4096-character
  URL limit, exact routing by path and method (404 for unknown paths, 405 with
  an `Allow` header for the wrong method), strict allowlists for query
  parameters, rejection of control characters, the same security headers on
  every response, generic error messages and no logging.

## Routes

| Method & path | Auth | Purpose |
|---|---|---|
| `GET /ebay/account-deletion?challenge_code=…` | none | eBay's handshake. Returns `{"challengeResponse": hex(SHA-256(challengeCode + verificationToken + endpoint))}` |
| `POST /ebay/account-deletion` | none (eBay) | Receives a notification. Returns `204` when it is stored or deliberately ignored, `400`/`413`/`415` when the request is malformed, and `503` when storage fails (eBay then retries) |
| `GET /account-deletion/pending[?cursor=…]` | `Authorization: Bearer <STATUS_API_SECRET>` | Returns up to 50 queued entries: `{"entries": [...], "cursor": "<next>" or null}` |
| `POST /account-deletion/acknowledge` | `Authorization: Bearer <STATUS_API_SECRET>` | Body `{"notificationId": "…"}`. Deletes every queued entry with that id. Repeating it is harmless: it always returns `200` |

Limits and why they were chosen:

- `challenge_code`: 1 to 512 characters, visible ASCII (American Standard Code
  for Information Interchange) only. eBay's codes are short random strings, so
  512 leaves plenty of room while blocking abuse.
- Notification body: at most 64 KiB (kibibytes). Real notifications are well
  under 1 KiB.
- Acknowledge body: at most 4 KiB.
- `notificationId`: at most 256 characters.
- Signature header: at most 8192 characters. It must decode from Base64 to a
  JSON object whose `alg`, `kid`, `signature` and `digest` fields are
  non-empty strings. Only the shape is checked. A wrong but well-formed
  signature is still queued.
- Unknown extra JSON fields in a notification are allowed, in case eBay adds
  fields later. Missing or wrongly-typed required fields get a `400`.

**Paging.** `pending` returns its results in pages because a single Worker
call is limited in how many storage operations it can make. Keep calling it
with the returned `cursor` until `cursor` is `null`.

**When to acknowledge.** Acknowledge an id only after you have handled every
entry carrying that id. That includes deleting entries that failed
verification, so junk does not pile up. Storage listings can take up to about
60 seconds to catch up, so an entry you just stored may not show up straight
away.

## Secrets

Make two **different** random secrets. Do not put either in a file or in git.

```sh
python -c "import secrets; print(secrets.token_urlsafe(48))"   # run twice, one value per secret
```

- `DELETION_VERIFICATION_TOKEN`: the verification token you give eBay.
  eBay's rules are 32 to 80 characters, using only letters, digits, `_` and
  `-`. `token_urlsafe(48)` gives 64 characters that meet these rules. If the
  value breaks these rules, the Worker refuses the handshake with a `503`.
- `STATUS_API_SECRET`: the secret your local app sends as the bearer token.
  It must be at least 32 visible ASCII characters. If it is not set, every
  `pending` and `acknowledge` call is refused with a `401`. This is the same
  value you put in the Python app's `ACCOUNT_DELETION_STATUS_SECRET` setting.

```sh
npx wrangler secret put DELETION_VERIFICATION_TOKEN
npx wrangler secret put STATUS_API_SECRET
```

The secret check is constant-time, so its response time does not reveal how
close a guess was. Both values are hashed with SHA-256 first, so they are
always the same length, and then compared with the runtime's
`crypto.subtle.timingSafeEqual`. That function is a non-standard Cloudflare
addition documented in the Workers Web Crypto reference. If it is missing, an
XOR (exclusive-or) loop that always checks every byte is used instead.

## Storage setup (KV namespace)

KV is Cloudflare Workers' key-value storage.

```sh
npx wrangler kv namespace create DELETION_EVENTS
```

Paste the printed id into `wrangler.jsonc` in place of
`REPLACE_WITH_KV_NAMESPACE_ID`.

Entries expire automatically after **90 days**. This keeps storage from
growing forever when nobody collects the entries. **Your local app must check
the queue well within 90 days.** Daily is a good choice. Anything left longer
than 90 days is lost.

## Watch list and cost

eBay sends a notification for **every** account closed anywhere on eBay to
every registered endpoint. eBay has reported about 13,000 a week.

- `WATCHED_EBAY_USER_IDS`: comma-separated eBay user IDs. Matching is exact.
- `WATCHED_EBAY_USERNAMES`: comma-separated usernames. Matching ignores
  upper/lower case.

If either list has at least one entry, a notification is stored only when its
`userId` or `username` is on a list. Anything else gets an immediate `204` and
is **not stored**. eBay is satisfied and nothing is spent. The response is
exactly the same whether or not the notification was stored, so an outsider
cannot use it to find out who is on your list.

If both lists are empty or not set, **everything is stored**. At about 13,000
writes a week, plus the matching reads and deletes, that will go over the free
Workers plan's daily KV write limit (1,000 a day). You would then need the
**paid Workers plan**. If writes fail, the Worker returns `503`, eBay keeps
retrying, and after 24 hours eBay marks the endpoint as down.

**Who to put on the list — this app's situation specifically.** This
application's own privacy notice (`ebay_app/views.py`'s `privacy_page()`)
states plainly that buyer identity, contact details, delivery addresses and
payment details are "not displayed or stored" by this app, and the Phase 9
audit confirmed no buyer data is written to its database. On that basis, the
recommended default here is to watch **only this account's own eBay user ID
and username** (the ones shown on the app's Account page) -- that covers the
one case this app can actually act on: its own connected seller account being
closed. Whether that is enough to satisfy eBay's notification requirement, or
whether you additionally need to apply for eBay's stated exemption (available
in the Developer Portal for apps that store no personal data) or widen this
list, is a compliance decision for you to make, not something this code
decides on its own.

Set the lists as secrets, so they survive `wrangler deploy` and stay out of
git:

```sh
npx wrangler secret put WATCHED_EBAY_USER_IDS
npx wrangler secret put WATCHED_EBAY_USERNAMES
```

## Deploy and register with eBay

1. `npx wrangler deploy`. The Worker is served at
   `https://ebay-account-deletion-receiver.<your-subdomain>.workers.dev`.
2. Optional check:
   `curl "https://ebay-account-deletion-receiver.<sub>.workers.dev/ebay/account-deletion?challenge_code=test123"`
   should return `{"challengeResponse":"…"}`. You can confirm the value with
   `printf '%s' "test123<token><endpoint>" | sha256sum`.
3. In the eBay Developer Portal, go to **Alerts & Notifications** for your
   Production keyset and choose **Marketplace Account Deletion**.
   - Notification endpoint URL:
     `https://ebay-account-deletion-receiver.<your-subdomain>.workers.dev/ebay/account-deletion`.
     Type it in **lower case**, with no trailing slash and no query string.
     The Worker hashes exactly that string, so any difference will make the
     handshake fail.
   - Verification token: the exact value of `DELETION_VERIFICATION_TOKEN`.
   - Save. eBay then sends the handshake. Use **Send Test Notification** to
     check that a test entry appears in `pending`.
4. In the Python app's `.env.production`, set `ACCOUNT_DELETION_WORKER_URL` to
   this Worker's base `https://...workers.dev` address (no path) and
   `ACCOUNT_DELETION_STATUS_SECRET` to the same value as `STATUS_API_SECRET`
   above. The app then polls automatically at startup and periodically while
   running; see the main project `README.md`'s "Account deletion" section.

**This appears to be a Production-only eBay feature.** Every eBay setup
screen and reference this project found when researching it pointed to
Production (there is no visible Sandbox option for registering this
notification, and nothing suggests eBay ever sends a real or test
notification inside Sandbox), though eBay does not say so in so many words.
Practically, that means this Worker and the Python verifier are tested here
using eBay's own published sample data (see
`tests/test_notification_signature.py`) rather than live Sandbox traffic, and
the first real end-to-end test happens only once this is connected to a real
Production eBay account.

The host pinning only accepts the `workers.dev` hostname. A custom domain
would need code changes, both in the pinning and in the endpoint string that
gets hashed.

## Recommended Cloudflare settings (outside this code)

- **Rate limiting:** add a Cloudflare dashboard Rate Limiting rule (in the
  WAF, or Web Application Firewall) covering `/ebay/account-deletion` (`GET`
  and `POST`) and `/account-deletion/*`. Do not set it so low that eBay's own
  bursts are blocked. A custom counter in the Worker would need extra storage
  and cost, so it was not built.
- Keep observability and Logpush turned off.

## Tests

```sh
node --test test/*.test.mjs
```

No dependencies. The tests use an in-memory stand-in for the KV storage.
118/118 passing as of this Worker's last review.
