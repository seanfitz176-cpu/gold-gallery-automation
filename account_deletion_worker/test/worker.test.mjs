import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";

import worker from "../src/index.js";

// ---------------------------------------------------------------------------
// Fixtures and fakes.
// ---------------------------------------------------------------------------

const HOST = "ebay-account-deletion-receiver.example-sub.workers.dev";
const BASE = `https://${HOST}`;
const ENDPOINT = `${BASE}/ebay/account-deletion`;
const VERIFICATION_TOKEN = "TestVerificationToken_0123456789abcdef-XYZ"; // 42 chars
const STATUS_SECRET = "status-secret-for-tests-ONLY-0123456789-abcdefghijkl";
const TTL_90_DAYS = 90 * 24 * 60 * 60;

const EXPECTED_SECURITY_HEADERS = {
  "cache-control": "no-store, max-age=0",
  pragma: "no-cache",
  "referrer-policy": "no-referrer",
  "x-content-type-options": "nosniff",
  "x-frame-options": "DENY",
  "content-security-policy":
    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
  "permissions-policy":
    "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
  "cross-origin-resource-policy": "same-origin",
};

class FakeKV {
  constructor() {
    this.store = new Map();
    this.putCalls = [];
    this.failOn = new Set(); // "get" | "put" | "delete" | "list"
  }
  #maybeFail(op) {
    if (this.failOn.has(op)) {
      throw new Error(`simulated KV ${op} failure`);
    }
  }
  async get(key, options) {
    this.#maybeFail("get");
    assert.deepEqual(options, { type: "text" });
    return this.store.has(key) ? this.store.get(key).value : null;
  }
  async put(key, value, options) {
    this.#maybeFail("put");
    assert.equal(typeof value, "string");
    this.putCalls.push({ key, value, options });
    this.store.set(key, { value, options });
  }
  async delete(key) {
    this.#maybeFail("delete");
    this.store.delete(key);
  }
  async list({ prefix = "", limit = 1000, cursor } = {}) {
    this.#maybeFail("list");
    const names = [...this.store.keys()].filter((k) => k.startsWith(prefix)).sort();
    const start = cursor === undefined ? 0 : Number(cursor.slice("c".length));
    const page = names.slice(start, start + limit);
    const end = start + page.length;
    const complete = end >= names.length;
    return {
      keys: page.map((name) => ({ name })),
      list_complete: complete,
      ...(complete ? {} : { cursor: `c${end}` }),
    };
  }
}

function makeEnv(overrides = {}) {
  return {
    DELETION_EVENTS: new FakeKV(),
    DELETION_VERIFICATION_TOKEN: VERIFICATION_TOKEN,
    STATUS_API_SECRET: STATUS_SECRET,
    ...overrides,
  };
}

function b64(text) {
  return Buffer.from(text, "utf8").toString("base64");
}

// Well-shaped but cryptographically meaningless signature header.
const GARBAGE_SIGNATURE = b64(
  JSON.stringify({
    alg: "ecdsa",
    kid: "99345d5d-1c0c-4a52-9f35-6e3a5b8f0000",
    signature: "MEUCIQDgarbagegarbagegarbagegarbagegarbagegarbage==",
    digest: "SHA1",
  }),
);

function notificationDoc({
  notificationId = "49feeaeb-4982-42d9-a377-9645b8479411_33f7e043-fed8-442b-9d44-791923bd9a6d",
  username = "test_buyer_1",
  userId = "ma8vp1jySJC",
  eiasToken = "nY+sHZ2PrBmdj6wVnY+sEZ2PrA2dj6wJnY+gAZGEpwmdj6x9nY+seQ==",
  publishAttemptCount = 1,
} = {}) {
  return {
    metadata: { topic: "MARKETPLACE_ACCOUNT_DELETION", schemaVersion: "1.0", deprecated: false },
    notification: {
      notificationId,
      eventDate: "2026-10-01T12:00:00.000Z",
      publishDate: "2026-10-01T12:00:01.000Z",
      publishAttemptCount,
      data: { username, userId, eiasToken },
    },
  };
}

function call(env, path, init = {}, base = BASE) {
  return worker.fetch(new Request(`${base}${path}`, init), env);
}

function postNotification(env, body, { signature = GARBAGE_SIGNATURE, headers = {}, path = "/ebay/account-deletion" } = {}) {
  const h = { "Content-Type": "application/json", ...headers };
  if (signature !== null) {
    h["X-EBAY-SIGNATURE"] = signature;
  }
  const payload = typeof body === "string" ? body : JSON.stringify(body);
  return call(env, path, { method: "POST", headers: h, body: payload });
}

function authed(extra = {}) {
  return { Authorization: `Bearer ${STATUS_SECRET}`, ...extra };
}

function getPending(env, { headers = authed(), query = "" } = {}) {
  return call(env, `/account-deletion/pending${query}`, { method: "GET", headers });
}

function acknowledge(env, body, { headers = authed({ "Content-Type": "application/json" }) } = {}) {
  return call(env, "/account-deletion/acknowledge", {
    method: "POST",
    headers,
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

function assertSecurityHeaders(response) {
  for (const [name, value] of Object.entries(EXPECTED_SECURITY_HEADERS)) {
    assert.equal(response.headers.get(name), value, `header ${name}`);
  }
}

// ---------------------------------------------------------------------------
// Challenge handshake.
// ---------------------------------------------------------------------------

describe("GET /ebay/account-deletion (challenge handshake)", () => {
  const challengeCode = "a8628072-3d33-45ee-9004-bee86830a22d";

  test("computes SHA-256(challengeCode + verificationToken + endpoint) as hex", async () => {
    const res = await call(makeEnv(), `/ebay/account-deletion?challenge_code=${challengeCode}`);
    assert.equal(res.status, 200);
    assert.equal(res.headers.get("content-type"), "application/json");
    assertSecurityHeaders(res);

    const raw = new Uint8Array(await res.arrayBuffer());
    // No UTF-8 byte-order mark.
    assert.notDeepEqual([...raw.slice(0, 3)], [0xef, 0xbb, 0xbf]);
    const body = JSON.parse(new TextDecoder().decode(raw));
    assert.deepEqual(Object.keys(body), ["challengeResponse"]);

    // Independent computation with Node's crypto.
    const expected = createHash("sha256")
      .update(challengeCode)
      .update(VERIFICATION_TOKEN)
      .update(ENDPOINT)
      .digest("hex");
    assert.equal(body.challengeResponse, expected);
    // And a hard-coded value computed separately with the `sha256sum` CLI over
    // the literal concatenated string, so the test does not only compare the
    // implementation against itself.
    assert.equal(
      body.challengeResponse,
      "9cb157c8548aa0570dbf001279f6d31f6cdaa9651b828764d1f0f5c561f4bc48",
    );
  });

  test("accepts a 512-character challenge code (upper bound)", async () => {
    const code = "x".repeat(512);
    const res = await call(makeEnv(), `/ebay/account-deletion?challenge_code=${code}`);
    assert.equal(res.status, 200);
    const body = await res.json();
    const expected = createHash("sha256").update(code + VERIFICATION_TOKEN + ENDPOINT).digest("hex");
    assert.equal(body.challengeResponse, expected);
  });

  const badQueries = {
    "missing challenge_code": "",
    "empty challenge_code": "?challenge_code=",
    "513-character challenge_code": `?challenge_code=${"x".repeat(513)}`,
    "newline in challenge_code": "?challenge_code=abc%0Adef",
    "NUL in challenge_code": "?challenge_code=abc%00def",
    "space in challenge_code": "?challenge_code=abc%20def",
    "non-ASCII challenge_code": "?challenge_code=abc%C3%A9",
    "unexpected extra parameter": "?challenge_code=abc&foo=bar",
    "only an unexpected parameter": "?foo=bar",
    "duplicated challenge_code": "?challenge_code=abc&challenge_code=def",
  };
  for (const [name, query] of Object.entries(badQueries)) {
    test(`rejects ${name} with 400`, async () => {
      const res = await call(makeEnv(), `/ebay/account-deletion${query}`);
      assert.equal(res.status, 400);
      assertSecurityHeaders(res);
    });
  }

  test("fails closed (503) when the verification token is missing or invalid", async () => {
    for (const token of [undefined, "", "short", "x".repeat(81), `${"a".repeat(40)}!`]) {
      const res = await call(
        makeEnv({ DELETION_VERIFICATION_TOKEN: token }),
        "/ebay/account-deletion?challenge_code=abc",
      );
      assert.equal(res.status, 503, `token=${token}`);
      const text = await res.text();
      assert.ok(!text.includes("challengeResponse"));
    }
  });
});

// ---------------------------------------------------------------------------
// Notification receiver.
// ---------------------------------------------------------------------------

describe("POST /ebay/account-deletion (notification receiver)", () => {
  test("rejects a POST with no signature header at all, storing nothing", async () => {
    const env = makeEnv();
    const res = await postNotification(env, notificationDoc(), { signature: null });
    assert.equal(res.status, 400);
    assertSecurityHeaders(res);
    assert.equal(env.DELETION_EVENTS.putCalls.length, 0);
  });

  const malformedSignatures = {
    "empty string": "",
    "not base64": "!!!not-base64!!!",
    "base64 with bad padding length": "abc",
    "base64 of non-JSON": b64("this is not json"),
    "base64 of a JSON array": b64(JSON.stringify(["ecdsa"])),
    "base64 of JSON missing kid": b64(JSON.stringify({ alg: "ecdsa", signature: "x", digest: "SHA1" })),
    "base64 of JSON with a non-string field": b64(
      JSON.stringify({ alg: "ecdsa", kid: 5, signature: "x", digest: "SHA1" }),
    ),
    "base64 of JSON with an empty field": b64(
      JSON.stringify({ alg: "ecdsa", kid: "", signature: "x", digest: "SHA1" }),
    ),
    "base64 of invalid UTF-8": Buffer.from([0xff, 0xfe, 0xfd, 0xfc]).toString("base64"),
    "oversized header": "A".repeat(8196),
  };
  for (const [name, signature] of Object.entries(malformedSignatures)) {
    test(`rejects a malformed signature header (${name}), storing nothing`, async () => {
      const env = makeEnv();
      const res = await postNotification(env, notificationDoc(), { signature });
      assert.equal(res.status, 400);
      assert.equal(env.DELETION_EVENTS.putCalls.length, 0);
    });
  }

  // CRITICAL BEHAVIOUR: this Worker does not (and must not) verify eBay's
  // ECDSA signature -- it holds no eBay credential and cannot fetch eBay's
  // public key. A structurally valid notification whose signature is wrong
  // must therefore still be ACCEPTED AND QUEUED, byte-for-byte, so the local
  // application can perform the real verification (and reject it there).
  // If this test ever starts failing because someone added "verification"
  // here, that change violates the design.
  test("accepts and queues a well-formed notification with a garbage signature", async () => {
    const env = makeEnv();
    // Deliberately odd whitespace and key order: the stored raw body must be
    // these exact bytes, not a re-serialisation.
    const rawBody =
      '{ "notification" : {"publishAttemptCount":1,"notificationId":"nid-123","eventDate":"2026-10-01T12:00:00.000Z",' +
      '"publishDate":"2026-10-01T12:00:01.000Z","data":{"userId":"ma8vp1jySJC","username":"test_buyer_1","eiasToken":"tok"}},' +
      '\n\t"metadata":{"topic":"MARKETPLACE_ACCOUNT_DELETION","schemaVersion":"1.0","deprecated":false} }';
    const res = await postNotification(env, rawBody);
    assert.equal(res.status, 204);
    assertSecurityHeaders(res);
    assert.equal(await res.text(), "");

    assert.equal(env.DELETION_EVENTS.putCalls.length, 1);
    const { key, value, options } = env.DELETION_EVENTS.putCalls[0];
    const idHash = createHash("sha256").update("nid-123").digest("hex");
    const bodyHash = createHash("sha256").update(rawBody).digest("hex");
    assert.equal(key, `pending:${idHash}:${bodyHash}`);
    assert.deepEqual(options, { expirationTtl: TTL_90_DAYS });

    const record = JSON.parse(value);
    assert.equal(record.rawBody, rawBody);
    assert.equal(record.rawBodySha256, bodyHash);
    assert.equal(record.signatureHeader, GARBAGE_SIGNATURE);
    assert.equal(record.signatureVerified, false);
    assert.equal(record.notificationId, "nid-123");
    assert.deepEqual(record.data, { username: "test_buyer_1", userId: "ma8vp1jySJC", eiasToken: "tok" });
    assert.deepEqual(record.notification, JSON.parse(rawBody));
  });

  test("preserves non-ASCII raw body bytes exactly", async () => {
    const env = makeEnv();
    const rawBody = JSON.stringify(notificationDoc({ username: "bjørn_ü_日本" }));
    const res = await postNotification(env, rawBody);
    assert.equal(res.status, 204);
    const record = JSON.parse(env.DELETION_EVENTS.putCalls[0].value);
    assert.deepEqual(Buffer.from(record.rawBody, "utf8"), Buffer.from(rawBody, "utf8"));
  });

  test("accepts Content-Type with parameters and mixed case", async () => {
    const env = makeEnv();
    const res = await postNotification(env, notificationDoc(), {
      headers: { "Content-Type": "Application/JSON; charset=utf-8" },
    });
    assert.equal(res.status, 204);
    assert.equal(env.DELETION_EVENTS.putCalls.length, 1);
  });

  test("rejects a non-JSON Content-Type with 415", async () => {
    const env = makeEnv();
    const res = await postNotification(env, notificationDoc(), {
      headers: { "Content-Type": "text/plain" },
    });
    assert.equal(res.status, 415);
    assert.equal(env.DELETION_EVENTS.putCalls.length, 0);
  });

  test("rejects query parameters on the notification POST", async () => {
    const env = makeEnv();
    const res = await postNotification(env, notificationDoc(), {
      path: "/ebay/account-deletion?x=1",
    });
    assert.equal(res.status, 400);
    assert.equal(env.DELETION_EVENTS.putCalls.length, 0);
  });

  const structurallyInvalid = {
    "empty body": "",
    "non-JSON body": "not json at all",
    "JSON array root": "[]",
    "JSON null": "null",
    "byte-order mark before JSON": `﻿${JSON.stringify(notificationDoc())}`,
    "wrong topic": (() => {
      const d = notificationDoc();
      d.metadata.topic = "SOMETHING_ELSE";
      return JSON.stringify(d);
    })(),
    "missing metadata": (() => {
      const d = notificationDoc();
      delete d.metadata;
      return JSON.stringify(d);
    })(),
    "deprecated is a string": (() => {
      const d = notificationDoc();
      d.metadata.deprecated = "false";
      return JSON.stringify(d);
    })(),
    "missing notificationId": (() => {
      const d = notificationDoc();
      delete d.notification.notificationId;
      return JSON.stringify(d);
    })(),
    "empty notificationId": JSON.stringify(notificationDoc({ notificationId: "" })),
    "control character in notificationId": JSON.stringify(notificationDoc({ notificationId: "a\nb" })),
    "publishAttemptCount is a string": JSON.stringify(notificationDoc({ publishAttemptCount: "1" })),
    "publishAttemptCount is zero": JSON.stringify(notificationDoc({ publishAttemptCount: 0 })),
    "missing data": (() => {
      const d = notificationDoc();
      delete d.notification.data;
      return JSON.stringify(d);
    })(),
    "userId is a number": (() => {
      const d = notificationDoc();
      d.notification.data.userId = 12345;
      return JSON.stringify(d);
    })(),
  };
  for (const [name, body] of Object.entries(structurallyInvalid)) {
    test(`rejects a structurally invalid body (${name}) with 400, storing nothing`, async () => {
      const env = makeEnv();
      const res = await postNotification(env, body);
      assert.equal(res.status, 400);
      assert.equal(env.DELETION_EVENTS.putCalls.length, 0);
    });
  }

  test("rejects invalid UTF-8 in the body with 400", async () => {
    const env = makeEnv();
    const res = await call(env, "/ebay/account-deletion", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-EBAY-SIGNATURE": GARBAGE_SIGNATURE },
      body: new Uint8Array([0x7b, 0xff, 0x7d]),
    });
    assert.equal(res.status, 400);
    assert.equal(env.DELETION_EVENTS.putCalls.length, 0);
  });

  test("rejects an oversized body with 413, storing nothing", async () => {
    const env = makeEnv();
    const doc = notificationDoc();
    doc.padding = "x".repeat(64 * 1024);
    const res = await postNotification(env, doc);
    assert.equal(res.status, 413);
    assertSecurityHeaders(res);
    assert.equal(env.DELETION_EVENTS.putCalls.length, 0);
  });

  test("accepts a body of exactly 64 KiB", async () => {
    const env = makeEnv();
    const base = JSON.stringify({ ...notificationDoc(), padding: "" });
    const doc = { ...notificationDoc(), padding: "x".repeat(64 * 1024 - Buffer.byteLength(base)) };
    const body = JSON.stringify(doc);
    assert.equal(Buffer.byteLength(body), 64 * 1024);
    const res = await postNotification(env, body);
    assert.equal(res.status, 204);
    assert.equal(env.DELETION_EVENTS.putCalls.length, 1);
  });

  test("rejects a streamed body that exceeds 64 KiB without a Content-Length", async () => {
    const env = makeEnv();
    const chunk = new Uint8Array(16 * 1024).fill(0x20);
    let sent = 0;
    const stream = new ReadableStream({
      pull(controller) {
        if (sent >= 10) {
          controller.close();
          return;
        }
        sent += 1;
        controller.enqueue(chunk);
      },
    });
    const req = new Request(`${BASE}/ebay/account-deletion`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-EBAY-SIGNATURE": GARBAGE_SIGNATURE },
      body: stream,
      duplex: "half",
    });
    assert.equal(req.headers.get("content-length"), null);
    const res = await worker.fetch(req, env);
    assert.equal(res.status, 413);
    assert.ok(sent < 10, "reader should stop pulling once over the cap");
    assert.equal(env.DELETION_EVENTS.putCalls.length, 0);
  });

  test("rejects a declared Content-Length over the cap before reading", async () => {
    const env = makeEnv();
    const req = new Request(`${BASE}/ebay/account-deletion`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-EBAY-SIGNATURE": GARBAGE_SIGNATURE,
        "Content-Length": String(64 * 1024 + 1),
      },
      body: "{}",
    });
    if (req.headers.get("content-length") === null) {
      // Some fetch implementations strip Content-Length from constructed
      // Requests; the streamed test above covers the cap in that case.
      return;
    }
    const res = await worker.fetch(req, env);
    assert.equal(res.status, 413);
  });

  test("a KV write failure returns an eBay-retriable 503 instead of crashing", async () => {
    const env = makeEnv();
    env.DELETION_EVENTS.failOn.add("put");
    const res = await postNotification(env, notificationDoc());
    assert.equal(res.status, 503);
    assertSecurityHeaders(res);
    const body = await res.json();
    assert.deepEqual(body, { error: "Service unavailable." });
  });

  test("a missing KV binding returns an eBay-retriable 503", async () => {
    const env = makeEnv({ DELETION_EVENTS: undefined });
    const res = await postNotification(env, notificationDoc());
    assert.equal(res.status, 503);
  });

  test("identical redeliveries collapse onto one entry", async () => {
    const env = makeEnv();
    const body = JSON.stringify(notificationDoc());
    assert.equal((await postNotification(env, body)).status, 204);
    assert.equal((await postNotification(env, body)).status, 204);
    assert.equal(env.DELETION_EVENTS.store.size, 1);
  });

  test("a forged body reusing a genuine notificationId cannot overwrite the genuine entry", async () => {
    const env = makeEnv();
    const genuine = JSON.stringify(notificationDoc({ userId: "genuine" }));
    const forged = JSON.stringify(notificationDoc({ userId: "forged" }));
    await postNotification(env, genuine);
    await postNotification(env, forged);
    assert.equal(env.DELETION_EVENTS.store.size, 2);
    const raws = [...env.DELETION_EVENTS.store.values()].map((v) => JSON.parse(v.value).rawBody);
    assert.ok(raws.includes(genuine));
    assert.ok(raws.includes(forged));
  });
});

// ---------------------------------------------------------------------------
// Watch-list cost control.
// ---------------------------------------------------------------------------

describe("watch-list filter", () => {
  test("discards a non-watched account: 204 to eBay, nothing written", async () => {
    const env = makeEnv({ WATCHED_EBAY_USER_IDS: "myOwnId, anotherId", WATCHED_EBAY_USERNAMES: "my_shop" });
    const res = await postNotification(env, notificationDoc({ userId: "someoneElse", username: "stranger" }));
    assert.equal(res.status, 204);
    assertSecurityHeaders(res);
    assert.equal(env.DELETION_EVENTS.putCalls.length, 0);
    assert.equal(env.DELETION_EVENTS.store.size, 0);
  });

  test("stores a notification whose userId is watched", async () => {
    const env = makeEnv({ WATCHED_EBAY_USER_IDS: " myOwnId , ma8vp1jySJC " });
    const res = await postNotification(env, notificationDoc());
    assert.equal(res.status, 204);
    assert.equal(env.DELETION_EVENTS.putCalls.length, 1);
  });

  test("userId matching is exact (case-sensitive)", async () => {
    const env = makeEnv({ WATCHED_EBAY_USER_IDS: "MA8VP1JYSJC" });
    await postNotification(env, notificationDoc());
    assert.equal(env.DELETION_EVENTS.putCalls.length, 0);
  });

  test("stores a notification whose username is watched (case-insensitive)", async () => {
    const env = makeEnv({ WATCHED_EBAY_USERNAMES: "Test_Buyer_1" });
    const res = await postNotification(env, notificationDoc());
    assert.equal(res.status, 204);
    assert.equal(env.DELETION_EVENTS.putCalls.length, 1);
  });

  test("stored and discarded responses are indistinguishable", async () => {
    const env = makeEnv({ WATCHED_EBAY_USER_IDS: "ma8vp1jySJC" });
    const stored = await postNotification(env, notificationDoc());
    const discarded = await postNotification(env, notificationDoc({ userId: "x", username: "y" }));
    assert.equal(stored.status, discarded.status);
    assert.deepEqual([...stored.headers], [...discarded.headers]);
    assert.equal(await stored.text(), await discarded.text());
  });

  test("empty or whitespace-only watch lists store everything", async () => {
    for (const value of [undefined, "", " , ,  "]) {
      const env = makeEnv({ WATCHED_EBAY_USER_IDS: value, WATCHED_EBAY_USERNAMES: value });
      await postNotification(env, notificationDoc({ userId: "anyone", username: "anybody" }));
      assert.equal(env.DELETION_EVENTS.putCalls.length, 1, `value=${JSON.stringify(value)}`);
    }
  });

  test("structurally invalid bodies are still rejected when the watch list is set", async () => {
    const env = makeEnv({ WATCHED_EBAY_USER_IDS: "myOwnId" });
    const res = await postNotification(env, "{}");
    assert.equal(res.status, 400);
  });

  test("an empty username/userId never matches", async () => {
    const env = makeEnv({ WATCHED_EBAY_USER_IDS: "myOwnId" });
    await postNotification(env, notificationDoc({ userId: "", username: "" }));
    assert.equal(env.DELETION_EVENTS.putCalls.length, 0);
  });
});

// ---------------------------------------------------------------------------
// Authenticated status endpoints.
// ---------------------------------------------------------------------------

describe("GET /account-deletion/pending", () => {
  const badAuthHeaders = {
    "no Authorization header": {},
    "wrong secret": { Authorization: "Bearer not-the-right-secret-at-all-0123456789" },
    "secret with one character changed": { Authorization: `Bearer ${STATUS_SECRET.slice(0, -1)}X` },
    "secret prefix only": { Authorization: `Bearer ${STATUS_SECRET.slice(0, 20)}` },
    "secret with suffix": { Authorization: `Bearer ${STATUS_SECRET}x` },
    "wrong scheme": { Authorization: `Basic ${STATUS_SECRET}` },
    "bare secret": { Authorization: STATUS_SECRET },
    "empty bearer": { Authorization: "Bearer " },
    "the eBay verification token instead": { Authorization: `Bearer ${VERIFICATION_TOKEN}` },
  };
  for (const [name, headers] of Object.entries(badAuthHeaders)) {
    test(`rejects ${name} with a generic 401`, async () => {
      const env = makeEnv();
      await postNotification(env, notificationDoc());
      const res = await getPending(env, { headers });
      assert.equal(res.status, 401);
      assertSecurityHeaders(res);
      const body = await res.json();
      assert.deepEqual(body, { error: "Unauthorized." });
    });
  }

  test("rejects every request when STATUS_API_SECRET is unset, empty, or too short", async () => {
    for (const secret of [undefined, "", "short-secret"]) {
      const env = makeEnv({ STATUS_API_SECRET: secret });
      const presented = secret === undefined ? "" : secret;
      const res = await getPending(env, { headers: { Authorization: `Bearer ${presented}` } });
      assert.equal(res.status, 401, `secret=${JSON.stringify(secret)}`);
    }
  });

  test("auth is checked before query validation (no information leak)", async () => {
    const res = await getPending(makeEnv(), { headers: {}, query: "?bogus=1" });
    assert.equal(res.status, 401);
  });

  test("returns queued entries with the exact raw body and signature header", async () => {
    const env = makeEnv();
    const raw = JSON.stringify(notificationDoc());
    await postNotification(env, raw);
    const res = await getPending(env);
    assert.equal(res.status, 200);
    assert.equal(res.headers.get("content-type"), "application/json");
    assertSecurityHeaders(res);
    const body = await res.json();
    assert.equal(body.cursor, null);
    assert.equal(body.entries.length, 1);
    const [entry] = body.entries;
    assert.equal(entry.notificationId, notificationDoc().notification.notificationId);
    assert.equal(entry.eventDate, "2026-10-01T12:00:00.000Z");
    assert.equal(entry.publishDate, "2026-10-01T12:00:01.000Z");
    assert.deepEqual(entry.data, notificationDoc().notification.data);
    assert.equal(entry.signatureHeader, GARBAGE_SIGNATURE);
    assert.equal(entry.rawBody, raw);
    assert.equal(entry.signatureVerified, false);
  });

  test("accepts a case-insensitive Bearer scheme", async () => {
    const res = await getPending(makeEnv(), { headers: { Authorization: `bearer ${STATUS_SECRET}` } });
    assert.equal(res.status, 200);
  });

  test("paginates with an opaque cursor", async () => {
    const env = makeEnv();
    for (let i = 0; i < 120; i += 1) {
      await postNotification(env, notificationDoc({ notificationId: `id-${i}` }));
    }
    const seen = new Set();
    let query = "";
    let pages = 0;
    for (;;) {
      const res = await getPending(env, { query });
      assert.equal(res.status, 200);
      const body = await res.json();
      pages += 1;
      assert.ok(body.entries.length <= 50);
      for (const e of body.entries) seen.add(e.notificationId);
      if (body.cursor === null) break;
      query = `?cursor=${encodeURIComponent(body.cursor)}`;
    }
    assert.equal(pages, 3);
    assert.equal(seen.size, 120);
  });

  test("rejects unexpected query parameters and invalid cursors", async () => {
    for (const query of ["?foo=1", "?cursor=", "?cursor=a%0Ab", "?cursor=a&cursor=b"]) {
      const res = await getPending(makeEnv(), { query });
      assert.equal(res.status, 400, query);
    }
  });

  test("skips entries that vanish or are corrupt", async () => {
    const env = makeEnv();
    await postNotification(env, notificationDoc());
    env.DELETION_EVENTS.store.set("pending:zz-corrupt", { value: "{not json" });
    const res = await getPending(env);
    const body = await res.json();
    assert.equal(body.entries.length, 1);
  });

  test("a KV list/get failure returns 503 instead of crashing", async () => {
    for (const op of ["list", "get"]) {
      const env = makeEnv();
      await postNotification(env, notificationDoc());
      env.DELETION_EVENTS.failOn.add(op);
      const res = await getPending(env);
      assert.equal(res.status, 503, op);
      assertSecurityHeaders(res);
    }
  });
});

describe("POST /account-deletion/acknowledge", () => {
  const ID = notificationDoc().notification.notificationId;

  test("requires the bearer secret", async () => {
    const env = makeEnv();
    await postNotification(env, notificationDoc());
    for (const headers of [
      { "Content-Type": "application/json" },
      { "Content-Type": "application/json", Authorization: "Bearer wrong-wrong-wrong-wrong-wrong-wrong" },
    ]) {
      const res = await acknowledge(env, { notificationId: ID }, { headers });
      assert.equal(res.status, 401);
      assertSecurityHeaders(res);
    }
    assert.equal(env.DELETION_EVENTS.store.size, 1, "nothing deleted without auth");
  });

  test("removes the entry and is idempotent", async () => {
    const env = makeEnv();
    await postNotification(env, notificationDoc());
    assert.equal(env.DELETION_EVENTS.store.size, 1);

    const first = await acknowledge(env, { notificationId: ID });
    assert.equal(first.status, 200);
    assertSecurityHeaders(first);
    assert.deepEqual(await first.json(), { acknowledged: true });
    assert.equal(env.DELETION_EVENTS.store.size, 0);

    const second = await acknowledge(env, { notificationId: ID });
    assert.equal(second.status, 200);
    assert.deepEqual(await second.json(), { acknowledged: true });

    const never = await acknowledge(env, { notificationId: "never-existed" });
    assert.equal(never.status, 200);
  });

  test("removes every sibling entry for that notificationId but nothing else", async () => {
    const env = makeEnv();
    await postNotification(env, notificationDoc({ userId: "genuine" }));
    await postNotification(env, notificationDoc({ userId: "forged" }));
    await postNotification(env, notificationDoc({ notificationId: "other-id" }));
    assert.equal(env.DELETION_EVENTS.store.size, 3);
    const res = await acknowledge(env, { notificationId: ID });
    assert.equal(res.status, 200);
    assert.equal(env.DELETION_EVENTS.store.size, 1);
    const remaining = JSON.parse([...env.DELETION_EVENTS.store.values()][0].value);
    assert.equal(remaining.notificationId, "other-id");
  });

  const badBodies = {
    "non-JSON": "nope",
    "empty object": "{}",
    "extra key": JSON.stringify({ notificationId: ID, extra: 1 }),
    "non-string id": JSON.stringify({ notificationId: 5 }),
    "empty id": JSON.stringify({ notificationId: "" }),
    "array": JSON.stringify([ID]),
    "oversized id": JSON.stringify({ notificationId: "x".repeat(257) }),
  };
  for (const [name, body] of Object.entries(badBodies)) {
    test(`rejects a bad body (${name}) with 400`, async () => {
      const res = await acknowledge(makeEnv(), body);
      assert.equal(res.status, 400);
    });
  }

  test("rejects an oversized body with 413", async () => {
    const res = await acknowledge(makeEnv(), JSON.stringify({ notificationId: "x".repeat(5000) }));
    assert.equal(res.status, 413);
  });

  test("rejects a non-JSON Content-Type with 415", async () => {
    const res = await acknowledge(makeEnv(), { notificationId: ID }, { headers: authed({ "Content-Type": "text/plain" }) });
    assert.equal(res.status, 415);
  });

  test("a KV failure returns 503 instead of crashing", async () => {
    for (const op of ["list", "delete"]) {
      const env = makeEnv();
      await postNotification(env, notificationDoc());
      env.DELETION_EVENTS.failOn.add(op);
      const res = await acknowledge(env, { notificationId: ID });
      assert.equal(res.status, 503, op);
    }
  });
});

describe("constant-time comparison", () => {
  test("uses crypto.subtle.timingSafeEqual when the runtime provides it", async () => {
    const subtle = globalThis.crypto.subtle;
    const hadOwn = Object.prototype.hasOwnProperty.call(subtle, "timingSafeEqual");
    assert.equal(typeof subtle.timingSafeEqual, "undefined", "Node should not provide it");
    let calls = 0;
    subtle.timingSafeEqual = (a, b) => {
      calls += 1;
      assert.equal(a.byteLength, b.byteLength, "must only be called with equal lengths");
      let diff = 0;
      for (let i = 0; i < a.byteLength; i += 1) diff |= a[i] ^ b[i];
      return diff === 0;
    };
    try {
      assert.equal((await getPending(makeEnv())).status, 200);
      assert.equal((await getPending(makeEnv(), { headers: { Authorization: "Bearer x" } })).status, 401);
      assert.equal((await getPending(makeEnv(), { headers: {} })).status, 401);
      assert.equal(calls, 3);
    } finally {
      if (!hadOwn) delete subtle.timingSafeEqual;
    }
  });

  test("falls back to the XOR loop when timingSafeEqual is absent", async () => {
    assert.equal(typeof globalThis.crypto.subtle.timingSafeEqual, "undefined");
    assert.equal((await getPending(makeEnv())).status, 200);
    assert.equal((await getPending(makeEnv(), { headers: { Authorization: "Bearer x" } })).status, 401);
  });
});

// ---------------------------------------------------------------------------
// Defensive checks shared by every route.
// ---------------------------------------------------------------------------

describe("host pinning, URL length, and fail-closed routing", () => {
  const badBases = {
    "different worker name": "https://other-worker.example-sub.workers.dev",
    "plain http": `http://${HOST}`,
    "explicit port": `https://${HOST}:8443`,
    "extra label": `https://x.${HOST}`,
    "custom domain": "https://ebay-account-deletion-receiver.example.com",
    "invalid account label": "https://ebay-account-deletion-receiver.-bad-.workers.dev",
    "trailing-dot FQDN": `https://${HOST}.`,
  };
  for (const [name, base] of Object.entries(badBases)) {
    test(`rejects ${name} with 421`, async () => {
      const env = makeEnv();
      const res = await call(env, "/ebay/account-deletion?challenge_code=abc", {}, base);
      assert.equal(res.status, 421);
      assertSecurityHeaders(res);
    });
  }

  test("accepts an upper-case hostname (normalised) and hashes the lower-case endpoint", async () => {
    const res = await call(makeEnv(), "/ebay/account-deletion?challenge_code=abc", {}, `https://${HOST.toUpperCase()}`);
    assert.equal(res.status, 200);
    const expected = createHash("sha256").update(`abc${VERIFICATION_TOKEN}${ENDPOINT}`).digest("hex");
    assert.equal((await res.json()).challengeResponse, expected);
  });

  test("rejects an over-long URL with 414", async () => {
    const res = await call(makeEnv(), `/ebay/account-deletion?challenge_code=${"a".repeat(4096)}`);
    assert.equal(res.status, 414);
    assertSecurityHeaders(res);
  });

  for (const path of [
    "/",
    "/ebay/account-deletion/",
    "/EBAY/account-deletion",
    "/ebay/account%2Ddeletion",
    "/account-deletion/pending/",
    "/__proto__",
    "/constructor",
    "/ebay//account-deletion",
  ]) {
    test(`unknown path ${path} returns 404`, async () => {
      const res = await call(makeEnv(), path);
      assert.equal(res.status, 404);
      assertSecurityHeaders(res);
    });
  }

  const wrongMethods = [
    ["/ebay/account-deletion", "PUT", "GET, POST"],
    ["/ebay/account-deletion", "DELETE", "GET, POST"],
    ["/ebay/account-deletion", "HEAD", "GET, POST"],
    ["/ebay/account-deletion", "OPTIONS", "GET, POST"],
    ["/account-deletion/pending", "POST", "GET"],
    ["/account-deletion/acknowledge", "GET", "POST"],
  ];
  for (const [path, method, allow] of wrongMethods) {
    test(`${method} ${path} returns 405 with Allow: ${allow}`, async () => {
      const res = await call(makeEnv(), path, { method });
      assert.equal(res.status, 405);
      assert.equal(res.headers.get("allow"), allow);
      assertSecurityHeaders(res);
    });
  }

  test("an unexpected internal error returns a generic 500 with security headers", async () => {
    const hostile = {
      get url() {
        throw new Error("boom");
      },
    };
    const res = await worker.fetch(hostile, makeEnv());
    assert.equal(res.status, 500);
    assertSecurityHeaders(res);
    assert.deepEqual(await res.json(), { error: "Internal error." });
  });

  test("no response ever sets cookies or CORS headers", async () => {
    const env = makeEnv();
    const responses = [
      await call(env, "/ebay/account-deletion?challenge_code=abc"),
      await postNotification(env, notificationDoc()),
      await getPending(env),
      await acknowledge(env, { notificationId: "x" }),
      await call(env, "/nope"),
    ];
    for (const res of responses) {
      assert.equal(res.headers.get("set-cookie"), null);
      assert.equal(res.headers.get("access-control-allow-origin"), null);
      assertSecurityHeaders(res);
    }
  });
});
