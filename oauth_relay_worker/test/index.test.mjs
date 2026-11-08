import assert from "node:assert/strict";
import test from "node:test";

import worker from "../src/index.js";

const HOST = "ebay-sandbox-oauth-relay.example-subdomain.workers.dev";
const STATE = "A".repeat(43);

function request(path, options = {}) {
  return worker.fetch(new Request(`https://${HOST}${path}`, options));
}

test("privacy GET and HEAD are static and non-cacheable", async () => {
  const get = await request("/privacy");
  assert.equal(get.status, 200);
  assert.match(await get.text(), /Gold Gallery Automation/);
  assert.equal(get.headers.get("cache-control"), "no-store, max-age=0");
  assert.equal(get.headers.get("referrer-policy"), "no-referrer");

  const head = await request("/privacy", { method: "HEAD" });
  assert.equal(head.status, 200);
  assert.equal(await head.text(), "");
});

test("valid success callback redirects only to the fixed loopback destination", async () => {
  const result = await request(
    `/oauth/callback?state=${STATE}&code=short-lived-code&expires_in=299`,
  );
  assert.equal(result.status, 302);
  assert.equal(
    result.headers.get("location"),
    `http://127.0.0.1:5000/oauth/callback?state=${STATE}&code=short-lived-code`,
  );
  assert.equal(result.headers.get("cache-control"), "no-store, max-age=0");
});

test("valid decline callback drops the description", async () => {
  const result = await request(
    `/oauth/callback?state=${STATE}&error=access_denied&error_description=No`,
  );
  assert.equal(result.status, 302);
  assert.equal(
    result.headers.get("location"),
    `http://127.0.0.1:5000/oauth/callback?state=${STATE}&error=access_denied`,
  );
});

test("invalid callbacks and open-redirect inputs are rejected", async () => {
  const invalid = [
    "/oauth/callback",
    `/oauth/callback?state=${STATE}&state=${STATE}&code=x`,
    `/oauth/callback?state=${STATE}&code=x&error=access_denied`,
    `/oauth/callback?state=${STATE}&code=x&next=https%3A%2F%2Fevil.example`,
    `/oauth/callback?state=too-short&code=x`,
    `/oauth/callback?state=${STATE}&code=x&expires_in=not-a-number`,
  ];
  for (const path of invalid) {
    const result = await request(path);
    assert.equal(result.status, 400, path);
    assert.equal(result.headers.get("location"), null, path);
  }
});

test("wrong method, path, scheme and hostname fail closed", async () => {
  assert.equal((await request("/oauth/callback", { method: "POST" })).status, 405);
  assert.equal((await request("/unexpected")).status, 404);
  assert.equal(
    (await worker.fetch(new Request(`http://${HOST}/privacy`))).status,
    421,
  );
  assert.equal(
    (
      await worker.fetch(
        new Request(
          `https://not-${HOST}/oauth/callback?state=${STATE}&code=x`,
        ),
      )
    ).status,
    421,
  );
});

test("oversized request URLs are rejected", async () => {
  const result = await request(`/oauth/callback?state=${STATE}&code=${"x".repeat(4100)}`);
  assert.equal(result.status, 414);
});

test("documented authorization-code boundary is consistent", async () => {
  const maximum = await request(
    `/oauth/callback?state=${STATE}&code=${"x".repeat(1024)}`,
  );
  assert.equal(maximum.status, 302);

  const tooLong = await request(
    `/oauth/callback?state=${STATE}&code=${"x".repeat(1025)}`,
  );
  assert.equal(tooLong.status, 400);
});
