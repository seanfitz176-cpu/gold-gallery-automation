const WORKER_NAME = "ebay-sandbox-oauth-relay";
const LOCAL_CALLBACK = "http://127.0.0.1:5000/oauth/callback";
const MAX_REQUEST_URL_LENGTH = 4096;
const STATE_PATTERN = /^[A-Za-z0-9_-]{43}$/;
const ERROR_PATTERN = /^[A-Za-z0-9._~-]{1,128}$/;
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f]/;

const SECURITY_HEADERS = {
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
};

const PRIVACY_HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Privacy Policy &mdash; Gold Gallery Automation</title>
</head>
<body>
<h1>Privacy Policy &mdash; Gold Gallery Automation</h1>
<p>Effective date: 25 October 2026.</p>
<h2>Who this covers</h2>
<p>Gold Gallery Automation is a private, single-operator application built and run by
Sean Fitzgerald ("the operator") to manage the operator's own eBay seller account. It
is not offered as a public or multi-user service, and no other person's eBay account
is connected to it.</p>
<h2>What data this application accesses</h2>
<p>When the operator connects it to eBay through eBay's OAuth sign-in, the application
accesses only the categories of data covered by the permissions the operator explicitly
grants at that time, which may include: basic eBay account identity, listing and
inventory data, order and fulfillment data, and seller account/business policy data. It
only ever acts on the one eBay seller account the operator connects; it does not access
any other eBay user's data.</p>
<h2>How this data is used</h2>
<p>Data is used solely to operate the operator's own eBay seller account: creating and
updating listings, adjusting prices and quantities, and related account-management
tasks the operator has explicitly enabled. It is not used for any other purpose.</p>
<h2>Where this data is stored</h2>
<p>All data is stored locally on the operator's own computer, in an encrypted local
database. It is not uploaded to any third-party server, and this application has no
cloud database of its own. The small Cloudflare Worker used only to relay eBay's OAuth
sign-in redirect (the service at this domain) does not store, log, or retain any of
this data &mdash; see the technical note below.</p>
<h2>Sharing and sale of data</h2>
<p>This data is never sold, rented, or shared with any third party, advertiser, or
analytics service.</p>
<h2>Retention and deletion</h2>
<p>Data is retained only for as long as the operator continues to use the application
to manage this eBay account. The operator can revoke this application's access at any
time from eBay's account settings, after which the application can no longer retrieve
new data; previously stored local data can be deleted by the operator at any time.</p>
<h2>Account-deletion notifications</h2>
<p>In compliance with eBay's Marketplace Account Deletion/Closure requirements, if any
eBay account (including the operator's own) is closed, this application deletes any
data it holds about that account.</p>
<h2>Technical note: this domain's role</h2>
<p>This page is served by the small Cloudflare Worker that only relays eBay's OAuth
authorization redirect to the application running on the operator's own computer. This
Worker itself never receives, stores, or logs an eBay password, API credential, access
token, refresh token, listing data, order data, or inventory data &mdash; it only
forwards eBay's short-lived authorization code and state value, immediately and
without storage, to 127.0.0.1 on the operator's machine.</p>
<h2>Contact</h2>
<p>Questions about this policy can be sent to seanfitz176@icloud.com.</p>
</body>
</html>`;

function response(status, body, extraHeaders = {}) {
  return new Response(body, {
    status,
    headers: {
      ...SECURITY_HEADERS,
      "Content-Type": "text/plain; charset=utf-8",
      ...extraHeaders,
    },
  });
}

function invalidCallback() {
  return response(400, "Invalid OAuth callback.\n");
}

function isCanonicalWorkerHost(url) {
  const labels = url.hostname.toLowerCase().split(".");
  return (
    url.protocol === "https:" &&
    url.port === "" &&
    labels.length === 4 &&
    labels[0] === WORKER_NAME &&
    labels[2] === "workers" &&
    labels[3] === "dev" &&
    /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(labels[1])
  );
}

export default {
  async fetch(request) {
    if (request.url.length > MAX_REQUEST_URL_LENGTH) {
      return response(414, "Request URL is too long.\n");
    }

    const url = new URL(request.url);
    if (!isCanonicalWorkerHost(url)) {
      return response(421, "Misdirected request.\n");
    }

    if (url.pathname === "/privacy") {
      if (request.method !== "GET" && request.method !== "HEAD") {
        return response(405, "Method not allowed.\n", { Allow: "GET, HEAD" });
      }
      return new Response(request.method === "HEAD" ? null : PRIVACY_HTML, {
        status: 200,
        headers: {
          ...SECURITY_HEADERS,
          "Content-Type": "text/html; charset=utf-8",
        },
      });
    }

    if (url.pathname !== "/oauth/callback") {
      return response(404, "Not found.\n");
    }
    if (request.method !== "GET") {
      return response(405, "Method not allowed.\n", { Allow: "GET" });
    }

    // eBay includes expires_in on successful callbacks. It is checked and then
    // discarded because the local application neither needs nor trusts it.
    // error_description is accepted for interoperability and is also discarded.
    const allowedIncoming = new Set([
      "state",
      "code",
      "error",
      "expires_in",
      "error_description",
    ]);
    for (const name of url.searchParams.keys()) {
      if (!allowedIncoming.has(name)) {
        return invalidCallback();
      }
    }

    const states = url.searchParams.getAll("state");
    const codes = url.searchParams.getAll("code");
    const errors = url.searchParams.getAll("error");
    const expirations = url.searchParams.getAll("expires_in");
    const descriptions = url.searchParams.getAll("error_description");

    if (
      states.length !== 1 ||
      !STATE_PATTERN.test(states[0]) ||
      codes.length + errors.length !== 1
    ) {
      return invalidCallback();
    }

    const target = new URL(LOCAL_CALLBACK);
    target.searchParams.set("state", states[0]);

    if (codes.length === 1) {
      const code = codes[0];
      if (
        errors.length !== 0 ||
        descriptions.length !== 0 ||
        code.length < 1 ||
        code.length > 1024 ||
        CONTROL_CHARACTERS.test(code) ||
        expirations.length > 1 ||
        (expirations.length === 1 && !/^[0-9]{1,6}$/.test(expirations[0]))
      ) {
        return invalidCallback();
      }
      target.searchParams.set("code", code);
    } else {
      const error = errors[0];
      if (
        codes.length !== 0 ||
        expirations.length !== 0 ||
        !ERROR_PATTERN.test(error) ||
        descriptions.length > 1 ||
        (descriptions.length === 1 &&
          (descriptions[0].length > 512 ||
            CONTROL_CHARACTERS.test(descriptions[0])))
      ) {
        return invalidCallback();
      }
      target.searchParams.set("error", error);
    }

    // The destination is constant. Callback parameters can never choose a URL.
    return new Response(null, {
      status: 302,
      headers: {
        ...SECURITY_HEADERS,
        Location: target.toString(),
      },
    });
  },
};
