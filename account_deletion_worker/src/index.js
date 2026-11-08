// eBay Marketplace Account Deletion/Closure notification receiver.
//
// SECURITY MODEL (read before changing anything):
//   * This Worker NEVER holds, receives, or uses any eBay application
//     credential (no Client ID, no Client Secret, no OAuth token). Do not add
//     any eBay API call, OAuth flow, or credential here.
//   * It does NOT verify eBay's ECDSA signature on notifications. Everything it
//     queues is UNTRUSTED. A separate local application, which holds the eBay
//     credentials on the user's own machine, fetches the queue, verifies each
//     entry against the exact raw bytes stored here, and acknowledges it.
//   * Its only jobs are: (1) answer eBay's ownership-verification handshake
//     using a user-invented verification token, and (2) durably queue incoming
//     notifications for the local application to collect.
//   * No logging of any kind. Fail closed on anything unexpected.

const WORKER_NAME = "ebay-account-deletion-receiver";

const MAX_REQUEST_URL_LENGTH = 4096;
const MAX_NOTIFICATION_BODY_BYTES = 64 * 1024; // 64 KiB
const MAX_ACK_BODY_BYTES = 4 * 1024; // 4 KiB
const MAX_CHALLENGE_CODE_LENGTH = 512;
const MAX_SIGNATURE_HEADER_LENGTH = 8192;
const MAX_SIGNATURE_FIELD_LENGTH = 4096;
const MAX_NOTIFICATION_ID_LENGTH = 256;
const MAX_CURSOR_LENGTH = 2048;
const MAX_WATCH_LIST_ENTRIES = 1000;
const MAX_ACK_LIST_ROUNDS = 20;

// 90 days. Entries the local app never collects expire on their own, so the
// namespace cannot grow without bound. The local app must poll more often
// than this or notifications will be lost.
const PENDING_TTL_SECONDS = 90 * 24 * 60 * 60;
const PENDING_PAGE_SIZE = 50;
const PENDING_KEY_PREFIX = "pending:";

const DELETION_PATH = "/ebay/account-deletion";
const PENDING_PATH = "/account-deletion/pending";
const ACK_PATH = "/account-deletion/acknowledge";

const EXPECTED_TOPIC = "MARKETPLACE_ACCOUNT_DELETION";
const RECORD_FORMAT_VERSION = 1;

// eBay's own rule for the verification token: 32-80 characters,
// alphanumerics, underscore and hyphen only.
const VERIFICATION_TOKEN_PATTERN = /^[A-Za-z0-9_-]{32,80}$/;
const MIN_STATUS_SECRET_LENGTH = 32;
const MAX_STATUS_SECRET_LENGTH = 1024;

// Visible ASCII only, no spaces (0x21-0x7E).
const VISIBLE_ASCII_PATTERN = /^[\x21-\x7E]+$/;
// C0 controls, DEL, C1 controls, line/paragraph separators.
const CONTROL_CHARACTER_PATTERN = /[\u0000-\u001F\u007F-\u009F\u2028\u2029]/;
const STRICT_BASE64_PATTERN = /^[A-Za-z0-9+/]+={0,2}$/;
const DIGITS_PATTERN = /^[0-9]{1,15}$/;

const SECURITY_HEADERS = Object.freeze({
  "Cache-Control": "no-store, max-age=0",
  Pragma: "no-cache",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Content-Security-Policy":
    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
  "Permissions-Policy":
    "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
  "Cross-Origin-Resource-Policy": "same-origin",
});

const textEncoder = new TextEncoder();

// ---------------------------------------------------------------------------
// Response helpers. Every response carries SECURITY_HEADERS.
// ---------------------------------------------------------------------------

function buildHeaders(extra) {
  const headers = new Headers(SECURITY_HEADERS);
  if (extra) {
    for (const [name, value] of Object.entries(extra)) {
      headers.set(name, value);
    }
  }
  return headers;
}

function jsonResponse(status, payload, extraHeaders) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: buildHeaders({
      "Content-Type": "application/json",
      ...extraHeaders,
    }),
  });
}

function errorResponse(status, message, extraHeaders) {
  return jsonResponse(status, { error: message }, extraHeaders);
}

function emptyResponse(status) {
  return new Response(null, { status, headers: buildHeaders() });
}

// ---------------------------------------------------------------------------
// Generic validation helpers.
// ---------------------------------------------------------------------------

function isCanonicalWorkerHost(url) {
  const labels = url.hostname.toLowerCase().split(".");
  return (
    url.protocol === "https:" &&
    url.port === "" &&
    url.username === "" &&
    url.password === "" &&
    labels.length === 4 &&
    labels[0] === WORKER_NAME &&
    labels[2] === "workers" &&
    labels[3] === "dev" &&
    /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(labels[1])
  );
}

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isBoundedString(value, minLength, maxLength) {
  return (
    typeof value === "string" &&
    value.length >= minLength &&
    value.length <= maxLength &&
    !CONTROL_CHARACTER_PATTERN.test(value)
  );
}

// Returns a Map of the query parameters, or null if any parameter is not in
// the allowlist, is repeated, or contains control characters.
function readAllowedQuery(url, allowedNames) {
  const result = new Map();
  for (const [name, value] of url.searchParams) {
    if (!allowedNames.includes(name) || result.has(name)) {
      return null;
    }
    if (CONTROL_CHARACTER_PATTERN.test(value)) {
      return null;
    }
    result.set(name, value);
  }
  return result;
}

function hasJsonContentType(request) {
  const header = request.headers.get("Content-Type");
  if (header === null) {
    return false;
  }
  const mediaType = header.split(";", 1)[0].trim().toLowerCase();
  return mediaType === "application/json";
}

// Reads at most maxBytes from the request body. Returns a Uint8Array, or the
// string "too_large" if the limit is exceeded, or "bad_length" if a
// Content-Length header is present but malformed.
async function readBodyCapped(request, maxBytes) {
  const declared = request.headers.get("Content-Length");
  if (declared !== null) {
    if (!DIGITS_PATTERN.test(declared)) {
      return "bad_length";
    }
    if (Number(declared) > maxBytes) {
      return "too_large";
    }
  }
  if (request.body === null) {
    return new Uint8Array(0);
  }
  const reader = request.body.getReader();
  const chunks = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    total += value.byteLength;
    if (total > maxBytes) {
      try {
        await reader.cancel();
      } catch {
        // Ignore: we are rejecting the request anyway.
      }
      return "too_large";
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

// Strict UTF-8 decode. ignoreBOM: true keeps any BOM in the string so the
// stored text is byte-for-byte what was received (a BOM then makes
// JSON.parse fail, which is the desired outcome).
function decodeUtf8Strict(bytes) {
  try {
    return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(
      bytes,
    );
  } catch {
    return null;
  }
}

function parseJson(text) {
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch {
    return { ok: false, value: undefined };
  }
}

// ---------------------------------------------------------------------------
// Crypto helpers.
// ---------------------------------------------------------------------------

function toHex(buffer) {
  const bytes = new Uint8Array(buffer);
  let out = "";
  for (let i = 0; i < bytes.length; i += 1) {
    out += bytes[i].toString(16).padStart(2, "0");
  }
  return out;
}

async function sha256Bytes(input) {
  const data = typeof input === "string" ? textEncoder.encode(input) : input;
  return new Uint8Array(await crypto.subtle.digest("SHA-256", data));
}

async function sha256Hex(input) {
  return toHex(await sha256Bytes(input));
}

// Constant-time comparison of two equal-length byte arrays. Uses the Workers
// runtime's non-standard crypto.subtle.timingSafeEqual when present (it throws
// on unequal lengths, which cannot happen here because callers only pass
// SHA-256 digests), and an XOR-accumulate loop otherwise.
function timingSafeEqualBytes(a, b) {
  if (a.byteLength !== b.byteLength) {
    return false;
  }
  const subtle = globalThis.crypto && globalThis.crypto.subtle;
  if (subtle && typeof subtle.timingSafeEqual === "function") {
    return subtle.timingSafeEqual(a, b) === true;
  }
  let diff = 0;
  for (let i = 0; i < a.byteLength; i += 1) {
    diff |= a[i] ^ b[i];
  }
  return diff === 0;
}

function isUsableStatusSecret(value) {
  return (
    typeof value === "string" &&
    value.length >= MIN_STATUS_SECRET_LENGTH &&
    value.length <= MAX_STATUS_SECRET_LENGTH &&
    VISIBLE_ASCII_PATTERN.test(value)
  );
}

// Both sides are hashed to fixed-length digests before the constant-time
// comparison, so neither the secret's length nor its content leaks through
// timing, and the comparison always runs even when the secret is unset.
async function isAuthorized(request, env) {
  const secret = env.STATUS_API_SECRET;
  const secretUsable = isUsableStatusSecret(secret);

  let presented = "";
  const header = request.headers.get("Authorization");
  if (typeof header === "string") {
    const match = /^Bearer ([\x21-\x7E]+)$/i.exec(header);
    if (match) {
      presented = match[1];
    }
  }

  const [presentedDigest, expectedDigest] = await Promise.all([
    sha256Bytes("status-auth\u0000" + presented),
    sha256Bytes("status-auth\u0000" + (secretUsable ? secret : "")),
  ]);
  const digestsEqual = timingSafeEqualBytes(presentedDigest, expectedDigest);
  return secretUsable && presented.length > 0 && digestsEqual;
}

// ---------------------------------------------------------------------------
// eBay-specific shape validation.
// ---------------------------------------------------------------------------

// Cheap shape check only: base64 -> UTF-8 JSON object with string fields
// alg, kid, signature, digest. Does NOT check the signature cryptographically
// and does not check field values; a well-shaped but wrong signature passes.
function isWellShapedSignatureHeader(value) {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > MAX_SIGNATURE_HEADER_LENGTH ||
    value.length % 4 !== 0 ||
    !STRICT_BASE64_PATTERN.test(value)
  ) {
    return false;
  }
  let binary;
  try {
    binary = atob(value);
  } catch {
    return false;
  }
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  const text = decodeUtf8Strict(bytes);
  if (text === null) {
    return false;
  }
  const parsed = parseJson(text);
  if (!parsed.ok || !isPlainObject(parsed.value)) {
    return false;
  }
  for (const field of ["alg", "kid", "signature", "digest"]) {
    if (!isBoundedString(parsed.value[field], 1, MAX_SIGNATURE_FIELD_LENGTH)) {
      return false;
    }
  }
  return true;
}

// Returns the validated fields, or null if the document is structurally
// invalid. Unknown extra fields are tolerated (forward compatibility); the
// downstream verifier works from the raw bytes anyway.
function validateNotificationDocument(doc) {
  if (!isPlainObject(doc)) return null;
  const metadata = doc.metadata;
  const notification = doc.notification;
  if (!isPlainObject(metadata) || !isPlainObject(notification)) return null;

  if (metadata.topic !== EXPECTED_TOPIC) return null;
  if (!isBoundedString(metadata.schemaVersion, 1, 32)) return null;
  if (typeof metadata.deprecated !== "boolean") return null;

  if (
    !isBoundedString(
      notification.notificationId,
      1,
      MAX_NOTIFICATION_ID_LENGTH,
    )
  ) {
    return null;
  }
  if (!isBoundedString(notification.eventDate, 1, 64)) return null;
  if (!isBoundedString(notification.publishDate, 1, 64)) return null;
  const attempts = notification.publishAttemptCount;
  if (!Number.isSafeInteger(attempts) || attempts < 1) return null;

  const data = notification.data;
  if (!isPlainObject(data)) return null;
  if (!isBoundedString(data.username, 0, 256)) return null;
  if (!isBoundedString(data.userId, 0, 256)) return null;
  if (!isBoundedString(data.eiasToken, 0, 1024)) return null;

  return {
    topic: metadata.topic,
    schemaVersion: metadata.schemaVersion,
    notificationId: notification.notificationId,
    eventDate: notification.eventDate,
    publishDate: notification.publishDate,
    publishAttemptCount: attempts,
    data: {
      username: data.username,
      userId: data.userId,
      eiasToken: data.eiasToken,
    },
  };
}

function parseWatchList(raw, normalise) {
  if (typeof raw !== "string") {
    return [];
  }
  const entries = [];
  for (const piece of raw.split(",")) {
    const trimmed = piece.trim();
    if (trimmed.length > 0 && !CONTROL_CHARACTER_PATTERN.test(trimmed)) {
      entries.push(normalise(trimmed));
    }
    if (entries.length >= MAX_WATCH_LIST_ENTRIES) {
      break;
    }
  }
  return entries;
}

const identity = (value) => value;
const asciiLower = (value) => value.replace(/[A-Z]/g, (c) => c.toLowerCase());

// Returns true if the notification should be stored. If neither watch list is
// configured, everything is stored. User IDs match exactly; usernames match
// ASCII-case-insensitively (over-matching only costs a KV write).
function isWatched(fields, env) {
  const userIds = parseWatchList(env.WATCHED_EBAY_USER_IDS, identity);
  const usernames = parseWatchList(env.WATCHED_EBAY_USERNAMES, asciiLower);
  if (userIds.length === 0 && usernames.length === 0) {
    return true;
  }
  const { userId, username } = fields.data;
  if (userId.length > 0 && userIds.includes(userId)) {
    return true;
  }
  if (username.length > 0 && usernames.includes(asciiLower(username))) {
    return true;
  }
  return false;
}

function hasKvBinding(env) {
  const kv = env.DELETION_EVENTS;
  return (
    kv !== null &&
    typeof kv === "object" &&
    typeof kv.get === "function" &&
    typeof kv.put === "function" &&
    typeof kv.delete === "function" &&
    typeof kv.list === "function"
  );
}

// ---------------------------------------------------------------------------
// Route handlers.
// ---------------------------------------------------------------------------

// GET /ebay/account-deletion?challenge_code=...
// challengeResponse = hex(SHA-256(challengeCode + verificationToken + endpoint))
async function handleChallenge(request, url, env) {
  const query = readAllowedQuery(url, ["challenge_code"]);
  if (query === null) {
    return errorResponse(400, "Unexpected query parameters.");
  }
  const challengeCode = query.get("challenge_code");
  if (
    typeof challengeCode !== "string" ||
    challengeCode.length < 1 ||
    challengeCode.length > MAX_CHALLENGE_CODE_LENGTH ||
    !VISIBLE_ASCII_PATTERN.test(challengeCode)
  ) {
    return errorResponse(400, "Invalid challenge_code.");
  }

  const token = env.DELETION_VERIFICATION_TOKEN;
  if (typeof token !== "string" || !VERIFICATION_TOKEN_PATTERN.test(token)) {
    return errorResponse(503, "Service unavailable.");
  }

  // The endpoint string must exactly equal the URL registered in the eBay
  // Developer Portal. The host has already been pinned to this Worker's
  // canonical (lower-case) workers.dev hostname.
  const endpoint = `https://${url.hostname.toLowerCase()}${DELETION_PATH}`;
  const challengeResponse = await sha256Hex(challengeCode + token + endpoint);
  return jsonResponse(200, { challengeResponse });
}

// POST /ebay/account-deletion
async function handleNotification(request, url, env) {
  if (readAllowedQuery(url, []) === null) {
    return errorResponse(400, "Unexpected query parameters.");
  }
  if (!hasJsonContentType(request)) {
    return errorResponse(415, "Content-Type must be application/json.");
  }

  const signatureHeader = request.headers.get("X-EBAY-SIGNATURE");
  if (signatureHeader === null) {
    return errorResponse(400, "Missing signature header.");
  }
  if (!isWellShapedSignatureHeader(signatureHeader)) {
    return errorResponse(400, "Malformed signature header.");
  }

  const body = await readBodyCapped(request, MAX_NOTIFICATION_BODY_BYTES);
  if (body === "too_large") {
    return errorResponse(413, "Request body too large.");
  }
  if (body === "bad_length") {
    return errorResponse(400, "Invalid Content-Length.");
  }
  const rawBody = decodeUtf8Strict(body);
  if (rawBody === null) {
    return errorResponse(400, "Body is not valid UTF-8.");
  }
  const parsed = parseJson(rawBody);
  if (!parsed.ok) {
    return errorResponse(400, "Body is not valid JSON.");
  }
  const fields = validateNotificationDocument(parsed.value);
  if (fields === null) {
    return errorResponse(400, "Unexpected notification structure.");
  }

  // Not a watched account: acknowledge without storing. The response is
  // identical to the stored case so an outsider cannot probe which accounts
  // are on the watch list.
  if (!isWatched(fields, env)) {
    return emptyResponse(204);
  }

  if (!hasKvBinding(env)) {
    return errorResponse(503, "Service unavailable.");
  }

  // Key = hash(notificationId) + hash(exact raw body). Because the body hash
  // is part of the key, an unsigned forged POST that reuses a genuine
  // notificationId can never overwrite or displace the genuine entry; it just
  // becomes a sibling entry that the local verifier will reject. Identical
  // redeliveries collapse onto the same key.
  const [idHash, rawBodySha256] = await Promise.all([
    sha256Hex(fields.notificationId),
    sha256Hex(body),
  ]);
  const key = `${PENDING_KEY_PREFIX}${idHash}:${rawBodySha256}`;

  const record = {
    formatVersion: RECORD_FORMAT_VERSION,
    signatureVerified: false,
    receivedAt: new Date().toISOString(),
    notificationId: fields.notificationId,
    topic: fields.topic,
    schemaVersion: fields.schemaVersion,
    eventDate: fields.eventDate,
    publishDate: fields.publishDate,
    publishAttemptCount: fields.publishAttemptCount,
    data: fields.data,
    notification: parsed.value,
    signatureHeader,
    rawBody,
    rawBodySha256,
  };

  try {
    await env.DELETION_EVENTS.put(key, JSON.stringify(record), {
      expirationTtl: PENDING_TTL_SECONDS,
    });
  } catch {
    // Non-2xx makes eBay retry later.
    return errorResponse(503, "Service unavailable.");
  }
  return emptyResponse(204);
}

// GET /account-deletion/pending[?cursor=...]
async function handlePending(request, url, env) {
  if (!(await isAuthorized(request, env))) {
    return errorResponse(401, "Unauthorized.", { "WWW-Authenticate": "Bearer" });
  }
  const query = readAllowedQuery(url, ["cursor"]);
  if (query === null) {
    return errorResponse(400, "Unexpected query parameters.");
  }
  const cursor = query.get("cursor");
  if (
    cursor !== undefined &&
    (cursor.length < 1 ||
      cursor.length > MAX_CURSOR_LENGTH ||
      !VISIBLE_ASCII_PATTERN.test(cursor))
  ) {
    return errorResponse(400, "Invalid cursor.");
  }
  if (!hasKvBinding(env)) {
    return errorResponse(503, "Service unavailable.");
  }

  let listing;
  let values;
  try {
    const options = { prefix: PENDING_KEY_PREFIX, limit: PENDING_PAGE_SIZE };
    if (cursor !== undefined) {
      options.cursor = cursor;
    }
    listing = await env.DELETION_EVENTS.list(options);
    const keys = Array.isArray(listing && listing.keys) ? listing.keys : [];
    values = await Promise.all(
      keys.map((entry) => env.DELETION_EVENTS.get(entry.name, { type: "text" })),
    );
  } catch {
    return errorResponse(503, "Service unavailable.");
  }

  const entries = [];
  for (const value of values) {
    if (typeof value !== "string") {
      continue; // Expired or deleted between list and get.
    }
    const parsed = parseJson(value);
    if (!parsed.ok || !isPlainObject(parsed.value)) {
      continue;
    }
    const r = parsed.value;
    entries.push({
      notificationId: r.notificationId,
      eventDate: r.eventDate,
      publishDate: r.publishDate,
      publishAttemptCount: r.publishAttemptCount,
      data: r.data,
      signatureHeader: r.signatureHeader,
      rawBody: r.rawBody,
      rawBodySha256: r.rawBodySha256,
      receivedAt: r.receivedAt,
      signatureVerified: false,
    });
  }

  const nextCursor =
    listing && listing.list_complete === false && typeof listing.cursor === "string"
      ? listing.cursor
      : null;
  return jsonResponse(200, { entries, cursor: nextCursor });
}

// POST /account-deletion/acknowledge  body: {"notificationId": "..."}
// Idempotent: deletes every stored entry with that notificationId (if any).
async function handleAcknowledge(request, url, env) {
  if (!(await isAuthorized(request, env))) {
    return errorResponse(401, "Unauthorized.", { "WWW-Authenticate": "Bearer" });
  }
  if (readAllowedQuery(url, []) === null) {
    return errorResponse(400, "Unexpected query parameters.");
  }
  if (!hasJsonContentType(request)) {
    return errorResponse(415, "Content-Type must be application/json.");
  }
  const body = await readBodyCapped(request, MAX_ACK_BODY_BYTES);
  if (body === "too_large") {
    return errorResponse(413, "Request body too large.");
  }
  if (body === "bad_length") {
    return errorResponse(400, "Invalid Content-Length.");
  }
  const text = decodeUtf8Strict(body);
  const parsed = text === null ? { ok: false } : parseJson(text);
  if (
    !parsed.ok ||
    !isPlainObject(parsed.value) ||
    Object.keys(parsed.value).length !== 1 ||
    !isBoundedString(
      parsed.value.notificationId,
      1,
      MAX_NOTIFICATION_ID_LENGTH,
    )
  ) {
    return errorResponse(400, "Body must be {\"notificationId\": \"...\"}.");
  }
  if (!hasKvBinding(env)) {
    return errorResponse(503, "Service unavailable.");
  }

  const prefix = `${PENDING_KEY_PREFIX}${await sha256Hex(parsed.value.notificationId)}:`;
  try {
    let cursor;
    for (let round = 0; round < MAX_ACK_LIST_ROUNDS; round += 1) {
      const options = { prefix, limit: 100 };
      if (cursor !== undefined) {
        options.cursor = cursor;
      }
      const listing = await env.DELETION_EVENTS.list(options);
      const keys = Array.isArray(listing && listing.keys) ? listing.keys : [];
      await Promise.all(keys.map((entry) => env.DELETION_EVENTS.delete(entry.name)));
      if (!listing || listing.list_complete !== false || typeof listing.cursor !== "string") {
        return jsonResponse(200, { acknowledged: true });
      }
      cursor = listing.cursor;
    }
  } catch {
    return errorResponse(503, "Service unavailable.");
  }
  // Pathological number of sibling entries: ask the caller to retry.
  return errorResponse(503, "Service unavailable.");
}

// ---------------------------------------------------------------------------
// Router. A Map (not a plain object) so paths like "/__proto__" cannot hit
// inherited properties.
// ---------------------------------------------------------------------------

const ROUTES = new Map([
  [DELETION_PATH, new Map([["GET", handleChallenge], ["POST", handleNotification]])],
  [PENDING_PATH, new Map([["GET", handlePending]])],
  [ACK_PATH, new Map([["POST", handleAcknowledge]])],
]);

async function route(request, env) {
  if (typeof request.url !== "string" || request.url.length > MAX_REQUEST_URL_LENGTH) {
    return errorResponse(414, "Request URL is too long.");
  }
  if (CONTROL_CHARACTER_PATTERN.test(request.url)) {
    return errorResponse(400, "Bad request.");
  }
  let url;
  try {
    url = new URL(request.url);
  } catch {
    return errorResponse(400, "Bad request.");
  }
  if (!isCanonicalWorkerHost(url)) {
    return errorResponse(421, "Misdirected request.");
  }

  const methods = ROUTES.get(url.pathname);
  if (methods === undefined) {
    return errorResponse(404, "Not found.");
  }
  const handler = methods.get(request.method);
  if (handler === undefined) {
    return errorResponse(405, "Method not allowed.", {
      Allow: Array.from(methods.keys()).join(", "),
    });
  }
  return handler(request, url, env || {});
}

export default {
  async fetch(request, env) {
    try {
      return await route(request, env);
    } catch {
      // Never leak internals. A 500 on the notification route is also an
      // eBay-retriable outcome.
      return errorResponse(500, "Internal error.");
    }
  },
};
