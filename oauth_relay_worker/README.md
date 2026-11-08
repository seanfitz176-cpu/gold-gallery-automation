# Stable Sandbox OAuth callback relay

This Cloudflare Worker supplies one stable public callback for the eBay Sandbox.
It does not proxy the Python application. After eBay finishes consent, the Worker
validates the callback shape and tells the same browser to return to the Python
application on `http://127.0.0.1:5000/oauth/callback`.

The Worker has no storage bindings, makes no outbound requests, intentionally
emits no application logs and cannot select a user-controlled redirect target.
The eBay Client Secret and OAuth tokens remain only in the local Python app.

Keep the Worker name exactly `ebay-sandbox-oauth-relay`, keep preview URLs and
observability disabled, and do not enable Cloudflare Access on its `workers.dev`
address. The detailed beginner deployment procedure is in the main project
`README.md`.
